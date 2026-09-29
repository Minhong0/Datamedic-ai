"""format_rules — DATE_FORMAT, PHONE_FORMAT, TEXT_NORMALIZE 테스트."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import NotFixable, RuleContext
from engine.rules.format_rules import DateFormatRule, PhoneFormatRule, TextNormalizeRule


def make_ds(sheet: str, column: str, values: list[str], file: str = "test.xlsx") -> Dataset:
    rows = list(range(2, 2 + len(values)))
    df = pd.DataFrame({"_row": rows, column: values})
    return Dataset({(file, sheet): df})


def make_binding(rule_id: str, targets: list[str]) -> RuleBinding:
    return RuleBinding(rule_id=rule_id, targets=targets, source="standard")


# ── DATE_FORMAT ───────────────────────────────────────────────────────────────

class TestDateFormat:
    rule = DateFormatRule()

    def _check(self, values, target="실적.지원일자"):
        ds = make_ds("실적", "지원일자", values)
        binding = make_binding("DATE_FORMAT", [target])
        return self.rule.check(ds, binding, RuleContext())

    def test_iso_date_no_issue(self):
        issues = self._check(["2026-03-15", "2026-04-01"])
        assert issues == []

    def test_compact_date_auto(self):
        issues = self._check(["20260315"])
        assert len(issues) == 1
        assert issues[0].tier == "auto"
        assert issues[0].suggestion == "2026-03-15"

    def test_dot_date_auto(self):
        issues = self._check(["2026.03.15"])
        assert len(issues) == 1
        assert issues[0].suggestion == "2026-03-15"

    def test_slash_date_auto(self):
        issues = self._check(["2026/03/15"])
        assert len(issues) == 1
        assert issues[0].suggestion == "2026-03-15"

    def test_ambiguous_date_approval(self):
        issues = self._check(["26-03-15"])
        assert len(issues) == 1
        assert issues[0].tier == "approval"
        assert issues[0].suggestion is None

    def test_unrecognized_date_approval(self):
        issues = self._check(["3월15일"])
        assert len(issues) == 1
        assert issues[0].tier == "approval"

    def test_empty_value_skipped(self):
        issues = self._check(["", "2026-01-01"])
        assert issues == []

    def test_mixed_values(self):
        issues = self._check(["2026-01-01", "20260102", ""])
        assert len(issues) == 1
        assert issues[0].value == "20260102"

    def test_fix_compact(self):
        from engine.models import Issue, CellRef
        issue = Issue(issue_id="E-0001", rule_id="DATE_FORMAT", kind="rule",
                      value="20260315", message="", tier="auto")
        assert self.rule.fix("20260315", issue) == "2026-03-15"

    def test_fix_ambiguous_raises(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="DATE_FORMAT", kind="rule",
                      value="26-03-15", message="", tier="approval")
        with pytest.raises(NotFixable):
            self.rule.fix("26-03-15", issue)

    def test_fix_none_raises(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="DATE_FORMAT", kind="rule",
                      value=None, message="", tier="approval")
        with pytest.raises(NotFixable):
            self.rule.fix(None, issue)


# ── PHONE_FORMAT ──────────────────────────────────────────────────────────────

class TestPhoneFormat:
    rule = PhoneFormatRule()

    def _check(self, values, target="실적.담당자연락처"):
        ds = make_ds("실적", "담당자연락처", values)
        binding = make_binding("PHONE_FORMAT", [target])
        return self.rule.check(ds, binding, RuleContext())

    def test_valid_phone_no_issue(self):
        issues = self._check(["010-1234-5678", "011-987-6543"])
        assert issues == []

    def test_no_hyphen_auto(self):
        issues = self._check(["01012345678"])
        assert len(issues) == 1
        assert issues[0].tier == "auto"
        assert issues[0].suggestion == "010-1234-5678"

    def test_spaces_auto(self):
        issues = self._check(["010 1234 5678"])
        assert len(issues) == 1
        assert issues[0].suggestion == "010-1234-5678"

    def test_10digit_phone(self):
        issues = self._check(["0512345678"])
        assert len(issues) == 1
        assert issues[0].suggestion == "051-234-5678"

    def test_invalid_phone_approval(self):
        issues = self._check(["1234567"])
        assert len(issues) == 1
        assert issues[0].tier == "approval"

    def test_empty_skipped(self):
        issues = self._check([""])
        assert issues == []

    def test_fix_no_hyphen(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="PHONE_FORMAT", kind="rule",
                      value="01012345678", message="", tier="auto")
        assert self.rule.fix("01012345678", issue) == "010-1234-5678"

    def test_fix_invalid_raises(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="PHONE_FORMAT", kind="rule",
                      value="1234", message="", tier="approval")
        with pytest.raises(NotFixable):
            self.rule.fix("1234", issue)


# ── TEXT_NORMALIZE ────────────────────────────────────────────────────────────

class TestTextNormalize:
    rule = TextNormalizeRule()

    def _check(self, values, target="실적.기관명"):
        ds = make_ds("실적", "기관명", values)
        binding = make_binding("TEXT_NORMALIZE", [target])
        return self.rule.check(ds, binding, RuleContext())

    def test_clean_text_no_issue(self):
        issues = self._check(["창원시", "(주)한국기업"])
        assert issues == []

    def test_leading_trailing_space(self):
        issues = self._check([" 창원시 "])
        assert len(issues) == 1
        assert issues[0].suggestion == "창원시"

    def test_fullwidth_to_halfwidth(self):
        # 전각 숫자/알파벳 변환
        issues = self._check(["ＡＢＣ"])
        assert len(issues) == 1
        assert issues[0].suggestion == "ABC"

    def test_circle_ju_normalize(self):
        issues = self._check(["㈜한국기업"])
        assert len(issues) == 1
        assert issues[0].suggestion == "(주)한국기업"

    def test_double_space(self):
        issues = self._check(["창원  시"])
        assert len(issues) == 1
        assert issues[0].suggestion == "창원 시"

    def test_empty_skipped(self):
        issues = self._check([""])
        assert issues == []

    def test_fix_space(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="TEXT_NORMALIZE", kind="rule",
                      value=" 창원시 ", message="", tier="auto")
        assert self.rule.fix(" 창원시 ", issue) == "창원시"

    def test_fix_circle_ju(self):
        from engine.models import Issue
        issue = Issue(issue_id="E-0001", rule_id="TEXT_NORMALIZE", kind="rule",
                      value="㈜한국기업", message="", tier="auto")
        assert self.rule.fix("㈜한국기업", issue) == "(주)한국기업"
