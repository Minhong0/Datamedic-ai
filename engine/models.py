from __future__ import annotations

from datetime import datetime
from typing import Literal

import pandas as pd
from pydantic import BaseModel, field_validator

Template = Literal["MAX_COUNT", "NEAR_DUPLICATE", "SUM_LIMIT"]
Period = Literal["year", "all", "business_period"]


SemanticType = Literal[
    "org_name", "biz_no", "date", "amount", "count",
    "phone", "email", "code", "text", "unknown"
]
Tier = Literal["auto", "approval", "request"]
ErrorKind = Literal["rule", "outlier"]


class CellRef(BaseModel):
    file: str
    sheet: str
    row: int
    column: str

    @property
    def a1(self) -> str:
        """Sheet1!A2 같은 표기로 변환."""
        return f"{self.sheet}!{self.column}{self.row}"


class ColumnProfile(BaseModel):
    name: str
    semantic_type: SemanticType
    required: bool = False
    confidence: float = 1.0
    params: dict = {}


class SheetProfile(BaseModel):
    file: str
    sheet: str
    columns: list[ColumnProfile]
    row_count: int


class RuleBinding(BaseModel):
    rule_id: str
    targets: list[str]
    params: dict = {}
    source: Literal["standard", "semantic_default", "llm_hint"]


class Issue(BaseModel):
    issue_id: str
    ref: CellRef | None = None
    rule_id: str
    kind: ErrorKind
    value: str | None
    message: str
    explanation: str | None = None
    tier: Tier
    suggestion: str | None = None
    confidence: float = 1.0


class Decision(BaseModel):
    issue_id: str
    action: Literal["apply", "reject", "edit", "request"]
    value: str | None = None
    decided_by: Literal["system", "operator"] = "operator"


class Change(BaseModel):
    change_id: str
    issue_id: str
    ref: CellRef
    before: str | None
    after: str | None
    tier: Tier
    decided_by: Literal["system", "operator"]
    timestamp: datetime


class RequestItem(BaseModel):
    org: str
    issues: list[str]
    subject: str
    body: str
    to: str | None = None
    sent: bool = False


class PlanStep(BaseModel):
    capability: str
    label: str
    status: Literal["run", "dependency", "skipped"]
    reason: str


class Plan(BaseModel):
    goal: str
    status: Literal["ready", "needs_clarification"]
    steps: list[PlanStep] = []
    question: str | None = None
    options: list[dict] = []
    delete_intent: bool = False
    unhandled: list[str] = []
    source: Literal["llm", "keyword", "full", "user"] = "keyword"


class RunReport(BaseModel):
    run_id: str
    files: list[str]
    issue_counts: dict[str, int]
    tier_counts: dict[str, int]
    recheck_passed: bool
    remaining_issues: list[str]


class RuleSpec(BaseModel):
    """집계 규칙 명세 (R5~R7). 템플릿 3종 + 파라미터로만 표현한다."""

    spec_id: str
    template: Template
    description: str
    group_by: list[str]
    params: dict
    period: Period = "year"
    filter_business: str | list[str] | None = None
    tier: Tier = "request"
    source: Literal["business_master", "user_query"] = "business_master"
    confirmed_by: str | None = None


class GroupIssue(Issue):
    """집계 규칙(R5~R7)이 생성하는 행 묶음 단위 Issue."""

    group_key: dict[str, str]
    rows: list[CellRef]
    metric: str
    spec_id: str


class Dataset:
    """엔진 내부 전용 — (file, sheet) → DataFrame (모든 값 str)."""

    def __init__(self, tables: dict[tuple[str, str], pd.DataFrame] | None = None) -> None:
        self.tables: dict[tuple[str, str], pd.DataFrame] = tables or {}

    def cell(self, ref: CellRef) -> str | None:
        """CellRef가 가리키는 셀 값을 반환. 없으면 None."""
        df = self.tables.get((ref.file, ref.sheet))
        if df is None:
            return None
        mask = df["_row"] == ref.row
        rows = df[mask]
        if rows.empty or ref.column not in df.columns:
            return None
        return rows.iloc[0][ref.column]

    def set_cell(self, ref: CellRef, value: str | None) -> None:
        """CellRef가 가리키는 셀 값을 수정. fixer.py만 호출한다."""
        df = self.tables[(ref.file, ref.sheet)]
        idx = df.index[df["_row"] == ref.row]
        if idx.empty:
            raise KeyError(f"row {ref.row} not found in {ref.file}/{ref.sheet}")
        df.loc[idx[0], ref.column] = value

    def drop_row(self, file: str, sheet: str, row: int) -> None:
        """_row 기준으로 행을 삭제. 나머지 행의 _row 값은 유지된다."""
        df = self.tables[(file, sheet)]
        idx = df.index[df["_row"] == row]
        if idx.empty:
            raise KeyError(f"row {row} not found in {file}/{sheet}")
        self.tables[(file, sheet)] = df.drop(index=idx[0]).reset_index(drop=True)

    def copy(self) -> "Dataset":
        """원본을 보존하면서 수정할 수 있는 깊은 복사본을 반환."""
        return Dataset({k: v.copy() for k, v in self.tables.items()})
