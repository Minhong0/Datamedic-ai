"""R1. BLANK 규칙 테스트 (RULES.md R1 예시표 + 필수 추가 케이스)."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.blank import BlankRule, is_blank


def make_ds(sheet: str, columns: list[str], rows: list[list], file: str = "t.xlsx") -> Dataset:
    data = {"_row": list(range(2, 2 + len(rows)))}
    for i, col in enumerate(columns):
        data[col] = [r[i] for r in rows]
    return Dataset({(file, sheet): pd.DataFrame(data)})


def make_binding(required_cols: list[str]) -> RuleBinding:
    return RuleBinding(
        rule_id="BLANK", targets=[], params={"required_cols": required_cols}, source="standard"
    )


rule = BlankRule()


# ── is_blank 헬퍼 ──────────────────────────────────────────────────────────────

class TestIsBlank:
    def test_empty_string(self):        assert is_blank("") is True
    def test_spaces_only(self):         assert is_blank("   ") is True
    def test_tab_only(self):            assert is_blank("\t") is True
    def test_newline_only(self):        assert is_blank("\n") is True
    def test_fullwidth_space(self):     assert is_blank("　") is True
    def test_nbsp(self):                assert is_blank(" ") is True
    def test_dash_substitute(self):     assert is_blank("-") is True
    def test_double_dash(self):         assert is_blank("--") is True
    def test_eomsi(self):               assert is_blank("없음") is True
    def test_mijung(self):              assert is_blank("미정") is True
    def test_na_lower(self):            assert is_blank("n/a") is True
    def test_null_upper(self):          assert is_blank("NULL") is True
    def test_zero_date(self):           assert is_blank("0000-00-00") is True
    def test_real_value(self):          assert is_blank("창원시") is False
    def test_number(self):              assert is_blank("100000") is False


# ── R1 케이스 1: 필수 컬럼 빈 값 → request ──────────────────────────────────
# 다른 컬럼을 채워 '빈 행(Case 4)' 판정을 피한다

class TestRequiredBlank:
    def test_empty_string_request(self):
        ds = make_ds("실적", ["기관명", "사업명"], [["", "스마트공장"]])
        issues = rule.check(ds, make_binding(["실적.기관명"]), RuleContext())
        req = [i for i in issues if i.tier == "request"]
        assert len(req) == 1
        assert req[0].rule_id == "BLANK"

    def test_spaces_only_request(self):
        ds = make_ds("실적", ["기관명", "사업명"], [["  ", "스마트공장"]])
        issues = rule.check(ds, make_binding(["실적.기관명"]), RuleContext())
        assert any(i.tier == "request" for i in issues)

    def test_substitute_eomsi_request(self):
        ds = make_ds("실적", ["기관명", "사업명"], [["없음", "스마트공장"]])
        issues = rule.check(ds, make_binding(["실적.기관명"]), RuleContext())
        assert any(i.tier == "request" for i in issues)

    def test_fullwidth_space_request(self):
        ds = make_ds("실적", ["기관명", "사업명"], [["　", "스마트공장"]])
        issues = rule.check(ds, make_binding(["실적.기관명"]), RuleContext())
        assert any(i.tier == "request" for i in issues)

    def test_nbsp_request(self):
        ds = make_ds("실적", ["기관명", "사업명"], [[" ", "스마트공장"]])
        issues = rule.check(ds, make_binding(["실적.기관명"]), RuleContext())
        assert any(i.tier == "request" for i in issues)


# ── R1 케이스 2: 선택 컬럼 빈 값 → Issue 없음 ────────────────────────────────

class TestOptionalBlankNoIssue:
    def test_no_issue_for_optional(self):
        ds = make_ds("실적", ["기관명", "담당자연락처"], [["창원시", ""]])
        issues = rule.check(ds, make_binding([]), RuleContext())
        req = [i for i in issues if i.tier == "request"]
        assert req == []


# ── R1 케이스 3: 공백 정규화 → auto ──────────────────────────────────────────

class TestWhitespaceTrim:
    def test_leading_trailing_space(self):
        ds = make_ds("실적", ["기관명"], [[" 산청군 "]])
        issues = rule.check(ds, make_binding([]), RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "auto"
        assert issues[0].suggestion == "산청군"

    def test_double_space_inside(self):
        ds = make_ds("실적", ["기관명"], [["창원  시"]])
        issues = rule.check(ds, make_binding([]), RuleContext())
        assert any(i.suggestion == "창원 시" for i in issues)

    def test_no_issue_clean_value(self):
        ds = make_ds("실적", ["기관명"], [["산청군"]])
        issues = rule.check(ds, make_binding([]), RuleContext())
        assert issues == []


# ── R1 케이스 4: 행 전체 빈 값 → auto ────────────────────────────────────────

class TestBlankRow:
    def test_blank_row_auto(self):
        ds = make_ds("실적", ["기관명", "사업명"], [["", ""]])
        issues = rule.check(ds, make_binding(["실적.기관명", "실적.사업명"]), RuleContext())
        blank_row = [i for i in issues if i.value == "(빈 행)"]
        assert len(blank_row) == 1
        assert blank_row[0].tier == "auto"

    def test_partial_blank_row_not_deleted(self):
        ds = make_ds("실적", ["기관명", "사업명"], [["창원시", ""]])
        issues = rule.check(ds, make_binding(["실적.기관명"]), RuleContext())
        blank_row = [i for i in issues if i.value == "(빈 행)"]
        assert blank_row == []
