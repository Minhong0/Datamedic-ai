"""JSON 파일 읽기·되돌려 쓰기."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from engine.masking import BIZNO_COLUMN, COMPANY_COLUMN, mask_cell_value

SENSITIVE = (BIZNO_COLUMN, COMPANY_COLUMN)
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


@dataclass
class JsonLayout:
    arrays: dict[str, list[dict]] = field(default_factory=dict)
    wrapped: bool = False
    unsupported: list[tuple[str, str]] = field(default_factory=list)


def split_arrays(data: Any) -> JsonLayout:
    """JSON 값에서 표로 펼칠 객체 배열을 찾는다 (parser·되돌려 쓰기·안내가 같은 규칙을 쓴다)."""
    layout = JsonLayout()
    if isinstance(data, list):
        candidates = {"root": data}
    elif isinstance(data, dict):
        lists = {k: v for k, v in data.items() if isinstance(v, list)}
        if lists:
            candidates = lists
        else:
            candidates, layout.wrapped = {"root": [data]}, True
    else:
        layout.unsupported.append(("(최상위)", "배열이나 객체가 아니라서 표로 표시할 수 없습니다"))
        return layout
    for key, items in candidates.items():
        if not items:
            layout.unsupported.append((key, "비어 있는 배열입니다"))
        elif not all(isinstance(i, dict) for i in items):
            layout.unsupported.append((key, "객체 배열이 아니라서 표로 표시할 수 없습니다"))
        else:
            layout.arrays[key] = items
    return layout


def describe_json(path: str | Path) -> JsonLayout:
    """파일을 읽어 어떤 부분이 표가 되고 어떤 부분이 안 되는지 알려 준다. 읽을 수 없으면 이유를 unsupported 에 담는다."""
    try:
        with open(path, encoding="utf-8") as f:
            return split_arrays(json.load(f))
    except (OSError, ValueError) as e:
        layout = JsonLayout()
        layout.unsupported.append(("(파일)", f"JSON 으로 읽을 수 없습니다: {e}"))
        return layout


def unsupported_notes(paths: list[str]) -> list[str]:
    """표시할 수 없는 JSON 구조의 안내 문구 목록 — '파일.json · 키: 이유'."""
    notes: list[str] = []
    for p in paths or []:
        if str(p).lower().endswith(".json"):
            for key, reason in describe_json(p).unsupported:
                notes.append(f"{Path(p).name} · {key}: {reason}")
    return notes


def to_frame(records: list[dict]) -> pd.DataFrame:
    """객체 배열 → 모든 값이 str 인 표 + `_row`(내부 행 번호, 2부터). 화면에는 배열 인덱스 [i]로 보인다."""
    df = pd.json_normalize(records).fillna("").astype(str)
    df.insert(0, "_row", range(2, 2 + len(df)))
    df["_row"] = df["_row"].astype(int)
    return df


def is_json_file(file: str) -> bool:
    return str(file).lower().endswith(".json")


def row_label(file: str, row: int) -> str:
    """화면용 행 이름 — JSON 은 배열 인덱스 `[0]`, 엑셀은 행 번호."""
    return f"[{row - 2}]" if is_json_file(file) else str(row)


def _typed(new: str, old: Any, is_amount: bool) -> Any:
    """바뀐 값을 원래 값의 JSON 형식에 맞춘다 — 숫자였거나 금액 열이면 정수·실수로, 그 밖에는 문자열 그대로."""
    was_number = isinstance(old, (int, float)) and not isinstance(old, bool)
    if _INT_RE.match(new) and (was_number or is_amount):
        return int(new)
    if _FLOAT_RE.match(new) and was_number:
        return float(new)
    return new


def _get_container(record: dict, column: str):
    """열 이름(점으로 연결된 경로)이 가리키는 (dict, 마지막 키) — 찾지 못하면 None."""
    if column in record:
        return record, column
    node: Any = record
    parts = column.split(".")
    for i, part in enumerate(parts[:-1]):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return (node, parts[-1]) if isinstance(node, dict) and parts[-1] in node else None


def apply_cleaned_to_json(
    path: str | Path,
    original: dict[str, pd.DataFrame],
    cleaned: dict[str, pd.DataFrame] | None,
    *,
    amount_columns: set[str] | None = None,
    masked: bool = False,
) -> bytes:
    """원본 JSON 구조에 정제된 값만 반영해 JSON 바이트를 만든다."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    layout = split_arrays(data)
    amount_columns = amount_columns or {"지원금액"}

    for sheet, records in layout.arrays.items():
        orig_df = original.get(sheet)
        if orig_df is None:
            continue
        clean_df = (cleaned or {}).get(sheet, orig_df)
        orig_by_row = {int(r["_row"]): r for _, r in orig_df.iterrows()}
        kept = {int(r) for r in clean_df["_row"]}
        for _, crow in clean_df.iterrows():
            idx = int(crow["_row"]) - 2
            if not 0 <= idx < len(records):
                continue
            orow = orig_by_row.get(int(crow["_row"]))
            for col in (c for c in clean_df.columns if c != "_row"):
                new = "" if pd.isna(crow[col]) else str(crow[col])
                if orow is not None and col in orow.index and new == str(orow[col]) and not (
                        masked and col.split(".")[-1] in SENSITIVE):
                    continue
                target = _get_container(records[idx], col)
                if target is None:
                    continue
                holder, key = target
                value: Any = _typed(new, holder[key], col.split(".")[-1] in amount_columns)
                if masked and col.split(".")[-1] in SENSITIVE:
                    value = mask_cell_value(col.split(".")[-1], new)
                holder[key] = value
        if not layout.wrapped:
            for row_no in sorted((r for r in orig_by_row if r not in kept), reverse=True):
                idx = row_no - 2
                if 0 <= idx < len(records):
                    del records[idx]
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
