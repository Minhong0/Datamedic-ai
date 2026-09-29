from __future__ import annotations

from engine.models import ColumnProfile, RuleBinding, SemanticType, SheetProfile


_SEMANTIC_RULES: dict[SemanticType, list[str]] = {
    "date":     ["DATE_FORMAT", "DATE_RANGE"],
    "biz_no":   ["BIZNO_CHECKSUM"],
    "phone":    ["PHONE_FORMAT"],
    "org_name": ["TEXT_NORMALIZE"],
    "text":     ["TEXT_NORMALIZE"],
    "code":     ["CODE_VALID"],
    "amount":   ["NUMERIC_UNIT", "OUTLIER"],
    "count":    ["NUMERIC_UNIT", "OUTLIER"],
    "email":    ["TEXT_NORMALIZE"],
    "unknown":  ["TYPE_CONSISTENCY"],
}

_TABLE_RULES = ["DUPLICATE_ROW", "NEAR_DUPLICATE"]


def bindings_from_profiles(profiles: list[SheetProfile]) -> list[RuleBinding]:
    """같은 sheet.column 조합은 하나의 binding으로 통합한다 (파일 중복 방지)."""
    # (rule_id, sheet, column) → params (첫 번째 것 사용)
    seen_col: dict[tuple[str, str, str], dict] = {}
    seen_table: set[tuple[str, str]] = set()  # (rule_id, sheet)

    for profile in profiles:
        sheet = profile.sheet

        # 테이블 단위 규칙
        for rule_id in _TABLE_RULES:
            key = (rule_id, sheet)
            if key not in seen_table:
                seen_table.add(key)

        for col in profile.columns:
            target_key_base = f"{sheet}.{col.name}"
            params: dict = dict(col.params)

            if col.required:
                key = ("REQUIRED", sheet, col.name)
                if key not in seen_col:
                    seen_col[key] = params

            rule_ids = _SEMANTIC_RULES.get(col.semantic_type, ["TYPE_CONSISTENCY"])
            for rule_id in rule_ids:
                if rule_id == "DATE_RANGE" and "range" not in params:
                    continue
                if rule_id == "CODE_VALID" and "valid_codes" not in params:
                    continue
                key = (rule_id, sheet, col.name)
                if key not in seen_col:
                    seen_col[key] = params

    bindings: list[RuleBinding] = []

    # 테이블 단위 바인딩 (sheet별 1개)
    for (rule_id, sheet) in seen_table:
        bindings.append(RuleBinding(
            rule_id=rule_id,
            targets=[],
            params={},
            source="semantic_default",
        ))

    # 컬럼 단위 바인딩
    for (rule_id, sheet, col_name), params in seen_col.items():
        bindings.append(RuleBinding(
            rule_id=rule_id,
            targets=[f"{sheet}.{col_name}"],
            params=params,
            source="semantic_default",
        ))

    return bindings
