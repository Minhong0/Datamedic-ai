"""Goal 기반 규칙 필터링 테스트.

rule_priorities가 설정된 경우 해당 규칙만 실행되는지,
비어 있으면 전체 규칙이 실행되는지 검증한다.
"""
from __future__ import annotations

import pytest

from agent.nodes.rules import rules_node
from engine.models import ColumnProfile, SheetProfile


def _make_state(priorities: list[str]) -> dict:
    profiles = [
        SheetProfile(
            file="test.xlsx",
            sheet="실적",
            columns=[
                ColumnProfile(name="지원금액",   semantic_type="amount",  required=True),
                ColumnProfile(name="사업자번호", semantic_type="biz_no",  required=False),
            ],
            row_count=5,
        )
    ]
    return {"input_paths": [], "profiles": profiles, "rule_priorities": priorities}


def test_goal_filter_runs_only_selected_rules():
    """rule_priorities에 포함된 규칙만 bindings에 남는다."""
    state = _make_state(["DUPLICATE_ROW", "AMOUNT_NORMALIZE"])
    result = rules_node(state)
    rule_ids = {b.rule_id for b in result["bindings"] if b.source != "standard"}

    assert "AMOUNT_NORMALIZE" in rule_ids
    assert "BIZNO_VERIFY" not in rule_ids, "선택되지 않은 BIZNO_VERIFY가 포함되면 안 됨"


def test_goal_filter_standard_bindings_always_included():
    """source='standard'인 바인딩은 rule_priorities와 무관하게 항상 포함된다."""
    from engine.models import RuleBinding
    from unittest.mock import patch

    std_binding = RuleBinding(
        rule_id="SUM_MATCH", targets=["실적.지원금액"], source="standard"
    )
    state = _make_state(["DUPLICATE_ROW"])  # SUM_MATCH 없음

    with patch("agent.nodes.rules._relations_from_standard", return_value=[std_binding]):
        result = rules_node(state)

    rule_ids = {b.rule_id for b in result["bindings"]}
    assert "SUM_MATCH" in rule_ids, "standard 바인딩은 목표와 무관하게 항상 포함돼야 함"


def test_empty_priorities_runs_all_rules():
    """rule_priorities가 비어 있으면 모든 시맨틱 규칙이 포함된다."""
    state = _make_state([])
    result = rules_node(state)
    rule_ids = {b.rule_id for b in result["bindings"]}

    assert "AMOUNT_NORMALIZE" in rule_ids
    assert "BIZNO_VERIFY" in rule_ids


def test_goal_filter_order_matches_priorities():
    """rule_priorities 순서대로 bindings가 정렬된다."""
    state = _make_state(["AMOUNT_NORMALIZE", "DUPLICATE_ROW"])
    result = rules_node(state)
    non_std = [b.rule_id for b in result["bindings"] if b.source != "standard"]

    # AMOUNT_NORMALIZE가 DUPLICATE_ROW보다 앞에 와야 한다
    if "AMOUNT_NORMALIZE" in non_std and "DUPLICATE_ROW" in non_std:
        assert non_std.index("AMOUNT_NORMALIZE") < non_std.index("DUPLICATE_ROW")
