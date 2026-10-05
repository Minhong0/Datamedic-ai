"""변경 내역 뷰어의 표시 정보 계산."""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Literal, Mapping

from engine.decisions import aggregates_stale
from engine.masking import BIZNO_COLUMN, mask_cell_value
from engine.models import Change, Decision, GroupIssue, Issue

CellKey = tuple[str, str, int, str]
RowKey = tuple[str, str, int]
CellStatus = Literal["auto", "auto_pending", "approval", "undecided", "operator", "rejected", "request"]

PRIORITY: tuple[str, ...] = ("approval", "undecided", "request", "rejected", "operator", "auto", "auto_pending")

AGGREGATE_RULES = ("SUM_LIMIT", "MAX_COUNT", "NEAR_DUPLICATE")
CHECK_LABELS = {"SUM_LIMIT": "한도 초과", "MAX_COUNT": "중복 수혜", "NEAR_DUPLICATE": "이중 지급"}
RULE_LABELS = {
    "AMOUNT_NORMALIZE": "금액 정규화", "AMOUNT_SUSPECT_UNIT": "금액 단위 의심", "AMOUNT_NONPOSITIVE": "금액 0 이하",
    "DATE_NORMALIZE": "날짜 정규화", "BIZNO_VERIFY": "사업자번호 표기 통일", "BIZNO_FORMAT": "사업자번호 형식",
    "BIZNO_CHECKSUM": "사업자번호 체크디지트", "BIZNO_NOT_FOUND": "국세청 미등록",
    "BIZNO_SUSPENDED": "휴업 사업자", "BIZNO_UNVERIFIED": "국세청 조회 실패",
    "BIZNO_CLOSED_BEFORE_SUPPORT": "폐업 후 지원 의심", "BIZNO_CLOSED_AFTER_SUPPORT": "지원 후 폐업",
    "BLANK": "필수값 누락", "DUPLICATE_ROW": "완전 중복 행", "DATE_OUT_OF_RANGE": "날짜 범위 오류",
    "CODE_VALID": "코드표 불일치", **CHECK_LABELS,
}
TIER_LABELS = {"auto": "자동", "approval": "승인", "request": "요청"}


def rule_label(rule_id: str) -> str:
    """화면에 보여 줄 규칙 이름 (한국어). 모르는 규칙은 코드 그대로."""
    return RULE_LABELS.get(rule_id, rule_id)


def tier_label(tier: str) -> str:
    return TIER_LABELS.get(tier, tier)


def localize_rule_ids(text: str) -> str:
    """문장 속의 규칙 코드(BIZNO_CHECKSUM 등)를 한국어 이름으로 바꾼다 — LLM 요약문에 코드가 섞여 나올 때 쓴다."""
    import re
    if not text:
        return text
    pattern = "|".join(sorted(map(re.escape, RULE_LABELS), key=len, reverse=True))
    out = re.sub(f"(?<![A-Za-z0-9_])({pattern})(?![A-Za-z0-9_])", lambda m: RULE_LABELS[m.group(1)], text)
    return re.sub(r"([가-힣A-Za-z ]{2,20})[(]([가-힣A-Za-z ]{2,20})[)]",
                  lambda m: m.group(1) if m.group(1).strip() == m.group(2).strip() else m.group(0), out)


@dataclass
class MarkDetail:
    status: CellStatus
    rule_id: str
    before: str | None
    after: str | None
    reason: str
    decided_by: str | None
    issue_id: str | None = None
    edited: bool = False
    timestamp: datetime | None = None


@dataclass
class CellMark:
    status: CellStatus
    details: list[MarkDetail]


@dataclass
class CheckSummary:
    rule_id: str
    label: str
    issue_count: int
    status_counts: dict[str, int]
    stale: bool = False


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


_FORMAT_ONLY_NOTE = " (숫자는 그대로, 하이픈만 추가)"


def _fmt_pair(column: str, before: str | None, after: str | None) -> tuple[str, str, str]:
    """변경 전·후 표시값과 보충 설명. 사업자번호는 마스킹하면 '7686945828'과 '768-69-45828'이 똑같이"""
    b, a = _fmt(column, before), _fmt(column, after)
    if column != BIZNO_COLUMN or not before or not after or b != a:
        return b, a, ""
    if re.sub(r"\D", "", str(before)) != re.sub(r"\D", "", str(after)) or str(before) == str(after):
        return b, a, ""

    def shaped(raw: str, masked: str) -> str:
        digits = re.sub(r"\D", "", str(raw))
        return masked if "-" in str(raw) else f"{digits[:5]}*****"

    return shaped(before, b), shaped(after, a), _FORMAT_ONLY_NOTE


def _pick(details: list[MarkDetail]) -> CellStatus:
    return min((d.status for d in details), key=PRIORITY.index)


def _last_decisions(decisions: Iterable[Decision]) -> dict[str, Decision]:
    out: dict[str, Decision] = {}
    for d in decisions:
        out[d.issue_id] = d
    return out


def _from_change(c: Change, decision: Decision | None, rule_id: str) -> MarkDetail:
    col = c.ref.column
    edited = bool(decision and decision.action == "edit")
    b, a, note = _fmt_pair(col, c.before, c.after)
    if c.decided_by == "system":
        status: CellStatus = "auto"
        reason = f"자동 · {_label(rule_id)}: {b} → {a}{note}"
    elif edited:
        status, reason = "operator", f"담당자 직접 수정 · {_fmt(col, c.after)}"
    else:
        status = "operator"
        reason = f"담당자 승인 · {_label(rule_id)}: {b} → {a}{note}"
    return MarkDetail(status=status, rule_id=rule_id, before=b, after=a,
                      reason=reason, decided_by=c.decided_by, issue_id=c.issue_id, edited=edited,
                      timestamp=c.timestamp)


def _from_unchanged_issue(issue: Issue, decision: Decision | None, finalized: bool) -> MarkDetail:
    """Change 가 없는 Issue — 거절·확인 요청·결정 전(예정·승인 대기)·미결정."""
    col = issue.ref.column if issue.ref else ""
    before, proposed = _fmt(col, issue.value), _fmt(col, issue.suggestion) if issue.suggestion else None
    note = ""
    if issue.suggestion:
        before, proposed, note = _fmt_pair(col, issue.value, issue.suggestion)
    label = _label(issue.rule_id)
    base = dict(rule_id=issue.rule_id, before=before, issue_id=issue.issue_id)

    is_dup = issue.rule_id == "DUPLICATE_ROW"
    if decision is not None and decision.action == "reject":
        return MarkDetail(status="rejected", after=proposed, decided_by=decision.decided_by,
                          reason="거절 · 삭제하지 않고 그대로 둠" if is_dup else f"거절 · 제안 {proposed or '없음'} 대신 원래 값 유지", **base)
    if (decision is not None and decision.action == "request") or (decision is None and issue.tier == "request"):
        who = decision.decided_by if decision else "system"
        return MarkDetail(status="request", after=None, decided_by=who,
                          reason=f"확인 요청 · {label} · 회신 대기", **base)
    if issue.tier == "auto" and not finalized:
        return MarkDetail(status="auto_pending", after=proposed, decided_by=None,
                          reason=f"자동 수정 예정 · {label}: {before} → {proposed or '?'}{note}", **base)
    if is_dup and issue.tier == "approval" and not finalized:
        return MarkDetail(status="approval", after=None, decided_by=None,
                          reason=f"승인 대기 · {label} — 승인하면 이 행을 삭제합니다 (원본 파일은 그대로)", **base)
    if issue.tier == "approval" and not finalized:
        return MarkDetail(status="approval", after=proposed, decided_by=None,
                          reason=f"승인 대기 · {label}: {before} → 제안 {proposed or '없음'}{note}", **base)
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
    """표시 정보를 계산한다."""
    issues, changes = list(issues), list(changes)
    decisions = list(decisions)
    dec = _last_decisions(decisions)
    executed = set(executed_rules)
    dispositions = dict(group_dispositions or {})
    rule_of = {i.issue_id: i.rule_id for i in issues}

    details: dict[CellKey, list[MarkDetail]] = defaultdict(list)
    deleted: dict[RowKey, MarkDetail] = {}

    changed_issue_ids: set[str] = set()
    for c in changes:
        changed_issue_ids.add(c.issue_id)
        md = _from_change(c, dec.get(c.issue_id), rule_of.get(c.issue_id, ""))
        if c.after is None:
            md.reason = f"삭제 · {_label(md.rule_id)}" + (" · 담당자 승인" if c.decided_by == "operator" else "")
            deleted[(c.ref.file, c.ref.sheet, c.ref.row)] = md
        else:
            details[(c.ref.file, c.ref.sheet, c.ref.row, c.ref.column)].append(md)

    for i in issues:
        if i.ref is None or i.issue_id in changed_issue_ids:
            continue
        details[(i.ref.file, i.ref.sheet, i.ref.row, i.ref.column)].append(
            _from_unchanged_issue(i, dec.get(i.issue_id), finalized))

    group_rows: dict[RowKey, list[str]] = defaultdict(list)
    group_cells: set[RowKey] = set()
    by_file_rule: dict[tuple[str, str, str], list[str]] = defaultdict(list)
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
        if not stale:
            has_cell = {(f, s, r) for (f, s, r, _c) in cells}
            for (f, s), rows in (row_index or {}).items():
                if s not in sheets:
                    continue
                for r in rows:
                    rk = (f, s, r)
                    if rk not in has_cell and rk not in deleted and rk not in group_cells:
                        ok_rows.add(rk)

    file_counts: dict[str, dict[str, int]] = defaultdict(dict)
    for (f, _s, _r, _c), m in cells.items():
        file_counts[f][m.status] = file_counts[f].get(m.status, 0) + 1
    for (f, _s, _r) in deleted:
        file_counts[f]["deleted"] = file_counts[f].get("deleted", 0) + 1

    return ViewerMarks(cells=cells, ok_rows=ok_rows, deleted_rows=deleted, group_rows=dict(group_rows),
                       sheet_checks=sheet_checks, file_counts=dict(file_counts), aggregates_stale=stale)
