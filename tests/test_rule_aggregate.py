"""R5~R7 집계 규칙 테스트 (RULES.md 6장 기준).

FakeLLM 없이 순수 코드로 실행 가능해야 한다.
"""
from __future__ import annotations

import pandas as pd
import pytest

from engine.models import Dataset, RuleSpec
from engine.rules.aggregate import (
    load_business_master,
    run_max_count,
    run_near_duplicate,
    run_sum_limit,
    validate_spec,
)
from engine.rules.base import RuleContext


# ── 픽스처 헬퍼 ──────────────────────────────────────────────────────────────

def _make_ds(*rows_by_file: tuple[str, list[dict]]) -> Dataset:
    """(파일명, 행 목록) 쌍들을 받아 Dataset을 생성한다.

    각 행 dict는 표준 컬럼을 포함해야 하며 _row는 자동 부여된다.
    """
    tables = {}
    for file, rows in rows_by_file:
        for i, row in enumerate(rows, 2):   # 헤더=1, 첫 데이터=2
            row.setdefault("_row", i)
        tables[(file, "실적")] = pd.DataFrame(rows)
    return Dataset(tables)


def _ctx() -> RuleContext:
    return RuleContext()


def _spec(**kwargs) -> RuleSpec:
    defaults = dict(
        spec_id="RS-TEST",
        template="MAX_COUNT",
        description="테스트",
        group_by=["사업자번호"],
        params={},
        source="user_query",
    )
    defaults.update(kwargs)
    return RuleSpec(**defaults)


# ── validate_spec ─────────────────────────────────────────────────────────────

class TestValidateSpec:
    def test_valid_max_count(self):
        s = _spec(group_by=["사업자번호", "기관명"], params={"max": "2", "count_distinct": "사업명"})
        assert validate_spec(s) == []

    def test_invalid_group_by_column(self):
        s = _spec(group_by=["비표준컬럼"])
        errors = validate_spec(s)
        assert any("표준 컬럼" in e for e in errors)

    def test_max_count_max_zero(self):
        s = _spec(params={"max": "0", "count_distinct": "행"})
        errors = validate_spec(s)
        assert any("max" in e for e in errors)

    def test_max_count_invalid_count_distinct(self):
        s = _spec(params={"max": "1", "count_distinct": "이름"})
        errors = validate_spec(s)
        assert any("count_distinct" in e for e in errors)

    def test_valid_near_duplicate(self):
        s = _spec(
            template="NEAR_DUPLICATE",
            group_by=["사업자번호", "사업명"],
            params={"amount_tolerance_pct": "5", "date_window_days": "30"},
        )
        assert validate_spec(s) == []

    def test_near_duplicate_tolerance_out_of_range(self):
        s = _spec(
            template="NEAR_DUPLICATE",
            group_by=["사업자번호"],
            params={"amount_tolerance_pct": "25"},
        )
        errors = validate_spec(s)
        assert any("amount_tolerance_pct" in e for e in errors)

    def test_valid_sum_limit(self):
        s = _spec(
            template="SUM_LIMIT",
            group_by=["사업자번호"],
            params={"max_amount": "30000000", "scope": "per_business", "sum_column": "지원금액"},
        )
        assert validate_spec(s) == []

    def test_sum_limit_invalid_scope(self):
        s = _spec(
            template="SUM_LIMIT",
            group_by=["사업자번호"],
            params={"max_amount": "10000000", "scope": "wrong"},
        )
        errors = validate_spec(s)
        assert any("scope" in e for e in errors)


# ── R5: MAX_COUNT ─────────────────────────────────────────────────────────────

class TestMaxCount:
    def _spec_district(self, max_val: int = 1) -> RuleSpec:
        """시군 내 사업 종류 중복 검사 spec (filter 없이 전체 데이터 대상)."""
        return _spec(
            template="MAX_COUNT",
            group_by=["기관명", "사업자번호"],
            params={"count_distinct": "사업명", "max": str(max_val)},
            period="year",
            filter_business=None,
        )

    def _spec_annual(self, max_val: int = 1) -> RuleSpec:
        """연간 횟수 검사 spec."""
        return _spec(
            template="MAX_COUNT",
            group_by=["사업명", "사업자번호"],
            params={"count_distinct": "행", "max": str(max_val)},
            period="year",
        )

    def _spec_province(self, max_val: int = 3) -> RuleSpec:
        """도 전체 사업 수 검사 spec."""
        return _spec(
            template="MAX_COUNT",
            group_by=["사업자번호"],
            params={"count_distinct": "사업명", "max": str(max_val)},
            period="year",
        )

    def test_district_over_limit(self):
        """한 시군에서 사업 2개 이상 받음 → 탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "스마트공장구축사업",
             "지원일자": "2026-04-01", "지원금액": "3000000"},
        ]))
        issues = run_max_count(ds, self._spec_district(), _ctx())
        assert len(issues) == 1
        assert issues[0].rule_id == "MAX_COUNT"
        assert "창원시" in issues[0].group_key.get("기관명", "")

    def test_district_within_limit(self):
        """한 시군에서 사업 1개만 받음 → 미탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
        ]))
        issues = run_max_count(ds, self._spec_district(), _ctx())
        assert issues == []

    def test_annual_over_limit(self):
        """같은 사업에서 연 2회 지원 받음 → 탐지."""
        ds = _make_ds(("김해시_사업실적.xlsx", [
            {"기관명": "김해시", "사업자번호": "9876543210", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
            {"기관명": "김해시", "사업자번호": "9876543210", "사업명": "청년창업지원사업",
             "지원일자": "2026-06-01", "지원금액": "5000000"},
        ]))
        spec = self._spec_annual(max_val=1)
        issues = run_max_count(ds, spec, _ctx())
        assert len(issues) == 1

    def test_year_boundary_different_years(self):
        """2025년 1회 + 2026년 1회 → period=year이면 미탐지."""
        ds = _make_ds(("김해시_사업실적.xlsx", [
            {"기관명": "김해시", "사업자번호": "9876543210", "사업명": "청년창업지원사업",
             "지원일자": "2025-12-01", "지원금액": "5000000"},
            {"기관명": "김해시", "사업자번호": "9876543210", "사업명": "청년창업지원사업",
             "지원일자": "2026-01-10", "지원금액": "5000000"},
        ]))
        spec = self._spec_annual(max_val=1)
        issues = run_max_count(ds, spec, _ctx())
        assert issues == []

    def test_province_over_limit(self):
        """도 전체에서 사업 4개 받음(한도 3개) → 탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1111111111", "사업명": "A사업",
             "지원일자": "2026-01-01", "지원금액": "1000000"},
            {"기관명": "창원시", "사업자번호": "1111111111", "사업명": "B사업",
             "지원일자": "2026-02-01", "지원금액": "1000000"},
        ]), ("진주시_사업실적.xlsx", [
            {"기관명": "진주시", "사업자번호": "1111111111", "사업명": "C사업",
             "지원일자": "2026-03-01", "지원금액": "1000000"},
            {"기관명": "진주시", "사업자번호": "1111111111", "사업명": "D사업",
             "지원일자": "2026-04-01", "지원금액": "1000000"},
        ]))
        issues = run_max_count(ds, self._spec_province(max_val=3), _ctx())
        assert len(issues) == 1
        assert issues[0].spec_id == "RS-TEST"

    def test_different_businesses_normal(self):
        """서로 다른 기업이 각각 1사업씩 → 미탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1111111111", "사업명": "청년창업지원사업",
             "지원일자": "2026-01-01", "지원금액": "5000000"},
            {"기관명": "창원시", "사업자번호": "2222222222", "사업명": "스마트공장구축사업",
             "지원일자": "2026-02-01", "지원금액": "3000000"},
        ]))
        issues = run_max_count(ds, self._spec_district(), _ctx())
        assert issues == []


# ── R6: NEAR_DUPLICATE ───────────────────────────────────────────────────────

class TestNearDuplicate:
    def _spec(self, tolerance_pct=0, window_days=30) -> RuleSpec:
        return _spec(
            template="NEAR_DUPLICATE",
            group_by=["사업자번호", "사업명"],
            params={"amount_tolerance_pct": str(tolerance_pct), "date_window_days": str(window_days)},
        )

    def test_exact_duplicate_same_file(self):
        """완전히 동일한 두 행 → 탐지 (동일 행 중복 입력 의심)."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
        ]))
        issues = run_near_duplicate(ds, self._spec(), _ctx())
        assert len(issues) == 1
        assert "중복" in issues[0].message

    def test_cross_file_duplicate(self):
        """파일이 다른 동일 건 → 탐지 (여러 시군 보고 의심)."""
        row = {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
               "지원일자": "2026-03-01", "지원금액": "5000000"}
        row2 = dict(row)
        row2["기관명"] = "진주시"
        ds = _make_ds(
            ("창원시_사업실적.xlsx", [row]),
            ("진주시_사업실적.xlsx", [row2]),
        )
        issues = run_near_duplicate(ds, self._spec(), _ctx())
        assert len(issues) == 1
        assert "시군" in issues[0].message

    def test_31_day_gap_no_detect(self):
        """날짜 차이 31일 (window_days=30) → 미탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-01-01", "지원금액": "5000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-02-01", "지원금액": "5000000"},
        ]))
        issues = run_near_duplicate(ds, self._spec(window_days=30), _ctx())
        assert issues == []

    def test_amount_1pct_diff_with_0pct_tolerance(self):
        """금액 1% 차이 + 허용 오차 0% → 미탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-05", "지원금액": "5050000"},  # 1% 차이
        ]))
        issues = run_near_duplicate(ds, self._spec(tolerance_pct=0), _ctx())
        assert issues == []

    def test_amount_1pct_diff_with_2pct_tolerance(self):
        """금액 1% 차이 + 허용 오차 2% → 탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "5000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-05", "지원금액": "5050000"},
        ]))
        issues = run_near_duplicate(ds, self._spec(tolerance_pct=2), _ctx())
        assert len(issues) == 1


# ── R7: SUM_LIMIT ─────────────────────────────────────────────────────────────

class TestSumLimit:
    def _spec_per(self, max_amount: int = 30_000_000, biz: str | None = None) -> RuleSpec:
        return _spec(
            template="SUM_LIMIT",
            group_by=["사업자번호"],
            params={"sum_column": "지원금액", "max_amount": str(max_amount), "scope": "per_business"},
            period="year",
            filter_business=biz,
        )

    def _spec_total(self, max_amount: int = 50_000_000) -> RuleSpec:
        return _spec(
            template="SUM_LIMIT",
            group_by=["사업자번호"],
            params={"sum_column": "지원금액", "max_amount": str(max_amount), "scope": "total"},
            period="year",
        )

    def test_per_business_over_limit(self):
        """사업별 합계 한도 초과 → 탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "20000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-06-01", "지원금액": "15000000"},
        ]))
        issues = run_sum_limit(ds, self._spec_per(max_amount=30_000_000, biz="청년창업지원사업"), _ctx())
        assert len(issues) == 1
        assert "30,000,000원" in issues[0].metric

    def test_per_business_exactly_at_limit(self):
        """사업별 합계 == 한도 → 미탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "15000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-06-01", "지원금액": "15000000"},
        ]))
        issues = run_sum_limit(ds, self._spec_per(max_amount=30_000_000, biz="청년창업지원사업"), _ctx())
        assert issues == []

    def test_total_scope_over_limit(self):
        """합산 한도 초과(여러 사업 합산) → 탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "30000000"},
        ]), ("진주시_사업실적.xlsx", [
            {"기관명": "진주시", "사업자번호": "1234567890", "사업명": "수출바우처사업",
             "지원일자": "2026-04-01", "지원금액": "30000000"},
        ]))
        issues = run_sum_limit(ds, self._spec_total(max_amount=50_000_000), _ctx())
        assert len(issues) == 1

    def test_total_scope_at_limit(self):
        """합산 합계 == 한도 → 미탐지."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "청년창업지원사업",
             "지원일자": "2026-03-01", "지원금액": "25000000"},
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "수출바우처사업",
             "지원일자": "2026-06-01", "지원금액": "25000000"},
        ]))
        issues = run_sum_limit(ds, self._spec_total(max_amount=50_000_000), _ctx())
        assert issues == []

    def test_unknown_business_name(self):
        """filter_business와 일치하는 사업명이 없음 → 빈 결과."""
        ds = _make_ds(("창원시_사업실적.xlsx", [
            {"기관명": "창원시", "사업자번호": "1234567890", "사업명": "다른사업",
             "지원일자": "2026-03-01", "지원금액": "40000000"},
        ]))
        issues = run_sum_limit(ds, self._spec_per(biz="청년창업지원사업"), _ctx())
        assert issues == []


# ── load_business_master ──────────────────────────────────────────────────────

class TestLoadBusinessMaster:
    def test_loads_standard_file(self, tmp_path):
        csv = tmp_path / "bm.csv"
        csv.write_text(
            "사업코드,사업명,기업당한도,연간허용횟수,시군내중복허용,사업기간시작,사업기간종료\n"
            "B001,테스트사업,30000000,1,N,2026-01-01,2026-12-31\n",
            encoding="utf-8",
        )
        specs = load_business_master(csv)
        # R5-A(연간1회), R5-B(시군내중복N), R7(한도) → 3건
        assert len(specs) == 3
        templates = {s.template for s in specs}
        assert "MAX_COUNT" in templates
        assert "SUM_LIMIT" in templates

    def test_returns_empty_for_missing_file(self, tmp_path):
        specs = load_business_master(tmp_path / "nonexistent.csv")
        assert specs == []

    def test_no_district_dup_rule_when_allowed(self, tmp_path):
        csv = tmp_path / "bm.csv"
        csv.write_text(
            "사업코드,사업명,기업당한도,연간허용횟수,시군내중복허용,사업기간시작,사업기간종료\n"
            "B002,스마트공장,10000000,3,Y,2026-01-01,2026-12-31\n",
            encoding="utf-8",
        )
        specs = load_business_master(csv)
        # 시군내중복허용=Y이면 R5-B 없음 → R5-A + R7 = 2건
        assert len(specs) == 2
        assert all(s.template != "MAX_COUNT" or s.params.get("count_distinct") == "행" for s in specs)
