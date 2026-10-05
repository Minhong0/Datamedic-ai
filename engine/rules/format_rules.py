from __future__ import annotations

import re
from datetime import date

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NotFixable, Rule, RuleContext, register


def _iter_column(ds: Dataset, target: str):
    """target="시트명.컬럼명" → (file, sheet, df, col) 순회."""
    parts = target.split(".")
    if len(parts) != 2:
        return
    sheet, column = parts
    for (file, s), df in ds.tables.items():
        if s == sheet and column in df.columns:
            yield file, sheet, df, column


_DATE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})$"), "iso"),
    (re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T]\d{2}:\d{2}(:\d{2})?.*$"), "iso_with_time"),
    (re.compile(r"^(\d{4})(\d{2})(\d{2})$"), "compact"),
    (re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})$"), "dot"),
    (re.compile(r"^(\d{4})/(\d{2})/(\d{2})$"), "slash"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "mdy"),
    (re.compile(r"^(\d{2})[-./](\d{2})[-./](\d{2})$"), "ambiguous"),
]

_SKIP_VALUES = {"nan", "none", "nat", "null", "", "-", "n/a"}


def _try_parse_date(value: str) -> tuple[date | None, bool]:
    """(파싱된 date, 모호한지) 반환. 인식 불가면 (None, False)."""
    v = value.strip()
    for pattern, fmt in _DATE_PATTERNS:
        m = pattern.match(v)
        if not m:
            continue
        if fmt == "ambiguous":
            return None, True
        if fmt == "mdy":
            mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            try:
                return date(y, mo, d), False
            except ValueError:
                continue
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return date(y, mo, d), False
        except ValueError:
            continue
    return None, False


@register
class DateFormatRule(Rule):
    rule_id = "DATE_FORMAT"
    default_tier = "auto"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column])
                    if val.strip().lower() in _SKIP_VALUES:
                        continue
                    parsed, ambiguous = _try_parse_date(val)
                    ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)
                    if ambiguous:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"날짜 형식이 모호합니다: '{val}'",
                            tier="approval",
                        ))
                    elif parsed is None:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"날짜 형식을 인식할 수 없습니다: '{val}'",
                            tier="approval",
                        ))
                    elif val.strip() != parsed.isoformat():
                        if " " in val.strip() or "T" in val.strip():
                            msg = f"날짜에 시간 정보가 포함되어 있습니다 (날짜만 필요): '{val}'"
                        else:
                            msg = f"날짜가 ISO 형식(YYYY-MM-DD)이 아닙니다: '{val}'"
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=msg,
                            tier="auto",
                            suggestion=parsed.isoformat(),
                        ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if value is None:
            raise NotFixable
        parsed, ambiguous = _try_parse_date(value)
        if ambiguous or parsed is None:
            raise NotFixable
        return parsed.isoformat()


_PHONE_VALID = re.compile(r"^0\d{1,2}-\d{3,4}-\d{4}$")


def _normalize_phone(value: str) -> str | None:
    digits = re.sub(r"\D", "", value)
    if not digits.startswith("0"):
        return None
    if len(digits) == 10:
        return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return None


@register
class PhoneFormatRule(Rule):
    rule_id = "PHONE_FORMAT"
    default_tier = "auto"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column])
                    if not val or val.strip() == "":
                        continue
                    if _PHONE_VALID.match(val.strip()):
                        continue
                    ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)
                    normalized = _normalize_phone(val)
                    if normalized:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"전화번호 형식이 맞지 않습니다: '{val}'",
                            tier="auto",
                            suggestion=normalized,
                        ))
                    else:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"유효하지 않은 전화번호입니다: '{val}'",
                            tier="approval",
                        ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        result = _normalize_phone(value)
        if result is None:
            raise NotFixable
        return result


_FULLWIDTH_TABLE = str.maketrans(
    "".join(chr(0xFF01 + i) for i in range(94)),
    "".join(chr(0x21 + i) for i in range(94)),
)


def _normalize_text(value: str) -> str:
    v = value.translate(_FULLWIDTH_TABLE)
    v = v.strip()
    v = re.sub(r" {2,}", " ", v)
    v = v.replace("㈜", "(주)")
    v = v.replace("（주）", "(주)").replace("（주)", "(주)").replace("(주）", "(주)")
    return v


@register
class TextNormalizeRule(Rule):
    rule_id = "TEXT_NORMALIZE"
    default_tier = "auto"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                for _, row in df.iterrows():
                    val = str(row[column])
                    if not val or val.strip() == "":
                        continue
                    normalized = _normalize_text(val)
                    if normalized != val:
                        ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"텍스트 정규화가 필요합니다: '{val}'",
                            tier="auto",
                            suggestion=normalized,
                        ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if value is None:
            raise NotFixable
        return _normalize_text(value)
