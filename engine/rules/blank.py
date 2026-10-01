"""R1. BLANK — 공백 여부 탐지 및 공백 정규화 (RULES.md R1)."""
from __future__ import annotations

import re

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import Rule, RuleContext, register

# ── 빈 값 판정 ────────────────────────────────────────────────────────────────

# 앞뒤 공백 제거 후 이 값이면 '빈 값 대용 문자'로 간주
_BLANK_SUBSTITUTES: frozenset[str] = frozenset({
    "-", "--", ".", "없음", "미정", "n/a", "na", "null", "none",
    "NULL", "None", "nan", "NaN", "0000-00-00",
})

# 공백 계열 유니코드: 탭·줄바꿈·전각공백·NBSP 등
_WHITESPACE_RE = re.compile(r"[\t\r\n　 ]+| {2,}")


def is_blank(value: str) -> bool:
    """R1이 정의하는 '빈 값'이면 True."""
    stripped = value.strip()
    if stripped == "":
        return True
    if stripped in _BLANK_SUBSTITUTES:
        return True
    # 전각 공백·NBSP·탭·줄바꿈만 있는 경우
    if not stripped.replace("　", "").replace(" ", "").replace("\t", "").replace("\n", "").strip():
        return True
    return False


def _normalize_ws(value: str) -> str | None:
    """앞뒤 공백 제거 + 내부 연속 공백을 1칸으로.

    변경이 없으면 None 반환.
    """
    cleaned = value.strip()
    cleaned = _WHITESPACE_RE.sub(" ", cleaned)
    return cleaned if cleaned != value else None


# ── BLANK Rule ────────────────────────────────────────────────────────────────

@register
class BlankRule(Rule):
    """R1: 필수 컬럼 빈값(request), 공백 정규화(auto), 빈 행 삭제(auto)."""

    rule_id = "BLANK"
    default_tier = "request"
    scope = "table"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        required_cols: set[str] = set(binding.params.get("required_cols", []))
        # required_cols 형식: "실적.기관명"

        # scope_cols 가 있으면 해당 컬럼의 빈 값만 점검한다 (Plan 의 컬럼 한정 공백 확인).
        # 앞뒤 공백 정규화(Case 3)·빈 행 삭제(Case 4)는 수행하지 않는다.
        scope_cols = binding.params.get("scope_cols")
        if scope_cols is not None:
            return self._check_scoped(ds, set(scope_cols), ctx)

        for (file, sheet), df in ds.tables.items():
            data_cols = [c for c in df.columns if c != "_row"]

            for _, row in df.iterrows():
                row_num = int(row["_row"])

                # ── Case 4: 행 전체가 빈 값 ────────────────────────────────
                if all(is_blank(str(row[c])) for c in data_cols):
                    issues.append(Issue(
                        issue_id=ctx.next_issue_id(),
                        ref=CellRef(file=file, sheet=sheet, row=row_num, column=data_cols[0]),
                        rule_id=self.rule_id, kind="rule",
                        value="(빈 행)",
                        message=f"행 {row_num} 전체가 비어 있습니다 — 삭제 대상",
                        tier="auto",
                    ))
                    continue

                for col in data_cols:
                    val = str(row[col])
                    target_key = f"{sheet}.{col}"

                    # ── Case 1/2: 빈 값 여부 ──────────────────────────────
                    if is_blank(val):
                        if target_key in required_cols:
                            issues.append(Issue(
                                issue_id=ctx.next_issue_id(),
                                ref=CellRef(file=file, sheet=sheet, row=row_num, column=col),
                                rule_id=self.rule_id, kind="rule",
                                value=val,
                                message=f"필수 항목이 비어 있습니다: '{col}' (입력값: {val!r})",
                                tier="request",
                            ))
                        # 선택 컬럼 빈 값은 Issue 없음 (Case 2)
                        continue

                    # ── Case 3: 앞뒤/연속 공백 정규화 ─────────────────────
                    normalized = _normalize_ws(val)
                    if normalized is not None:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=row_num, column=col),
                            rule_id=self.rule_id, kind="rule",
                            value=val,
                            message=f"앞뒤 공백 또는 연속 공백이 있습니다: {val!r}",
                            tier="auto",
                            suggestion=normalized,
                        ))

        return issues

    def _check_scoped(self, ds: Dataset, scope_cols: set[str], ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for (file, sheet), df in ds.tables.items():
            cols = [c for c in df.columns if c != "_row" and f"{sheet}.{c}" in scope_cols]
            for _, row in df.iterrows():
                for col in cols:
                    val = str(row[col])
                    if is_blank(val):
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=col),
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"필수 항목이 비어 있습니다: '{col}' (입력값: {val!r})",
                            tier="request",
                        ))
        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if value is None:
            return None
        return _normalize_ws(value) or value
