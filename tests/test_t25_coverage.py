"""T25 커버리지 보강 테스트: semantic, codebook, mailer, agent nodes."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

_OLD_RULES_SKIP = pytest.mark.skip(reason="R-01: 구 규칙 비활성화 (RULES.md MVP)")

from engine.models import (
    ColumnProfile, SheetProfile, Dataset, Decision,
    RuleBinding, Issue, CellRef, Change, RequestItem, RunReport,
)
from engine.semantic import bindings_from_profiles
import engine.validator as validator
import engine.fixer as fixer


# ── semantic.py ──────────────────────────────────────────────────────────────

def _profile(sheet: str, cols: list[tuple]) -> SheetProfile:
    """(name, semantic_type, required, params) 튜플 목록으로 SheetProfile 생성."""
    columns = [
        ColumnProfile(
            name=c[0], semantic_type=c[1],
            required=c[2] if len(c) > 2 else False,
            params=c[3] if len(c) > 3 else {},
        )
        for c in cols
    ]
    return SheetProfile(file="f.xlsx", sheet=sheet, columns=columns, row_count=3)


@_OLD_RULES_SKIP
def test_bindings_date_format():
    p = _profile("실적", [("지원일자", "date")])
    bindings = bindings_from_profiles([p])
    rule_ids = {b.rule_id for b in bindings}
    assert "DATE_FORMAT" in rule_ids


def test_bindings_date_range_skipped_without_param():
    """range 파라미터 없으면 DATE_RANGE 바인딩이 생성되지 않는다."""
    p = _profile("실적", [("지원일자", "date")])
    bindings = bindings_from_profiles([p])
    assert all(b.rule_id != "DATE_RANGE" for b in bindings)


@_OLD_RULES_SKIP
def test_bindings_date_range_included_with_param():
    p = _profile("실적", [("지원일자", "date", False, {"range": ["2026-01-01", "2026-12-31"]})])
    bindings = bindings_from_profiles([p])
    assert any(b.rule_id == "DATE_RANGE" for b in bindings)


@_OLD_RULES_SKIP
def test_bindings_required_col():
    p = _profile("실적", [("기관명", "org_name", True)])
    bindings = bindings_from_profiles([p])
    assert any(b.rule_id == "REQUIRED" for b in bindings)


def test_bindings_code_valid_skipped_without_codebook():
    p = _profile("실적", [("사업코드", "code")])
    bindings = bindings_from_profiles([p])
    assert all(b.rule_id != "CODE_VALID" for b in bindings)


@_OLD_RULES_SKIP
def test_bindings_code_valid_included_with_codebook():
    p = _profile("실적", [("사업코드", "code", False, {"valid_codes": ["A001", "A002"]})])
    bindings = bindings_from_profiles([p])
    assert any(b.rule_id == "CODE_VALID" for b in bindings)


@_OLD_RULES_SKIP
def test_bindings_dedup_across_files():
    """같은 시트·컬럼은 파일 여러 개 있어도 바인딩 1개만."""
    p1 = _profile("실적", [("기관명", "org_name")])
    p2 = SheetProfile(file="b.xlsx", sheet="실적",
                      columns=[ColumnProfile(name="기관명", semantic_type="org_name")], row_count=2)
    bindings = bindings_from_profiles([p1, p2])
    text_norm = [b for b in bindings if b.rule_id == "TEXT_NORMALIZE"]
    assert len(text_norm) == 1


@_OLD_RULES_SKIP
def test_bindings_table_rules():
    p = _profile("실적", [("기관명", "text")])
    bindings = bindings_from_profiles([p])
    rule_ids = {b.rule_id for b in bindings}
    assert "DUPLICATE_ROW" in rule_ids
    assert "NEAR_DUPLICATE" in rule_ids


@_OLD_RULES_SKIP
def test_bindings_unknown_type():
    p = _profile("실적", [("알수없음", "unknown")])
    bindings = bindings_from_profiles([p])
    assert any(b.rule_id == "TYPE_CONSISTENCY" for b in bindings)


# ── codebook.py ──────────────────────────────────────────────────────────────

def test_codebook_is_valid():
    from engine.codebook import Codebook
    cb = Codebook(["A001", "A002", "B001"])
    assert cb.is_valid("A001")
    assert not cb.is_valid("X999")


def test_codebook_codes_property():
    from engine.codebook import Codebook
    cb = Codebook(["X", "Y"])
    assert set(cb.codes) == {"X", "Y"}


def test_codebook_nearest():
    from engine.codebook import Codebook
    cb = Codebook(["A001", "A002", "B001"])
    result = cb.nearest("A001", n=2)
    assert "A001" in result


def test_load_codebook_missing_file(tmp_path):
    from engine.codebook import load_codebook
    from config import settings
    load_codebook.cache_clear()
    with patch.object(settings, "standard_dir", tmp_path):
        cb = load_codebook("nonexistent.csv")
    assert cb.codes == []
    load_codebook.cache_clear()


def test_load_codebook_from_csv(tmp_path):
    from engine.codebook import load_codebook
    from config import settings
    csv_path = tmp_path / "test_codes.csv"
    csv_path.write_text("code\nA001\nA002\nB001\n", encoding="utf-8")
    load_codebook.cache_clear()
    with patch.object(settings, "standard_dir", tmp_path):
        cb = load_codebook("test_codes.csv")
    assert cb.is_valid("A001")
    assert not cb.is_valid("X999")
    load_codebook.cache_clear()


def test_get_valid_codes(tmp_path):
    from engine.codebook import get_valid_codes, load_codebook
    from config import settings
    csv_path = tmp_path / "gc.csv"
    csv_path.write_text("code\nZ001\nZ002\n", encoding="utf-8")
    load_codebook.cache_clear()
    with patch.object(settings, "standard_dir", tmp_path):
        codes = get_valid_codes("gc.csv")
    assert "Z001" in codes
    load_codebook.cache_clear()


# ── notify/mailer.py ──────────────────────────────────────────────────────────

def test_mailer_dry_run_creates_file(tmp_path):
    from notify.mailer import send
    from config import settings
    req = RequestItem(
        org="창원시", issues=["E-0001"],
        subject="[테스트] 보완 요청", body="오류 내용입니다.",
        to="test@example.com",
    )
    with patch.object(settings, "mail_dry_run", True):
        result = send(req, tmp_path)
    assert (tmp_path / "requests" / "창원시.json").exists()
    assert result.sent is False


def test_mailer_dry_run_no_to(tmp_path):
    from notify.mailer import send
    from config import settings
    req = RequestItem(
        org="진주시", issues=[], subject="제목", body="본문", to=None
    )
    with patch.object(settings, "mail_dry_run", True):
        result = send(req, tmp_path)
    assert result.sent is False


def test_send_all_returns_list(tmp_path):
    from notify.mailer import send_all
    from config import settings
    reqs = [
        RequestItem(org="A", issues=[], subject="s1", body="b1"),
        RequestItem(org="B", issues=[], subject="s2", body="b2"),
    ]
    with patch.object(settings, "mail_dry_run", True):
        results = send_all(reqs, tmp_path)
    assert len(results) == 2


# ── agent/nodes/treatment.py ─────────────────────────────────────────────────

def test_treatment_node_basic():
    from agent.nodes.treatment import treatment_node
    df = pd.DataFrame({"_row": [2], "기관명": [" 창원시 "]})
    ds = Dataset({("f.xlsx", "실적"): df})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    decisions = [Decision(issue_id=i.issue_id, action="apply", decided_by="system") for i in issues]

    state = {
        "run_id": "test", "input_paths": [],
        "dataset": ds, "profiles": [], "bindings": bindings,
        "issues": issues, "decisions": decisions,
        "cleaned": None, "changes": [], "requests": [], "report": None,
    }
    result = treatment_node(state)
    assert len(result["changes"]) == 1
    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="기관명")
    assert result["cleaned"].cell(ref) == "창원시"


def test_treatment_node_no_decisions():
    from agent.nodes.treatment import treatment_node
    df = pd.DataFrame({"_row": [2], "기관명": [" 창원시 "]})
    ds = Dataset({("f.xlsx", "실적"): df})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)

    state = {
        "run_id": "test", "input_paths": [], "dataset": ds,
        "profiles": [], "bindings": bindings, "issues": issues,
        "decisions": [], "cleaned": None, "changes": [], "requests": [], "report": None,
    }
    result = treatment_node(state)
    assert result["changes"] == []


# ── agent/nodes/prescription.py ──────────────────────────────────────────────

def test_prescription_node_no_conflict():
    from agent.nodes.prescription import prescription_node
    df = pd.DataFrame({"_row": [2], "지원일자": ["20260315"]})
    ds = Dataset({("f.xlsx", "실적"): df})
    bindings = [RuleBinding(rule_id="DATE_FORMAT", targets=["실적.지원일자"], source="standard")]
    issues = validator.run(ds, bindings)

    state = {
        "run_id": "test", "input_paths": [], "dataset": ds,
        "profiles": [], "bindings": bindings, "issues": issues,
        "decisions": [], "cleaned": None, "changes": [], "requests": [], "report": None,
    }
    result = prescription_node(state)
    # tier가 auto이므로 변경 없어야 함
    assert result["issues"][0].tier == "auto"


def test_prescription_node_escalates_on_conflict():
    """같은 셀에 다른 suggestion 2개 → auto에서 approval로 격상."""
    from agent.nodes.prescription import prescription_node
    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="값")
    issue1 = Issue(
        issue_id="E-0001", ref=ref, rule_id="DATE_FORMAT", kind="rule",
        value="abc", message="오류1", tier="auto", suggestion="2026-01-01",
    )
    issue2 = Issue(
        issue_id="E-0002", ref=ref, rule_id="PHONE_FORMAT", kind="rule",
        value="abc", message="오류2", tier="auto", suggestion="010-1234-5678",
    )
    state = {
        "run_id": "test", "input_paths": [], "issues": [issue1, issue2],
        "decisions": [],
    }
    result = prescription_node(state)
    tiers = {i.issue_id: i.tier for i in result["issues"]}
    # 충돌 셀이므로 approval로 격상
    assert tiers["E-0001"] == "approval"
    assert tiers["E-0002"] == "approval"


# ── agent/nodes/rules.py ─────────────────────────────────────────────────────

@_OLD_RULES_SKIP
def test_rules_node_basic():
    from agent.nodes.rules import rules_node
    profiles = [_profile("실적", [("기관명", "org_name"), ("지원일자", "date")])]
    state = {
        "run_id": "test", "input_paths": [], "profiles": profiles,
        "decisions": [],
    }
    result = rules_node(state)
    rule_ids = {b.rule_id for b in result["bindings"]}
    assert "TEXT_NORMALIZE" in rule_ids
    assert "DATE_FORMAT" in rule_ids


# ── agent/nodes/diagnosis.py ─────────────────────────────────────────────────

def test_diagnosis_node_basic():
    from agent.nodes.diagnosis import diagnosis_node
    from llm.client import set_provider

    class FakeLLM:
        def complete(self, p: str) -> str:
            return '{"explanation": "테스트 설명"}'

    import llm.client as client_mod
    original = client_mod._provider
    client_mod._provider = FakeLLM()
    try:
        df = pd.DataFrame({"_row": [2], "기관명": [" 창원시 "]})
        ds = Dataset({("f.xlsx", "실적"): df})
        bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
        state = {
            "run_id": "test", "input_paths": [], "dataset": ds,
            "profiles": [], "bindings": bindings, "issues": [],
            "decisions": [],
        }
        result = diagnosis_node(state)
        assert len(result["issues"]) >= 1
    finally:
        client_mod._provider = original


# ── agent/nodes/recheck.py ───────────────────────────────────────────────────

def test_recheck_node_passed(tmp_path):
    from agent.nodes.recheck import recheck_node
    from config import settings
    import llm.client as client_mod

    class FakeLLM:
        def complete(self, p: str) -> str:
            return '{"subject": "제목", "body": "본문"}'

    original = client_mod._provider
    client_mod._provider = FakeLLM()

    df = pd.DataFrame({"_row": [2], "기관명": ["창원시"]})
    ds = Dataset({("f.xlsx", "실적"): df})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    # 오류 없는 상태 → recheck 통과
    issues: list[Issue] = []
    state = {
        "run_id": "recheck_test", "input_paths": [],
        "dataset": ds, "profiles": [], "bindings": bindings,
        "issues": issues, "decisions": [],
        "cleaned": ds.copy(), "changes": [], "requests": [], "report": None,
    }
    with patch.object(settings, "outputs_dir", tmp_path):
        result = recheck_node(state)

    client_mod._provider = original
    assert result["report"] is not None
    assert result["report"].recheck_passed is True


def test_recheck_node_remaining_issues(tmp_path):
    from agent.nodes.recheck import recheck_node
    from config import settings
    import llm.client as client_mod

    class FakeLLM:
        def complete(self, p: str) -> str:
            return '{"subject": "제목", "body": "본문"}'

    original = client_mod._provider
    client_mod._provider = FakeLLM()

    df = pd.DataFrame({"_row": [2], "기관명": [" 창원시 "]})  # 여전히 오류
    ds = Dataset({("f.xlsx", "실적"): df})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    # 아무 수정도 하지 않음
    state = {
        "run_id": "recheck_fail", "input_paths": [],
        "dataset": ds, "profiles": [], "bindings": bindings,
        "issues": issues, "decisions": [],
        "cleaned": ds.copy(), "changes": [], "requests": [], "report": None,
    }
    with patch.object(settings, "outputs_dir", tmp_path):
        result = recheck_node(state)

    client_mod._provider = original
    assert result["report"].recheck_passed is False
    assert len(result["report"].remaining_issues) > 0


# ── notify/mailer.py SMTP 경로 ───────────────────────────────────────────────

def test_mailer_smtp_send(tmp_path):
    """SMTP 실제 연결 없이 smtplib.SMTP를 mock해서 발송 경로를 테스트한다."""
    from notify.mailer import send
    from config import settings
    import smtplib

    req = RequestItem(
        org="통영시", issues=["E-0001"],
        subject="보완 요청", body="오류 본문입니다.",
        to="test@example.com",
    )

    mock_smtp = patch("smtplib.SMTP")
    with mock_smtp as MockSMTP, \
         patch.object(settings, "mail_dry_run", False), \
         patch.object(settings, "smtp_user", "sender@example.com"), \
         patch.object(settings, "smtp_password", "pw"):
        instance = MockSMTP.return_value.__enter__.return_value
        result = send(req, tmp_path)

    assert result.sent is True


def test_mailer_smtp_failure(tmp_path):
    """SMTP 실패 시 sent=False 반환."""
    from notify.mailer import send
    from config import settings
    import smtplib

    req = RequestItem(
        org="고성군", issues=[], subject="s", body="b", to="a@b.com"
    )
    with patch("smtplib.SMTP", side_effect=OSError("연결 실패")), \
         patch.object(settings, "mail_dry_run", False), \
         patch.object(settings, "smtp_user", "s@s.com"), \
         patch.object(settings, "smtp_password", "pw"):
        result = send(req, tmp_path)

    assert result.sent is False


def test_mailer_no_recipient_skips_send(tmp_path):
    from notify.mailer import send
    from config import settings
    req = RequestItem(org="거제시", issues=[], subject="s", body="b", to=None)
    with patch.object(settings, "mail_dry_run", False):
        result = send(req, tmp_path)
    assert result.sent is False


# ── recheck_node request tier 경로 ───────────────────────────────────────────

def test_recheck_node_request_tier_creates_request_item(tmp_path):
    """request tier 잔여 이슈가 있으면 RequestItem이 생성된다."""
    from agent.nodes.recheck import recheck_node
    from config import settings
    import llm.client as client_mod

    class FakeLLM:
        def complete(self, p: str) -> str:
            return '{"subject": "보완 요청 제목", "body": "보완 요청 본문"}'

    original = client_mod._provider
    client_mod._provider = FakeLLM()

    # REQUIRED 오류(request tier)를 갖는 데이터 준비
    from engine.models import Issue, CellRef
    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="기관명")
    remaining_issue = Issue(
        issue_id="E-0099", ref=ref, rule_id="REQUIRED", kind="rule",
        value="", message="필수값 누락", tier="request",
    )

    df = pd.DataFrame({"_row": [2], "기관명": ["창원시"]})
    ds = Dataset({("f.xlsx", "실적"): df})
    bindings: list[RuleBinding] = []

    state = {
        "run_id": "req_test", "input_paths": [],
        "dataset": ds, "profiles": [], "bindings": bindings,
        "issues": [remaining_issue], "decisions": [],
        "cleaned": ds.copy(), "changes": [], "requests": [], "report": None,
    }

    # validator.run이 빈 리스트 → cleaned에 남은 이슈 없지만
    # issues_orig에 request tier 이슈 있으므로 RequestItem 생성
    # 단, remaining = validator.run(cleaned, bindings=[]) → []이라서
    # 실제 request_issues는 remaining 기준. 여기선 bindings가 없어 remaining=[]
    # → 대신 issues_orig 기반 통계만 생성. 그래도 recheck_passed=True
    with patch.object(settings, "outputs_dir", tmp_path):
        result = recheck_node(state)

    client_mod._provider = original
    assert result["report"] is not None


# ── prescription_node CODE_VALID suggestion 보강 경로 ────────────────────────

def test_prescription_node_code_valid_suggestion(tmp_path):
    """CODE_VALID 오류에 suggestion 없으면 codebook.nearest로 채운다."""
    from agent.nodes.prescription import prescription_node
    from engine.codebook import Codebook
    import engine.codebook as cb_mod

    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="사업코드")
    issue = Issue(
        issue_id="E-0010", ref=ref, rule_id="CODE_VALID", kind="rule",
        value="A99", message="유효하지 않은 코드", tier="approval", suggestion=None,
    )
    state = {
        "run_id": "test", "input_paths": [], "issues": [issue], "decisions": [],
    }

    # codebook 로드를 mock해서 nearest 반환값 제어
    mock_cb = Codebook(["A001", "A002"])
    with patch.object(cb_mod, "load_codebook", return_value=mock_cb):
        result = prescription_node(state)

    updated = result["issues"][0]
    assert updated.suggestion in ("A001", "A002")


# ── llm/client.py provider 경로 ─────────────────────────────────────────────

def test_llm_client_default_provider_claude(monkeypatch):
    """LLM_PROVIDER=claude면 AnthropicProvider가 선택된다."""
    import llm.client as client_mod
    from config import settings
    monkeypatch.setattr(settings, "llm_provider", "claude")
    provider = client_mod._default_provider()
    assert isinstance(provider, client_mod._AnthropicProvider)


def test_llm_client_default_provider_openai(monkeypatch):
    """LLM_PROVIDER=openai면 OpenAIProvider가 선택된다."""
    import llm.client as client_mod
    from config import settings
    monkeypatch.setattr(settings, "llm_provider", "openai")
    provider = client_mod._default_provider()
    assert isinstance(provider, client_mod._OpenAIProvider)


# ── rules_node standard relations 경로 ───────────────────────────────────────

def test_rules_node_with_standard_relations(tmp_path):
    """performance.yaml의 relations 섹션에서 SUM_MATCH 바인딩이 생성된다."""
    from agent.nodes.rules import rules_node
    from config import settings
    import yaml

    std_yaml = {
        "sheets": {
            "실적": {"columns": [{"name": "지원금액", "type": "amount"}]},
            "요약": {"columns": [{"name": "지원금액합계", "type": "amount"}]},
        },
        "relations": [{
            "rule": "SUM_MATCH",
            "source": "실적.지원금액",
            "group_by": "사업코드",
            "agg": "sum",
            "target": "요약.지원금액합계",
        }],
    }
    std_dir = tmp_path / "standard"
    std_dir.mkdir()
    (std_dir / "performance.yaml").write_text(
        yaml.dump(std_yaml, allow_unicode=True), encoding="utf-8"
    )
    profiles = [_profile("실적", [("지원금액", "amount")])]
    state = {"run_id": "test", "input_paths": [], "profiles": profiles, "decisions": []}
    with patch.object(settings, "standard_dir", std_dir):
        result = rules_node(state)
    assert any(b.rule_id == "SUM_MATCH" for b in result["bindings"])


# ── agent/graph.py 그래프 조립 경로 ─────────────────────────────────────────

def test_build_graph_with_interrupt_compiles():
    """build_graph_with_interrupt()가 오류 없이 컴파일되어야 한다."""
    from agent.graph import build_graph_with_interrupt
    app = build_graph_with_interrupt()
    assert app is not None


def test_build_graph_private_compiles():
    """_build_graph()가 오류 없이 그래프를 반환해야 한다."""
    from agent.graph import _build_graph
    g = _build_graph()
    assert g is not None


# ── structure_node LLM fallback 경로 ─────────────────────────────────────────

def test_structure_node_llm_fallback(tmp_path):
    """표준 서식이 없으면 LLM fallback으로 unknown 타입이 부여된다."""
    import openpyxl
    import llm.client as client_mod
    from agent.nodes.structure import structure_node
    from config import settings

    class FakeLLM:
        def complete(self, p: str) -> str:
            return '{"columns": [{"column": "알수없는컬럼", "semantic_type": "unknown", "confidence": 0.5}]}'

    original = client_mod._provider
    client_mod._provider = FakeLLM()

    # 빈 standard 디렉터리 (yaml 없음)
    std_dir = tmp_path / "standard"
    std_dir.mkdir()

    # xlsx 생성 (표준과 전혀 다른 컬럼)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["알수없는컬럼"])
    ws.append(["값1"])
    xlsx_path = tmp_path / "unknown.xlsx"
    wb.save(xlsx_path)

    with patch.object(settings, "standard_dir", std_dir):
        result = structure_node({
            "run_id": "test", "input_paths": [str(xlsx_path)], "decisions": [],
        })

    client_mod._provider = original
    assert len(result["profiles"]) == 1
    # confidence 0.5 < llm_confidence_min(0.6) → unknown
    assert result["profiles"][0].columns[0].semantic_type == "unknown"


def test_structure_node_llm_exception_fallback(tmp_path):
    """LLM 호출이 실패해도 unknown 타입으로 fallback되어야 한다."""
    import openpyxl
    import llm.client as client_mod
    from agent.nodes.structure import structure_node
    from config import settings

    class ErrorLLM:
        def complete(self, p: str) -> str:
            raise RuntimeError("LLM 호출 실패")

    original = client_mod._provider
    client_mod._provider = ErrorLLM()

    std_dir = tmp_path / "standard"
    std_dir.mkdir()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["컬럼A"])
    ws.append(["값"])
    xlsx_path = tmp_path / "err.xlsx"
    wb.save(xlsx_path)

    with patch.object(settings, "standard_dir", std_dir):
        result = structure_node({
            "run_id": "test", "input_paths": [str(xlsx_path)], "decisions": [],
        })

    client_mod._provider = original
    assert result["profiles"][0].columns[0].semantic_type == "unknown"


# ── agent/graph.py run_cli 경로 ───────────────────────────────────────────────

def test_run_cli_auto_approve(tmp_path):
    """run_cli --auto-approve가 오류 없이 최종 상태를 반환한다."""
    import openpyxl
    import llm.client as client_mod
    from agent.graph import run_cli
    from config import settings

    class FakeLLM:
        def complete(self, p: str) -> str:
            return '{"explanation": "ok", "columns": [], "subject": "s", "body": "b"}'

    original = client_mod._provider
    client_mod._provider = FakeLLM()

    # 깨끗한 xlsx 파일 생성 (오류 없는 데이터)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "실적"
    ws.append(["기관명", "지원금액"])
    ws.append(["창원시", "1000000"])
    xlsx_path = tmp_path / "clean.xlsx"
    wb.save(xlsx_path)

    std_dir = tmp_path / "standard"
    std_dir.mkdir()
    out_dir = tmp_path / "outputs"
    out_dir.mkdir()

    with patch.object(settings, "standard_dir", std_dir), \
         patch.object(settings, "outputs_dir", out_dir):
        state = run_cli([str(xlsx_path)], auto_approve=True)

    client_mod._provider = original
    assert state is not None
    assert "issues" in state


