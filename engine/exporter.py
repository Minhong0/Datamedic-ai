from __future__ import annotations

import csv
import json
from pathlib import Path

import openpyxl
from openpyxl.styles import PatternFill, Font
import pandas as pd

from engine.models import Change, Dataset, Issue, RunReport

# 오류 셀: 연한 빨간 배경 + 진한 빨간 글씨
_FILL_ERROR = PatternFill("solid", fgColor="FFCCCC")
_FONT_ERROR = Font(color="CC0000", bold=True)
# 수정 셀: 연한 초록 배경 + 진한 초록 글씨
_FILL_FIXED = PatternFill("solid", fgColor="CCFFCC")
_FONT_FIXED = Font(color="006600", bold=True)


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_cleaned(ds: Dataset, run_dir: Path) -> None:
    out_dir = _ensure(run_dir / "cleaned")
    written: dict[str, openpyxl.Workbook] = {}

    for (file, sheet), df in ds.tables.items():
        if file not in written:
            written[file] = openpyxl.Workbook()
            written[file].remove(written[file].active)  # 기본 시트 제거
        wb = written[file]
        ws = wb.create_sheet(sheet)
        data_cols = [c for c in df.columns if c != "_row"]
        ws.append(data_cols)
        for _, row in df.iterrows():
            ws.append([row[c] for c in data_cols])

    for file, wb in written.items():
        wb.save(out_dir / file)


def save_changelog(changes: list[Change], run_dir: Path) -> None:
    from engine.masking import mask_changes
    changes = mask_changes(changes)   # 사업자번호 표시 마스킹 (RULES.md 0절 6항)
    path = run_dir / "changelog.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "change_id", "issue_id", "file", "sheet", "row", "column",
            "before", "after", "tier", "decided_by", "timestamp",
        ])
        writer.writeheader()
        for c in changes:
            writer.writerow({
                "change_id": c.change_id,
                "issue_id": c.issue_id,
                "file": c.ref.file,
                "sheet": c.ref.sheet,
                "row": c.ref.row,
                "column": c.ref.column,
                "before": c.before,
                "after": c.after,
                "tier": c.tier,
                "decided_by": c.decided_by,
                "timestamp": c.timestamp.isoformat(),
            })


def save_issues(issues: list[Issue], run_dir: Path) -> None:
    from engine.masking import mask_issues
    issues = mask_issues(issues)
    path = run_dir / "issues.csv"
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "issue_id", "rule_id", "kind", "tier", "value",
            "message", "explanation", "suggestion",
            "file", "sheet", "row", "column",
        ])
        writer.writeheader()
        for i in issues:
            writer.writerow({
                "issue_id": i.issue_id,
                "rule_id": i.rule_id,
                "kind": i.kind,
                "tier": i.tier,
                "value": i.value,
                "message": i.message,
                "explanation": i.explanation,
                "suggestion": i.suggestion,
                "file": i.ref.file if i.ref else "",
                "sheet": i.ref.sheet if i.ref else "",
                "row": i.ref.row if i.ref else "",
                "column": i.ref.column if i.ref else "",
            })


def save_report(report: RunReport, run_dir: Path) -> None:
    path = run_dir / "report.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report.model_dump(), f, ensure_ascii=False, indent=2)


def save_highlighted(
    ds: Dataset,
    issues: list[Issue],
    changes: list[Change],
    run_dir: Path,
) -> None:
    """정제본에 오류·수정 셀을 색상으로 표시한 하이라이트 엑셀을 저장한다.

    - 오류 셀 (Issue.ref): 연한 빨간 배경
    - 수정 셀 (Change.ref, after is not None): 연한 초록 배경
    - 행 삭제(after=None)는 색상 없음 (이미 행이 사라짐)
    """
    out_dir = _ensure(run_dir / "highlighted")

    # (file, sheet, row, col) → 상태: "error" | "fixed"
    # 수정이 오류보다 우선 (초록이 빨간보다 우선)
    cell_status: dict[tuple[str, str, int, str], str] = {}
    for i in issues:
        if i.ref:
            key = (i.ref.file, i.ref.sheet, i.ref.row, i.ref.column)
            cell_status[key] = "error"
    for c in changes:
        if c.after is not None:
            key = (c.ref.file, c.ref.sheet, c.ref.row, c.ref.column)
            cell_status[key] = "fixed"

    written: dict[str, openpyxl.Workbook] = {}
    for (file, sheet), df in ds.tables.items():
        if file not in written:
            written[file] = openpyxl.Workbook()
            written[file].remove(written[file].active)
        wb = written[file]
        ws = wb.create_sheet(sheet)
        data_cols = [c for c in df.columns if c != "_row"]
        ws.append(data_cols)

        for _, row in df.iterrows():
            ws.append([row[c] for c in data_cols])

        # 컬럼명 → 1-based 열 인덱스 맵
        col_idx = {col: idx + 1 for idx, col in enumerate(data_cols)}

        # 헤더=행1, 첫 데이터=행2 → ws_row = _row 값 (엑셀 기준과 동일)
        for _, row in df.iterrows():
            excel_row = int(row["_row"])
            for col in data_cols:
                status = cell_status.get((file, sheet, excel_row, col))
                if status is None:
                    continue
                cell = ws.cell(row=excel_row, column=col_idx[col])
                if status == "fixed":
                    cell.fill = _FILL_FIXED
                    cell.font = _FONT_FIXED
                else:
                    cell.fill = _FILL_ERROR
                    cell.font = _FONT_ERROR

    for file, wb in written.items():
        wb.save(out_dir / file)


def save_diagnosis_report(issues: list[Issue], run_dir: Path) -> None:
    path = run_dir / "diagnosis_report.md"
    total = len(issues)
    by_rule: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    by_file: dict[str, int] = {}

    for i in issues:
        by_rule[i.rule_id] = by_rule.get(i.rule_id, 0) + 1
        by_tier[i.tier] = by_tier.get(i.tier, 0) + 1
        fname = i.ref.file if i.ref else "unknown"
        by_file[fname] = by_file.get(fname, 0) + 1

    lines = [
        "# 진단 리포트\n",
        f"**총 오류**: {total}건\n",
        "\n## 티어별\n",
        *[f"- {k}: {v}건" for k, v in sorted(by_tier.items())],
        "\n## 규칙별\n",
        *[f"- {k}: {v}건" for k, v in sorted(by_rule.items())],
        "\n## 파일별\n",
        *[f"- {k}: {v}건" for k, v in sorted(by_file.items())],
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def save_diagnosis_xlsx(issues: list[Issue], run_dir: Path) -> None:
    """진단 리포트를 xlsx로 저장한다 (파일별·규칙별 시트 + 전체 목록 시트).

    시트 구성:
      - 요약: tier별·rule별·파일별 집계
      - 전체목록: 모든 Issue 행 단위 상세
      - 파일별 시트: 각 파일의 Issue만 모아서 표시
    """
    from engine.masking import mask_issues
    issues = mask_issues(issues)

    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ── 시트 1: 요약 ──────────────────────────────────────────────────
    ws_sum = wb.create_sheet("요약")
    ws_sum.append(["구분", "항목", "건수"])
    by_tier: dict[str, int] = {}
    by_rule: dict[str, int] = {}
    by_file: dict[str, int] = {}
    for i in issues:
        by_tier[i.tier]    = by_tier.get(i.tier, 0) + 1
        by_rule[i.rule_id] = by_rule.get(i.rule_id, 0) + 1
        fname = i.ref.file if i.ref else "unknown"
        by_file[fname]     = by_file.get(fname, 0) + 1
    for k, v in sorted(by_tier.items()):
        ws_sum.append(["tier", k, v])
    for k, v in sorted(by_rule.items()):
        ws_sum.append(["rule", k, v])
    for k, v in sorted(by_file.items()):
        ws_sum.append(["file", k, v])

    _ISSUE_HEADER = ["issue_id", "rule_id", "tier", "kind", "파일", "시트", "행", "컬럼", "값", "메시지", "설명", "제안"]

    def _issue_row(i: Issue) -> list:
        return [
            i.issue_id, i.rule_id, i.tier, i.kind,
            i.ref.file if i.ref else "", i.ref.sheet if i.ref else "",
            i.ref.row if i.ref else "", i.ref.column if i.ref else "",
            i.value or "", i.message, i.explanation or "", i.suggestion or "",
        ]

    # ── 시트 2: 전체 목록 ─────────────────────────────────────────────
    ws_all = wb.create_sheet("전체목록")
    ws_all.append(_ISSUE_HEADER)
    for i in issues:
        ws_all.append(_issue_row(i))

    # ── 시트 3+: 파일별 ───────────────────────────────────────────────
    files_seen: dict[str, list[Issue]] = {}
    for i in issues:
        fname = i.ref.file if i.ref else "unknown"
        files_seen.setdefault(fname, []).append(i)

    for fname, file_issues in files_seen.items():
        # 시트명은 31자 제한
        sheet_name = fname[:28] + "..." if len(fname) > 31 else fname
        ws_f = wb.create_sheet(sheet_name)
        ws_f.append(_ISSUE_HEADER)
        for i in file_issues:
            ws_f.append(_issue_row(i))

    wb.save(run_dir / "diagnosis_report.xlsx")
