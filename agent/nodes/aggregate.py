"""집계 규칙(R5~R7) 그래프 노드 — 목표(Plan)의 sum_limit·aggregate_view 실행 (docs/planner_design.md).

기존 query.py 의 번역·검증·실행·요약 함수를 재사용하고, 되묻기·확인 카드는 interrupt 로 처리한다.
interrupt 는 노드당 1회만 쓰도록 노드를 분리했다 (재개 시 앞 단계 LLM 호출이 반복되지 않게).

  agg_translate ─┬─ 슬롯 부족/범위 밖 → agg_clarify ─┐
                 ├─ 완전 → agg_confirm → agg_execute  │ (agg_translate 로 복귀)
                 └─ 포기 ────────────────► agg_execute
  agg_execute ─┬─ 대기열 남음 → agg_translate
               └─ 끝 → planner (phase=aggregated)
"""
from __future__ import annotations

import logging

from langgraph.types import interrupt

from agent.nodes.query import (
    MAX_CLARIFY_ROUNDS,
    clarify_node,
    execute_node,
    respond_node,
    translate_node,
    validate_node,
)
from agent.state import GraphState

logger = logging.getLogger(__name__)

# 능력 → 질문 시드 (목표 문장 뒤에 덧붙여 번역 정확도를 높인다)
_CAPABILITY_SEED = {"sum_limit": " 한도 초과 검사", "aggregate_view": ""}

# 범위 밖일 때 사용자가 고르는 검사 → 시드 문장
TEMPLATE_CHOICES: dict[str, tuple[str, str]] = {
    "MAX_COUNT": ("횟수 초과(중복 수혜) 검사", "한 기업이 사업 두 개 이상 받은 기업 찾아줘"),
    "NEAR_DUPLICATE": ("이중 지급 의심 검사", "같은 사업에서 돈이 두 번 나간 것 같은 거"),
    "SUM_LIMIT": ("합계 한도 초과 검사", "한도 초과"),
}


def aggregate_capabilities(plan) -> list[str]:
    """계획에서 직접 요청된 집계 능력 (그래프 안에서 실행)."""
    from engine.planning import BY_ID
    return [s.capability for s in plan.steps
            if s.status == "run" and BY_ID[s.capability].aggregate]


def agg_translate_node(state: GraphState) -> GraphState:
    queue = state.get("agg_queue", [])
    q = dict(state.get("query") or {})
    if not q.get("query_text"):
        q["query_text"] = state.get("goal", "") + _CAPABILITY_SEED.get(queue[0], "")
        q["clarify_count"] = 0
    q.pop("clarify_question", None)
    q = translate_node(q)
    if q.get("intent") != "out_of_scope":
        q = validate_node(q)
    # 범위 밖이 되풀이되면 포기 (무한 되묻기 방지)
    if q.get("intent") == "out_of_scope" and q.get("clarify_count", 0) >= MAX_CLARIFY_ROUNDS:
        q["skipped"] = True
    return {**state, "query": q}


def route_after_translate(state: GraphState) -> str:
    q = state.get("query", {})
    if q.get("skipped"):
        return "agg_execute"
    if q.get("confirmed_spec") is not None and not q.get("clarify_question"):
        return "agg_confirm"
    return "agg_clarify"


def agg_clarify_node(state: GraphState) -> GraphState:
    q = dict(state["query"])
    out_of_scope = q.get("intent") == "out_of_scope"
    answer = interrupt({
        "type": "aggregate_clarify",
        "capability": state["agg_queue"][0],
        "question": ("어떤 집계 검사를 실행할까요?" if out_of_scope
                     else q.get("clarify_question", "조건을 알려주세요.")),
        "options": ([{"id": k, "label": v[0]} for k, v in TEMPLATE_CHOICES.items()]
                    if out_of_scope else []),
    })
    text = str(answer).strip()
    if text in TEMPLATE_CHOICES:
        text = TEMPLATE_CHOICES[text][1]
    q = clarify_node(q, text)
    q.pop("clarify_question", None)
    q.pop("intent", None)
    return {**state, "query": q}


def agg_confirm_node(state: GraphState) -> GraphState:
    q = dict(state["query"])
    decision = interrupt({
        "type": "aggregate_confirm",
        "capability": state["agg_queue"][0],
        "card": q.get("confirm_card", {}),
    })
    if decision != "run":
        q["skipped"] = True
    return {**state, "query": q}


def agg_execute_node(state: GraphState) -> GraphState:
    queue = list(state.get("agg_queue", []))
    cap = queue.pop(0)
    q = dict(state.get("query") or {})
    results = list(state.get("agg_results", []))

    if q.get("skipped") or q.get("confirmed_spec") is None:
        results.append({"capability": cap, "skipped": True, "spec": None,
                        "group_issues": [], "response_text": "실행하지 않았습니다 (취소 또는 조건 미확정)."})
    else:
        dataset = state.get("cleaned") or state["dataset"]
        q = respond_node(execute_node(q, dataset))
        results.append({"capability": cap, "skipped": False, "spec": q["confirmed_spec"],
                        "group_issues": q.get("group_issues", []),
                        "response_text": q.get("response_text", "")})
        logger.info("집계 규칙 실행: %s → %d건", q["confirmed_spec"].template,
                    len(q.get("group_issues", [])))

    out: GraphState = {**state, "agg_queue": queue, "agg_results": results, "query": {}}
    if not queue:
        out["agent_phase"] = "aggregated"
    return out


def route_after_execute(state: GraphState) -> str:
    return "agg_translate" if state.get("agg_queue") else "planner"
