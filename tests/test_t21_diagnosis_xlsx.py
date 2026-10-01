"""T21 테스트: save_diagnosis_xlsx — xlsx 시트 구조 검증."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import openpyxl
import pytest

from engine.models import Dataset, Decision, RuleBinding
from engine.exporter import save_diagnosis_xlsx
import engine.validator as validator


def _make_issues():
    df = pd.DataFrame({
        "_row": [2, 3, 4],
        "기관명":   [" 창원시 ", "진주시", "통영시"],
        "지원일자": ["20260315", "2026-04-01", "bad-date"],
    })
    ds = Dataset({("a.xlsx", "실적"): df})
    bindings = [
        RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"],   source="standard"),
        RuleBinding(rule_id="DATE_FORMAT",    targets=["실적.지원일자"], source="standard"),
    ]
    return validator.run(ds, bindings)


def test_diagnosis_xlsx_created():
    issues = _make_issues()
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_diagnosis_xlsx(issues, run_dir)
        assert (run_dir / "diagnosis_report.xlsx").exists()


def test_diagnosis_xlsx_sheets():
    """요약, 전체목록, 파일별 시트가 있어야 한다."""
    issues = _make_issues()
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_diagnosis_xlsx(issues, run_dir)
        wb = openpyxl.load_workbook(run_dir / "diagnosis_report.xlsx")
        sheet_names = wb.sheetnames
        assert "요약" in sheet_names
        assert "전체목록" in sheet_names
        assert "a.xlsx" in sheet_names


def test_diagnosis_xlsx_total_list_rows():
    """전체목록 시트의 데이터 행 수 = issues 수."""
    issues = _make_issues()
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_diagnosis_xlsx(issues, run_dir)
        wb = openpyxl.load_workbook(run_dir / "diagnosis_report.xlsx")
        ws = wb["전체목록"]
        # 헤더 제외
        data_rows = ws.max_row - 1
        assert data_rows == len(issues)


def test_diagnosis_xlsx_summary_has_tier_rows():
    """요약 시트에 tier 집계 행이 있어야 한다."""
    issues = _make_issues()
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_diagnosis_xlsx(issues, run_dir)
        wb = openpyxl.load_workbook(run_dir / "diagnosis_report.xlsx")
        ws = wb["요약"]
        sections = [row[0].value for row in ws.iter_rows(min_row=2)]
        assert "tier" in sections
