from __future__ import annotations

import importlib

from engine.models import Dataset, Issue, RuleBinding
from engine.rules.base import REGISTRY, RuleContext

for _mod in ("blank", "amount", "date", "bizno", "row_rules", "table_rules"):
    importlib.import_module(f"engine.rules.{_mod}")


def run(
    ds: Dataset,
    bindings: list[RuleBinding],
    ctx: RuleContext | None = None,
    allowed_rule_ids: set[str] | None = None,
) -> list[Issue]:
    """RuleBinding 목록을 순회하며 각 규칙을 실행하고 Issue 목록을 반환한다."""
    from config import settings
    disabled = settings.disabled_rule_ids

    if ctx is None:
        ctx = RuleContext()

    issues: list[Issue] = []
    for binding in bindings:
        if binding.rule_id in disabled:
            continue
        if allowed_rule_ids is not None and binding.rule_id not in allowed_rule_ids:
            continue
        rule_cls = REGISTRY.get(binding.rule_id)
        if rule_cls is None:
            continue
        rule = rule_cls()
        try:
            found = rule.check(ds, binding, ctx)
            issues.extend(found)
        except Exception:
            pass

    return issues
