from __future__ import annotations

import numpy as np
import pandas as pd

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import Rule, RuleContext, register


# ── TYPE_CONSISTENCY ──────────────────────────────────────────────────────────

def _infer_type(value: str) -> str:
    v = value.strip()
    if not v:
        return "empty"
    try:
        float(v.replace(",", ""))
        return "numeric"
    except ValueError:
        pass
    try:
        from datetime import date
        date.fromisoformat(v)
        return "date"
    except ValueError:
        pass
    return "text"


@register
class TypeConsistencyRule(Rule):
    rule_id = "TYPE_CONSISTENCY"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        for (file, sheet), df in ds.tables.items():
            for col in (c for c in df.columns if c != "_row"):
                types = df[col].apply(_infer_type)
                non_empty = types[types != "empty"]
                if len(non_empty) < 2:
                    continue
                majority = non_empty.value_counts().idxmax()
                for _, row in df.iterrows():
                    t = _infer_type(str(row[col]))
                    if t == "empty" or t == majority:
                        continue
                    issues.append(Issue(
                        issue_id=ctx.next_issue_id(),
                        ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=col),
                        rule_id=self.rule_id, kind="rule",
                        value=str(row[col]),
                        message=(
                            f"컬럼 '{col}'의 다수 타입은 {majority}인데 "
                            f"이 값은 {t}입니다: '{row[col]}'"
                        ),
                        tier="approval",
                    ))
        return issues


# ── SUM_MATCH ─────────────────────────────────────────────────────────────────

@register
class SumMatchRule(Rule):
    rule_id = "SUM_MATCH"
    default_tier = "request"
    scope = "cross_sheet"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        # params: source_sheet, source_col, group_by, agg, target_sheet, target_col
        p = binding.params
        src_sheet = p.get("source_sheet", "")
        src_col = p.get("source_col", "")
        group_col = p.get("group_by", "")
        agg = p.get("agg", "sum")
        tgt_sheet = p.get("target_sheet", "")
        tgt_col = p.get("target_col", "")

        if not all([src_sheet, src_col, group_col, tgt_sheet, tgt_col]):
            return issues

        for (file, sheet), df in ds.tables.items():
            if sheet != src_sheet:
                continue
            if src_col not in df.columns or group_col not in df.columns:
                continue
            tgt_df = ds.tables.get((file, tgt_sheet))
            if tgt_df is None or tgt_col not in tgt_df.columns or group_col not in tgt_df.columns:
                continue

            # 숫자 변환
            src_num = pd.to_numeric(df[src_col].str.replace(",", ""), errors="coerce")
            if agg == "sum":
                computed = df.assign(_val=src_num).groupby(group_col)["_val"].sum()
            elif agg == "count":
                computed = df.groupby(group_col)[src_col].count().astype(float)
            else:
                continue

            for _, trow in tgt_df.iterrows():
                key = str(trow[group_col]).strip()
                if key not in computed.index:
                    continue
                expected = computed[key]
                try:
                    actual = float(str(trow[tgt_col]).replace(",", "").strip())
                except ValueError:
                    continue
                if abs(expected - actual) > 0.01:
                    issues.append(Issue(
                        issue_id=ctx.next_issue_id(),
                        ref=CellRef(file=file, sheet=tgt_sheet, row=int(trow["_row"]), column=tgt_col),
                        rule_id=self.rule_id, kind="rule",
                        value=str(trow[tgt_col]),
                        message=(
                            f"[{key}] {tgt_col} 합계 불일치: "
                            f"요약={actual:.0f}, 계산={expected:.0f}"
                        ),
                        tier="request",
                    ))
        return issues


# ── OUTLIER ───────────────────────────────────────────────────────────────────

@register
class OutlierRule(Rule):
    rule_id = "OUTLIER"
    default_tier = "request"
    scope = "table"
    kind = "outlier"  # type: ignore[assignment]

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        iqr_factor: float = binding.params.get("iqr_factor", 1.5)
        org_factor: float = binding.params.get("org_factor", 5.0)

        for target in binding.targets:
            parts = target.split(".")
            if len(parts) != 2:
                continue
            sheet, column = parts
            for (file, s), df in ds.tables.items():
                if s != sheet or column not in df.columns:
                    continue
                nums = pd.to_numeric(df[column].str.replace(",", ""), errors="coerce")
                valid = nums.dropna()
                if len(valid) < 4:
                    continue
                q1, q3 = float(valid.quantile(0.25)), float(valid.quantile(0.75))
                iqr = q3 - q1
                lo, hi = q1 - iqr_factor * iqr, q3 + iqr_factor * iqr
                mean_val = float(valid.mean())

                for idx, val in nums.items():
                    if pd.isna(val):
                        continue
                    row_num = int(df.loc[idx, "_row"])
                    raw = str(df.loc[idx, column])
                    if not (lo <= val <= hi) or (mean_val > 0 and val > mean_val * org_factor):
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=s, row=row_num, column=column),
                            rule_id=self.rule_id, kind="outlier",
                            value=raw,
                            message=f"이상값 감지: {raw} (IQR 범위 {lo:.0f}~{hi:.0f})",
                            tier="request",
                        ))
        return issues
