from __future__ import annotations

import importlib

from engine.models import Dataset, Issue, RuleBinding
from engine.rules.base import REGISTRY, RuleContext

# 규칙 모듈을 임포트하면 각 모듈의 @register 데코레이터가 실행되어 REGISTRY에 등록된다
for _mod in ("format_rules", "value_rules", "row_rules", "table_rules", "json_rules"):
    importlib.import_module(f"engine.rules.{_mod}")


def run(
    ds: Dataset,
    bindings: list[RuleBinding],
    ctx: RuleContext | None = None,
) -> list[Issue]:
    """RuleBinding 목록을 순회하며 각 규칙을 실행하고 Issue 목록을 반환한다.

    - config.disabled_rule_ids 에 포함된 규칙은 스킵한다
    - binding.rule_id로 REGISTRY에서 규칙 클래스를 찾아 인스턴스화
    - rule.check(ds, binding, ctx) → Issue 리스트
    - 개별 규칙이 예외를 던져도 나머지 규칙은 계속 실행된다
    - diagnosis 노드와 recheck 노드 양쪽에서 호출된다
    """
    from config import settings
    disabled = settings.disabled_rule_ids

    if ctx is None:
        ctx = RuleContext()

    issues: list[Issue] = []
    for binding in bindings:
        if binding.rule_id in disabled:
            continue
        rule_cls = REGISTRY.get(binding.rule_id)
        if rule_cls is None:
            continue
        rule = rule_cls()
        try:
            found = rule.check(ds, binding, ctx)
            issues.extend(found)
        except Exception:
            pass  # 개별 규칙 실패가 전체 검진을 막지 않도록

    return issues
