from __future__ import annotations

import logging

from engine import fixer
from engine.planning import PlanScope
from agent.state import GraphState

logger = logging.getLogger(__name__)


def guard_decisions(issues, decisions, scope: PlanScope | None):
    """범위 밖 issue의 결정과 DUPLICATE_ROW의 시스템 자동 결정을 걸러낸다."""
    by_id = {i.issue_id: i for i in issues}
    kept = []
    for d in decisions:
        issue = by_id.get(d.issue_id)
        if issue is None:
            continue
        if scope is not None and not scope.allows_issue(issue.rule_id):
            logger.warning("계획 밖 결정 폐기: %s (%s)", d.issue_id, issue.rule_id)
            continue
        if issue.rule_id == "DUPLICATE_ROW" and d.action == "apply" and d.decided_by != "operator":
            logger.warning("중복 행 자동 삭제 차단: %s", d.issue_id)
            continue
        kept.append(d)
    return kept


def treatment_node(state: GraphState) -> GraphState:
    """파이프라인 5단계: decisions에 따라 Dataset을 수정하고 변경 이력을 기록한다."""
    ds = state["dataset"]
    issues = state["issues"]
    decisions = state.get("decisions", [])
    decisions = guard_decisions(issues, decisions, PlanScope.from_plan(state.get("plan")))

    cleaned, changes = fixer.apply(ds, issues, decisions)
    logger.info("치료 완료: %d건 변경", len(changes))
    return {**state, "cleaned": cleaned, "changes": changes}
