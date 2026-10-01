"""value_rules 테스트: REQUIRED, DATE_RANGE, BIZNO_CHECKSUM, CODE_VALID, NUMERIC_UNIT.

MVP에서는 이 규칙들이 비활성화됨 (RULES.md R-01). 전체 skip 처리.
"""
import pytest
pytestmark = pytest.mark.skip(reason="R-01: value_rules 비활성화 (RULES.md MVP)")
"""_original_docstring"""
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

    def _check_won(self, vals):
        # 표준에 unit='원'이 선언된 컬럼(지원금액) → 순수 정수만 정상
        ds = make_ds("실적", "지원금액", vals)
        b = make_binding("NUMERIC_UNIT", ["실적.지원금액"], {"unit": "원"})
        return self.rule.check(ds, b, RuleContext())

    def test_won_plain_ok(self):
        # unit=원: 순수 정수는 정상
        assert self._check_won(["117480000", "48960000"]) == []

    def test_won_comma_always_flagged(self):
        # unit=원: 쉼표가 다수여도 항상 오류 → 순수 정수로 제거
        issues = self._check_won(["1,000,000", "2,000,000", "3,000,000"])
        assert len(issues) == 3
        assert all(i.tier == "auto" for i in issues)
        assert all("," not in i.suggestion for i in issues)
        assert issues[0].suggestion == "1000000"

    def test_won_manwon_flagged(self):
        # unit=원: '1321만원' 표기는 오류 → 원 단위 정수로 환산
        issues = self._check_won(["1321만원"])
        assert len(issues) == 1
        assert issues[0].suggestion == "13210000"

    def test_clean_number(self):
        assert self._check(["1000000", "2000000"]) == []

    def test_comma_standard_no_issue(self):
        # 컬럼 전체가 쉼표 포맷이면 이슈 없음 (쉼표가 표준)
        assert self._check(["1,000,000", "2,000,000"]) == []

    def test_comma_auto(self):
        # plain 다수 컬럼에서 쉼표 혼입 → auto 플래그
        issues = self._check(["1000000", "2000000", "1,500,000"])
        comma_issues = [i for i in issues if "," in (i.value or "")]
        assert len(comma_issues) == 1
        assert comma_issues[0].tier == "auto"
        assert comma_issues[0].suggestion == "1500000"

    def test_plain_flagged_when_comma_standard(self):
        # comma 다수 컬럼에서 쉼표 없는 값 → auto 플래그
        issues = self._check(["1,000,000", "2,000,000", "1500000"])
        plain_issues = [i for i in issues if "," not in (i.value or "")]
        assert len(plain_issues) == 1
        assert plain_issues[0].tier == "auto"
        assert plain_issues[0].suggestion == "1,500,000"

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

    def test_fix_unit_scale(self):
        """단위 환산 가능한 경우 제안값 반환."""
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="NUMERIC_UNIT", kind="rule",
                      value="1000천원", message="", tier="auto")
        assert self.rule.fix("1000천원", issue) == "1000000"
