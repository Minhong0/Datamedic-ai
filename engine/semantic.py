from __future__ import annotations

from engine.models import ColumnProfile, RuleBinding, SemanticType, SheetProfile

# MVP 활성 규칙: BLANK / AMOUNT_NORMALIZE / DATE_NORMALIZE / BIZNO_VERIFY (RULES.md)
# 나머지 규칙(DATE_FORMAT, NUMERIC_UNIT, BIZNO_CHECKSUM, PHONE_FORMAT, … )은
# validator.py가 이 모듈들을 로드하지 않으므로 REGISTRY에 등록되지 않는다.

_SEMANTIC_RULES: dict[SemanticType, list[str]] = {
    "date":     ["DATE_NORMALIZE"],
    "biz_no":   ["BIZNO_VERIFY"],
    "phone":    [],
    "org_name": [],
    "text":     [],
    "code":     [],
    "amount":   ["AMOUNT_NORMALIZE"],
    "count":    [],
    "email":    [],
    "unknown":  [],
}


def bindings_from_profiles(profiles: list[SheetProfile]) -> list[RuleBinding]:
    """프로파일에서 RuleBinding 목록을 생성한다.

    - BLANK: 시트별 1개, params에 required_cols 목록을 담아 전달
    - 나머지: 컬럼별 binding (기존 방식 유지)
    """
    seen_col: dict[tuple[str, str, str], dict] = {}

    # BLANK: 시트별로 required 컬럼 목록을 모은다
    blank_required: dict[str, list[str]] = {}  # sheet → ["sheet.col", ...]

    for profile in profiles:
        sheet = profile.sheet

        for col in profile.columns:
            params: dict = dict(col.params)

            # BLANK 바인딩용 required 컬럼 수집
            if col.required:
                blank_required.setdefault(sheet, []).append(f"{sheet}.{col.name}")

            # 시맨틱 타입 → 규칙 바인딩
            rule_ids = _SEMANTIC_RULES.get(col.semantic_type, [])
            for rule_id in rule_ids:
                if rule_id == "DATE_NORMALIZE" and "range" not in params:
                    continue
                key = (rule_id, sheet, col.name)
                if key not in seen_col:
                    seen_col[key] = params

    bindings: list[RuleBinding] = []

    # BLANK 바인딩 — 시트별 1개, 전체 required 목록을 params에 담음
    all_required = [col for cols in blank_required.values() for col in cols]
    bindings.append(RuleBinding(
        rule_id="BLANK",
        targets=[],
        params={"required_cols": all_required},
        source="semantic_default",
    ))

    # 컬럼 단위 바인딩 (AMOUNT_NORMALIZE, DATE_NORMALIZE, BIZNO_VERIFY)
    for (rule_id, sheet, col_name), params in seen_col.items():
        bindings.append(RuleBinding(
            rule_id=rule_id,
            targets=[f"{sheet}.{col_name}"],
            params=params,
            source="semantic_default",
        ))

    return bindings
