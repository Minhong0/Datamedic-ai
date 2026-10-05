from __future__ import annotations

from pathlib import Path

import pandas as pd

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import Rule, RuleContext, register

try:
    from rapidfuzz import fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False


@register
class DuplicateRowRule(Rule):
    """완전 동일 행 탐지. tier는 approval로 잠근다 (자동 삭제 금지)."""
    rule_id = "DUPLICATE_ROW"
    default_tier = "approval"
    scope = "row"

    SAME_FILE = "(완전중복)"
    CROSS_FILE = "(파일간중복)"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        seen: dict[tuple, tuple[str, int]] = {}
        for (file, sheet), df in ds.tables.items():
            data_cols = [c for c in df.columns if c != "_row"]
            for _, row in df.iterrows():
                key = (sheet, tuple(str(row[c]) for c in data_cols))
                first = seen.get(key)
                if first is None:
                    seen[key] = (file, int(row["_row"]))
                    continue
                first_file, first_row = first
                cross = first_file != file
                issues.append(Issue(
                    issue_id=ctx.next_issue_id(),
                    ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=data_cols[0]),
                    rule_id=self.rule_id, kind="rule",
                    value=self.CROSS_FILE if cross else self.SAME_FILE,
                    message=(
                        f"{int(row['_row'])}행은 다른 파일 {Path(first_file).name}의 {first_row}행과 모든 칸이 똑같습니다. "
                        "다른 파일이라 자동으로 지우지 않고 하나씩 확인해야 합니다."
                        if cross else
                        f"{int(row['_row'])}행은 {first_row}행과 모든 칸이 똑같은 중복 입력입니다. "
                        f"승인하면 {int(row['_row'])}행을 정제본에서 삭제하고 {first_row}행만 남깁니다 (원본 파일은 그대로)."
                    ),
                    tier="approval",
                ))
        return issues


_MIN_COMPARE_COLS = 2


def _is_numberish(value: str) -> bool:
    """순수 숫자(쉼표/소수점 포함)면 True — 퍼지 유사도 비교에서 제외한다."""
    t = value.strip()
    if not t:
        return False
    try:
        float(t.replace(",", ""))
        return True
    except ValueError:
        return False


def _discriminating_cols(df) -> list[str]:
    """유사 중복 판단에 쓸 컬럼만 고른다."""
    cols: list[str] = []
    for c in df.columns:
        if c == "_row":
            continue
        series = df[c].astype(str)
        non_blank = [v for v in series if v.strip()]
        if not non_blank:
            continue
        if len(set(non_blank)) <= 1:
            continue
        numish = sum(1 for v in non_blank if _is_numberish(v))
        if numish >= len(non_blank) * 0.5:
            continue
        cols.append(c)
    return cols


@register
class NearDuplicateRule(Rule):
    rule_id = "NEAR_DUPLICATE"
    default_tier = "approval"
    scope = "row"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        if not _HAS_RAPIDFUZZ:
            return issues

        threshold: float = binding.params.get("threshold", 0.85)
        key_cols: list[str] = binding.params.get("key_cols", [])

        for (file, sheet), df in ds.tables.items():
            if len(df) < 2:
                continue

            cols = key_cols if key_cols else _discriminating_cols(df)
            if len(cols) < _MIN_COMPARE_COLS:
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
                        and (str(ri.get(c, "")).strip() or str(rj.get(c, "")).strip())
                    ]
                    if len(scores) >= _MIN_COMPARE_COLS and sum(scores) / len(scores) >= threshold:
                        reported.add(rj["_row"])
                        avg = sum(scores) / len(scores)
                        msg = (
                            f"행 {int(rj['_row'])}와 행 {int(ri['_row'])}의 내용이 "
                            f"{avg:.0%} 일치합니다 (중복 확인 필요)"
                        )
                        issue_value = f"유사도 {avg:.0%}"

                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=sheet, row=int(rj["_row"]), column=cols[0]),
                            rule_id=self.rule_id, kind="rule",
                            value=issue_value,
                            message=msg,
                            tier="approval",
                        ))
        return issues
