"""T18 테스트: 복수 표준 서식 yaml 지원 — 최적 매칭 검증."""
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import yaml
import pytest

from agent.nodes.structure import _load_all_standards, _match_ratio, _profiles_from_standard


def _write_yaml(path: Path, content: dict) -> None:
    path.write_text(yaml.dump(content, allow_unicode=True), encoding="utf-8")


def test_load_all_standards_empty_dir():
    with tempfile.TemporaryDirectory() as d:
        from config import settings
        with patch.object(settings, "standard_dir", Path(d)):
            result = _load_all_standards()
    assert result == []


def test_load_all_standards_multiple_files():
    with tempfile.TemporaryDirectory() as d:
        std_dir = Path(d)
        _write_yaml(std_dir / "a.yaml", {"sheets": {"A": {"columns": [{"name": "col1", "type": "text"}]}}})
        _write_yaml(std_dir / "b.yaml", {"sheets": {"B": {"columns": [{"name": "col2", "type": "date"}]}}})
        from config import settings
        with patch.object(settings, "standard_dir", std_dir):
            result = _load_all_standards()
    assert len(result) == 2


def test_match_ratio_full_match():
    assert _match_ratio(["a", "b", "c"], ["a", "b", "c", "d"]) == 1.0


def test_match_ratio_partial():
    assert _match_ratio(["a", "b", "c"], ["a", "c"]) == pytest.approx(2/3)


def test_match_ratio_empty_std():
    assert _match_ratio([], ["a", "b"]) == 0.0


def test_profiles_from_standard_range_is_str():
    """YAML range가 date 객체로 파싱되어도 params['range']는 ISO 문자열이어야 한다.

    (DATE_RANGE 규칙이 str.strip()을 호출하므로 date 객체면 크래시 → 범위 검증 무력화)
    """
    import datetime as _dt

    sheet_def = {"columns": [
        {"name": "지원일자", "type": "date", "required": True,
         "range": [_dt.date(2026, 1, 1), _dt.date(2026, 12, 31)]},
    ]}
    profile = _profiles_from_standard("t.xlsx", "실적", sheet_def, row_count=1)
    rng = profile.columns[0].params["range"]
    assert rng == ["2026-01-01", "2026-12-31"]
    assert all(isinstance(x, str) for x in rng)


def test_date_range_catches_out_of_range_via_standard():
    """표준에서 만든 range 파라미터로 DATE_RANGE가 범위 밖 날짜를 잡아야 한다."""
    import datetime as _dt

    from engine.models import Dataset, RuleBinding
    from engine.rules.base import RuleContext
    from engine.rules.value_rules import DateRangeRule

    sheet_def = {"columns": [
        {"name": "지원일자", "type": "date", "required": True,
         "range": [_dt.date(2026, 1, 1), _dt.date(2026, 12, 31)]},
    ]}
    profile = _profiles_from_standard("t.xlsx", "실적", sheet_def, row_count=2)
    params = profile.columns[0].params

    df = pd.DataFrame({"_row": [2, 3], "지원일자": ["2026-06-15", "2025-12-31"]})
    ds = Dataset({("t.xlsx", "실적"): df})
    b = RuleBinding(rule_id="DATE_RANGE", targets=["실적.지원일자"], params=params, source="standard")
    issues = DateRangeRule().check(ds, b, RuleContext())
    assert len(issues) == 1
    assert issues[0].ref.row == 3


def test_structure_node_picks_best_standard():
    """두 yaml 중 더 잘 매칭되는 것을 선택해야 한다."""
    import openpyxl

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        std_dir = base / "standard"
        std_dir.mkdir()
        file_dir = base / "files"
        file_dir.mkdir()

        # yaml A: 실적 서식 (기관명, 지원일자, 지원금액)
        _write_yaml(std_dir / "perf.yaml", {
            "sheets": {"실적": {"columns": [
                {"name": "기관명",   "type": "org_name", "required": True},
                {"name": "지원일자", "type": "date",     "required": True},
                {"name": "지원금액", "type": "amount",   "required": True},
            ]}}
        })
        # yaml B: 인사 서식 (성명, 직급, 입사일) — 매칭률이 낮아야 함
        _write_yaml(std_dir / "hr.yaml", {
            "sheets": {"인사": {"columns": [
                {"name": "성명",   "type": "text", "required": True},
                {"name": "직급",   "type": "code", "required": True},
                {"name": "입사일", "type": "date", "required": True},
            ]}}
        })

        # 실제 xlsx 파일 생성 (기관명, 지원일자, 지원금액 컬럼)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "실적"
        ws.append(["기관명", "지원일자", "지원금액"])
        ws.append(["창원시", "2026-03-15", "1000000"])
        ws.append(["진주시", "2026-04-01", "2000000"])
        xlsx_path = file_dir / "test.xlsx"
        wb.save(xlsx_path)

        from config import settings
        with patch.object(settings, "standard_dir", std_dir):
            from agent.nodes.structure import structure_node
            result = structure_node({
                "run_id": "test",
                "input_paths": [str(xlsx_path)],
                "decisions": [],
            })

        profiles = result["profiles"]
        assert len(profiles) == 1
        col_names = [c.name for c in profiles[0].columns]
        # 실적 서식(yaml A)의 컬럼이 선택되어야 함
        assert "기관명" in col_names
        assert "지원일자" in col_names
