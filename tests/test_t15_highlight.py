"""T15 테스트: save_highlighted — 오류·수정 셀 색상 검증."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import openpyxl
import pytest

from engine.models import Dataset, Decision, RuleBinding, CellRef, Change
from engine.exporter import save_highlighted
import engine.validator as validator
import engine.fixer as fixer
from datetime import datetime, timezone


def _make_ds() -> Dataset:
    df = pd.DataFrame({
        "_row": [2, 3, 4],
        "기관명":   ["창원시", " 진주시 ", "통영시"],  # row3 공백 오류
        "지원일자": ["2026-03-15", "20260401", "2026-05-10"],  # row3 날짜 형식 오류
    })
    return Dataset({("test.xlsx", "실적"): df})


def test_highlighted_file_created():
    ds = _make_ds()
    bindings = [
        RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard"),
        RuleBinding(rule_id="DATE_FORMAT", targets=["실적.지원일자"], source="standard"),
    ]
    issues = validator.run(ds, bindings)
    decisions = [Decision(issue_id=i.issue_id, action="apply", decided_by="system") for i in issues]
    cleaned, changes = fixer.apply(ds, issues, decisions)

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_highlighted(cleaned, issues, changes, run_dir)
        out = run_dir / "highlighted" / "test.xlsx"
        assert out.exists()


def test_error_cells_are_red():
    """오류가 수정되지 않은 셀(reject)은 빨간색이어야 한다."""
    ds = _make_ds()
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    # reject → 수정 없음, 오류 색상만 남아야 함
    decisions = [Decision(issue_id=i.issue_id, action="reject", decided_by="operator") for i in issues]
    cleaned, changes = fixer.apply(ds, issues, decisions)

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_highlighted(cleaned, issues, changes, run_dir)
        wb = openpyxl.load_workbook(run_dir / "highlighted" / "test.xlsx")
        ws = wb["실적"]
        # 헤더 행 제외, 오류가 있는 셀의 fill을 확인
        col_idx = {cell.value: cell.column for cell in ws[1]}
        error_rows = {i.ref.row for i in issues if i.ref}
        for row_num in error_rows:
            cell = ws.cell(row=row_num, column=col_idx["기관명"])
            assert cell.fill.fgColor.rgb.endswith("FFCCCC"), f"row {row_num} 오류 셀이 빨간색이 아님"


def test_fixed_cells_are_green():
    """수정된 셀은 초록색이어야 한다."""
    ds = _make_ds()
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    decisions = [Decision(issue_id=i.issue_id, action="apply", decided_by="system") for i in issues]
    cleaned, changes = fixer.apply(ds, issues, decisions)

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_highlighted(cleaned, issues, changes, run_dir)
        wb = openpyxl.load_workbook(run_dir / "highlighted" / "test.xlsx")
        ws = wb["실적"]
        col_idx = {cell.value: cell.column for cell in ws[1]}
        fixed_rows = {c.ref.row for c in changes if c.after is not None}
        for row_num in fixed_rows:
            cell = ws.cell(row=row_num, column=col_idx["기관명"])
            assert cell.fill.fgColor.rgb.endswith("CCFFCC"), f"row {row_num} 수정 셀이 초록색이 아님"


def test_clean_cells_have_no_fill():
    """오류도 수정도 없는 셀은 색상이 없어야 한다."""
    ds = _make_ds()
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    decisions = [Decision(issue_id=i.issue_id, action="apply", decided_by="system") for i in issues]
    cleaned, changes = fixer.apply(ds, issues, decisions)

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        save_highlighted(cleaned, issues, changes, run_dir)
        wb = openpyxl.load_workbook(run_dir / "highlighted" / "test.xlsx")
        ws = wb["실적"]
        col_idx = {cell.value: cell.column for cell in ws[1]}
        error_rows = {i.ref.row for i in issues if i.ref}
        # row 2 (창원시) = 오류 없음
        if 2 not in error_rows:
            cell = ws.cell(row=2, column=col_idx["기관명"])
            # 기본 fill은 None 또는 00000000
            rgb = cell.fill.fgColor.rgb if cell.fill and cell.fill.fgColor else "00000000"
            assert rgb not in ("FFCCCC", "CCFFCC"), "깨끗한 셀에 색상이 붙었음"
