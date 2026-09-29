from __future__ import annotations

from datetime import datetime
from typing import Literal

import pandas as pd
from pydantic import BaseModel, field_validator


# 컬럼의 의미론적 분류 (LLM 또는 표준 정의에서 부여)
SemanticType = Literal[
    "org_name", "biz_no", "date", "amount", "count",
    "phone", "email", "code", "text", "unknown"
]
# 오류 처리 등급: auto=자동수정, approval=사람 승인 필요, request=원본 제공처에 요청
Tier = Literal["auto", "approval", "request"]
ErrorKind = Literal["rule", "outlier"]


# 특정 셀을 가리키는 좌표 (엑셀 행 번호 기준, 헤더=1 → 첫 데이터=2)
class CellRef(BaseModel):
    file: str
    sheet: str
    row: int    # 원본 엑셀 행 번호 (_row 컬럼 값)
    column: str

    @property
    def a1(self) -> str:
        """Sheet1!A2 같은 표기로 변환."""
        return f"{self.sheet}!{self.column}{self.row}"


# 하나의 컬럼에 대한 메타데이터
class ColumnProfile(BaseModel):
    name: str
    semantic_type: SemanticType
    required: bool = False
    confidence: float = 1.0
    params: dict = {}   # codebook, range, unit 등 규칙별 파라미터


# 하나의 시트 전체 메타데이터 (structure 노드가 생성)
class SheetProfile(BaseModel):
    file: str
    sheet: str
    columns: list[ColumnProfile]
    row_count: int


# "어떤 규칙(rule_id)을 어떤 컬럼(targets)에 적용하라"는 지시
# rules 노드가 생성하고, validator/fixer가 참조한다
class RuleBinding(BaseModel):
    rule_id: str
    targets: list[str]
    params: dict = {}
    source: Literal["standard", "semantic_default", "llm_hint"]


# 발견된 오류 하나를 표현 (diagnosis 노드가 생성)
class Issue(BaseModel):
    issue_id: str           # E-0001 형식
    ref: CellRef | None = None
    rule_id: str            # 위반한 규칙 ID (예: DATE_FORMAT, DUPLICATE_ROW)
    kind: ErrorKind
    value: str | None       # 문제가 된 원래 값
    message: str            # 사용자에게 보여줄 한국어 오류 설명
    explanation: str | None = None  # LLM이 생성한 상세 설명
    tier: Tier
    suggestion: str | None = None   # 자동 수정 후보값
    confidence: float = 1.0


# Issue 하나에 대한 처리 결정 (treatment 노드가 소비)
class Decision(BaseModel):
    issue_id: str
    action: Literal["apply", "reject", "edit", "request"]
    value: str | None = None        # action=="edit" 일 때 직접 입력값
    decided_by: Literal["system", "operator"] = "operator"


# 실제로 적용된 변경 이력 (treatment 노드가 생성)
class Change(BaseModel):
    change_id: str          # C-0001 형식
    issue_id: str
    ref: CellRef
    before: str | None
    after: str | None       # None이면 행 삭제(DUPLICATE_ROW)
    tier: Tier
    decided_by: Literal["system", "operator"]
    timestamp: datetime


# 원본 데이터 제공처에 보낼 보완 요청 (recheck 노드가 생성)
class RequestItem(BaseModel):
    org: str
    issues: list[str]
    subject: str
    body: str
    to: str | None = None
    sent: bool = False


# 한 번의 실행 전체 요약 (recheck 노드가 생성)
class RunReport(BaseModel):
    run_id: str
    files: list[str]
    issue_counts: dict[str, int]    # rule_id → 건수
    tier_counts: dict[str, int]     # tier → 건수
    recheck_passed: bool
    remaining_issues: list[str]


class Dataset:
    """엔진 내부 전용 — (file, sheet) → DataFrame (모든 값 str).

    _row 컬럼이 엑셀 원본 행 번호를 보존하므로, 행 삭제 후에도
    나머지 행의 참조 번호가 바뀌지 않는다.
    """

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
