"""Dataset에 수정을 적용하는 모듈. treatment_node만 이 모듈을 호출한다."""
from __future__ import annotations

import importlib
from datetime import datetime, timezone

from engine.models import Change, Dataset, Decision, Issue, Tier
from engine.rules.base import REGISTRY, NotFixable, Rule

for _mod in ("format_rules", "value_rules", "row_rules", "table_rules", "json_rules"):
    importlib.import_module(f"engine.rules.{_mod}")


def validate_edit(issue: Issue, value: str) -> tuple[str | None, str | None]:
    """담당자가 직접 입력한 값을 해당 규칙으로 검증한다. 반환: (저장할 값, 오류 문구)."""
    import engine.validator

    text = (value or "").strip()
    if not text:
        return None, "값을 입력해 주세요."
    rule_cls = REGISTRY.get(issue.rule_id)
    if rule_cls is None or rule_cls.fix is Rule.fix:
        return text, None
    try:
        fixed = rule_cls().fix(text, issue)
    except NotFixable:
        return None, f"입력한 값 '{text}'을(를) 이 항목의 형식({issue.rule_id})으로 해석할 수 없습니다."
    return (fixed, None) if fixed else (None, "값을 해석할 수 없습니다.")


def _next_change_id(counter: list[int]) -> str:
    counter[0] += 1
    return f"C-{counter[0]:04d}"


def apply(
    ds: Dataset,
    issues: list[Issue],
    decisions: list[Decision],
) -> tuple[Dataset, list[Change]]:
    """decisions에 따라 Dataset의 복사본에 수정을 적용하고 변경 이력을 반환한다."""
    cleaned = ds.copy()
    changes: list[Change] = []
    counter = [0]

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
