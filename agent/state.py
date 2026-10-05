from __future__ import annotations

from typing import TypedDict

from engine.models import (
    Change, Dataset, Decision, Issue, Plan, RequestItem, RuleBinding,
    RunReport, SheetProfile,
)


class GraphState(TypedDict, total=False):
    run_id: str
    input_paths: list[str]
    goal: str

    plan: Plan
    plan_choices: list[str]

    agg_queue: list[str]
    query: dict
    agg_results: list[dict]
    agg_dispositions: dict
    aggregate_basis: str

    rule_priorities: list[str]

    analysis_summary: dict
    warnings: list[str]

    chat_history: list[dict]

    dataset: Dataset
    profiles: list[SheetProfile]

    bindings: list[RuleBinding]

    issues: list[Issue]


    decisions: list[Decision]

    cleaned: Dataset | None
    changes: list[Change]

    requests: list[RequestItem]
    report: RunReport | None
    remaining_issue_list: list[Issue]
    loop_count: int

    agent_action: str
    agent_reason: str
    agent_loop_count: int
    agent_phase: str
