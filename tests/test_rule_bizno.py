"""R4. BIZNO_VERIFY 규칙 테스트 (RULES.md R4 예시표 + 필수 추가 케이스)."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.bizno import BiznoVerifyRule, bizno_checksum_ok, normalize_bizno
from engine.tools.nts_client import NtsRecord, OffProvider


# ── 인메모리 Mock Provider (nts_mock.json 파일 의존 없음) ────────────────────

class _InMemoryProvider:
    """테스트 전용: 미리 정의한 dict 기반 provider."""
    disabled = False

    def __init__(self, db: dict[str, dict]) -> None:
        self._db = {k: NtsRecord(v) for k, v in db.items()}

    def query(self, bizno_list: list[str]) -> dict[str, "NtsRecord"]:
        return {b: self._db[b] for b in bizno_list if b in self._db}


def _make_mock(entries: dict[str, dict]) -> _InMemoryProvider:
    return _InMemoryProvider(entries)


# ── 테스트 고정 사업자번호 (체크디지트 유효, 파일 독립) ──────────────────────

MOCK_ACTIVE   = "1234567891"   # 계속사업자
MOCK_SUSPEND  = "9876543215"   # 휴업자
MOCK_CLOSED_B = "1111111119"   # 폐업 (2025-01-01, 지원 전)
MOCK_CLOSED_A = "2222222227"   # 폐업 (2026-12-01, 지원 후)
MOCK_NOTFOUND = "3333333336"   # 국세청 미등록

_NTS_DB = {
    MOCK_ACTIVE:   {"b_no": MOCK_ACTIVE,   "b_stt": "계속사업자",   "b_stt_cd": "01", "tax_type": "일반과세자", "tax_type_cd": "1", "end_dt": ""},
    MOCK_SUSPEND:  {"b_no": MOCK_SUSPEND,  "b_stt": "휴업자",       "b_stt_cd": "02", "tax_type": "", "tax_type_cd": "", "end_dt": ""},
    MOCK_CLOSED_B: {"b_no": MOCK_CLOSED_B, "b_stt": "폐업자",       "b_stt_cd": "03", "tax_type": "", "tax_type_cd": "", "end_dt": "20250101"},
    MOCK_CLOSED_A: {"b_no": MOCK_CLOSED_A, "b_stt": "폐업자",       "b_stt_cd": "03", "tax_type": "", "tax_type_cd": "", "end_dt": "20261201"},
    MOCK_NOTFOUND: {"b_no": MOCK_NOTFOUND, "b_stt": "국세청 미등록", "b_stt_cd": None,  "tax_type": "", "tax_type_cd": "", "end_dt": ""},
}

_MOCK_PROVIDER = _make_mock(_NTS_DB)


def make_ds(
    bizno_vals: list[str],
    date_vals: list[str] | None = None,
    file: str = "t.xlsx",
) -> Dataset:
    n = len(bizno_vals)
    data: dict = {"_row": list(range(2, 2 + n)), "사업자번호": bizno_vals}
    if date_vals:
        data["지원일자"] = date_vals
    return Dataset({(file, "실적"): pd.DataFrame(data)})


def make_binding(params: dict | None = None) -> RuleBinding:
    return RuleBinding(
        rule_id="BIZNO_VERIFY",
        targets=["실적.사업자번호"],
        params=params or {},
        source="standard",
    )


rule = BiznoVerifyRule()


def fmt(d: str) -> str:
    return f"{d[:3]}-{d[3:5]}-{d[5:]}"


# ── 로컬 함수 테스트 ──────────────────────────────────────────────────────────

class TestBiznoChecksumOk:
    def test_valid(self):
        assert bizno_checksum_ok(MOCK_ACTIVE) is True

    def test_invalid(self):
        assert bizno_checksum_ok("1234567890") is False

    def test_wrong_length(self):
        assert bizno_checksum_ok("123456789") is False


class TestNormalizeBizno:
    def test_hyphen_format(self):
        r = normalize_bizno(fmt(MOCK_ACTIVE))
        assert r.value == fmt(MOCK_ACTIVE)

    def test_no_hyphen_normalized(self):
        r = normalize_bizno(MOCK_ACTIVE)
        assert r.value == fmt(MOCK_ACTIVE)

    def test_9digit_fails(self):
        r = normalize_bizno("123456789")
        assert r.value is None

    def test_13digit_fails(self):
        r = normalize_bizno("1234567890123")
        assert r.value is None


# ── R4-1: 형식 정규화 ─────────────────────────────────────────────────────────

class TestFormatNormalization:
    def _check(self, val: str):
        ds = make_ds([val])
        return rule.check(ds, make_binding({"_provider": OffProvider()}), RuleContext())

    def test_9digit_bizno_format_request(self):
        issues = self._check("123456789")
        assert any(i.rule_id == "BIZNO_FORMAT" and i.tier == "request" for i in issues)

    def test_no_hyphen_auto_format(self):
        issues = self._check(MOCK_ACTIVE)
        fmt_issues = [i for i in issues if i.rule_id == "BIZNO_VERIFY" and i.tier == "auto"]
        assert len(fmt_issues) == 1
        assert fmt_issues[0].suggestion == fmt(MOCK_ACTIVE)

    def test_correct_format_no_format_issue(self):
        issues = self._check(fmt(MOCK_ACTIVE))
        fmt_issues = [i for i in issues if i.rule_id == "BIZNO_VERIFY" and i.tier == "auto"]
        assert fmt_issues == []


# ── R4-2: 체크디지트 ──────────────────────────────────────────────────────────

class TestChecksumVerification:
    def test_checksum_fail_approval(self):
        ds = make_ds(["123-45-67890"])
        issues = rule.check(ds, make_binding({"_provider": OffProvider()}), RuleContext())
        assert any(i.rule_id == "BIZNO_CHECKSUM" and i.tier == "approval" for i in issues)

    def test_checksum_fail_no_api_call(self):
        ds = make_ds(["123-45-67890"])
        issues = rule.check(ds, make_binding({"_provider": OffProvider()}), RuleContext())
        assert not any(i.rule_id == "BIZNO_UNVERIFIED" for i in issues)


# ── R4-3: 국세청 상태조회 (인메모리 mock) ─────────────────────────────────────

class TestNtsStatusMock:
    def _check(self, bizno: str, date_val: str | None = None):
        ds = make_ds([fmt(bizno)], [date_val] if date_val else None)
        params: dict = {"_provider": _MOCK_PROVIDER}
        if date_val:
            params["date_col"] = "지원일자"
        return rule.check(ds, make_binding(params), RuleContext())

    def test_active_no_issue(self):
        issues = self._check(MOCK_ACTIVE)
        status_rules = {"BIZNO_SUSPENDED", "BIZNO_CLOSED_BEFORE_SUPPORT",
                        "BIZNO_CLOSED_AFTER_SUPPORT", "BIZNO_NOT_FOUND"}
        assert not any(i.rule_id in status_rules for i in issues)

    def test_suspended_approval(self):
        issues = self._check(MOCK_SUSPEND)
        assert any(i.rule_id == "BIZNO_SUSPENDED" and i.tier == "approval" for i in issues)

    def test_closed_before_support_request(self):
        issues = self._check(MOCK_CLOSED_B, "2026-03-15")
        assert any(i.rule_id == "BIZNO_CLOSED_BEFORE_SUPPORT" and i.tier == "request"
                   for i in issues)

    def test_closed_after_support_approval(self):
        issues = self._check(MOCK_CLOSED_A, "2026-03-15")
        assert any(i.rule_id == "BIZNO_CLOSED_AFTER_SUPPORT" and i.tier == "approval"
                   for i in issues)

    def test_not_found_request(self):
        issues = self._check(MOCK_NOTFOUND)
        assert any(i.rule_id == "BIZNO_NOT_FOUND" and i.tier == "request" for i in issues)

    def test_api_off_no_nts_issue(self):
        # NTS_MODE=off: 체크디지트까지만 검증, NTS 관련 Issue 없음
        ds = make_ds([fmt(MOCK_ACTIVE)])
        issues = rule.check(ds, make_binding({"_provider": OffProvider()}), RuleContext())
        nts_ids = {"BIZNO_SUSPENDED", "BIZNO_CLOSED_BEFORE_SUPPORT",
                   "BIZNO_CLOSED_AFTER_SUPPORT", "BIZNO_NOT_FOUND", "BIZNO_UNVERIFIED"}
        assert not any(i.rule_id in nts_ids for i in issues)
