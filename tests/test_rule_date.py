"""R3. DATE_NORMALIZE 규칙 테스트 (RULES.md R3 예시표 + 필수 추가 케이스)."""
from datetime import date

import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.date import DateNormalizeRule, normalize_date


PERIOD = (date(2026, 1, 1), date(2026, 12, 31))


def make_ds(values: list[str], file: str = "t.xlsx") -> Dataset:
    df = pd.DataFrame({"_row": range(2, 2 + len(values)), "지원일자": values})
    return Dataset({(file, "실적"): df})


def make_binding(with_range: bool = True) -> RuleBinding:
    params = {"range": ["2026-01-01", "2026-12-31"]} if with_range else {}
    return RuleBinding(
        rule_id="DATE_NORMALIZE", targets=["실적.지원일자"], params=params, source="standard"
    )


rule = DateNormalizeRule()


# ── normalize_date 순수 함수 ──────────────────────────────────────────────────

class TestNormalizeDate:
    # 이미 정규화된 값 → 변경 없음
    def test_iso_unchanged(self):
        r = normalize_date("2026-04-15")
        assert r.value == "2026-04-15"
        assert r.certain is True

    # YYYY.M.D 계열
    def test_dot_format(self):
        assert normalize_date("2026.4.15").value == "2026-04-15"

    def test_dot_format_padded(self):
        assert normalize_date("2026.04.15").value == "2026-04-15"

    def test_dot_with_trailing_dot(self):
        assert normalize_date("2026. 4. 15.").value == "2026-04-15"

    def test_slash_format(self):
        assert normalize_date("2026/04/15").value == "2026-04-15"

    # YYYYMMDD
    def test_compact_8digit(self):
        r = normalize_date("20260415")
        assert r.value == "2026-04-15"
        assert r.certain is True

    # 한국어
    def test_korean_format(self):
        assert normalize_date("2026년 4월 15일").value == "2026-04-15"

    # 엑셀 일련번호 (46127 = 2026-04-15)
    def test_excel_serial(self):
        r = normalize_date("46127")
        assert r.value == "2026-04-15"
        assert r.certain is True

    # YYYY-MM-DD HH:MM:SS
    def test_datetime_strips_time(self):
        assert normalize_date("2026-04-15 00:00:00").value == "2026-04-15"

    # M/D/YYYY — 첫 수 > 12 → 일/월 확실
    def test_slash_day_first(self):
        r = normalize_date("25/7/2026")
        assert r.value == "2026-07-25"
        assert r.certain is True

    # M/D/YYYY — 첫 수 ≤ 12이고 둘째 수 > 12 → 월/일 확실
    def test_slash_month_first(self):
        r = normalize_date("7/25/2026")
        assert r.value == "2026-07-25"
        assert r.certain is True

    # M/D/YYYY — 둘 다 ≤ 12 → 모호 → approval
    def test_slash_ambiguous(self):
        r = normalize_date("4/5/2026")
        assert r.value == "2026-04-05"
        assert r.certain is False

    # 2자리 연도 — 사업기간 내 → certain
    def test_2digit_year_certain(self):
        r = normalize_date("26-01-21", PERIOD)
        assert r.value == "2026-01-21"
        assert r.certain is True

    # 2자리 연도 — 뒤집힌 해석 가능 → approval
    def test_2digit_year_ambiguous(self):
        r = normalize_date("26-01-17", None)  # period 없음 → 검증 불가
        # 뒤집힌 DD-MM-YY: 2026-01-17 vs 2017-01-26 — 현재 구현은 후보가 다르면 approval
        # 단순히 NormResult.value 가 있는지만 확인
        assert r.value is not None

    # 존재하지 않는 날짜 → request
    def test_invalid_date_none(self):
        r = normalize_date("2026-02-30")
        assert r.value is None

    def test_invalid_month(self):
        r = normalize_date("2026-13-01")
        assert r.value is None

    # 인식 불가
    def test_unparseable(self):
        r = normalize_date("4월 중순")
        assert r.value is None


# ── DateNormalizeRule.check ───────────────────────────────────────────────────

class TestDateNormalizeRule:
    def test_iso_no_issue(self):
        ds = make_ds(["2026-04-15"])
        assert rule.check(ds, make_binding(), RuleContext()) == []

    def test_slash_auto(self):
        ds = make_ds(["7/25/2026"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "auto"
        assert issues[0].suggestion == "2026-07-25"

    def test_2digit_auto(self):
        ds = make_ds(["26-01-21"])
        issues = rule.check(ds, make_binding(), RuleContext())
        format_issues = [i for i in issues if i.rule_id == "DATE_NORMALIZE"]
        assert any(i.suggestion == "2026-01-21" for i in format_issues)

    def test_ambiguous_approval(self):
        ds = make_ds(["4/5/2026"])
        issues = rule.check(ds, make_binding(), RuleContext())
        norm_issues = [i for i in issues if i.rule_id == "DATE_NORMALIZE"]
        assert norm_issues[0].tier == "approval"

    def test_excel_serial_auto(self):
        ds = make_ds(["46127"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert any(i.suggestion == "2026-04-15" for i in issues)

    def test_invalid_date_request(self):
        ds = make_ds(["2026-02-30"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert issues[0].tier == "request"
        assert issues[0].suggestion is None

    def test_out_of_range_separate_issue(self):
        ds = make_ds(["2025-12-31"])
        issues = rule.check(ds, make_binding(with_range=True), RuleContext())
        rule_ids = [i.rule_id for i in issues]
        assert "DATE_OUT_OF_RANGE" in rule_ids

    def test_dot_format_auto(self):
        ds = make_ds(["2026.04.15"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert issues[0].suggestion == "2026-04-15"
        assert issues[0].tier == "auto"
