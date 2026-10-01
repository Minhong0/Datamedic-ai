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
    """완전 동일 행 탐지. tier는 approval로 잠근다 (자동 삭제 금지).

    value 표지: "(완전중복)" = 같은 파일 안 중복(일괄 삭제 대상),
                "(파일간중복)" = 다른 파일의 행과 중복(개별 확인).
    """
    rule_id = "DUPLICATE_ROW"
    default_tier = "approval"
    scope = "row"

    SAME_FILE = "(완전중복)"
    CROSS_FILE = "(파일간중복)"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        seen: dict[tuple, str] = {}   # (시트, 행 값) → 처음 나온 파일
        for (file, sheet), df in ds.tables.items():
            data_cols = [c for c in df.columns if c != "_row"]
            for _, row in df.iterrows():
                key = (sheet, tuple(str(row[c]) for c in data_cols))
                first_file = seen.get(key)
                if first_file is None:
                    seen[key] = file
                    continue
                cross = first_file != file
                issues.append(Issue(
                    issue_id=ctx.next_issue_id(),
                    ref=CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=data_cols[0]),
                    rule_id=self.rule_id, kind="rule",
                    value=self.CROSS_FILE if cross else self.SAME_FILE,
                    message=(
                        f"행 {int(row['_row'])}이 다른 파일의 행과 완전히 동일합니다 (개별 확인 필요)"
                        if cross else
                        f"행 {int(row['_row'])}이 앞 행과 완전히 동일한 중복 행입니다"
                    ),
                    tier="approval",
                ))
        return issues


# ── NEAR_DUPLICATE ────────────────────────────────────────────────────────────

# 퍼지 비교에서 제외할 '변별력 없는' 값 판정 기준
_MIN_COMPARE_COLS = 2   # 의미 있는 텍스트 컬럼이 이 수 미만이면 유사도 판단 자체를 포기


def _is_numberish(value: str) -> bool:
    """순수 숫자(쉼표/소수점 포함)면 True — 퍼지 유사도 비교에서 제외한다.

    작은 정수는 자주 100% 일치하고, 긴 숫자는 자릿수가 겹쳐 유사도를 부풀린다.
    """
    t = value.strip()
    if not t:
        return False
    try:
        float(t.replace(",", ""))
        return True
    except ValueError:
        return False


def _discriminating_cols(df) -> list[str]:
    """유사 중복 판단에 쓸 컬럼만 고른다.

    제외 대상:
      - _row 메타 컬럼
      - 시트 전체에서 값이 하나뿐인 상수 컬럼 (예: 단일 시군 파일의 '기관명')
        → 모든 행이 100% 일치해 유사도를 왜곡한다
      - 값이 대부분 숫자인 컬럼 (코드·건수·금액 등)
    """
    cols: list[str] = []
    for c in df.columns:
        if c == "_row":
            continue
        series = df[c].astype(str)
        non_blank = [v for v in series if v.strip()]
        if not non_blank:
            continue
        if len(set(non_blank)) <= 1:   # 상수 컬럼
            continue
        numish = sum(1 for v in non_blank if _is_numberish(v))
        if numish >= len(non_blank) * 0.5:   # 숫자 위주 컬럼
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

            # key_cols가 명시되면 그대로, 아니면 변별력 있는 텍스트 컬럼만 자동 선택
            cols = key_cols if key_cols else _discriminating_cols(df)
            # 비교할 실질 컬럼이 부족하면(예: 코드+숫자만 있는 요약 시트) 판단하지 않는다
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
                        # 양쪽 모두 빈값인 컬럼은 제외 — 빈값끼리 100% 유사로 오판 방지
                        and (str(ri.get(c, "")).strip() or str(rj.get(c, "")).strip())
                    ]
                    if len(scores) >= _MIN_COMPARE_COLS and sum(scores) / len(scores) >= threshold:
                        reported.add(rj["_row"])
                        avg = sum(scores) / len(scores)
                        # 공백은 REQUIRED 규칙 담당 — 여기서는 순수 '유사 중복 행'만 보고한다.
                        # (공백을 유사도로 환산해 '정확도'로 표시하지 않는다)
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
