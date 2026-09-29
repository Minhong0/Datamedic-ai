from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import pandas as pd

try:
    from rapidfuzz import process, fuzz
    _HAS_RAPIDFUZZ = True
except ImportError:
    _HAS_RAPIDFUZZ = False

from config import settings


class Codebook:
    def __init__(self, codes: list[str]) -> None:
        self._codes = codes
        self._code_set = set(codes)

    def is_valid(self, code: str) -> bool:
        return code in self._code_set

    def nearest(self, code: str, n: int = 3) -> list[str]:
        if not _HAS_RAPIDFUZZ or not self._codes:
            return []
        results = process.extract(code, self._codes, scorer=fuzz.WRatio, limit=n)
        return [r[0] for r in results]

    @property
    def codes(self) -> list[str]:
        return list(self._codes)


@lru_cache(maxsize=32)
def load_codebook(filename: str) -> Codebook:
    path = settings.standard_dir / filename
    if not path.exists():
        return Codebook([])
    df = pd.read_csv(path, dtype=str)
    code_col = df.columns[0]
    codes = df[code_col].dropna().str.strip().tolist()
    return Codebook(codes)


def get_valid_codes(filename: str) -> list[str]:
    return load_codebook(filename).codes
