"""R3. DATE_NORMALIZE — 지원일자 정규화."""
from __future__ import annotations

import re
from datetime import date, timedelta

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NormResult, NotFixable, Rule, RuleContext, register
from engine.rules.blank import is_blank

_EXCEL_EPOCH = date(1899, 12, 30)


def normalize_date(raw: str, period: tuple[date, date] | None = None) -> NormResult:
    """YYYY-MM-DD 형식으로 정규화."""
    v = raw.strip()

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        if d.isoformat() == v:
            return NormResult(v, True, "")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T]\d{2}:\d{2}.*", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜에서 시간 제거")

    m = re.fullmatch(r"(\d{4})[./]\s*(\d{1,2})[./]\s*(\d{1,2})[.]?", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    m = re.fullmatch(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일?", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    m = re.fullmatch(r"(\d+)", v)
    if m:
        n = int(m.group(1))
        if 30_000 <= n <= 60_000:
            d = _EXCEL_EPOCH + timedelta(days=n)
            return NormResult(d.isoformat(), True, "엑셀 날짜 일련번호 변환")

    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _parse_slash_date(a, b, y)

    m = re.fullmatch(r"(\d{2})[-./](\d{1,2})[-./](\d{1,2})", v)
    if m:
        yy, mo, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _parse_2digit_year(yy, mo, dd, period)

    return NormResult(None, True, f"날짜 패턴을 인식할 수 없습니다: {v}")


def _safe_date(y: int, mo: int, d: int) -> date | None:
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _parse_slash_date(a: int, b: int, y: int) -> NormResult:
    """M/D/YYYY 또는 D/M/YYYY 해석."""
    a_valid = 1 <= a <= 12
    b_valid = 1 <= b <= 12

    if a_valid and not b_valid:
        d = _safe_date(y, a, b)
        if d:
            return NormResult(d.isoformat(), True, "날짜 형식 정규화 (M/D/YYYY)")
    elif not a_valid and b_valid:
        d = _safe_date(y, b, a)
        if d:
            return NormResult(d.isoformat(), True, "날짜 형식 정규화 (D/M/YYYY)")
    else:
        d = _safe_date(y, a, b)
        d2 = _safe_date(y, b, a)
        if d:
            candidates = d.isoformat()
            if d2 and d2 != d:
                candidates += f" 또는 {d2.isoformat()}"
            return NormResult(d.isoformat(), False,
                              f"날짜가 모호합니다 (후보: {candidates}) — 원본 확인 필요")
        if d2:
            return NormResult(d2.isoformat(), False, "날짜 형식 정규화 (D/M/YYYY, 모호)")

    return NormResult(None, True, f"존재하지 않는 날짜: {a}/{b}/{y}")


def _parse_2digit_year(yy: int, mo: int, dd: int, period: tuple[date, date] | None) -> NormResult:
    """2자리 연도 해석. 범위 검증을 거쳐 approval 여부 결정."""
    y = 2000 + yy
    d_primary = _safe_date(y, mo, dd)
    if d_primary is None:
        return NormResult(None, True, f"존재하지 않는 날짜: {yy:02d}-{mo:02d}-{dd:02d}")

    if period and period[0] <= d_primary <= period[1]:
        return NormResult(d_primary.isoformat(), True, "날짜 형식 정규화 (2자리 연도)")

    d_alt = _safe_date(2000 + dd, mo, yy) if dd > 31 else _safe_date(2000 + yy, dd, mo)
    if d_alt and d_alt != d_primary:
        return NormResult(
            d_primary.isoformat(), False,
            f"2자리 연도 모호 — {d_primary.isoformat()} 또는 {d_alt.isoformat()} 확인 필요",
        )

    return NormResult(d_primary.isoformat(), True, "날짜 형식 정규화 (2자리 연도)")


def _iter_column(ds: Dataset, target: str):
    parts = target.split(".")
    if len(parts) != 2:
        return
    sheet, column = parts
    for (file, s), df in ds.tables.items():
        if s == sheet and column in df.columns:
            yield file, sheet, df, column


def _parse_period(params: dict) -> tuple[date, date] | None:
    rng = params.get("range")
    if not rng or len(rng) != 2:
        return None
    try:
        return date.fromisoformat(str(rng[0])), date.fromisoformat(str(rng[1]))
    except ValueError:
        return None


@register
class DateNormalizeRule(Rule):
    """R3: 지원일자를 YYYY-MM-DD 형식으로 정규화하고, 범위를 확인한다."""

    rule_id = "DATE_NORMALIZE"
    default_tier = "auto"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        period = _parse_period(binding.params)

        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if is_blank(val):
                        continue

                    result = normalize_date(val, period)
                    ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)

                    if result.value is None:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"날짜를 인식할 수 없습니다: '{val}' — {result.reason}",
                            tier="request",
                        ))
                        continue

                    if result.value != val:
                        tier = "auto" if result.certain else "approval"
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"날짜 형식을 정규화합니다: '{val}' → '{result.value}'"
                                    + ("" if result.certain else f" ({result.reason})"),
                            tier=tier,
                            suggestion=result.value,
                        ))

                    if period and result.value:
                        try:
                            d = date.fromisoformat(result.value)
                            if not (period[0] <= d <= period[1]):
                                issues.append(Issue(
                                    issue_id=ctx.next_issue_id(), ref=ref,
                                    rule_id="DATE_OUT_OF_RANGE", kind="rule",
                                    value=result.value,
                                    message=(f"날짜 {result.value}이 사업기간 "
                                             f"({period[0]}~{period[1]}) 밖입니다"),
                                    tier="request",
                                ))
                        except ValueError:
                            pass

        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        r = normalize_date(value)
        if r.value is None:
            raise NotFixable
        return r.value
