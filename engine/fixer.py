"""Dataset에 수정을 적용하는 모듈. treatment_node만 이 모듈을 호출한다.

규칙:
  - 입력 Dataset은 복사본에만 수정한다 (원본 보존).
  - 모든 변경은 Change 객체로 남긴다.
  - LLM 출력으로 직접 셀 값을 바꾸지 않는다. 값 변경은 이 파일만 담당한다.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timezone

from engine.models import Change, Dataset, Decision, Issue, Tier
from engine.rules.base import REGISTRY, NotFixable

# 규칙 모듈 등록 (rule.fix() 메서드를 사용하기 위해)
for _mod in ("format_rules", "value_rules", "row_rules", "table_rules", "json_rules"):
    importlib.import_module(f"engine.rules.{_mod}")


def _next_change_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"C-{counter[0]:04d}"


def apply(
    ds: Dataset,
    issues: list[Issue],
    decisions: list[Decision],
) -> tuple[Dataset, list[Change]]:
    """decisions에 따라 Dataset의 복사본에 수정을 적용하고 변경 이력을 반환한다.

    action별 처리:
      apply  : rule.fix()로 값 계산 → 실패 시 issue.suggestion 사용
               DUPLICATE_ROW는 행 삭제 (after=None)
      edit   : decision.value를 직접 셀에 쓴다 (사람이 직접 입력한 값)
      reject/request : 아무것도 하지 않는다
    """
    cleaned = ds.copy()
    changes: list[Change] = []
    counter = [0]

    # issue_id → Decision 맵으로 O(1) 조회
    decision_map = {d.issue_id: d for d in decisions}

    for issue in issues:
        decision = decision_map.get(issue.issue_id)
        if decision is None:
            continue

        if decision.action in ("reject", "request"):
            continue

        ref = issue.ref
        if ref is None:
            continue

        ts = datetime.now(timezone.utc)

        if decision.action == "apply":
            rule_cls = REGISTRY.get(issue.rule_id)
            if rule_cls is None:
                continue

            if issue.rule_id == "DUPLICATE_ROW":
                # 중복 행 삭제 — after=None으로 기록
                before = cleaned.cell(ref)
                cleaned.drop_row(ref.file, ref.sheet, ref.row)
                changes.append(Change(
                    change_id=_next_change_id(counter),
                    issue_id=issue.issue_id,
                    ref=ref,
                    before=before,
                    after=None,
                    tier=issue.tier,
                    decided_by=decision.decided_by,
                    timestamp=ts,
                ))
            else:
                before = cleaned.cell(ref)
                rule = rule_cls()
                try:
                    after = rule.fix(before, issue)
                except NotFixable:
                    # 규칙이 자동 수정 불가를 선언하면 suggestion을 대신 사용
                    if issue.suggestion is not None:
                        after = issue.suggestion
                    else:
                        continue
                cleaned.set_cell(ref, after)
                changes.append(Change(
                    change_id=_next_change_id(counter),
                    issue_id=issue.issue_id,
                    ref=ref,
                    before=before,
                    after=after,
                    tier=issue.tier,
                    decided_by=decision.decided_by,
                    timestamp=ts,
                ))

        elif decision.action == "edit":
            # 사람이 직접 입력한 값으로 덮어씀
            if decision.value is None:
                continue
            before = cleaned.cell(ref)
            cleaned.set_cell(ref, decision.value)
            changes.append(Change(
                change_id=_next_change_id(counter),
                issue_id=issue.issue_id,
                ref=ref,
                before=before,
                after=decision.value,
                tier=issue.tier,
                decided_by=decision.decided_by,
                timestamp=ts,
            ))

    return cleaned, changes
