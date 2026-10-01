"""row_rules 테스트: DUPLICATE_ROW, NEAR_DUPLICATE.

MVP에서는 이 규칙들이 비활성화됨 (RULES.md R-01). 전체 skip 처리.
"""
import pytest
pytestmark = pytest.mark.skip(reason="R-01: row_rules 비활성화 (RULES.md MVP)")
"""_original_docstring"""
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
        # 변별력 있는 텍스트 컬럼(수혜기업명·사업명)이 거의 동일한 near-dup 한 쌍.
        # 다른 행들은 서로 달라 이 한 쌍만 잡혀야 한다.
        ds = self._ds([
            {"_row": 2, "수혜기업명": "창원정밀공업", "사업명": "스마트공장 구축"},
            {"_row": 3, "수혜기업명": "진주바이오",   "사업명": "바이오 R&D 지원"},
            {"_row": 4, "수혜기업명": "김해전자",     "사업명": "수출 판로 개척"},
            {"_row": 5, "수혜기업명": "창원정밀공업", "사업명": "스마트공장  구축"},  # row2와 공백 1개 차이
        ])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.9})
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) == 1
        assert issues[0].ref.row == 5

    def test_different_rows_no_issue(self):
        ds = self._ds([
            {"_row": 2, "수혜기업명": "창원정밀공업", "사업명": "스마트공장 구축"},
            {"_row": 3, "수혜기업명": "진주바이오",   "사업명": "바이오 R&D 지원"},
            {"_row": 4, "수혜기업명": "김해전자",     "사업명": "수출 판로 개척"},
        ])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.9})
        issues = self.rule.check(ds, b, RuleContext())
        assert issues == []

    def test_summary_sheet_not_flagged(self):
        # 코드 + 숫자만 있는 요약 시트: 코드가 4/5글자 겹치고 건수가 같아도
        # 서로 다른 코드별 집계는 유사 중복이 아니다 → 오탐 없어야 함
        ds = self._ds([
            {"_row": 2, "사업코드": "BC001", "지원건수": "2", "지원금액합계": "179080000"},
            {"_row": 3, "사업코드": "BC002", "지원건수": "3", "지원금액합계": "209550000"},
            {"_row": 4, "사업코드": "BC003", "지원건수": "2", "지원금액합계": "406510000"},
        ])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.85})
        assert self.rule.check(ds, b, RuleContext()) == []

    def test_single_row_no_issue(self):
        ds = self._ds([{"_row": 2, "기관명": "창원시", "금액": "1000000"}])
        b = make_binding("NEAR_DUPLICATE", {"threshold": 0.9})
        issues = self.rule.check(ds, b, RuleContext())
        assert issues == []
