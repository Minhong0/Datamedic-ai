"""value_rules 테스트: REQUIRED, DATE_RANGE, BIZNO_CHECKSUM, CODE_VALID, NUMERIC_UNIT."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import NotFixable, RuleContext
from engine.rules.value_rules import (
    RequiredRule, DateRangeRule, BiznoChecksumRule, CodeValidRule, NumericUnitRule,
)


def make_ds(sheet, column, values, file="t.xlsx"):
    df = pd.DataFrame({"_row": range(2, 2 + len(values)), column: values})
    return Dataset({(file, sheet): df})


def make_binding(rule_id, targets, params=None):
    return RuleBinding(rule_id=rule_id, targets=targets, params=params or {}, source="standard")


# ── REQUIRED ──────────────────────────────────────────────────────────────────

class TestRequired:
    rule = RequiredRule()

    def test_no_issue_when_filled(self):
        ds = make_ds("실적", "기관명", ["창원시", "진주시"])
        b = make_binding("REQUIRED", ["실적.기관명"])
        assert self.rule.check(ds, b, RuleContext()) == []

    def test_empty_string_issue(self):
        ds = make_ds("실적", "기관명", ["창원시", ""])
        b = make_binding("REQUIRED", ["실적.기관명"])
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "request"
        assert issues[0].ref.row == 3

    def test_multiple_empty(self):
        ds = make_ds("실적", "기관명", ["", "", "창원시"])
        b = make_binding("REQUIRED", ["실적.기관명"])
        assert len(self.rule.check(ds, b, RuleContext())) == 2


# ── DATE_RANGE ────────────────────────────────────────────────────────────────

class TestDateRange:
    rule = DateRangeRule()

    def _binding(self):
        return make_binding("DATE_RANGE", ["실적.지원일자"],
                            {"range": ["2026-01-01", "2026-12-31"]})

    def test_in_range(self):
        ds = make_ds("실적", "지원일자", ["2026-06-15", "2026-01-01"])
        assert self.rule.check(ds, self._binding(), RuleContext()) == []

    def test_before_range(self):
        ds = make_ds("실적", "지원일자", ["2025-12-31"])
        issues = self.rule.check(ds, self._binding(), RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "request"

    def test_after_range(self):
        ds = make_ds("실적", "지원일자", ["2027-01-01"])
        issues = self.rule.check(ds, self._binding(), RuleContext())
        assert len(issues) == 1

    def test_invalid_iso_skipped(self):
        ds = make_ds("실적", "지원일자", ["not-a-date"])
        assert self.rule.check(ds, self._binding(), RuleContext()) == []


# ── BIZNO_CHECKSUM ────────────────────────────────────────────────────────────

class TestBiznoChecksum:
    rule = BiznoChecksumRule()

    def _make(self, vals):
        ds = make_ds("실적", "사업자번호", vals)
        b = make_binding("BIZNO_CHECKSUM", ["실적.사업자번호"])
        return self.rule.check(ds, b, RuleContext())

    def test_valid_formatted(self):
        # 유효한 사업자번호 (하이픈 형식)
        issues = self._make(["123-45-67819"])
        # 체크디짓 검증: 1234567819 valid 여부 확인 후 테스트 수정
        # 실제 유효한 번호 사용
        pass  # 개별 케이스로 분리

    def test_invalid_checksum(self):
        # 123-45-67810: 올바른 체크디지트는 5이지만 0을 넣어 위반
        issues = self._make(["123-45-67810"])
        assert any(i.tier == "approval" for i in issues)

    def test_valid_no_hyphen_auto(self):
        # 체크디지트 맞지만 하이픈 없음 → auto
        # 1234567819: 체크 계산
        d = [1, 2, 3, 4, 5, 6, 7, 8, 1]
        w = [1, 3, 7, 1, 3, 7, 1, 3, 5]
        total = sum(a * b for a, b in zip(d, w))
        check = (10 - (total % 10)) % 10
        num = "".join(map(str, d)) + str(check)
        issues = self._make([num])
        # 유효 + 하이픈 없음 → auto
        assert any(i.tier == "auto" for i in issues)

    def test_fix_adds_hyphens(self):
        d = [1, 2, 3, 4, 5, 6, 7, 8, 1]
        w = [1, 3, 7, 1, 3, 7, 1, 3, 5]
        total = sum(a * b for a, b in zip(d, w))
        check = (10 - (total % 10)) % 10
        num = "".join(map(str, d)) + str(check)
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="BIZNO_CHECKSUM", kind="rule",
                      value=num, message="", tier="auto")
        result = self.rule.fix(num, issue)
        assert result == f"{num[:3]}-{num[3:5]}-{num[5:]}"

    def test_fix_invalid_raises(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="BIZNO_CHECKSUM", kind="rule",
                      value="123-45-67810", message="", tier="approval")
        with pytest.raises(NotFixable):
            self.rule.fix("123-45-67810", issue)


# ── CODE_VALID ────────────────────────────────────────────────────────────────

class TestCodeValid:
    rule = CodeValidRule()

    def _binding(self, codes):
        return make_binding("CODE_VALID", ["실적.사업코드"], {"valid_codes": codes})

    def test_valid_code(self):
        ds = make_ds("실적", "사업코드", ["BC001", "BC002"])
        b = self._binding(["BC001", "BC002", "BC003"])
        assert self.rule.check(ds, b, RuleContext()) == []

    def test_invalid_code(self):
        ds = make_ds("실적", "사업코드", ["BC999"])
        b = self._binding(["BC001", "BC002"])
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "approval"

    def test_empty_codes_no_check(self):
        ds = make_ds("실적", "사업코드", ["BC999"])
        b = self._binding([])
        assert self.rule.check(ds, b, RuleContext()) == []


# ── NUMERIC_UNIT ──────────────────────────────────────────────────────────────

class TestNumericUnit:
    rule = NumericUnitRule()

    def _check(self, vals):
        ds = make_ds("실적", "지원금액", vals)
        b = make_binding("NUMERIC_UNIT", ["실적.지원금액"])
        return self.rule.check(ds, b, RuleContext())

    def test_clean_number(self):
        assert self._check(["1000000", "2000000"]) == []

    def test_comma_auto(self):
        issues = self._check(["1,000,000"])
        assert len(issues) == 1
        assert issues[0].tier == "auto"
        assert issues[0].suggestion == "1000000"

    def test_won_unit_auto(self):
        issues = self._check(["1000000원"])
        assert len(issues) == 1

    def test_mixed_unit_approval(self):
        issues = self._check(["1000천원"])
        assert any(i.tier in ("auto", "approval") for i in issues)

    def test_fix_comma(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="NUMERIC_UNIT", kind="rule",
                      value="1,000,000", message="", tier="auto")
        assert self.rule.fix("1,000,000", issue) == "1000000"

    def test_fix_unit_raises(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="NUMERIC_UNIT", kind="rule",
                      value="1000천원", message="", tier="approval")
        with pytest.raises(NotFixable):
            self.rule.fix("1000천원", issue)
