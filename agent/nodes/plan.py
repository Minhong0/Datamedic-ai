"""plan 노드 — 목표를 실행 계획(Plan)으로 해석한다 (docs/planner_design.md).

해석 순서: (목표 없음→기본 전체) → LLM → 키워드 → 되묻기.
어떤 경우에도 해석 실패가 '전체 규칙 실행'으로 이어지지 않는다.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel

from agent.state import GraphState
from engine import planning
from engine.models import Plan

logger = logging.getLogger(__name__)


class PlanResp(BaseModel):
    capabilities: list[str]
    reasoning: str = ""


def _llm_select(goal: str) -> list[str]:
    from llm.client import complete_json

    catalog_text = "\n".join(
        f"- `{c.id}`: {c.label}" for c in planning.CATALOG
    )
    resp = complete_json("plan_goal", {"goal": goal, "capabilities": catalog_text}, PlanResp)
    return planning.sanitize_llm_selection(goal, resp.capabilities)


def build_plan(goal: str, choices: list[str] | None = None) -> Plan:
    """목표(+되묻기 선택) → Plan. LLM 호출은 실패해도 키워드 해석으로 대체된다."""
    goal = planning.sanitize_goal(goal)
    choices = [c for c in (choices or []) if c in planning.BY_ID]

    if not goal and not choices:
        return planning.resolve_plan(goal, planning.default_capabilities(), "full")

    selected: list[str] = []
    source = "keyword"
    if goal:
        try:
            selected = _llm_select(goal)
            source = "llm"
        except Exception as exc:
            logger.debug("plan LLM 실패 → 키워드 해석: %s", exc)
        if not selected:
            selected = planning.keyword_select(goal)
            source = "keyword"

    if choices:
        selected = list(dict.fromkeys([*selected, *choices]))
        source = "user"

    if not selected:
        return planning.clarify_empty(goal)

    ambiguity = planning.find_ambiguity(goal, selected)
    if ambiguity is not None:
        return ambiguity

    return planning.resolve_plan(goal, selected, source)


def plan_node(state: GraphState) -> GraphState:
    plan = build_plan(state.get("goal", ""), state.get("plan_choices"))
    if plan.status == "ready":
        for s in plan.steps:
            if s.status == "skipped":
                logger.info("계획: 생략(%s) — %s", s.label, s.reason)
            else:
                logger.info("계획: %s(%s) — %s", s.status, s.label, s.reason)
    else:
        logger.info("계획: 되묻기 — %s", plan.question)
    out: GraphState = {**state, "plan": plan}
    if plan.status == "ready":
        from agent.nodes.aggregate import aggregate_capabilities
        out["agg_queue"] = aggregate_capabilities(plan)
        out["agg_results"] = []
    return out
