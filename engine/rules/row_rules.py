from __future__ import annotations

import pandas as pd

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import Rule, RuleContext, register

try:
    from rapidfuzz import fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False


# ── DUPLICATE_ROW ─────────────────────────────────────────────────────────────

@register
class DuplicateRowRule(Rule):
    rule_id = "DUPLICATE_ROW"
    default_tier = "auto"
    scope = "row"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for (file, sheet), df in ds.tables.items():
            data_cols = [c for c in df.columns if c != "_row"]
            dup_mask = df.duplicated(subset=data_cols, keep="first")
            for _, row in df[dup_mask].iterrows():
                issues.append(Issue(
                    issue_id=ctx.next_issue_id(),
                    ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=data_cols[0]),
                    rule_id=self.rule_id, kind="rule",
                    value=None,
                    message=f"완전히 동일한 중복 행입니다 (행 {int(row['_row'])})",
                    tier="auto",
                ))
        return issues


# ── NEAR_DUPLICATE ────────────────────────────────────────────────────────────

@register
class NearDuplicateRule(Rule):
    rule_id = "NEAR_DUPLICATE"
    default_tier = "approval"
    scope = "row"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        if not _HAS_RAPIDFUZZ:
            return issues

        threshold: float = binding.params.get("threshold", 0.9)
        key_cols: list[str] = binding.params.get("key_cols", [])

        for (file, sheet), df in ds.tables.items():
            cols = key_cols if key_cols else [c for c in df.columns if c != "_row"]
            if len(df) < 2:
                continue

            reported: set[int] = set()
            rows = df.to_dict("records")
            for i in range(len(rows)):
                for j in range(i + 1, len(rows)):
                    ri, rj = rows[i], rows[j]
                    if ri["_row"] in reported or rj["_row"] in reported:
                        continue
                    scores = [
                        fuzz.ratio(str(ri.get(c, "")), str(rj.get(c, ""))) / 100.0
                        for c in cols if c in ri and c in rj
                    ]
                    if scores and sum(scores) / len(scores) >= threshold:
                        reported.add(rj["_row"])
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=int(rj["_row"]), column=cols[0]),
                            rule_id=self.rule_id, kind="rule",
                            value=None,
                            message=(
                                f"행 {int(rj['_row'])}이 행 {int(ri['_row'])}과 "
                                f"유사합니다 (유사도 {sum(scores)/len(scores):.0%})"
                            ),
                            tier="approval",
                        ))
        return issues
