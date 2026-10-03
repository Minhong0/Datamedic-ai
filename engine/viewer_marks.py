"""변경 내역 뷰어의 표시 정보 계산.

기존 Issue·Decision·Change(와 집계 위반·그 처리 결과)만으로 셀·행 상태를 계산한다. 새 데이터를 저장하지 않고,
Streamlit을 모른다 (그리기는 ui 쪽). 화면에 나가는 문자열은 모두 마스킹한다.
엔진은 llm/·agent/를 import하지 않는다.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal, Mapping

from engine.decisions import aggregates_stale
from engine.masking import mask_cell_value
from engine.models import Change, Decision, GroupIssue, Issue

CellKey = tuple[str, str, int, str]        # (file, sheet, row, column)
RowKey = tuple[str, str, int]              # (file, sheet, row)
CellStatus = Literal["auto", "auto_pending", "approval", "undecided", "operator", "rejected", "request"]

# 한 셀에 여러 상태가 겹치면 앞에 있는 것이 대표 상태
PRIORITY: tuple[str, ...] = ("approval", "undecided", "request", "rejected", "operator", "auto", "auto_pending")

AGGREGATE_RULES = ("SUM_LIMIT", "MAX_COUNT", "NEAR_DUPLICATE")
CHECK_LABELS = {"SUM_LIMIT": "한도 초과", "MAX_COUNT": "중복 수혜", "NEAR_DUPLICATE": "이중 지급"}
RULE_LABELS = {
    "AMOUNT_NORMALIZE": "금액 정규화", "AMOUNT_SUSPECT_UNIT": "금액 단위 의심", "AMOUNT_NONPOSITIVE": "금액 0 이하",
    "DATE_NORMALIZE": "날짜 정규화", "BIZNO_VERIFY": "사업자번호 정리", "BIZNO_FORMAT": "사업자번호 형식",
    "BIZNO_CHECKSUM": "사업자번호 체크디지트", "BIZNO_NOT_FOUND": "국세청 미등록",
    "BIZNO_SUSPENDED": "휴업 사업자", "BIZNO_UNVERIFIED": "국세청 조회 실패",
    "BIZNO_CLOSED_BEFORE_SUPPORT": "폐업 후 지원 의심", "BIZNO_CLOSED_AFTER_SUPPORT": "지원 후 폐업",
    "BLANK": "필수값 누락", "DUPLICATE_ROW": "중복 행", **CHECK_LABELS,
}


@dataclass
class MarkDetail:
    status: CellStatus
    rule_id: str
    before: str | None           # 현재(원래) 값 (마스킹 적용)
    after: str | None            # 적용된 값 / 제안값 / 거절된 제안값 (마스킹 적용)
    reason: str                  # 사람이 읽는 한 줄 (마스킹 적용)
    decided_by: str | None       # 처리자 (툴팁·메모 전용)
    issue_id: str | None = None  # 변경 목록에서 결정 버튼을 붙이기 위한 연결 키
    edited: bool = False         # Decision.action == "edit"
    timestamp: datetime | None = None


@dataclass
class CellMark:
    status: CellStatus           # 우선순위로 고른 대표 상태
    details: list[MarkDetail]    # 이 셀의 모든 상태


@dataclass
class CheckSummary:
    rule_id: str
    label: str                   # "한도 초과", "중복 수혜" ...
    issue_count: int
    status_counts: dict[str, int]    # {"request": 1, "ok": 2}
    stale: bool = False          # 집계 실행 이후 담당자 결정이 바뀜


@dataclass
class ViewerMarks:
    cells: dict[CellKey, CellMark] = field(default_factory=dict)
    ok_rows: set[RowKey] = field(default_factory=set)
    deleted_rows: dict[RowKey, MarkDetail] = field(default_factory=dict)
    group_rows: dict[RowKey, list[str]] = field(default_factory=dict)
    sheet_checks: dict[tuple[str, str], list[CheckSummary]] = field(default_factory=dict)
    file_counts: dict[str, dict[str, int]] = field(default_factory=dict)
    aggregates_stale: bool = False


def _label(rule_id: str) -> str:
    return RULE_LABELS.get(rule_id, rule_id)


def _fmt(column: str, value: str | None) -> str:
    return "(공백)" if value in (None, "") else str(mask_cell_value(column, value))


def _pick(details: list[MarkDetail]) -> CellStatus:
    return min((d.status for d in details), key=PRIORITY.index)


def _last_decisions(decisions: Iterable[Decision]) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for d in decisions:
        out[d.issue_id] = d          # 같은 Issue에 결정이 여러 번 있으면 마지막이 이긴다
    return out


def _from_change(c: Change, decision: Decision | None, rule_id: str) -> MarkDetail:
    col = c.ref.column
    edited = bool(decision and decision.action == "edit")
    if c.decided_by == "system":
        status: CellStatus = "auto"
        reason = f"자동 · {_label(rule_id)}: {_fmt(col, c.before)} → {_fmt(col, c.after)}"
    elif edited:
        status, reason = "operator", f"담당자 직접 수정 · {_fmt(col, c.after)}"
    else:
        status = "operator"
        reason = f"담당자 승인 · {_label(rule_id)}: {_fmt(col, c.before)} → {_fmt(col, c.after)}"
    return MarkDetail(status=status, rule_id=rule_id, before=_fmt(col, c.before), after=_fmt(col, c.after),
                      reason=reason, decided_by=c.decided_by, issue_id=c.issue_id, edited=edited,
                      timestamp=c.timestamp)


def _from_unchanged_issue(issue: Issue, decision: Decision | None, finalized: bool) -> MarkDetail:
    """Change 가 없는 Issue — 거절·확인 요청·결정 전(예정·승인 대기)·미결정."""
    col = issue.ref.column if issue.ref else ""
    before, proposed = _fmt(col, issue.value), _fmt(col, issue.suggestion) if issue.suggestion else None
    label = _label(issue.rule_id)
    base = dict(rule_id=issue.rule_id, before=before, issue_id=issue.issue_id)

    if decision is not None and decision.action == "reject":
        return MarkDetail(status="rejected", after=proposed, decided_by=decision.decided_by,
                          reason=f"거절 · 제안 {proposed or '없음'} 대신 원래 값 유지", **base)
    if (decision is not None and decision.action == "request") or (decision is None and issue.tier == "request"):
        who = decision.decided_by if decision else "system"
        return MarkDetail(status="request", after=None, decided_by=who,
                          reason=f"확인 요청 · {label} · 회신 대기", **base)
    # 여기부터는 아직 값이 바뀌지 않은 상태
    if issue.tier == "auto" and not finalized:
        return MarkDetail(status="auto_pending", after=proposed, decided_by=None,
                          reason=f"자동 수정 예정 · {label}: {before} → {proposed or '?'}", **base)
    if issue.tier == "approval" and not finalized:
        return MarkDetail(status="approval", after=proposed, decided_by=None,
                          reason=f"승인 대기 · {label}: {before} → 제안 {proposed or '없음'}", **base)
    # 확정됐는데 처리되지 않은 항목 — 예외로 만들지 않고 미결정으로 보여 준다
    return MarkDetail(status="undecided", after=proposed, decided_by=None,
                      reason="미결정 · 원래 값 유지 (결정 없이 종료)", **base)


def _group_label(g: GroupIssue, key: str, disposition: str) -> str:
    note = "문제 없음으로 확인" if disposition == "ok" else "확인 요청"
    return f"{CHECK_LABELS.get(g.rule_id, g.rule_id)} ({key}) · {note}"


def build_marks(
    issues: Iterable[Issue],
    decisions: Iterable[Decision],
    changes: Iterable[Change],
    executed_rules: Iterable[str],
    *,
    finalized: bool,
    group_issues: Iterable[tuple[str, GroupIssue]] = (),
    group_dispositions: Mapping[str, str] | None = None,
    aggregate_basis: str | None = None,
    row_index: Mapping[tuple[str, str], Iterable[int]] | None = None,
    aggregate_sheets: Iterable[str] = ("실적",),
) -> ViewerMarks:
    """표시 정보를 계산한다.

    group_issues  : (검사 이름, 묶음) 목록 — 키는 `<검사>:<묶음 ID>` (ID 는 검사마다 겹친다)
    group_dispositions : {키: "request"|"ok"} — 없는 묶음은 기본인 확인 요청으로 본다
    aggregate_basis: 집계를 실행했을 때의 담당자 결정 버전 (오래됨 판정용)
    row_index      : {(파일, 시트): 원본 행 번호들} — ok_rows(✓)를 계산하려면 필요하다
    """
    issues, changes = list(issues), list(changes)
    decisions = list(decisions)
    dec = _last_decisions(decisions)
    executed = set(executed_rules)
    dispositions = dict(group_dispositions or {})
    rule_of = {i.issue_id: i.rule_id for i in issues}

    details: dict[CellKey, list[MarkDetail]] = defaultdict(list)
    deleted: dict[RowKey, MarkDetail] = {}

    # 1) 적용된 변경 — 자동·담당자 수정, 삭제된 행
    changed_issue_ids: set[str] = set()
    for c in changes:
        changed_issue_ids.add(c.issue_id)
        md = _from_change(c, dec.get(c.issue_id), rule_of.get(c.issue_id, ""))
        if c.after is None:
            md.reason = f"삭제 · {_label(md.rule_id)}" + (" · 담당자 승인" if c.decided_by == "operator" else "")
            deleted[(c.ref.file, c.ref.sheet, c.ref.row)] = md
        else:
            details[(c.ref.file, c.ref.sheet, c.ref.row, c.ref.column)].append(md)

    # 2) 변경이 없는 Issue — 거절·확인 요청·결정 전·미결정
    for i in issues:
        if i.ref is None or i.issue_id in changed_issue_ids:
            continue                                  # 테이블 단위 Issue 는 셀·행에 넣지 않는다
        details[(i.ref.file, i.ref.sheet, i.ref.row, i.ref.column)].append(
            _from_unchanged_issue(i, dec.get(i.issue_id), finalized))

    # 3) 집계 위반 묶음
    group_rows: dict[RowKey, list[str]] = defaultdict(list)
    group_cells: set[RowKey] = set()
    by_file_rule: dict[tuple[str, str, str], list[str]] = defaultdict(list)   # (file, sheet, rule) → 처리 목록
    for capability, g in group_issues:
        key = f"{capability}:{g.issue_id}"
        disp = dispositions.get(key, "request")
        label = _group_label(g, key, disp)
        files: set[tuple[str, str]] = set()
        for r in g.rows:
            rk = (r.file, r.sheet, r.row)
            group_rows[rk].append(label)
            group_cells.add(rk)
            files.add((r.file, r.sheet))
            if disp != "ok":
                details[(r.file, r.sheet, r.row, r.column)].append(MarkDetail(
                    status="request", rule_id=g.rule_id, before=None, after=None,
                    reason=f"확인 요청 · {CHECK_LABELS.get(g.rule_id, g.rule_id)} · {g.metric}",
                    decided_by="operator" if key in dispositions else "system"))
        for f, s in files:
            by_file_rule[(f, s, g.rule_id)].append("ok" if disp == "ok" else "request")

    cells = {k: CellMark(status=_pick(v), details=v) for k, v in details.items()}

    # 4) 집계 검사가 실행됐는지·오래됐는지, ✓ 행, 시트 요약
    ran = sorted(r for r in executed if r in AGGREGATE_RULES)
    stale = aggregates_stale(aggregate_basis, decisions, has_results=bool(ran))
    sheets = set(aggregate_sheets)
    ok_rows: set[RowKey] = set()
    sheet_checks: dict[tuple[str, str], list[CheckSummary]] = {}
    if ran:
        known = {(f, s) for (f, s) in (row_index or {})} | {(f, s) for (f, s, _) in by_file_rule}
        for f, s in sorted(known):
            if s not in sheets:
                continue
            sheet_checks[(f, s)] = []
            for rule in ran:
                outcomes = by_file_rule.get((f, s, rule), [])
                counts: dict[str, int] = {}
                for o in outcomes:
                    counts[o] = counts.get(o, 0) + 1
                sheet_checks[(f, s)].append(CheckSummary(
                    rule_id=rule, label=CHECK_LABELS[rule], issue_count=len(outcomes),
                    status_counts=counts, stale=stale))
        if not stale:                                  # 오래된 집계 결과로는 '통과'를 단정하지 않는다
            has_cell = {(f, s, r) for (f, s, r, _c) in cells}
            for (f, s), rows in (row_index or {}).items():
                if s not in sheets:
                    continue
                for r in rows:
                    rk = (f, s, r)
                    if rk not in has_cell and rk not in deleted and rk not in group_cells:
                        ok_rows.add(rk)

    # 5) 파일별 상태 건수 (대표 상태 기준 + 삭제된 행)
    file_counts: dict[str, dict[str, int]] = defaultdict(dict)
    for (f, _s, _r, _c), m in cells.items():
        file_counts[f][m.status] = file_counts[f].get(m.status, 0) + 1
    for (f, _s, _r) in deleted:
        file_counts[f]["deleted"] = file_counts[f].get("deleted", 0) + 1

    return ViewerMarks(cells=cells, ok_rows=ok_rows, deleted_rows=deleted, group_rows=dict(group_rows),
                       sheet_checks=sheet_checks, file_counts=dict(file_counts), aggregates_stale=stale)
