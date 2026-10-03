from __future__ import annotations

import importlib

from engine.models import Dataset, Issue, RuleBinding
from engine.rules.base import REGISTRY, RuleContext

# MVP 규칙 모듈만 로드
# 구 규칙 모듈(format_rules, value_rules 등)은 로드하지 않으므로 REGISTRY에 등록되지 않는다
# 목표 선택형 규칙(row_rules, table_rules)은 REGISTRY에 올리기만 하며,
# 계획(Plan)에 의해 binding이 만들어진 경우에만 실행된다.
for _mod in ("blank", "amount", "date", "bizno", "row_rules", "table_rules"):
    importlib.import_module(f"engine.rules.{_mod}")


def run(
    ds: Dataset,
    bindings: list[RuleBinding],
    ctx: RuleContext | None = None,
    allowed_rule_ids: set[str] | None = None,
) -> list[Issue]:
    """RuleBinding 목록을 순회하며 각 규칙을 실행하고 Issue 목록을 반환한다.

    - allowed_rule_ids 가 주어지면 그 밖의 규칙은 실행하지 않는다 (Plan 범위 가드)
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
            pass  # 개별 규칙 실패가 전체 검진을 막지 않도록

    return issues
