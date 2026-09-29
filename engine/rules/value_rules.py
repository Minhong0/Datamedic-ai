from __future__ import annotations

import re
from datetime import date

import pandas as pd

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NotFixable, Rule, RuleContext, register
from engine.rules.format_rules import _iter_column


# ── REQUIRED ──────────────────────────────────────────────────────────────────

@register
class RequiredRule(Rule):
    rule_id = "REQUIRED"
    default_tier = "request"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if val == "" or val.lower() in ("nan", "none"):
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                            rule_id=self.rule_id, kind="rule",
                            value=None,
                            message=f"필수 항목이 비어 있습니다: '{column}'",
                            tier="request",
                        ))
        return issues


# ── DATE_RANGE ────────────────────────────────────────────────────────────────

def _parse_iso(v: str) -> date | None:
    try:
        return date.fromisoformat(v.strip())
    except ValueError:
        return None


@register
class DateRangeRule(Rule):
    rule_id = "DATE_RANGE"
    default_tier = "request"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        range_param: list[str] = binding.params.get("range", [])
        if len(range_param) != 2:
            return issues
        lo = _parse_iso(range_param[0])
        hi = _parse_iso(range_param[1])
        if lo is None or hi is None:
            return issues

        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if not val:
                        continue
                    d = _parse_iso(val)
                    if d is None:
                        continue
                    if not (lo <= d <= hi):
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                            rule_id=self.rule_id, kind="rule",
                            value=val,
                            message=f"날짜 {val}이 허용 범위({lo}~{hi}) 밖입니다",
                            tier="request",
                        ))
        return issues


# ── BIZNO_CHECKSUM ────────────────────────────────────────────────────────────

_BIZNO_WEIGHTS = [1, 3, 7, 1, 3, 7, 1, 3, 5]


def _bizno_valid(digits: str) -> bool:
    if len(digits) != 10 or not digits.isdigit():
        return False
    total = sum(int(digits[i]) * _BIZNO_WEIGHTS[i] for i in range(9))
    check = (10 - (total % 10)) % 10
    return check == int(digits[9])


def _bizno_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _bizno_format(digits: str) -> str:
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


@register
class BiznoChecksumRule(Rule):
    rule_id = "BIZNO_CHECKSUM"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if not val:
                        continue
                    digits = _bizno_digits(val)
                    ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)
                    if not _bizno_valid(digits):
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"사업자번호 체크디지트가 맞지 않습니다: '{val}'",
                            tier="approval",
                        ))
                    else:
                        # 유효하지만 형식이 다른 경우(하이픈 통일) → auto
                        formatted = _bizno_format(digits)
                        if val != formatted:
                            issues.append(Issue(
                                issue_id=ctx.next_issue_id(), ref=ref,
                                rule_id=self.rule_id, kind="rule", value=val,
                                message=f"사업자번호 형식을 통일합니다: '{val}' → '{formatted}'",
                                tier="auto",
                                suggestion=formatted,
                            ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        digits = _bizno_digits(value)
        if not _bizno_valid(digits):
            raise NotFixable
        return _bizno_format(digits)


# ── CODE_VALID ────────────────────────────────────────────────────────────────

@register
class CodeValidRule(Rule):
    rule_id = "CODE_VALID"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        valid_codes: set[str] = set(binding.params.get("valid_codes", []))
        if not valid_codes:
            return issues

        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if not val:
                        continue
                    if val not in valid_codes:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"코드표에 없는 코드입니다: '{val}'",
                            tier="approval",
                        ))
        return issues


# ── NUMERIC_UNIT ──────────────────────────────────────────────────────────────

_STRIP_PATTERN = re.compile(r"[,원천원]")
_UNIT_PATTERN = re.compile(r"(천원|만원|억원|원)")


def _numeric_clean(value: str) -> tuple[str | None, bool]:
    """(정제된 숫자 문자열, 단위 환산 필요 여부). 파싱 불가면 None."""
    v = value.strip()
    needs_conversion = bool(_UNIT_PATTERN.search(v))
    cleaned = re.sub(r"[,원]", "", v)
    cleaned = cleaned.replace("천", "").replace("만", "").replace("억", "")
    cleaned = cleaned.strip()
    try:
        float(cleaned)
        return cleaned, needs_conversion
    except ValueError:
        return None, False


@register
class NumericUnitRule(Rule):
    rule_id = "NUMERIC_UNIT"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if not val:
                        continue
                    # 숫자만 있으면 정상
                    try:
                        float(val.replace(",", ""))
                        # 쉼표 포함 → auto 제거
                        if "," in val:
                            cleaned = val.replace(",", "")
                            issues.append(Issue(
                                issue_id=ctx.next_issue_id(),
                                ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                                rule_id=self.rule_id, kind="rule", value=val,
                                message=f"숫자에 쉼표 구분자가 있습니다: '{val}'",
                                tier="auto",
                                suggestion=cleaned,
                            ))
                        continue
                    except ValueError:
                        pass

                    cleaned, needs_conv = _numeric_clean(val)
                    if cleaned is None:
                        continue
                    tier: str = "approval" if needs_conv else "auto"
                    issues.append(Issue(
                        issue_id=ctx.next_issue_id(),
                        ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                        rule_id=self.rule_id, kind="rule", value=val,
                        message=f"숫자 형식에 단위/기호가 섞여 있습니다: '{val}'",
                        tier=tier,
                        suggestion=cleaned if not needs_conv else None,
                    ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        cleaned, needs_conv = _numeric_clean(value)
        if cleaned is None or needs_conv:
            raise NotFixable
        return cleaned
