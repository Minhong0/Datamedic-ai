from __future__ import annotations

import re
from datetime import date

import pandas as pd

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NotFixable, Rule, RuleContext, register
from engine.rules.bizno import bizno_checksum_ok
from engine.rules.format_rules import _iter_column


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
                            value="(공백)",
                            message=f"필수 항목이 비어 있습니다: '{column}'",
                            tier="request",
                        ))
        return issues


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


_bizno_valid = bizno_checksum_ok


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
                        api_note = ""
                        try:
                            from config import settings as _s
                            from engine.tools.bizno_api import lookup
                            status = lookup(val, _s.odcloud_api_key)
                            if status:
                                api_note = f" [국세청: {status['status']}]"
                        except Exception:
                            pass
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"사업자번호 체크디지트가 맞지 않습니다: '{val}'{api_note}",
                            tier="approval",
                        ))
                    else:
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


_VALID_COMMA_NUM = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$")
_SCALE_UNITS: list[tuple[str, int]] = [
    ("억원", 100_000_000),
    ("만원", 10_000),
    ("천원", 1_000),
]


def _numeric_clean(value: str) -> tuple[str | None, str | None]:
    """(단위 제거 숫자 문자열, 제안값). 파싱 불가면 (None, None)."""
    v = value.strip()

    scale = 1
    for unit, mult in _SCALE_UNITS:
        if unit in v:
            scale = mult
            break

    cleaned = v.replace("억원", "").replace("만원", "").replace("천원", "").replace("원", "")
    cleaned = cleaned.replace(",", "").replace("억", "").replace("만", "").replace("천", "")
    cleaned = cleaned.strip()

    try:
        num = float(cleaned)
    except ValueError:
        return None, None

    converted = int(num * scale)
    return cleaned, str(converted)


def _majority_format(df: "pd.DataFrame", column: str) -> str:
    """컬럼 내 순수 숫자 값의 다수 포맷을 반환: 'comma' 또는 'plain'."""
    comma, plain = 0, 0
    for v in df[column]:
        s = str(v).strip()
        if not s:
            continue
        try:
            float(s.replace(",", ""))
        except ValueError:
            continue
        if "," in s:
            comma += 1
        else:
            plain += 1
    return "comma" if comma > plain else "plain"


def _add_commas(value: str) -> str:
    """순수 숫자 문자열에 천 단위 쉼표를 추가한다."""
    try:
        n = float(value)
        return f"{n:,.0f}" if n == int(n) else f"{n:,}"
    except ValueError:
        return value


@register
class NumericUnitRule(Rule):
    rule_id = "NUMERIC_UNIT"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        import pandas as _pd
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                plain_target = binding.params.get("unit") == "원"
                fmt_std = "plain" if plain_target else _majority_format(df, column)
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if not val:
                        continue
                    try:
                        float(val.replace(",", ""))
                        if "," in val:
                            if not _VALID_COMMA_NUM.match(val.strip()):
                                issues.append(Issue(
                                    issue_id=ctx.next_issue_id(),
                                    ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                                    rule_id=self.rule_id, kind="rule", value=val,
                                    message=f"숫자의 쉼표 위치가 올바르지 않습니다: '{val}'",
                                    tier="auto",
                                    suggestion=(val.replace(",", "") if plain_target
                                                else _add_commas(val.replace(",", ""))),
                                ))
                            elif fmt_std == "plain":
                                issues.append(Issue(
                                    issue_id=ctx.next_issue_id(),
                                    ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                                    rule_id=self.rule_id, kind="rule", value=val,
                                    message=f"숫자에 쉼표 구분자가 있습니다: '{val}'",
                                    tier="auto",
                                    suggestion=val.replace(",", ""),
                                ))
                        else:
                            if fmt_std == "comma":
                                issues.append(Issue(
                                    issue_id=ctx.next_issue_id(),
                                    ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                                    rule_id=self.rule_id, kind="rule", value=val,
                                    message=f"금액 표기가 일관되지 않습니다 (이 컬럼 표준: 쉼표 포맷): '{val}'",
                                    tier="auto",
                                    suggestion=_add_commas(val),
                                ))
                        continue
                    except ValueError:
                        pass

                    cleaned, suggestion = _numeric_clean(val)
                    if cleaned is None:
                        continue
                    issues.append(Issue(
                        issue_id=ctx.next_issue_id(),
                        ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column),
                        rule_id=self.rule_id, kind="rule", value=val,
                        message=f"숫자 형식에 단위/기호가 섞여 있습니다: '{val}'",
                        tier="auto",
                        suggestion=suggestion,
                    ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        _, suggestion = _numeric_clean(value)
        if suggestion is None:
            raise NotFixable
        return suggestion
