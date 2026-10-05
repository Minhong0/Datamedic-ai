from __future__ import annotations

import logging

import yaml

from config import settings
from engine.models import Plan, RuleBinding
from engine.planning import SCOPED_BLANK_TYPES, PlanScope
from engine.semantic import bindings_from_profiles
from agent.state import GraphState

logger = logging.getLogger(__name__)


def _relations_from_standard() -> list[RuleBinding]:
    std_path = settings.standard_dir / "performance.yaml"
    if not std_path.exists():
        return []
    with open(std_path, encoding="utf-8") as f:
        standard = yaml.safe_load(f)
    bindings = []
    for rel in standard.get("relations", []):
        src_parts = rel["source"].split(".")
        tgt_parts = rel["target"].split(".")
        if len(src_parts) != 2 or len(tgt_parts) != 2:
            continue
        bindings.append(RuleBinding(
            rule_id=rel["rule"],
            targets=[rel["source"], rel["target"]],
            params={
                "source_sheet": src_parts[0],
                "source_col": src_parts[1],
                "group_by": rel.get("group_by", ""),
                "agg": rel.get("agg", "sum"),
                "target_sheet": tgt_parts[0],
                "target_col": tgt_parts[1],
            },
            source="standard",
        ))
    return bindings


def rules_node(state: GraphState) -> GraphState:
    """파이프라인 2단계: 각 컬럼에 어떤 규칙을 적용할지 RuleBinding 목록을 만든다."""
    profiles = state["profiles"]
    plan = state.get("plan")
    if plan is not None:
        return {**state, "bindings": _bindings_from_plan(plan, profiles)}

    bindings = bindings_from_profiles(profiles)
    bindings.extend(_relations_from_standard())
    priorities: list[str] = state.get("rule_priorities", [])
    if priorities:
        def _rank(b: RuleBinding) -> int:
            try:
                return priorities.index(b.rule_id)
            except ValueError:
                return len(priorities)
        bindings.sort(key=_rank)
        bindings = [
            b for b in bindings
            if b.rule_id in priorities or b.source == "standard"
        ]
        logger.info("목표 기반 규칙 선택: %d개 (%s)", len(bindings), priorities)
    return {**state, "bindings": bindings}


def _scoped_blank_cols(scope: PlanScope, profiles) -> list[str]:
    """계획에 포함된 능력의 대상 컬럼('시트.컬럼')을 모은다."""
    types = {t for c, t in SCOPED_BLANK_TYPES.items() if c in scope.capabilities}
    return sorted({
        f"{p.sheet}.{col.name}"
        for p in profiles for col in p.columns if col.semantic_type in types
    })


def _bindings_from_plan(plan: Plan, profiles) -> list[RuleBinding]:
    """계획의 run·dependency 능력에 해당하는 binding만 만든다 (범위 가드, fail-open 없음)."""
    scope = PlanScope.from_plan(plan)
    allowed = scope.rule_ids
    use_nts = "bizno_api" in scope.capabilities

    candidates = bindings_from_profiles(profiles)
    if "duplicate_row" in scope.capabilities:
        candidates.append(RuleBinding(rule_id="DUPLICATE_ROW", targets=[], source="semantic_default"))

    scoped_cols = _scoped_blank_cols(scope, profiles)

    bindings: list[RuleBinding] = []
    for b in candidates:
        if b.rule_id not in allowed:
            continue
        if b.rule_id == "BLANK" and "blank_check" not in scope.capabilities:
            b = b.model_copy(update={"params": {**b.params, "scope_cols": scoped_cols}})
        if b.rule_id == "BIZNO_VERIFY":
            b = b.model_copy(update={"params": {**b.params, "use_nts": use_nts}})
        bindings.append(b)
    logger.info("계획 기반 규칙 선택: %s", sorted({b.rule_id for b in bindings}))
    return bindings
