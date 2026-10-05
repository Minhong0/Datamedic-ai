from __future__ import annotations

import numpy as np
import pandas as pd

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import Rule, RuleContext, register


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


@register
class OutlierRule(Rule):
    rule_id = "OUTLIER"
    default_tier = "request"
    scope = "table"
    kind = "outlier"

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
                    iqr_out = not (lo <= val <= hi)
                    org_out = mean_val > 0 and val > mean_val * org_factor
                    if iqr_out or org_out:
                        if org_out and val > hi:
                            msg = (f"[참고] 이상값 후보 (IQR 기준): {int(val):,} "
                                   f"— 평균 대비 {val/mean_val:.1f}배 (평균 {int(mean_val):,})")
                        elif val < lo:
                            msg = (f"[참고] 이상값 후보 (IQR 기준): {int(val):,} "
                                   f"— 분포 하단 (IQR 범위 {lo:,.0f} ~ {hi:,.0f})")
                        else:
                            msg = (f"[참고] 이상값 후보 (IQR 기준): {int(val):,} "
                                   f"— 분포 상단 (IQR 범위 {lo:,.0f} ~ {hi:,.0f})")
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=s, row=row_num, column=column),
                            rule_id=self.rule_id, kind="outlier",
                            value=raw,
                            message=msg,
                            tier="request",
                        ))
        return issues
