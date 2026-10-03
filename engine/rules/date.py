"""R3. DATE_NORMALIZE — 지원일자 정규화."""
from __future__ import annotations

import re
from datetime import date, timedelta

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NormResult, NotFixable, Rule, RuleContext, register
from engine.rules.blank import is_blank

_EXCEL_EPOCH = date(1899, 12, 30)


# ── 정규화 함수 (순수 함수) ───────────────────────────────────────────────────

def normalize_date(raw: str, period: tuple[date, date] | None = None) -> NormResult:
    """YYYY-MM-DD 형식으로 정규화.

    period: 사업기간 (이미 정규화된 날짜를 범위 검증에 사용하지 않음; 여기선 2자리 연도 검증에만 사용)
    """
    v = raw.strip()

    # ── 패턴 1: 이미 YYYY-MM-DD ────────────────────────────────────────────
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        if d.isoformat() == v:
            return NormResult(v, True, "")   # 변경 없음
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    # ── 패턴 2: YYYY-MM-DD HH:MM(:SS) ─────────────────────────────────────
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})[ T]\d{2}:\d{2}.*", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜에서 시간 제거")

    # ── 패턴 3: YYYY.M.D 또는 YYYY/M/D (점·슬래시, 앞뒤 공백 포함) ────────
    m = re.fullmatch(r"(\d{4})[./]\s*(\d{1,2})[./]\s*(\d{1,2})[.]?", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    # ── 패턴 4: YYYYMMDD (8자리 숫자) ─────────────────────────────────────
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    # ── 패턴 5: YYYY년 M월 D일 ─────────────────────────────────────────────
    m = re.fullmatch(r"(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일?", v)
    if m:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d is None:
            return NormResult(None, True, f"존재하지 않는 날짜: {v}")
        return NormResult(d.isoformat(), True, "날짜 형식 정규화")

    # ── 패턴 6: 엑셀 일련번호 (30000~60000) ───────────────────────────────
    m = re.fullmatch(r"(\d+)", v)
    if m:
        n = int(m.group(1))
        if 30_000 <= n <= 60_000:
            d = _EXCEL_EPOCH + timedelta(days=n)
            return NormResult(d.isoformat(), True, "엑셀 날짜 일련번호 변환")

    # ── 패턴 7: M/D/YYYY (연도가 끝) ─────────────────────────────────────
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _parse_slash_date(a, b, y)

    # ── 패턴 8: YY-MM-DD / YY.MM.DD / YY/MM/DD (2자리 연도) ──────────────
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
        # a=월, b=일 확실
        d = _safe_date(y, a, b)
        if d:
            return NormResult(d.isoformat(), True, "날짜 형식 정규화 (M/D/YYYY)")
    elif not a_valid and b_valid:
        # a=일, b=월 확실
        d = _safe_date(y, b, a)
        if d:
            return NormResult(d.isoformat(), True, "날짜 형식 정규화 (D/M/YYYY)")
    else:
        # 둘 다 ≤12 → 모호 (월/일로 우선 해석, approval)
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

    # 사업기간 안에 있으면 certain
    if period and period[0] <= d_primary <= period[1]:
        return NormResult(d_primary.isoformat(), True, "날짜 형식 정규화 (2자리 연도)")

    # 뒤집힌 해석이 가능하면 (DD-MM-YY 로도 해석 가능) approval
    d_alt = _safe_date(2000 + dd, mo, yy) if dd > 31 else _safe_date(2000 + yy, dd, mo)
    if d_alt and d_alt != d_primary:
        return NormResult(
            d_primary.isoformat(), False,
            f"2자리 연도 모호 — {d_primary.isoformat()} 또는 {d_alt.isoformat()} 확인 필요",
        )

    return NormResult(d_primary.isoformat(), True, "날짜 형식 정규화 (2자리 연도)")


# ── DATE_NORMALIZE Rule ───────────────────────────────────────────────────────

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
                        # 패턴 불인식 또는 존재하지 않는 날짜
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
                            tier=tier,  # type: ignore[arg-type]
                            suggestion=result.value,
                        ))

                    # 범위 확인 (정규화 성공 후 별도 Issue)
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
