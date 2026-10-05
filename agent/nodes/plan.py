"""plan 노드 — 목표를 실행 계획(Plan)으로 해석한다."""
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


def build_plan(goal: str, choices: list[str] | None = None, *, ask_unhandled: bool = True) -> Plan:
    """목표(+되묻기 선택) → Plan. LLM 호출은 실패해도 키워드 해석으로 대체된다."""
    goal = planning.sanitize_goal(goal)
    raw_choices = list(choices or [])
    clause_caps, used, skipped = planning.parse_clause_choices(raw_choices)
    choices = [c for c in raw_choices if c in planning.BY_ID] + clause_caps

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

    if choices or skipped:
        selected = list(dict.fromkeys([*selected, *choices]))
        source = "user"

    if not selected:
        return planning.clarify_empty(goal)

    ambiguity = planning.find_ambiguity(goal, selected)
    if ambiguity is not None:
        return ambiguity

    plan = planning.resolve_plan(goal, selected, source)
    if ask_unhandled:
        pending = [c for c in plan.unhandled if c not in used and c not in skipped]
        if pending:
            return planning.clarify_unhandled(goal, pending[0], len(pending))
        plan.unhandled = [c for c in plan.unhandled if c in skipped]
    return plan


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
    for c in plan.unhandled:
        logger.warning("계획: 해석하지 못한 조항 — %s", c)
    out: GraphState = {**state, "plan": plan}
    if plan.status == "ready":
        from agent.nodes.aggregate import aggregate_capabilities
        out["agg_queue"] = aggregate_capabilities(plan)
        out["agg_results"] = []
        out["agg_dispositions"] = {}
    return out
