"""T07 통합 테스트: 오류 있는 샘플 → validator → fixer → exporter."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from engine.models import Dataset, Decision, RuleBinding, CellRef
from engine.rules.base import RuleContext
import engine.validator as validator
import engine.fixer as fixer
import engine.exporter as exporter
from engine.semantic import bindings_from_profiles
from engine.models import ColumnProfile, SheetProfile


def make_dirty_ds() -> Dataset:
    df = pd.DataFrame({
        "_row": [2, 3, 4, 5],
        "기관명":     ["창원시", " 진주시 ", "통영시", "통영시"],  # 공백 오류, 중복행
        "지원일자":   ["2026-03-15", "20260401", "2026-05-10", "2026-05-10"],  # 날짜 형식
        "지원금액":   ["1000000", "2,000,000", "1500000", "1500000"],  # 쉼표
    })
    return Dataset({("dirty.xlsx", "실적"): df})


# ── validator ─────────────────────────────────────────────────────────────────

def test_validator_finds_issues():
    ds = make_dirty_ds()
    bindings = [
        RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard"),
        RuleBinding(rule_id="DATE_FORMAT",    targets=["실적.지원일자"], source="standard"),
        RuleBinding(rule_id="NUMERIC_UNIT",   targets=["실적.지원금액"], source="standard"),
        RuleBinding(rule_id="DUPLICATE_ROW",  targets=[], source="standard"),
    ]
    issues = validator.run(ds, bindings)
    rule_ids = {i.rule_id for i in issues}
    assert "TEXT_NORMALIZE" in rule_ids
    assert "DATE_FORMAT" in rule_ids
    assert "NUMERIC_UNIT" in rule_ids
    assert "DUPLICATE_ROW" in rule_ids


def test_validator_unknown_rule_skipped():
    ds = make_dirty_ds()
    bindings = [RuleBinding(rule_id="NONEXISTENT_RULE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    assert issues == []


# ── fixer ─────────────────────────────────────────────────────────────────────

def test_fixer_applies_text_normalize():
    ds = Dataset({("f.xlsx", "실적"): pd.DataFrame({
        "_row": [2], "기관명": [" 창원시 "]
    })})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    assert len(issues) == 1

    decisions = [Decision(issue_id=issues[0].issue_id, action="apply", decided_by="system")]
    cleaned, changes = fixer.apply(ds, issues, decisions)

    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="기관명")
    assert cleaned.cell(ref) == "창원시"
    assert len(changes) == 1
    assert changes[0].before == " 창원시 "
    assert changes[0].after == "창원시"


def test_fixer_reject_no_change():
    ds = Dataset({("f.xlsx", "실적"): pd.DataFrame({
        "_row": [2], "기관명": [" 창원시 "]
    })})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    decisions = [Decision(issue_id=issues[0].issue_id, action="reject", decided_by="operator")]
    cleaned, changes = fixer.apply(ds, issues, decisions)
    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="기관명")
    assert cleaned.cell(ref) == " 창원시 "
    assert changes == []


def test_fixer_edit_action():
    ds = Dataset({("f.xlsx", "실적"): pd.DataFrame({
        "_row": [2], "지원일자": ["20260315"]
    })})
    bindings = [RuleBinding(rule_id="DATE_FORMAT", targets=["실적.지원일자"], source="standard")]
    issues = validator.run(ds, bindings)
    decisions = [Decision(
        issue_id=issues[0].issue_id, action="edit",
        value="2026-03-15", decided_by="operator"
    )]
    cleaned, changes = fixer.apply(ds, issues, decisions)
    ref = CellRef(file="f.xlsx", sheet="실적", row=2, column="지원일자")
    assert cleaned.cell(ref) == "2026-03-15"


def test_fixer_duplicate_row_deleted():
    ds = Dataset({("f.xlsx", "실적"): pd.DataFrame({
        "_row": [2, 3],
        "기관명": ["창원시", "창원시"],
        "금액": ["1000", "1000"],
    })})
    bindings = [RuleBinding(rule_id="DUPLICATE_ROW", targets=[], source="standard")]
    issues = validator.run(ds, bindings)
    assert len(issues) == 1
    decisions = [Decision(issue_id=issues[0].issue_id, action="apply", decided_by="system")]
    cleaned, changes = fixer.apply(ds, issues, decisions)
    df = cleaned.tables[("f.xlsx", "실적")]
    assert len(df) == 1
    assert len(changes) == 1
    assert changes[0].after is None


# ── exporter ──────────────────────────────────────────────────────────────────

def test_exporter_saves_files():
    ds = Dataset({("test.xlsx", "실적"): pd.DataFrame({
        "_row": [2, 3], "기관명": ["창원시", "진주시"]
    })})
    bindings = [RuleBinding(rule_id="TEXT_NORMALIZE", targets=["실적.기관명"], source="standard")]
    issues = validator.run(ds, bindings)
    decisions = [Decision(issue_id=i.issue_id, action="apply", decided_by="system") for i in issues]
    cleaned, changes = fixer.apply(ds, issues, decisions)

    from engine.models import RunReport
    report = RunReport(
        run_id="20260929-120000", files=["test.xlsx"],
        issue_counts={}, tier_counts={},
        recheck_passed=True, remaining_issues=[],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir)
        exporter.save_cleaned(cleaned, run_dir)
        exporter.save_changelog(changes, run_dir)
        exporter.save_issues(issues, run_dir)
        exporter.save_report(report, run_dir)
        exporter.save_diagnosis_report(issues, run_dir)

        assert (run_dir / "cleaned" / "test.xlsx").exists()
        assert (run_dir / "changelog.csv").exists()
        assert (run_dir / "issues.csv").exists()
        assert (run_dir / "report.json").exists()
        assert (run_dir / "diagnosis_report.md").exists()
