"""row_rules 테스트: DUPLICATE_ROW, NEAR_DUPLICATE."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.row_rules import DuplicateRowRule, NearDuplicateRule


def make_binding(rule_id, params=None):
    return RuleBinding(rule_id=rule_id, targets=[], params=params or {}, source="standard")


# ── DUPLICATE_ROW ─────────────────────────────────────────────────────────────

class TestDuplicateRow:
    rule = DuplicateRowRule()

    def test_no_duplicates(self):
        df = pd.DataFrame({"_row": [2, 3], "기관명": ["창원시", "진주시"], "금액": ["1000", "2000"]})
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("DUPLICATE_ROW"), RuleContext())
        assert issues == []

    def test_exact_duplicate_detected(self):
        df = pd.DataFrame({
            "_row": [2, 3, 4],
            "기관명": ["창원시", "창원시", "진주시"],
            "금액": ["1000", "1000", "2000"],
        })
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("DUPLICATE_ROW"), RuleContext())
        assert len(issues) == 1
        assert issues[0].ref.row == 3  # 두 번째 행
        assert issues[0].tier == "auto"

    def test_three_duplicates(self):
        df = pd.DataFrame({
            "_row": [2, 3, 4],
            "기관명": ["창원시", "창원시", "창원시"],
            "금액": ["1000", "1000", "1000"],
        })
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("DUPLICATE_ROW"), RuleContext())
        assert len(issues) == 2

    def test_partial_match_not_duplicate(self):
        df = pd.DataFrame({
            "_row": [2, 3],
            "기관명": ["창원시", "창원시"],
            "금액": ["1000", "2000"],  # 금액 다름
        })
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("DUPLICATE_ROW"), RuleContext())
        assert issues == []


# ── NEAR_DUPLICATE ────────────────────────────────────────────────────────────

class TestNearDuplicate:
    rule = NearDuplicateRule()

    def _ds(self, rows):
        df = pd.DataFrame(rows)
        return Dataset({("t.xlsx", "실적"): df})

    def test_very_similar_detected(self):
        ds = self._ds([
            {"_row": 2, "기관명": "창원시스마트공장", "금액": "1000000"},
            {"_row": 3, "기관명": "창원시스마트공장", "금액": "1000001"},
        ])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.9})
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) >= 1

    def test_different_rows_no_issue(self):
        ds = self._ds([
            {"_row": 2, "기관명": "창원시", "금액": "1000000"},
            {"_row": 3, "기관명": "진주시", "금액": "9999999"},
        ])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.9})
        issues = self.rule.check(ds, b, RuleContext())
        assert issues == []

    def test_single_row_no_issue(self):
        ds = self._ds([{"_row": 2, "기관명": "창원시", "금액": "1000000"}])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.9})
        issues = self.rule.check(ds, b, RuleContext())
        assert issues == []
