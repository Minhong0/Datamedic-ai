from datetime import datetime, timezone

import pandas as pd
import pytest

from engine.models import (
    CellRef, ColumnProfile, SheetProfile, RuleBinding,
    Issue, Decision, Change, RequestItem, RunReport, Dataset,
)


def make_ref(**kw):
    defaults = dict(file="a.xlsx", sheet="실적", row=2, column="기관명")
    defaults.update(kw)
    return CellRef(**defaults)


# ── CellRef ──────────────────────────────────────────────────────────────────

def test_cellref_a1():
    ref = make_ref(sheet="실적", column="지원금액", row=12)
    assert ref.a1 == "실적!지원금액12"


def test_cellref_serialise():
    ref = make_ref()
    data = ref.model_dump()
    assert data["file"] == "a.xlsx"
    restored = CellRef(**data)
    assert restored == ref


# ── ColumnProfile ─────────────────────────────────────────────────────────────

def test_column_profile_defaults():
    col = ColumnProfile(name="기관명", semantic_type="org_name")
    assert col.required is False
    assert col.confidence == 1.0
    assert col.params == {}


# ── SheetProfile ──────────────────────────────────────────────────────────────

def test_sheet_profile():
    sp = SheetProfile(
        file="a.xlsx", sheet="실적",
        columns=[ColumnProfile(name="기관명", semantic_type="org_name")],
        row_count=10,
    )
    assert sp.row_count == 10


# ── RuleBinding ───────────────────────────────────────────────────────────────

def test_rule_binding():
    rb = RuleBinding(rule_id="DATE_FORMAT", targets=["실적.지원일자"], source="standard")
    assert rb.params == {}


# ── Issue ─────────────────────────────────────────────────────────────────────

def test_issue_defaults():
    issue = Issue(
        issue_id="E-0001", rule_id="DATE_FORMAT", kind="rule",
        value="20260101", message="날짜 형식 오류", tier="auto",
    )
    assert issue.explanation is None
    assert issue.confidence == 1.0


def test_issue_with_ref():
    ref = make_ref(row=5, column="지원일자")
    issue = Issue(
        issue_id="E-0002", ref=ref, rule_id="DATE_FORMAT", kind="rule",
        value="20260101", message="날짜 형식 오류", tier="auto",
    )
    assert issue.ref.row == 5


# ── Decision ──────────────────────────────────────────────────────────────────

def test_decision_apply():
    d = Decision(issue_id="E-0001", action="apply")
    assert d.decided_by == "operator"


def test_decision_edit():
    d = Decision(issue_id="E-0001", action="edit", value="2026-01-01", decided_by="operator")
    assert d.value == "2026-01-01"


# ── Change ────────────────────────────────────────────────────────────────────

def test_change_serialise():
    ts = datetime(2026, 9, 29, 12, 0, 0, tzinfo=timezone.utc)
    c = Change(
        change_id="C-0001", issue_id="E-0001",
        ref=make_ref(row=3, column="지원일자"),
        before="20260101", after="2026-01-01",
        tier="auto", decided_by="system", timestamp=ts,
    )
    data = c.model_dump()
    assert data["change_id"] == "C-0001"
    assert data["before"] == "20260101"


# ── RunReport ─────────────────────────────────────────────────────────────────

def test_run_report():
    r = RunReport(
        run_id="20260929-120000", files=["a.xlsx"],
        issue_counts={"DATE_FORMAT": 3}, tier_counts={"auto": 3},
        recheck_passed=True, remaining_issues=[],
    )
    assert r.recheck_passed is True


# ── Dataset ───────────────────────────────────────────────────────────────────

def make_ds():
    df = pd.DataFrame({
        "_row": [2, 3, 4],
        "기관명": ["창원시", "진주시", "통영시"],
        "지원금액": ["1000", "2000", "3000"],
    })
    ds = Dataset({("a.xlsx", "실적"): df})
    return ds


def test_dataset_cell_found():
    ds = make_ds()
    ref = CellRef(file="a.xlsx", sheet="실적", row=3, column="기관명")
    assert ds.cell(ref) == "진주시"


def test_dataset_cell_missing_file():
    ds = make_ds()
    ref = CellRef(file="b.xlsx", sheet="실적", row=2, column="기관명")
    assert ds.cell(ref) is None


def test_dataset_set_cell():
    ds = make_ds()
    ref = CellRef(file="a.xlsx", sheet="실적", row=2, column="기관명")
    ds.set_cell(ref, "마산시")
    assert ds.cell(ref) == "마산시"


def test_dataset_drop_row():
    ds = make_ds()
    ds.drop_row("a.xlsx", "실적", 3)
    df = ds.tables[("a.xlsx", "실적")]
    assert 3 not in df["_row"].values
    assert len(df) == 2


def test_dataset_copy_independence():
    ds = make_ds()
    ds2 = ds.copy()
    ref = CellRef(file="a.xlsx", sheet="실적", row=2, column="기관명")
    ds2.set_cell(ref, "수정됨")
    assert ds.cell(ref) == "창원시"
