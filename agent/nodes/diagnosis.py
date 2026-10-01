from __future__ import annotations

import logging

from engine import validator
from engine.masking import mask_issue
from engine.planning import PlanScope, active_scope
from engine.models import Issue
from agent.state import GraphState

logger = logging.getLogger(__name__)


def _explain_representative(issues: list[Issue], goal: str = "") -> list[Issue]:
    """같은 rule_id끼리 대표 1건만 LLM 설명, 나머지 복사."""
    from pydantic import BaseModel
    try:
        from llm.client import complete_json

        class ExplainResp(BaseModel):
            explanation: str

        representative: dict[str, str] = {}
        result: list[Issue] = []

        for issue in issues:
            key = issue.rule_id
            if key not in representative:
                try:
                    resp = complete_json(
                        "explain_issue",
                        {
                            "rule_id": issue.rule_id,
                            "value": mask_issue(issue).value or "",
                            "message": mask_issue(issue).message,
                            "user_goal": goal or "데이터 정합성 검증",
                        },
                        ExplainResp,
                    )
                    representative[key] = resp.explanation
                except Exception:
                    representative[key] = issue.message
            result.append(issue.model_copy(update={"explanation": representative[key]}))
        return result
    except Exception:
        return issues


def diagnosis_node(state: GraphState) -> GraphState:
    """파이프라인 3단계: 규칙을 실행해 오류를 탐지하고 LLM 설명을 추가한다.

    처리 순서:
      1. validator.run: 각 RuleBinding에 대해 해당 규칙 클래스를 실행 → Issue 목록
      2. _explain_representative: 동일 rule_id끼리는 LLM을 1번만 호출하고
         나머지는 그 설명을 복사 (API 비용 절감)
    """
    ds = state["dataset"]
    bindings = state["bindings"]
    goal = state.get("goal", "")
    scope = PlanScope.from_plan(state.get("plan"))
    with active_scope(scope):
        issues = validator.run(
            ds, bindings, allowed_rule_ids=scope.rule_ids if scope else None,
        )
    if scope is not None:
        issues = scope.filter_issues(issues)   # 계획 밖 Issue 폐기 (이중 가드)
    logger.info("진단 완료: %d건", len(issues))
    return {**state, "issues": issues}
