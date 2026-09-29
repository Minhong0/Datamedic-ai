"""json_rules 테스트: JSON_SCHEMA."""
import pandas as pd
import pytest

from engine.models import Dataset, RuleBinding
from engine.rules.base import RuleContext
from engine.rules.json_rules import JsonSchemaRule


def make_binding(schema):
    return RuleBinding(rule_id="JSON_SCHEMA", targets=["data.payload"],
                       params={"schema": schema}, source="standard")


def make_ds(values):
    df = pd.DataFrame({"_row": list(range(2, 2 + len(values))), "payload": values})
    return Dataset({("t.xlsx", "data"): df})


class TestJsonSchema:
    rule = JsonSchemaRule()
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def test_valid_json_no_issue(self):
        ds = make_ds(['{"name": "창원시"}'])
        b = make_binding(self.schema)
        assert self.rule.check(ds, b, RuleContext()) == []

    def test_invalid_json_parse_error(self):
        ds = make_ds(["not-json"])
        b = make_binding(self.schema)
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) == 1
        assert issues[0].tier == "approval"

    def test_schema_violation(self):
        ds = make_ds(['{"age": 30}'])  # name 필드 없음
        b = make_binding(self.schema)
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) == 1

    def test_wrong_type(self):
        ds = make_ds(['{"name": 123}'])  # name이 string이어야 함
        b = make_binding(self.schema)
        issues = self.rule.check(ds, b, RuleContext())
        assert len(issues) == 1

    def test_empty_value_skipped(self):
        ds = make_ds([""])
        b = make_binding(self.schema)
        assert self.rule.check(ds, b, RuleContext()) == []

    def test_no_schema_no_check(self):
        ds = make_ds(["not-json"])
        b = RuleBinding(rule_id="JSON_SCHEMA", targets=["data.payload"], params={}, source="standard")
        assert self.rule.check(ds, b, RuleContext()) == []
