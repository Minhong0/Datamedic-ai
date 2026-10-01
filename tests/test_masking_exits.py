"""사업자번호 표시 마스킹 — 화면·리포트·메일·LLM 프롬프트 출구 (RULES.md 0절 6항)."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone

import openpyxl

import llm.client as llm_client
from engine import exporter
from engine.masking import mask_change, mask_issue, mask_text
from engine.models import CellRef, Change, Issue

RAW = "123-45-67890"
RAW_PLAIN = "1234567890"


def _issue(value=RAW, message=None, suggestion=None, rule="BIZNO_CHECKSUM", col="사업자번호", iid="E-0001"):
    return Issue(
        issue_id=iid, ref=CellRef(file="a.xlsx", sheet="실적", row=2, column=col),
        rule_id=rule, kind="rule", value=value,
        message=message or f"사업자번호 체크디지트가 맞지 않습니다: '{value}'",
        tier="request", suggestion=suggestion,
    )


def test_mask_text_hyphenated_only():
    assert mask_text(f"번호 {RAW} 확인") == "번호 123-45-***** 확인"
    assert mask_text("지원금액 1000000000원") == "지원금액 1000000000원"   # 10자리 금액은 가리지 않는다


def test_mask_issue_hides_hyphenated_and_plain_values_and_keeps_original():
    iss = _issue(value=RAW_PLAIN, suggestion=RAW)
    masked = mask_issue(iss)
    for text in (masked.value, masked.suggestion, masked.message):
        assert "67890" not in text and RAW_PLAIN not in text
    assert masked.value == "123-45-*****"
    assert iss.value == RAW_PLAIN and iss.suggestion == RAW   # 원본(fixer 입력)은 불변


def test_mask_issue_leaves_other_columns_untouched():
    iss = _issue(value="1,000원", message="금액 오류: 1,000원", rule="AMOUNT_NORMALIZE", col="지원금액")
    assert mask_issue(iss).value == "1,000원"


def test_mask_issue_masks_bizno_inside_other_column_message():
    iss = _issue(value="청년창업", message=f"수혜기업 불일치: {RAW}", rule="BLANK", col="사업명")
    assert RAW not in mask_issue(iss).message


def test_mask_change_only_bizno_column_is_fully_masked():
    ts = datetime.now(timezone.utc)
    c = Change(change_id="C-0001", issue_id="E-0001", ref=CellRef(file="a", sheet="실적", row=2, column="사업자번호"),
               before=RAW_PLAIN, after=RAW, tier="auto", decided_by="system", timestamp=ts)
    m = mask_change(c)
    assert m.before == "123-45-*****" and m.after == "123-45-*****"
    assert c.before == RAW_PLAIN


def test_exports_are_masked(tmp_path):
    iss = _issue(suggestion=RAW)
    ts = datetime.now(timezone.utc)
    ch = Change(change_id="C-0001", issue_id="E-0001", ref=iss.ref, before=RAW_PLAIN, after=RAW,
                tier="auto", decided_by="system", timestamp=ts)
    exporter.save_issues([iss], tmp_path)
    exporter.save_changelog([ch], tmp_path)
    exporter.save_diagnosis_xlsx([iss], tmp_path)
    for name in ("issues.csv", "changelog.csv"):
        text = (tmp_path / name).read_text(encoding="utf-8-sig")
        assert "67890" not in text and RAW_PLAIN not in text, name
        assert "123-45-*****" in text
    wb = openpyxl.load_workbook(tmp_path / "diagnosis.xlsx") if (tmp_path / "diagnosis.xlsx").exists() else None
    if wb is None:
        wb = openpyxl.load_workbook(next(tmp_path.glob("*.xlsx")))
    cells = [str(c.value) for ws in wb for row in ws.iter_rows() for c in row if c.value is not None]
    assert not any("67890" in c for c in cells)


class _Capture:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps({"reasoning": "ok", "subject": "s", "body": "b"})


def test_llm_prompts_and_mail_do_not_contain_raw_bizno(monkeypatch, tmp_path):
    from agent.nodes.prescription import _add_tier_reasoning
    from engine.models import Dataset
    from agent.nodes.recheck import recheck_node
    from config import settings

    monkeypatch.setattr(llm_client, "_cache_get", lambda k: None)
    monkeypatch.setattr(llm_client, "_cache_set", lambda k, v: None)
    cap = _Capture()
    llm_client.set_provider(cap)
    try:
        _add_tier_reasoning([_issue(suggestion=RAW)], "사업자번호 점검")
        assert cap.prompts and not any("67890" in p for p in cap.prompts)

        # 보완 요청 메일: 잔여 request 이슈의 사업자번호가 프롬프트·본문에 없어야 한다
        import pandas as pd
        df = pd.DataFrame([[RAW]], columns=["사업자번호"], dtype=str)
        df.insert(0, "_row", [2])
        ds = Dataset({("a.xlsx", "실적"): df})
        from engine.models import RuleBinding
        monkeypatch.setattr(settings, "outputs_dir", tmp_path)
        monkeypatch.setattr("notify.mailer.send_all", lambda reqs, run_dir: reqs)
        cap.prompts.clear()
        # 잔여 request 이슈(국세청 미등록 등)를 강제로 만들어 메일 경로를 실행한다
        monkeypatch.setattr("engine.validator.run", lambda *a, **k: [
            _issue(rule="BIZNO_NOT_FOUND", message=f"국세청에 등록되지 않은 사업자번호: '{RAW}'")])
        bind = RuleBinding(rule_id="BIZNO_VERIFY", targets=["실적.사업자번호"], source="semantic_default")
        state = recheck_node({"dataset": ds, "cleaned": ds, "bindings": [bind], "run_id": "20260101-000001",
                              "issues": [_issue()], "changes": []})
        assert state["requests"], "보완 요청이 생성돼야 한다"
        assert not any("67890" in p for p in cap.prompts)
        for r in state["requests"]:
            assert "67890" not in r.subject + r.body
    finally:
        llm_client.set_provider(None)  # type: ignore[arg-type]


def test_malformed_bizno_is_masked_too():
    """자릿수가 틀린 번호(9자리 등)는 mask_bizno 가 그대로 돌려주므로 별도로 가린다."""
    iss = _issue(value="123-45-6789", rule="BIZNO_FORMAT",
                 message="사업자번호 형식이 올바르지 않습니다: '123-45-6789' (숫자 10자리가 아닙니다)")
    m = mask_issue(iss)
    assert "6789" not in (m.value or "") and "6789" not in m.message
    assert m.value == "123-45-****"
    assert mask_text("번호 123-45-6789 확인") == "번호 123-45-***** 확인"
    short = mask_issue(_issue(value="12345", rule="BIZNO_FORMAT", message="형식 오류: '12345'"))
    assert short.value == "*****" and "12345" not in short.message
    # 번호가 아닌 값('없음')과 날짜는 건드리지 않는다
    assert mask_issue(_issue(value="없음", rule="BIZNO_FORMAT", message="필수값 누락")).value == "없음"
    assert mask_text("지원일자 2026-01-21") == "지원일자 2026-01-21"
