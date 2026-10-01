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
    """파이프라인 5단계: decisions에 따라 Dataset을 수정하고 변경 이력을 기록한다.

    - fixer.apply가 원본(ds)을 복사한 뒤 Decision별로 셀 수정/행 삭제를 적용한다.
    - 수정마다 Change(C-0001 형식)가 생성되어 감사 추적에 사용된다.
    - decisions가 비어 있으면 아무것도 수정하지 않는다.
    - Streamlit에서는 이 노드 직전에 그래프가 중단되므로,
      UI가 decisions를 채운 뒤 재개한다.
    """
    ds = state["dataset"]
    issues = state["issues"]
    decisions = state.get("decisions", [])
    decisions = guard_decisions(issues, decisions, PlanScope.from_plan(state.get("plan")))

    cleaned, changes = fixer.apply(ds, issues, decisions)
    logger.info("치료 완료: %d건 변경", len(changes))
    return {**state, "cleaned": cleaned, "changes": changes}
