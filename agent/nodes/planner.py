"""ReAct 오케스트레이터 — LLM이 다음 액션을 결정하는 planner 노드."""
from __future__ import annotations

import logging
from typing import Literal

from pydantic import BaseModel

from agent.state import GraphState
from engine.models import Decision

logger = logging.getLogger(__name__)

AgentActionType = Literal[
    "analyze_files",
    "run_diagnosis",
    "fix_auto",
    "ask_approval",
    "apply_decisions",
    "run_aggregate",
    "finalize",
    "done",
]

_MAX_LOOPS = 10


class AgentDecision(BaseModel):
    action: str
    reason: str


def _make_all_decisions(state: GraphState) -> list[Decision]:
    """auto/request 등급 오류에 대한 시스템 결정을 생성한다 (approval 은 담당자가 결정)."""
    issues = state.get("issues", [])
    existing_ids = {d.issue_id for d in state.get("decisions", [])}
    decs: list[Decision] = list(state.get("decisions", []))
    for i in issues:
        if i.issue_id in existing_ids:
            continue
        if i.tier == "auto":
            decs.append(Decision(issue_id=i.issue_id, action="apply", decided_by="system"))
        elif i.tier == "request":
            decs.append(Decision(issue_id=i.issue_id, action="request", decided_by="system"))
    return decs


def _fallback_decision(state: GraphState) -> AgentDecision:
    """LLM 미설정 시 결정론적 상태 기계."""
    phase = state.get("agent_phase", "init")
    issues = state.get("issues", [])
    approval_issues = [i for i in issues if i.tier == "approval"]
    decisions = state.get("decisions", [])
    remaining = state.get("remaining_issue_list", [])
    loop = state.get("agent_loop_count", 0)

    if loop >= _MAX_LOOPS:
        return AgentDecision(action="done", reason="최대 루프 횟수 도달")

    if phase == "init":
        return AgentDecision(action="analyze_files", reason="파일 구조 분석을 시작합니다.")
    if phase == "analyzed":
        return AgentDecision(action="run_diagnosis", reason="규칙 기반 오류 진단을 실행합니다.")
    if phase == "diagnosed":
        if approval_issues and not decisions:
            return AgentDecision(
                action="ask_approval",
                reason=f"승인이 필요한 항목 {len(approval_issues)}건이 있어 담당자 결정을 기다립니다.",
            )
        return AgentDecision(action="fix_auto", reason="자동 수정 가능한 오류를 처리합니다.")
    if phase == "approved":
        return AgentDecision(action="apply_decisions", reason="승인 결정을 적용합니다.")
    if phase in ("fixed", "applied"):
        if state.get("agg_queue"):
            return AgentDecision(action="run_aggregate",
                                 reason="수정된 데이터에 집계 규칙(R5~R7)을 실행합니다.")
        return AgentDecision(action="finalize", reason="재검진 및 결과를 저장합니다.")
    if phase == "aggregated":
        return AgentDecision(action="finalize", reason="재검진 및 결과를 저장합니다.")
    if phase == "finalized":
        if remaining and loop < 3:
            return AgentDecision(action="run_diagnosis", reason=f"잔여 오류 {len(remaining)}건을 재진단합니다.")
        return AgentDecision(action="done", reason="처리가 완료되었습니다.")
    return AgentDecision(action="done", reason="처리가 완료되었습니다.")


_ALLOWED_AFTER: dict[str, set[str]] = {
    "init":      {"analyze_files"},
    "analyzed":  {"run_diagnosis"},
    "diagnosed": {"fix_auto", "ask_approval"},
    "approved":  {"apply_decisions"},
    "fixed":     {"finalize", "run_aggregate"},
    "applied":   {"finalize", "run_aggregate"},
    "aggregated": {"finalize"},
    "finalized": {"run_diagnosis", "done"},
}


def _llm_decision(state: GraphState) -> AgentDecision:
    from llm.client import complete_json

    issues = state.get("issues", [])
    auto_c = sum(1 for i in issues if i.tier == "auto")
    appr_c = sum(1 for i in issues if i.tier == "approval")
    req_c  = sum(1 for i in issues if i.tier == "request")
    issue_summary = (
        f"총 {len(issues)}건 (자동:{auto_c} 승인:{appr_c} 요청:{req_c})"
        if issues else "아직 진단 전"
    )
    remaining = state.get("remaining_issue_list", [])

    resp = complete_json(
        "agent_plan",
        {
            "goal":             state.get("goal", "데이터 정합성 검증"),
            "agent_phase":      state.get("agent_phase", "init"),
            "file_count":       len(state.get("input_paths", [])),
            "issue_summary":    issue_summary,
            "remaining_count":  len(remaining),
            "last_action":      state.get("agent_action", "-"),
            "last_reason":      state.get("agent_reason", "-"),
            "agent_loop_count": state.get("agent_loop_count", 0),
        },
        AgentDecision,
    )
    allowed = set(_ALLOWED_AFTER.get(state.get("agent_phase", "init"), {"done"}))
    if state.get("agg_queue"):
        allowed.discard("finalize")
    if resp.action not in allowed:
        raise ValueError(f"단계 {state.get('agent_phase')}에서 허용되지 않는 액션: {resp.action}")
    return resp


def planner_node(state: GraphState) -> GraphState:
    """LLM(또는 폴백) 으로 다음 액션을 결정한다."""
    loop = state.get("agent_loop_count", 0) + 1

    try:
        decision = _llm_decision(state)
    except Exception as e:
        logger.debug("LLM 플래너 폴백: %s", e)
        decision = _fallback_decision(state)

    logger.info("planner [%d] %s — %s", loop, decision.action, decision.reason)

    updated: GraphState = {
        **state,
        "agent_action":     decision.action,
        "agent_reason":     decision.reason,
        "agent_loop_count": loop,
    }

    if decision.action == "fix_auto":
        updated["decisions"] = _make_all_decisions({**state, "decisions": []})

    if decision.action == "apply_decisions":
        updated["decisions"] = _make_all_decisions(state)

    return updated
