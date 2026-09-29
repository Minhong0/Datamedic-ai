from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from engine.models import Dataset


_EXCEL_EPOCH = pd.Timestamp("1899-12-30")


def _excel_serial_to_iso(value: str) -> str:
    """엑셀 날짜 일련번호(정수 문자열)를 ISO 8601 날짜 문자열로 변환."""
    try:
        n = int(value)
        # 엑셀은 1900-03-01 이전에 윤일을 잘못 계산하므로 60 이상만 신뢰
        if n < 60:
            return value
        ts = _EXCEL_EPOCH + pd.Timedelta(days=n)
        return ts.strftime("%Y-%m-%d")
    except (ValueError, OverflowError):
        return value


def _looks_like_serial(series: pd.Series) -> bool:
    """컬럼 값이 대부분 정수 문자열이고 범위가 날짜 일련번호처럼 보이면 True."""
    nums = pd.to_numeric(series.dropna().replace("", None).dropna(), errors="coerce")
    valid = nums.dropna()
    if len(valid) < len(series) * 0.5:
        return False
    return bool((valid >= 40_000).all() and (valid <= 55_000).all())


def _load_xlsx(path: Path) -> dict[str, pd.DataFrame]:
    raw = pd.read_excel(path, sheet_name=None, dtype=str, header=0)
    result: dict[str, pd.DataFrame] = {}
    for sheet, df in raw.items():
        df = df.fillna("").astype(str)
        # 엑셀 행 번호: 헤더=1, 첫 데이터=2
        df.insert(0, "_row", range(2, 2 + len(df)))
        df["_row"] = df["_row"].astype(int)
        # 날짜 일련번호 컬럼 변환
        for col in df.columns:
            if col == "_row":
                continue
            if _looks_like_serial(df[col]):
                df[col] = df[col].apply(_excel_serial_to_iso)
        result[sheet] = df
    return result


def _load_json(path: Path) -> dict[str, pd.DataFrame]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        arrays = {"root": data}
    elif isinstance(data, dict):
        arrays = {
            k: v for k, v in data.items() if isinstance(v, list)
        }
        if not arrays:
            arrays = {"root": [data]}
    else:
        raise ValueError(f"지원하지 않는 JSON 구조: {path}")

    result: dict[str, pd.DataFrame] = {}
    for sheet, records in arrays.items():
        df = pd.json_normalize(records).astype(str).fillna("")
        df.insert(0, "_row", range(2, 2 + len(df)))
        df["_row"] = df["_row"].astype(int)
        result[sheet] = df
    return result


def load_files(paths: list[str | Path]) -> Dataset:
    """xlsx/json 파일 목록을 읽어 Dataset으로 반환한다.

    - 모든 셀 값은 str로 통일 (dtype=str)
    - 각 DataFrame에 _row 컬럼이 추가됨 (엑셀 원본 행 번호: 헤더=1, 첫 데이터=2)
    - 엑셀 날짜 일련번호(40000~55000 범위 정수)는 ISO 문자열로 자동 변환
    """
    tables: dict[tuple[str, str], pd.DataFrame] = {}
    for p in paths:
        path = Path(p)
        if not path.exists():
            raise FileNotFoundError(path)
        ext = path.suffix.lower()
        fname = path.name
        if ext in (".xlsx", ".xls"):
            sheets = _load_xlsx(path)
        elif ext == ".json":
            sheets = _load_json(path)
        else:
            raise ValueError(f"지원하지 않는 형식: {ext}")
        for sheet, df in sheets.items():
            tables[(fname, sheet)] = df
    return Dataset(tables)
