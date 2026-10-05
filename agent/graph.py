"""LangGraph 그래프 조립 + CLI 진입점."""
from __future__ import annotations

import argparse
import glob
import logging
from datetime import datetime

from langgraph.graph import END, StateGraph
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt

from agent.state import GraphState
from agent.nodes.aggregate import (
    agg_clarify_node, agg_confirm_node, agg_execute_node, agg_translate_node,
    route_after_confirm, route_after_execute, route_after_translate,
)
from agent.nodes.plan import plan_node
from agent.nodes.planner import planner_node
from agent.nodes.structure import structure_node
from agent.nodes.rules import rules_node
from agent.nodes.diagnosis import diagnosis_node
from agent.nodes.prescription import prescription_node
from agent.nodes.treatment import treatment_node
from agent.nodes.recheck import recheck_node
from engine.models import Decision

_MAX_LOOPS = 2

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def _analyze_step(state: GraphState) -> GraphState:
    """structure + rules 를 연속 실행하는 복합 노드."""
    s = structure_node(state)
    s = rules_node(s)
    return {**s, "agent_phase": "analyzed"}


def _diagnose_step(state: GraphState) -> GraphState:
    """diagnosis + prescription 을 연속 실행하는 복합 노드."""
    s = diagnosis_node(state)
    s = prescription_node(s)
    return {**s, "agent_phase": "diagnosed"}


def _treatment_step(state: GraphState) -> GraphState:
    """treatment 실행 후 phase 를 갱신한다."""
    s = treatment_node(state)
    prev_phase = state.get("agent_phase", "")
    new_phase = "applied" if prev_phase == "approved" else "fixed"
    return {**s, "agent_phase": new_phase}


def _recheck_step(state: GraphState) -> GraphState:
    """recheck 실행 후 phase 를 갱신한다."""
    s = recheck_node(state)
    return {**s, "agent_phase": "finalized"}


def _ask_approval_node(state: GraphState) -> GraphState:
    """approval 등급 이슈를 사람에게 제시하고 결정을 기다린다."""
    approval_issues = [i for i in state.get("issues", []) if i.tier == "approval"]
    raw_decisions = interrupt({
        "type": "approval_required",
        "issues": [i.model_dump() for i in approval_issues],
        "count": len(approval_issues),
    })

    human_decisions: list[Decision] = []
    if isinstance(raw_decisions, list):
        for d in raw_decisions:
            if isinstance(d, dict):
                human_decisions.append(Decision(**d))
            elif isinstance(d, Decision):
                human_decisions.append(d)

    return {
        **state,
        "decisions": human_decisions,
        "agent_phase": "approved",
    }


def _ask_clarify_node(state: GraphState) -> GraphState:
    """계획이 모호하거나 비었을 때 사용자에게 되묻는다 (interrupt)."""
    plan = state["plan"]
    answer = interrupt({
        "type": "clarification_required",
        "question": plan.question,
        "options": plan.options,
    })
    choices = [a for a in (answer if isinstance(answer, list) else [answer]) if isinstance(a, str)]
    return {**state, "plan_choices": [*state.get("plan_choices", []), *choices]}


def _route_from_plan(state: GraphState) -> str:
    return "ask_clarify" if state["plan"].status == "needs_clarification" else "planner"


def _route_from_planner(state: GraphState) -> str:
    action = state.get("agent_action", "done")
    if state.get("agent_loop_count", 0) >= 10:
        return END
    return {
        "analyze_files":   "analyze",
        "run_diagnosis":   "diagnose",
        "fix_auto":        "treatment",
        "ask_approval":    "ask_approval",
        "apply_decisions": "treatment",
        "run_aggregate":   "agg_translate",
        "finalize":        "recheck",
        "done":            END,
    }.get(action, END)


def build_react_graph():
    """ReAct 에이전트 그래프 (Streamlit 용)."""
    g = StateGraph(GraphState)
    g.add_node("plan",         plan_node)
    g.add_node("ask_clarify",  _ask_clarify_node)
    g.add_node("planner",      planner_node)
    g.add_node("analyze",      _analyze_step)
    g.add_node("diagnose",     _diagnose_step)
    g.add_node("treatment",    _treatment_step)
    g.add_node("ask_approval", _ask_approval_node)
    g.add_node("recheck",      _recheck_step)
    g.add_node("agg_translate", agg_translate_node)
    g.add_node("agg_clarify",   agg_clarify_node)
    g.add_node("agg_confirm",   agg_confirm_node)
    g.add_node("agg_execute",   agg_execute_node)

    g.set_entry_point("plan")
    g.add_conditional_edges("plan", _route_from_plan,
                            {"ask_clarify": "ask_clarify", "planner": "planner"})
    g.add_edge("ask_clarify", "plan")
    g.add_conditional_edges(
        "planner",
        _route_from_planner,
        {
            "analyze":      "analyze",
            "diagnose":     "diagnose",
            "treatment":    "treatment",
            "ask_approval": "ask_approval",
            "recheck":      "recheck",
            "agg_translate": "agg_translate",
            END:            END,
        },
    )
    g.add_conditional_edges("agg_translate", route_after_translate,
                            {"agg_clarify": "agg_clarify", "agg_confirm": "agg_confirm",
                             "agg_execute": "agg_execute"})
    g.add_edge("agg_clarify", "agg_translate")
    g.add_conditional_edges("agg_confirm", route_after_confirm,
                            {"agg_translate": "agg_translate", "agg_execute": "agg_execute"})
    g.add_conditional_edges("agg_execute", route_after_execute,
                            {"agg_translate": "agg_translate", "planner": "planner"})
    for node in ("analyze", "diagnose", "treatment", "ask_approval", "recheck"):
        g.add_edge(node, "planner")

    from agent.serde import PickleSerde
    checkpointer = MemorySaver(serde=PickleSerde())
    return g.compile(checkpointer=checkpointer)


def _loop_back_node(state: GraphState) -> GraphState:
    """재시도: cleaned 데이터셋을 기준으로 잔여 오류를 재처리한다."""
    return {
        **state,
        "dataset": state["cleaned"],
        "issues": state.get("remaining_issue_list", []),
        "decisions": [],
        "cleaned": None,
        "changes": [],
    }


def _route_after_recheck(state: GraphState) -> str:
    remaining = state.get("remaining_issue_list", [])
    if remaining and state.get("loop_count", 1) < _MAX_LOOPS:
        return "loop_back"
    return END

def _build_graph():
    """중단 없이 끝까지 실행하는 단순 그래프 (auto_approve CLI용)."""
    g = StateGraph(GraphState)
    g.add_node("structure",    structure_node)
    g.add_node("rules",        rules_node)
    g.add_node("diagnosis",    diagnosis_node)
    g.add_node("prescription", prescription_node)
    g.add_node("treatment",    treatment_node)
    g.add_node("recheck",      recheck_node)
    g.add_node("loop_back",    _loop_back_node)

    g.set_entry_point("structure")
    g.add_edge("structure",    "rules")
    g.add_edge("rules",        "diagnosis")
    g.add_edge("diagnosis",    "prescription")
    g.add_edge("prescription", "treatment")
    g.add_edge("treatment",    "recheck")
    g.add_conditional_edges("recheck", _route_after_recheck, {"loop_back": "loop_back", END: END})
    g.add_edge("loop_back",    "prescription")
    return g


def build_graph_with_interrupt():
    """approval 단계에서 중단하는 그래프 (Streamlit용)."""
    g = StateGraph(GraphState)
    g.add_node("structure",    structure_node)
    g.add_node("rules",        rules_node)
    g.add_node("diagnosis",    diagnosis_node)
    g.add_node("prescription", prescription_node)
    g.add_node("treatment",    treatment_node)
    g.add_node("recheck",      recheck_node)
    g.add_node("loop_back",    _loop_back_node)

    g.set_entry_point("structure")
    g.add_edge("structure",    "rules")
    g.add_edge("rules",        "diagnosis")
    g.add_edge("diagnosis",    "prescription")
    g.add_edge("prescription", "treatment")
    g.add_edge("treatment",    "recheck")
    g.add_conditional_edges("recheck", _route_after_recheck, {"loop_back": "loop_back", END: END})
    g.add_edge("loop_back",    "prescription")

    checkpointer = MemorySaver()
    return g.compile(
        checkpointer=checkpointer,
        interrupt_before=["treatment"],
    )


def run_cli(input_paths: list[str], auto_approve: bool = False) -> GraphState:
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    if auto_approve:
        g = _build_graph().compile()
        initial: GraphState = {
            "run_id": run_id,
            "input_paths": input_paths,
            "decisions": [],
        }

        partial_g = StateGraph(GraphState)
        partial_g.add_node("structure",    structure_node)
        partial_g.add_node("rules",        rules_node)
        partial_g.add_node("diagnosis",    diagnosis_node)
        partial_g.add_node("prescription", prescription_node)
        partial_g.set_entry_point("structure")
        partial_g.add_edge("structure", "rules")
        partial_g.add_edge("rules", "diagnosis")
        partial_g.add_edge("diagnosis", "prescription")
        partial_g.add_edge("prescription", END)
        partial = partial_g.compile()
        mid_state = partial.invoke(initial)

        decisions = []
        for issue in mid_state.get("issues", []):
            if issue.tier == "auto":
                decisions.append(Decision(issue_id=issue.issue_id, action="apply", decided_by="system"))
            elif issue.tier == "approval":
                decisions.append(Decision(issue_id=issue.issue_id, action="apply", decided_by="operator"))
            else:
                decisions.append(Decision(issue_id=issue.issue_id, action="request", decided_by="system"))

        final_initial = {**mid_state, "decisions": decisions}
        treat_g = StateGraph(GraphState)
        treat_g.add_node("treatment", treatment_node)
        treat_g.add_node("recheck",   recheck_node)
        treat_g.set_entry_point("treatment")
        treat_g.add_edge("treatment", "recheck")
        treat_g.add_edge("recheck", END)
        final_state = treat_g.compile().invoke(final_initial)
        return final_state

    else:
        app = build_graph_with_interrupt()
        config = {"configurable": {"thread_id": run_id}}
        initial = {"run_id": run_id, "input_paths": input_paths, "decisions": []}
        state = app.invoke(initial, config=config)

        approval_issues = [i for i in state.get("issues", []) if i.tier == "approval"]
        if approval_issues:
            print(f"\n[승인 필요] {len(approval_issues)}건")
            for i in approval_issues:
                ref_str = i.ref.a1 if i.ref else "N/A"
                print(f"  {i.issue_id} [{ref_str}] {i.message}")
            print("approval 이슈가 있습니다. --auto-approve 옵션 또는 UI에서 승인하세요.")
            return state

        return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DataMedic AI CLI")
    parser.add_argument("--inputs", required=True, help="입력 파일 glob 패턴")
    parser.add_argument("--auto-approve", action="store_true")
    args = parser.parse_args()

    paths = glob.glob(args.inputs)
    if not paths:
        print(f"파일을 찾을 수 없습니다: {args.inputs}")
        raise SystemExit(1)

    print(f"처리 파일: {len(paths)}개")
    final = run_cli(paths, auto_approve=args.auto_approve)
    report = final.get("report")
    if report:
        print(f"\n=== 실행 완료 ===")
        print(f"run_id: {report.run_id}")
        print(f"총 오류: {sum(report.issue_counts.values())}건")
        print(f"재검진 통과: {report.recheck_passed}")
