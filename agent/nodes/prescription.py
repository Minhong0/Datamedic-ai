from __future__ import annotations

import logging
from collections import defaultdict

from engine.models import Issue, Tier
from agent.state import GraphState

logger = logging.getLogger(__name__)


def _add_tier_reasoning(issues: list[Issue], goal: str) -> list[Issue]:
    """rule_id+tier 조합별 대표 1건만 LLM으로 처리 근거 생성, 나머지는 복사."""
    try:
        from pydantic import BaseModel
        from llm.client import complete_json

        class ReasonResp(BaseModel):
            reasoning: str

        cache: dict[str, str] = {}
        result: list[Issue] = []

        for issue in issues:
            key = f"{issue.rule_id}:{issue.tier}"
            if key not in cache:
                try:
                    from engine.masking import mask_issue
                    safe = mask_issue(issue)
                    resp = complete_json(
                        "tier_reasoning",
                        {
                            "rule_id": issue.rule_id,
                            "value": safe.value or "",
                            "message": safe.message,
                            "tier": issue.tier,
                            "user_goal": goal or "데이터 정합성 검증",
                        },
                        ReasonResp,
                    )
                    cache[key] = resp.reasoning
                except Exception:
                    cache[key] = ""

            reasoning = cache[key]
            if reasoning:
                base = issue.explanation or issue.message
                new_exp = f"{base}\n처리 방향({issue.tier}): {reasoning}"
                result.append(issue.model_copy(update={"explanation": new_exp}))
            else:
                result.append(issue)
        return result
    except Exception:
        return issues


def _should_escalate(issue: Issue, cell_conflicting: set[str]) -> bool:
    if issue.ref is None:
        return False
    cell_key = f"{issue.ref.file}:{issue.ref.sheet}:{issue.ref.row}:{issue.ref.column}"
    return cell_key in cell_conflicting


def prescription_node(state: GraphState) -> GraphState:
    """파이프라인 4단계: 오류의 tier를 조정하고 수정 제안(suggestion)을 보강한다."""
    issues = state["issues"]

    from collections import defaultdict as _dd
    cell_suggestions: dict[str, set[str]] = _dd(set)
    for issue in issues:
        if issue.ref and issue.suggestion is not None:
            key = f"{issue.ref.file}:{issue.ref.sheet}:{issue.ref.row}:{issue.ref.column}"
            cell_suggestions[key].add(issue.suggestion)

    cell_conflicting: set[str] = {k for k, v in cell_suggestions.items() if len(v) >= 2}

    _TIER_UP: dict[Tier, Tier] = {"auto": "approval", "approval": "request", "request": "request"}

    updated: list[Issue] = []
    for issue in issues:
        tier = issue.tier
        if issue.rule_id != "DUPLICATE_ROW" and _should_escalate(issue, cell_conflicting):
            tier = _TIER_UP[tier]

        suggestion = issue.suggestion
        if issue.rule_id == "CODE_VALID" and issue.value and suggestion is None:
            try:
                from engine.codebook import load_codebook
                cb = load_codebook("business_codes.csv")
                nearest = cb.nearest(issue.value, n=1)
                if nearest:
                    suggestion = nearest[0]
            except Exception:
                pass

        if issue.rule_id == "DUPLICATE_ROW":
            tier = "approval"
        updated.append(issue.model_copy(update={"tier": tier, "suggestion": suggestion}))

    goal = state.get("goal", "")
    updated = _add_tier_reasoning(updated, goal)

    analysis_summary: dict = {}
    try:
        import json as _json
        from pydantic import BaseModel
        from llm.client import complete_json
        from pathlib import Path

        class AnalysisResp(BaseModel):
            headline: str
            findings: list[str]
            recommendation: str

        from collections import Counter
        actionable = updated
        rule_counts = Counter(i.rule_id for i in actionable)
        rule_summary = "\n".join(f"- {rule}: {cnt}건" for rule, cnt in rule_counts.most_common(5))

        resp = complete_json(
            "agent_analysis",
            {
                "user_goal": goal or "데이터 정합성 검증",
                "files": ", ".join({Path(i.ref.file).name for i in actionable if i.ref}),
                "total_issues": len(actionable),
                "auto_count": sum(1 for i in actionable if i.tier == "auto"),
                "approval_count": sum(1 for i in actionable if i.tier == "approval"),
                "request_count": sum(1 for i in actionable if i.tier == "request"),
                "rule_summary": rule_summary or "(없음)",
                "priority_rules": ", ".join(state.get("rule_priorities", [])) or "(기본 순서)",
            },
            AnalysisResp,
        )
        from engine.viewer_marks import localize_rule_ids
        analysis_summary = {"headline": localize_rule_ids(resp.headline),
                            "findings": [localize_rule_ids(f) for f in resp.findings],
                            "recommendation": localize_rule_ids(resp.recommendation)}
        logger.info("Agent 분석 요약: %s", resp.headline)
    except Exception as exc:
        logger.debug("Agent 분석 LLM 실패: %s", exc)

    return {**state, "issues": updated, "analysis_summary": analysis_summary}
