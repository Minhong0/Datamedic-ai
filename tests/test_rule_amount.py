"""R2. AMOUNT_NORMALIZE 규칙 테스트 (RULES.md R2 예시표 + 필수 추가 케이스)."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.amount import AmountNormalizeRule, normalize_amount


def make_ds(values: list[str], file: str = "t.xlsx") -> Dataset:
    df = pd.DataFrame({"_row": range(2, 2 + len(values)), "지원금액": values})
    return Dataset({(file, "실적"): df})


def make_binding(params: dict | None = None) -> RuleBinding:
    return RuleBinding(
        rule_id="AMOUNT_NORMALIZE",
        targets=["실적.지원금액"],
        params=params or {},
        source="standard",
    )


rule = AmountNormalizeRule()


# ── normalize_amount 순수 함수 ────────────────────────────────────────────────

class TestNormalizeAmount:
    def test_plain_integer_unchanged(self):
        r = normalize_amount("171321210")
        assert r.value == "171321210"
        assert r.certain is True

    def test_comma_won(self):
        r = normalize_amount("171,321,210원")
        assert r.value == "171321210"
        assert r.certain is True

    def test_manwon_decimal(self):
        r = normalize_amount("17,132.121만원")
        assert r.value == "171321210"
        assert r.certain is True

    def test_mixed_units(self):
        r = normalize_amount("1억 7,132만 1,210원")
        assert r.value == "171321210"
        assert r.certain is True

    def test_eok_only(self):
        r = normalize_amount("2억")
        assert r.value == "200000000"
        assert r.certain is True

    def test_decimal_won_fails(self):
        r = normalize_amount("171321210.5")
        assert r.value is None
        assert r.certain is False

    def test_unparseable(self):
        r = normalize_amount("약 1.7억")
        assert r.value is None

    def test_fullwidth_digits(self):
        r = normalize_amount("１７１３２１２１０")
        assert r.value == "171321210"

    def test_won_sign(self):
        r = normalize_amount("₩171321210")
        assert r.value == "171321210"

    def test_negative(self):
        r = normalize_amount("-100000")
        assert r.value == "-100000"

    def test_header_unit_cheonwon(self):
        r = normalize_amount("171321", header_unit="천원")
        assert r.value == "171321000"
        assert r.certain is False  # approval


# ── AmountNormalizeRule.check ─────────────────────────────────────────────────

class TestAmountNormalizeRule:
    def test_clean_integer_no_issue(self):
        ds = make_ds(["117480000", "48960000"])
        assert rule.check(ds, make_binding(), RuleContext()) == []

    def test_comma_flagged_auto(self):
        ds = make_ds(["117,480,000"])
        issues = rule.check(ds, make_binding({"unit": "원"}), RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "auto"
        assert issues[0].suggestion == "117480000"

    def test_manwon_flagged_auto(self):
        ds = make_ds(["2434만원"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert len(issues) == 1
        assert issues[0].suggestion == "24340000"

    def test_unparseable_approval(self):
        ds = make_ds(["약 1.7억"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "approval"
        assert issues[0].suggestion is None

    def test_mixed_units_auto(self):
        ds = make_ds(["1억 7,132만 1,210원"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert len(issues) == 1
        assert issues[0].suggestion == "171321210"

    def test_won_unit_removed_auto(self):
        ds = make_ds(["45190000원"])
        issues = rule.check(ds, make_binding(), RuleContext())
        assert issues[0].suggestion == "45190000"
