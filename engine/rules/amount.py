"""R2. AMOUNT_NORMALIZE — 지원금액 정규화."""
from __future__ import annotations

import re
import statistics

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NormResult, NotFixable, Rule, RuleContext, register
from engine.rules.blank import is_blank


_FULLWIDTH = str.maketrans(
    "０１２３４５６７８９，．",
    "0123456789,.",
)

_CURRENCY_RE = re.compile(r"[₩\\]|KRW")

_UNIT_PATTERN = re.compile(
    r"(?P<val>[\d,_. ]+)\s*(?P<unit>억|만|천|원)?",
    re.UNICODE,
)
_UNIT_MULT = {"억": 100_000_000, "만": 10_000, "천": 1_000, "원": 1, "": 1}


def normalize_amount(raw: str, header_unit: str | None = None) -> NormResult:
    """원 단위 순수 정수로 정규화."""
    v = raw.strip().translate(_FULLWIDTH)
    v = _CURRENCY_RE.sub("", v)

    effective_unit = (header_unit or "").rstrip("원").strip()
    if effective_unit and effective_unit in _UNIT_MULT:
        digits_only = v.replace(",", "").replace(" ", "")
        try:
            num = float(digits_only)
            converted = int(num * _UNIT_MULT[effective_unit])
            if converted != num * _UNIT_MULT[effective_unit]:
                return NormResult(None, False, f"헤더 단위({header_unit}) 적용 시 소수점 발생")
            return NormResult(str(converted), False,
                              f"헤더에 {header_unit} 단위 표기 → {converted}원으로 변환 제안")
        except ValueError:
            pass

    total = _parse_mixed(v)
    if total is not None:
        if isinstance(total, float) and total != int(total):
            return NormResult(None, False, "원 미만 소수점이 있어 정수로 변환 불가")
        int_total = int(total)
        if int_total < 0:
            return NormResult(str(int_total), True, "음수 금액 — 별도 확인 필요")
        return NormResult(str(int_total), True, "금액 표기 정규화")

    return NormResult(None, False, "금액으로 해석할 수 없는 값")


def _parse_small(seg: str) -> float | None:
    """억·만 구간 안의 값을 해석한다. 예: "6천" → 6000, "5천 5백" → 5500, "7,132" → 7132, "천" → 1000."""
    rem = re.sub(r"[,_\s]", "", seg)
    if not rem:
        return None
    total = 0.0
    for unit, mult in [("천", 1000), ("백", 100), ("십", 10)]:
        m = re.search(rf"([\d.]*){unit}", rem)
        if m:
            try:
                total += (float(m.group(1)) if m.group(1) else 1.0) * mult
            except ValueError:
                return None
            rem = rem[: m.start()] + rem[m.end():]
    if rem:
        try:
            total += float(rem)
        except ValueError:
            return None
    return total


def _parse_mixed(v: str) -> float | None:
    """혼합 단위 금액 문자열을 파싱해 원 단위 숫자를 반환."""
    s = v.strip()

    plain = s.replace(",", "").replace(" ", "")
    if _has_unit(s) is False:
        try:
            return float(plain)
        except ValueError:
            pass

    total = 0.0
    remainder = s
    for unit, mult in [("억", 100_000_000), ("만", 10_000)]:
        m = re.search(rf"([\d,_.\s천백십]+?)\s*{unit}", remainder)
        if m:
            seg = _parse_small(m.group(1))
            if seg is None:
                return None
            total += seg * mult
            remainder = remainder[: m.start()] + remainder[m.end():]

    remainder = re.sub(r"원$", "", remainder.strip()).strip()
    if remainder:
        rest = _parse_small(remainder)
        if rest is None:
            return None
        total += rest

    return total if (total != 0.0 or s.startswith("0")) else None


def _has_unit(s: str) -> bool | None:
    """단위 문자가 있으면 True, 없으면 False."""
    return bool(re.search(r"[억만천원]", s))


def _iter_column(ds: Dataset, target: str):
    parts = target.split(".")
    if len(parts) != 2:
        return
    sheet, column = parts
    for (file, s), df in ds.tables.items():
        if s == sheet and column in df.columns:
            yield file, sheet, df, column


_SUSPECT_RATIO = 1000


@register
class AmountNormalizeRule(Rule):
    """R2: 지원금액을 원 단위 순수 정수로 정규화."""

    rule_id = "AMOUNT_NORMALIZE"
    default_tier = "auto"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        header_unit: str | None = binding.params.get("unit")

        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                parsed_vals: list[float] = []
                for v in df[column]:
                    s = str(v).strip()
                    if is_blank(s):
                        continue
                    r = normalize_amount(s, header_unit)
                    if r.value is not None:
                        try:
                            parsed_vals.append(float(r.value))
                        except ValueError:
                            pass
                median = statistics.median(parsed_vals) if len(parsed_vals) >= 3 else None

                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if is_blank(val):
                        continue

                    result = normalize_amount(val, header_unit)
                    ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)

                    if result.value is None:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"금액으로 해석할 수 없습니다: '{val}' — {result.reason}",
                            tier="approval",
                        ))
                        continue

                    if result.value == val:
                        if median and float(result.value) > 0:
                            if median / float(result.value) >= _SUSPECT_RATIO:
                                issues.append(Issue(
                                    issue_id=ctx.next_issue_id(), ref=ref,
                                    rule_id="AMOUNT_SUSPECT_UNIT", kind="rule", value=val,
                                    message=(f"금액이 같은 파일 중앙값({median:.0f}원)의 "
                                             f"1/{_SUSPECT_RATIO} 이하 — 만원 단위 입력 의심"),
                                    tier="approval",
                                ))
                        continue

                    tier: str = "auto" if result.certain else "approval"
                    issues.append(Issue(
                        issue_id=ctx.next_issue_id(), ref=ref,
                        rule_id=self.rule_id, kind="rule", value=val,
                        message=f"금액 표기를 정규화합니다: '{val}' → {result.value}원 ({result.reason})",
                        tier=tier,
                        suggestion=result.value,
                    ))

        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        r = normalize_amount(value)
        if r.value is None:
            raise NotFixable
        return r.value
