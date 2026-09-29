from __future__ import annotations

import json

import jsonschema

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import Rule, RuleContext, register


@register
class JsonSchemaRule(Rule):
    rule_id = "JSON_SCHEMA"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []
        schema: dict = binding.params.get("schema", {})
        if not schema:
            return issues

        for target in binding.targets:
            parts = target.split(".")
            if len(parts) != 2:
                continue
            sheet, column = parts
            for (file, s), df in ds.tables.items():
                if s != sheet or column not in df.columns:
                    continue
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if not val:
                        continue
                    try:
                        data = json.loads(val)
                    except json.JSONDecodeError:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=s, row=int(row["_row"]), column=column),
                            rule_id=self.rule_id, kind="rule",
                            value=val,
                            message=f"JSON 파싱 오류: '{val[:60]}'",
                            tier="approval",
                        ))
                        continue
                    try:
                        jsonschema.validate(data, schema)
                    except jsonschema.ValidationError as e:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(),
                            ref=CellRef(file=file, sheet=s, row=int(row["_row"]), column=column),
                            rule_id=self.rule_id, kind="rule",
                            value=val,
                            message=f"JSON 스키마 위반: {e.message[:120]}",
                            tier="approval",
                        ))
        return issues
