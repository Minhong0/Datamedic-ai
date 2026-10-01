"""T30: 목표(Plan) 범위 가드 — docs/planner_design.md.

실제 LLM은 호출하지 않는다 (FakeLLM + 캐시 비활성).
"""
from __future__ import annotations

import json

import pandas as pd
import pytest
from langgraph.types import Command

import llm.client as llm_client
from agent.graph import build_react_graph
from agent.nodes.plan import build_plan
from agent.nodes.treatment import guard_decisions, treatment_node
from engine.models import CellRef, Dataset, Decision, Issue, RuleBinding
from engine.planning import PlanScope, PlanScopeError, active_scope, require_capability
from engine.rules.base import RuleContext
from engine.rules.bizno import bizno_checksum_ok
from engine.rules.row_rules import DuplicateRowRule

SAMPLE_GOAL = "분기 지원금액 합계 검증 및 중복 행 제거"


class FakeLLM:
    """plan_goal 에만 응답하고 나머지 프롬프트는 실패시킨다 (각 노드의 폴백 경로 사용)."""

    def __init__(self, plan_ids: list[str] | None = None, fail: bool = False) -> None:
        self.plan_ids = plan_ids
        self.fail = fail

    def complete(self, prompt: str) -> str:
        if self.fail or self.plan_ids is None or "계획 수립기" not in prompt:
            raise RuntimeError("fake llm: 응답 없음")
        return json.dumps({"capabilities": self.plan_ids, "reasoning": "fake"}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _no_real_llm(monkeypatch):
    monkeypatch.setattr(llm_client, "_cache_get", lambda key: None)
    monkeypatch.setattr(llm_client, "_cache_set", lambda key, value: None)
    llm_client.set_provider(FakeLLM(fail=True))
    yield
    llm_client.set_provider(None)  # type: ignore[arg-type]


def _caps(plan) -> set[str]:
    """목표가 직접 요청한 능력 (선행 작업 제외)."""
    return {s.capability for s in plan.steps if s.status == "run"}


# ── 계획 해석 ─────────────────────────────────────────────────────────────────

def test_sample_goal_asks_clarification_when_llm_fails():
    plan = build_plan(SAMPLE_GOAL)
    assert plan.status == "needs_clarification"
    assert {o["id"] for o in plan.options} == {"sum_match", "sum_limit", "aggregate_view"}
    assert plan.delete_intent is True


def test_llm_picking_sum_without_evidence_still_clarifies():
    llm_client.set_provider(FakeLLM(["sum_match", "duplicate_row", "bizno_api"]))
    plan = build_plan(SAMPLE_GOAL)
    assert plan.status == "needs_clarification"   # sum_match 는 근거 키워드가 없어 버려짐


def test_clarification_answer_builds_ready_plan_with_dependency():
    plan = build_plan(SAMPLE_GOAL, ["sum_match"])
    assert plan.status == "ready"
    by = {s.capability: s for s in plan.steps}
    assert by["sum_match"].status == "run"
    assert by["duplicate_row"].status == "run"
    assert by["amount_normalize"].status == "dependency"
    for skipped in ("bizno_api", "bizno_format", "date_normalize", "blank_check"):
        assert by[skipped].status == "skipped" and by[skipped].reason


def test_closed_business_goal_includes_api_and_skips_amount():
    plan = build_plan("폐업 업체 찾아줘")
    by = {s.capability: s.status for s in plan.steps}
    assert by["bizno_api"] == "run"
    assert by["bizno_format"] == "dependency"
    assert by["amount_normalize"] == "skipped"


def test_llm_cannot_add_api_or_duplicate_without_keyword_evidence():
    llm_client.set_provider(FakeLLM(["bizno_api", "duplicate_row", "date_normalize"]))
    plan = build_plan("날짜 형식 점검")
    assert _caps(plan) == {"date_normalize"}


def test_llm_failure_never_falls_back_to_full_run():
    plan = build_plan("ㅁㄴㅇㄹ 알 수 없는 목표")
    assert plan.status == "needs_clarification"
    assert plan.options


@pytest.mark.parametrize("goal,expected", [
    ("날짜 형식 오류 점검", {"date_normalize"}),
    ("지원금액 표기 오류 점검", {"amount_normalize"}),
    ("사업자번호 형식 오류 점검", {"bizno_format"}),
    ("기관별 엑셀 형식 오류·누락값 일괄 정리", {"blank_check"}),
])
def test_field_specific_goal_does_not_expand_to_general(goal, expected):
    llm_client.set_provider(FakeLLM(["date_normalize", "amount_normalize", "bizno_format", "blank_check"]))
    assert _caps(build_plan(goal)) == expected


def test_general_goal_expands_without_field_keywords():
    caps = _caps(build_plan("경남 20개 시군 사업실적 취합 전 오류 점검"))
    assert {"blank_check", "amount_normalize", "date_normalize", "bizno_format"} <= caps
    assert "bizno_api" not in caps and "duplicate_row" not in caps


def test_empty_goal_runs_default_set_only():
    plan = build_plan("")
    caps = _caps(plan)
    assert plan.source == "full"
    assert {"blank_check", "amount_normalize", "date_normalize", "bizno_format", "bizno_api"} <= caps
    assert "duplicate_row" not in caps and "sum_match" not in caps   # 목표 선택형은 기본 목록에 없음


# ── 범위 가드 ─────────────────────────────────────────────────────────────────

def test_nts_provider_blocked_without_bizno_api():
    from engine.tools.nts_client import MockProvider, get_provider

    with active_scope(PlanScope.from_plan(build_plan("날짜 형식 점검"))):
        with pytest.raises(PlanScopeError):
            get_provider()
        with pytest.raises(PlanScopeError):
            MockProvider().query(["1234567890"])
    with active_scope(PlanScope.from_plan(build_plan("폐업 업체 찾아줘"))):
        require_capability("bizno_api")  # 통과


def test_rule_ids_follow_dependencies():
    scope = PlanScope.from_plan(build_plan(SAMPLE_GOAL, ["sum_match"]))
    assert scope.rule_ids == {"SUM_MATCH", "DUPLICATE_ROW", "AMOUNT_NORMALIZE", "BLANK"}
    assert not scope.allows_issue("BIZNO_NOT_FOUND")
    assert scope.allows_issue("AMOUNT_SUSPECT_UNIT")


# ── DUPLICATE_ROW ─────────────────────────────────────────────────────────────

def _df(rows):
    df = pd.DataFrame(rows, columns=["a", "b"], dtype=str)
    df.insert(0, "_row", range(2, 2 + len(df)))
    return df


def test_duplicate_row_is_approval_and_distinguishes_cross_file():
    ds = Dataset({
        ("f1.xlsx", "실적"): _df([["1", "x"], ["1", "x"], ["2", "y"]]),
        ("f2.xlsx", "실적"): _df([["2", "y"], ["3", "z"]]),
    })
    binding = RuleBinding(rule_id="DUPLICATE_ROW", targets=[], source="semantic_default")
    issues = DuplicateRowRule().check(ds, binding, RuleContext())
    assert {i.tier for i in issues} == {"approval"}
    same = [i for i in issues if i.value == DuplicateRowRule.SAME_FILE]
    cross = [i for i in issues if i.value == DuplicateRowRule.CROSS_FILE]
    assert len(same) == 1 and same[0].ref.file == "f1.xlsx" and same[0].ref.row == 3
    assert len(cross) == 1 and cross[0].ref.file == "f2.xlsx"


def _dup_issue(issue_id="E-0001", row=3):
    return Issue(issue_id=issue_id, ref=CellRef(file="f1.xlsx", sheet="실적", row=row, column="a"),
                 rule_id="DUPLICATE_ROW", kind="rule", value="(완전중복)", message="m", tier="approval")


def test_system_decision_cannot_delete_duplicate_rows():
    issue = _dup_issue()
    kept = guard_decisions([issue], [Decision(issue_id="E-0001", action="apply", decided_by="system")], None)
    assert kept == []
    ok = guard_decisions([issue], [Decision(issue_id="E-0001", action="apply", decided_by="operator")], None)
    assert len(ok) == 1


def test_treatment_deletes_per_row_with_change_only_for_operator():
    ds = Dataset({("f1.xlsx", "실적"): _df([["1", "x"], ["1", "x"], ["1", "x"]])})
    issues = [_dup_issue("E-0001", 3), _dup_issue("E-0002", 4)]
    sys_state = {"dataset": ds, "issues": issues,
                 "decisions": [Decision(issue_id=i.issue_id, action="apply", decided_by="system") for i in issues]}
    assert treatment_node(sys_state)["changes"] == []

    op_state = {"dataset": ds, "issues": issues,
                "decisions": [Decision(issue_id=i.issue_id, action="apply", decided_by="operator") for i in issues]}
    changes = treatment_node(op_state)["changes"]
    assert len(changes) == 2 and all(c.after is None for c in changes)


# ── 그래프 E2E ────────────────────────────────────────────────────────────────

def _valid_bizno(seed: int) -> str:
    n = seed
    while True:
        d = f"{n:010d}"
        if bizno_checksum_ok(d):
            return f"{d[:3]}-{d[3:5]}-{d[5:]}"
        n += 1


@pytest.fixture
def workbook(tmp_path):
    biz = _valid_bizno(1234567000)
    cols = ["기관명", "사업코드", "사업명", "수혜기업명", "사업자번호", "지원일자", "지원금액"]
    rows = [
        ["거제시", "B001", "청년창업지원사업", "가나(주)", biz, "26-01-21", "1,000원"],
        ["거제시", "B001", "청년창업지원사업", "가나(주)", biz, "26-01-21", "1,000원"],   # 완전 중복
        ["거제시", "B002", "스마트공장구축사업", "다라(주)", biz, "2026/02/03", "2,000"],
    ]
    path = tmp_path / "거제시.xlsx"
    with pd.ExcelWriter(path) as w:
        pd.DataFrame(rows, columns=cols).to_excel(w, sheet_name="실적", index=False)
        pd.DataFrame([["B001", "2", "2000"], ["B002", "1", "2000"]],
                     columns=["사업코드", "지원건수", "지원금액합계"]).to_excel(w, sheet_name="요약", index=False)
    return str(path)


class CountingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def query(self, bizno_list):
        self.calls += 1
        from engine.tools.nts_client import MockProvider
        return MockProvider().query(bizno_list)


def _start(graph, path, goal, tmp_path, monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "outputs")
    cfg = {"configurable": {"thread_id": f"t-{abs(hash(goal))}"}}
    graph.invoke({"run_id": "20260101-000000", "input_paths": [path], "goal": goal,
                  "decisions": [], "agent_phase": "init", "agent_loop_count": 0}, cfg)
    return cfg


def test_e2e_sample_goal_clarifies_then_runs_only_planned_rules(workbook, tmp_path, monkeypatch):
    counter = CountingProvider()
    monkeypatch.setattr("engine.tools.nts_client.get_provider", lambda: counter)
    graph = build_react_graph()
    cfg = _start(graph, workbook, SAMPLE_GOAL, tmp_path, monkeypatch)

    snap = graph.get_state(cfg)
    assert snap.next == ("ask_clarify",)           # 되묻기에서 멈춤
    assert not snap.values.get("issues")           # 아직 어떤 규칙도 실행되지 않음

    graph.invoke(Command(resume=["sum_match"]), cfg)
    snap = graph.get_state(cfg)
    assert snap.next == ("ask_approval",)          # 중복 행은 approval → 사람 확인
    issues = snap.values["issues"]
    rule_ids = {i.rule_id for i in issues}
    assert "DUPLICATE_ROW" in rule_ids
    assert all(i.tier == "approval" for i in issues if i.rule_id == "DUPLICATE_ROW")
    assert not {r for r in rule_ids if r.startswith(("BIZNO", "DATE", "BLANK"))}

    graph.invoke(Command(resume=[]), cfg)          # 사람이 아무 결정도 하지 않음
    final = graph.get_state(cfg).values
    assert not any(c.after is None for c in final["changes"])   # 중복 행 자동 삭제 0건
    assert counter.calls == 0                                   # bizno_api 호출 0회
    assert all(c.ref.column != "지원일자" for c in final["changes"])


def test_e2e_closed_business_goal_calls_api_and_skips_amount(workbook, tmp_path, monkeypatch):
    counter = CountingProvider()
    monkeypatch.setattr("engine.tools.nts_client.get_provider", lambda: counter)
    graph = build_react_graph()
    cfg = _start(graph, workbook, "폐업 업체 찾아줘", tmp_path, monkeypatch)

    values = graph.get_state(cfg).values
    assert values["plan"].status == "ready"
    assert counter.calls >= 1                                    # 국세청 조회 실행
    assert not any(i.rule_id.startswith(("AMOUNT", "DATE", "BLANK", "DUPLICATE"))
                   for i in values["issues"])
    assert all(c.ref.column != "지원금액" for c in values.get("changes", []))


# ── 컬럼 한정 공백 확인 (blank_scoped) ────────────────────────────────────────

@pytest.mark.parametrize("goal,cap,col_type", [
    ("날짜 형식 오류 점검", "date_normalize", "date"),
    ("지원금액 표기 오류 점검", "amount_normalize", "amount"),
    ("사업자번호 형식 오류 점검", "bizno_format", "biz_no"),
])
def test_field_goal_adds_scoped_blank_as_dependency(goal, cap, col_type):
    plan = build_plan(goal)
    by = {s.capability: s.status for s in plan.steps}
    assert by[cap] == "run" and by["blank_scoped"] == "dependency"
    assert by["blank_check"] == "skipped"


def test_blank_check_goal_does_not_add_scoped_blank():
    plan = build_plan("누락값 점검")
    by = {s.capability: s.status for s in plan.steps}
    assert by["blank_check"] == "run" and by["blank_scoped"] == "skipped"


def test_scoped_blank_dependency_is_transitive():
    by = {s.capability: s.status for s in build_plan("폐업 업체 찾아줘").steps}
    assert by["bizno_format"] == "dependency" and by["blank_scoped"] == "dependency"


def test_scoped_blank_checks_only_target_columns():
    from engine.rules.blank import BlankRule
    cols = ["기관명", "지원일자", "지원금액"]
    df = pd.DataFrame([["", " ", "1000"], ["거제시", "2026-01-01", " 2000 "], ["  ", "", ""]],
                      columns=cols, dtype=str)
    df.insert(0, "_row", range(2, 5))
    ds = Dataset({("f.xlsx", "실적"): df})
    b = RuleBinding(rule_id="BLANK", targets=[], source="semantic_default",
                    params={"required_cols": ["실적.기관명", "실적.지원일자"], "scope_cols": ["실적.지원일자"]})
    issues = BlankRule().check(ds, b, RuleContext())
    assert {(i.ref.row, i.ref.column) for i in issues} == {(2, "지원일자"), (4, "지원일자")}
    assert all(i.tier == "request" for i in issues)   # 공백 정규화(auto)·빈 행 삭제 없음


def test_e2e_date_goal_reports_blank_dates_only(tmp_path, monkeypatch):
    cols = ["기관명", "사업코드", "사업명", "수혜기업명", "사업자번호", "지원일자", "지원금액"]
    rows = [["", "B001", "청년창업지원사업", "가나(주)", "", "", "1,000원"],
            ["거제시", "B001", "청년창업지원사업", "가나(주)", "123-45-67890", "26-01-21", ""]]
    path = tmp_path / "거제시.xlsx"
    with pd.ExcelWriter(path) as w:
        pd.DataFrame(rows, columns=cols).to_excel(w, sheet_name="실적", index=False)
        pd.DataFrame([["B001", "2", "2000"]], columns=["사업코드", "지원건수", "지원금액합계"]
                     ).to_excel(w, sheet_name="요약", index=False)
    graph = build_react_graph()
    cfg = _start(graph, str(path), "날짜 형식 오류 점검", tmp_path, monkeypatch)
    issues = graph.get_state(cfg).values["issues"]
    blanks = [i for i in issues if i.rule_id == "BLANK"]
    assert [(i.ref.row, i.ref.column) for i in blanks] == [(2, "지원일자")]   # 다른 컬럼 공백은 제외
    assert not any(i.rule_id.startswith(("AMOUNT", "BIZNO", "DUPLICATE")) for i in issues)


# ── 집계 규칙 통합 (R5~R7 을 그래프 안에서 실행) ──────────────────────────────

def _payload(graph, cfg) -> dict:
    snap = graph.get_state(cfg)
    return snap.tasks[0].interrupts[0].value


def test_e2e_sum_limit_goal_runs_aggregate_inside_graph(workbook, tmp_path, monkeypatch):
    graph = build_react_graph()
    cfg = _start(graph, workbook, "기업당 한도 초과 확인", tmp_path, monkeypatch)

    assert graph.get_state(cfg).values["plan"].status == "ready"
    p = _payload(graph, cfg)
    assert p["type"] == "aggregate_clarify"            # 한도 금액·범위를 되묻는다
    assert "한도 금액" in p["question"]

    graph.invoke(Command(resume="5천만원 사업별"), cfg)
    p = _payload(graph, cfg)
    assert p["type"] == "aggregate_confirm"            # 확인 카드
    assert p["card"]["검사 종류"] == "한도 초과 검사"

    graph.invoke(Command(resume="run"), cfg)
    snap = graph.get_state(cfg)
    assert snap.next == ()
    res = snap.values["agg_results"]
    assert len(res) == 1 and not res[0]["skipped"]
    assert res[0]["spec"].template == "SUM_LIMIT"
    assert snap.values["agent_phase"] == "finalized"
    assert not any(i.rule_id.startswith(("BIZNO", "DUPLICATE", "DATE")) for i in snap.values["issues"])


def test_e2e_aggregate_confirm_cancel_skips(workbook, tmp_path, monkeypatch):
    graph = build_react_graph()
    cfg = _start(graph, workbook, "기업당 한도 초과 확인", tmp_path, monkeypatch)
    graph.invoke(Command(resume="5천만원 사업별"), cfg)
    graph.invoke(Command(resume="skip"), cfg)
    res = graph.get_state(cfg).values["agg_results"]
    assert res[0]["skipped"] is True and res[0]["group_issues"] == []


def test_e2e_aggregate_view_out_of_scope_offers_template_choices(workbook, tmp_path, monkeypatch):
    graph = build_react_graph()
    cfg = _start(graph, workbook, SAMPLE_GOAL, tmp_path, monkeypatch)
    graph.invoke(Command(resume=["aggregate_view"]), cfg)      # 합계 되묻기 → 집계 보기
    graph.invoke(Command(resume=[]), cfg)                      # 중복 행 승인 단계: 결정 없음
    p = _payload(graph, cfg)
    assert p["type"] == "aggregate_clarify"
    assert {o["id"] for o in p["options"]} == {"MAX_COUNT", "NEAR_DUPLICATE", "SUM_LIMIT"}

    graph.invoke(Command(resume="MAX_COUNT"), cfg)
    assert _payload(graph, cfg)["type"] == "aggregate_confirm"
    graph.invoke(Command(resume="run"), cfg)
    snap = graph.get_state(cfg)
    assert snap.next == () and snap.values["agg_results"][0]["spec"].template == "MAX_COUNT"
    assert not any(c.after is None for c in snap.values["changes"])   # 중복 행 자동 삭제 0건
