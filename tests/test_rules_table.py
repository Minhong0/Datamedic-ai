"""table_rules 테스트: TYPE_CONSISTENCY, SUM_MATCH, OUTLIER."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.table_rules import TypeConsistencyRule, SumMatchRule, OutlierRule


def make_binding(rule_id, targets=None, params=None):
    return RuleBinding(rule_id=rule_id, targets=targets or [], params=params or {}, source="standard")


# ── TYPE_CONSISTENCY ──────────────────────────────────────────────────────────

class TestTypeConsistency:
    rule = TypeConsistencyRule()

    def test_all_numeric_no_issue(self):
        df = pd.DataFrame({"_row": [2, 3, 4], "금액": ["1000", "2000", "3000"]})
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("TYPE_CONSISTENCY"), RuleContext())
        assert issues == []

    def test_text_in_numeric_col(self):
        df = pd.DataFrame({"_row": [2, 3, 4], "금액": ["1000", "2000", "오류값"]})
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("TYPE_CONSISTENCY"), RuleContext())
        assert len(issues) == 1
        assert issues[0].ref.row == 4

    def test_numeric_in_text_col(self):
        df = pd.DataFrame({"_row": [2, 3, 4], "기관명": ["창원시", "진주시", "12345"]})
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("TYPE_CONSISTENCY"), RuleContext())
        assert len(issues) == 1

    def test_all_same_type_no_issue(self):
        df = pd.DataFrame({"_row": [2, 3], "날짜": ["2026-01-01", "2026-02-01"]})
        ds = Dataset({("t.xlsx", "실적"): df})
        issues = self.rule.check(ds, make_binding("TYPE_CONSISTENCY"), RuleContext())
        assert issues == []


# ── SUM_MATCH ─────────────────────────────────────────────────────────────────

class TestSumMatch:
    rule = SumMatchRule()

    def _make_ds(self, src_amounts, tgt_sum):
        perf_df = pd.DataFrame({
            "_row": list(range(2, 2 + len(src_amounts))),
            "사업코드": ["BC001"] * len(src_amounts),
            "지원금액": [str(a) for a in src_amounts],
        })
        sum_df = pd.DataFrame({
            "_row": [2],
            "사업코드": ["BC001"],
            "지원금액합계": [str(tgt_sum)],
        })
        return Dataset({("t.xlsx", "실적"): perf_df, ("t.xlsx", "요약"): sum_df})

    def _binding(self):
        return make_binding("SUM_MATCH", params={
            "source_sheet": "실적", "source_col": "지원금액",
            "group_by": "사업코드", "agg": "sum",
            "target_sheet": "요약", "target_col": "지원금액합계",
        })

    def test_correct_sum_no_issue(self):
        ds = self._make_ds([1000, 2000, 3000], 6000)
        assert self.rule.check(ds, self._binding(), RuleContext()) == []

    def test_wrong_sum_issue(self):
        ds = self._make_ds([1000, 2000, 3000], 5000)
        issues = self.rule.check(ds, self._binding(), RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "request"
        assert "6000" in issues[0].message or "5000" in issues[0].message

    def test_missing_params_no_check(self):
        ds = self._make_ds([1000], 1000)
        b = make_binding("SUM_MATCH", params={})
        assert self.rule.check(ds, b, RuleContext()) == []


# ── OUTLIER ───────────────────────────────────────────────────────────────────

class TestOutlier:
    rule = OutlierRule()

    def test_normal_values_no_issue(self):
        df = pd.DataFrame({
            "_row": list(range(2, 10)),
            "금액": ["1000", "1100", "1050", "980", "1020", "1070", "990", "1030"],
        })
        ds = Dataset({("t.xlsx", "실적"): df})
        b = make_binding("OUTLIER", targets=["실적.금액"], params={"iqr_factor": 1.5, "org_factor": 5.0})
        issues = self.rule.check(ds, b, RuleContext())
        assert issues == []

    def test_outlier_detected(self):
        df = pd.DataFrame({
            "_row": list(range(2, 10)),
            "금액": ["1000", "1100", "1050", "980", "1020", "1070", "990", "99999"],
        })
        ds = Dataset({("t.xlsx", "실적"): df})
        b = make_binding("OUTLIER", targets=["실적.금액"], params={"iqr_factor": 1.5, "org_factor": 5.0})
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) >= 1
        assert issues[-1].kind == "outlier"

    def test_too_few_rows_skipped(self):
        df = pd.DataFrame({"_row": [2, 3], "금액": ["1000", "9999999"]})
        ds = Dataset({("t.xlsx", "실적"): df})
        b = make_binding("OUTLIER", targets=["실적.금액"])
        issues = self.rule.check(ds, b, RuleContext())
        assert issues == []
