from __future__ import annotations

from engine.models import ColumnProfile, RuleBinding, SemanticType, SheetProfile


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


def _support_date_col(profile: SheetProfile) -> str | None:
    """시트의 지원일자 컬럼명. date 타입 컬럼 중 '지원일자'를 우선하고, 없으면 첫 번째 date 컬럼."""
    dates = [c.name for c in profile.columns if c.semantic_type == "date"]
    if not dates:
        return None
    return "지원일자" if "지원일자" in dates else dates[0]


def bindings_from_profiles(profiles: list[SheetProfile]) -> list[RuleBinding]:
    """프로파일에서 RuleBinding 목록을 생성한다."""
    seen_col: dict[tuple[str, str, str], dict] = {}

    blank_required: dict[str, list[str]] = {}

    for profile in profiles:
        sheet = profile.sheet
        date_col = _support_date_col(profile)

        for col in profile.columns:
            params: dict = dict(col.params)

            if col.required:
                blank_required.setdefault(sheet, []).append(f"{sheet}.{col.name}")

            rule_ids = _SEMANTIC_RULES.get(col.semantic_type, [])
            for rule_id in rule_ids:
                if rule_id == "DATE_NORMALIZE" and "range" not in params:
                    continue
                key = (rule_id, sheet, col.name)
                if key not in seen_col:
                    if rule_id == "BIZNO_VERIFY" and date_col:
                        seen_col[key] = {**params, "date_col": date_col}
                    else:
                        seen_col[key] = params

    bindings: list[RuleBinding] = []

    all_required = [col for cols in blank_required.values() for col in cols]
    bindings.append(RuleBinding(
        rule_id="BLANK",
        targets=[],
        params={"required_cols": all_required},
        source="semantic_default",
    ))

    for (rule_id, sheet, col_name), params in seen_col.items():
        bindings.append(RuleBinding(
            rule_id=rule_id,
            targets=[f"{sheet}.{col_name}"],
            params=params,
            source="semantic_default",
        ))

    return bindings
