from __future__ import annotations

import logging
from pathlib import Path

import yaml

from config import settings
from engine import parser
from engine.models import ColumnProfile, SheetProfile
from agent.state import GraphState

logger = logging.getLogger(__name__)

_STANDARD_THRESHOLD = settings.standard_match_threshold


def _load_standard(yaml_path: Path) -> dict:
    with open(yaml_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _load_all_standards() -> list[dict]:
    """standard/ 디렉터리의 모든 yaml 파일을 읽어 반환한다."""
    standards = []
    if settings.standard_dir.exists():
        for path in sorted(settings.standard_dir.glob("*.yaml")):
            try:
                standards.append(_load_standard(path))
            except Exception:
                logger.warning("표준 서식 로드 실패: %s", path)
    return standards


def _match_ratio(std_cols: list[str], df_cols: list[str]) -> float:
    if not std_cols:
        return 0.0
    matched = sum(1 for c in std_cols if c in df_cols)
    return matched / len(std_cols)


def _profiles_from_standard(file: str, sheet: str, sheet_def: dict, row_count: int) -> SheetProfile:
    cols = []
    for col_def in sheet_def.get("columns", []):
        params: dict = {}
        if "codebook" in col_def:
            from engine.codebook import get_valid_codes
            params["valid_codes"] = get_valid_codes(col_def["codebook"])
        if "range" in col_def:
            params["range"] = [str(x) for x in col_def["range"]]
        if "unit" in col_def:
            params["unit"] = col_def["unit"]
        cols.append(ColumnProfile(
            name=col_def["name"],
            semantic_type=col_def["type"],
            required=col_def.get("required", False),
            confidence=1.0,
            params=params,
        ))
    return SheetProfile(file=file, sheet=sheet, columns=cols, row_count=row_count)


def structure_node(state: GraphState) -> GraphState:
    """파이프라인 1단계: 파일을 읽고 각 컬럼의 시맨틱 타입을 결정한다."""
    paths = state["input_paths"]
    ds = parser.load_files(paths)
    profiles: list[SheetProfile] = []

    all_standards = _load_all_standards()

    for (file, sheet), df in ds.tables.items():
        df_cols = [c for c in df.columns if c != "_row"]
        row_count = len(df)

        matched_standard = False
        best_ratio = 0.0
        best_sheet_def: dict | None = None

        for standard in all_standards:
            for sheet_name, sheet_def in standard.get("sheets", {}).items():
                std_cols = [c["name"] for c in sheet_def.get("columns", [])]
                ratio = _match_ratio(std_cols, df_cols)
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_sheet_def = sheet_def

        if best_ratio >= _STANDARD_THRESHOLD and best_sheet_def is not None:
            profiles.append(_profiles_from_standard(file, sheet, best_sheet_def, row_count))
            matched_standard = True
            logger.info("표준 서식 매칭: %s/%s (일치율 %.0f%%)", file, sheet, best_ratio * 100)

        if not matched_standard:
            try:
                from llm.client import complete_json, mask_sensitive
                from pydantic import BaseModel

                class ColResult(BaseModel):
                    column: str
                    semantic_type: str
                    confidence: float

                class SemanticResult(BaseModel):
                    columns: list[ColResult]

                sample = df.head(5).to_dict("records")
                masked_sample = [
                    {k: mask_sensitive(str(v)) for k, v in row.items() if k != "_row"}
                    for row in sample
                ]
                result = complete_json(
                    "column_semantics",
                    {"headers": df_cols, "sample_rows": masked_sample},
                    SemanticResult,
                )
                col_map = {r.column: r for r in result.columns}
            except Exception:
                col_map = {}

            cols = []
            for col in df_cols:
                r = col_map.get(col)
                if r and r.confidence >= settings.llm_confidence_min:
                    sem = r.semantic_type
                else:
                    sem = "unknown"
                cols.append(ColumnProfile(name=col, semantic_type=sem, confidence=getattr(r, "confidence", 1.0) if r else 1.0))
            profiles.append(SheetProfile(file=file, sheet=sheet, columns=cols, row_count=row_count))

    return {**state, "dataset": ds, "profiles": profiles}
