"""T19 테스트: 규칙 on/off — disabled_rules 설정으로 규칙 스킵 검증."""
from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
import engine.validator as validator


def _make_ds() -> Dataset:
    df = pd.DataFrame({
        "_row": [2, 3],
        "기관명":   [" 창원시 ", "진주시"],  # TEXT_NORMALIZE 오류
        "지원일자": ["20260315", "2026-04-01"],  # DATE_FORMAT 오류
    })
    return Dataset({("f.xlsx", "실적"): df})


_BINDINGS = [
    RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"],   source="standard"),
    RuleBinding(rule_id="DATE_FORMAT",    targets=["실적.지원일자"], source="standard"),
]


def test_all_rules_enabled_by_default():
    """disabled_rules가 비어 있으면 모든 규칙이 실행된다."""
    ds = _make_ds()
    from config import settings
    with patch.object(settings, "disabled_rules", ""):
        issues = validator.run(ds, _BINDINGS)
    rule_ids = {i.rule_id for i in issues}
    assert "TEXT_NORMALIZE" in rule_ids
    assert "DATE_FORMAT" in rule_ids


def test_disabled_rule_is_skipped():
    """disabled_rules에 포함된 규칙은 Issue를 만들지 않는다."""
    ds = _make_ds()
    from config import settings
    with patch.object(settings, "disabled_rules", "TEXT_NORMALIZE"):
        issues = validator.run(ds, _BINDINGS)
    rule_ids = {i.rule_id for i in issues}
    assert "TEXT_NORMALIZE" not in rule_ids
    assert "DATE_FORMAT" in rule_ids


def test_multiple_disabled_rules():
    """쉼표로 여러 규칙을 비활성화할 수 있다."""
    ds = _make_ds()
    from config import settings
    with patch.object(settings, "disabled_rules", "TEXT_NORMALIZE,DATE_FORMAT"):
        issues = validator.run(ds, _BINDINGS)
    assert issues == []


def test_disabled_rule_ids_property():
    """disabled_rule_ids property가 집합을 올바르게 파싱한다."""
    from config import settings
    with patch.object(settings, "disabled_rules", " OUTLIER , NEAR_DUPLICATE "):
        ids = settings.disabled_rule_ids
    assert ids == {"OUTLIER", "NEAR_DUPLICATE"}
