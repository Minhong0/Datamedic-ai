"""변경 내역 뷰어 — 시트 HTML 표 렌더링."""
from __future__ import annotations

import re

import html as _html
from dataclasses import dataclass
from typing import Literal

import pandas as pd

from engine.json_io import is_json_file, row_label
from engine.masking import mask_cell_value
from engine.viewer_marks import CellMark, MarkDetail, ViewerMarks, rule_label
from ui.components import viewer_styles as sty

Mode = Literal["compare", "cleaned", "original"]
MODE_LABELS: dict[str, str] = {"compare": "비교", "cleaned": "정제본만", "original": "원본만"}
_HIDDEN_WITH_AUTO_TOGGLE = ("auto", "auto_pending")
_WHO = {"system": "시스템", "operator": "담당자"}


@dataclass
class ViewOptions:
    mode: Mode = "compare"
    changed_only: bool = True
    show_auto: bool = True
    page: int = 0
    page_size: int = 100


@dataclass
class PageInfo:
    page: int
    total_pages: int
    total_rows: int
    shown_rows: int


def col_letter(idx: int) -> str:
    """0 → A, 25 → Z, 26 → AA."""
    out = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        out = chr(65 + rem) + out
    return out


def _esc(text: object) -> str:
    return _html.escape("" if text is None else str(text), quote=True)


def _plain(column: str, value: object) -> str:
    """셀의 현재 값 — 마스킹을 거친 화면용 문자열."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(mask_cell_value(column, str(value)) or "")


def _tooltip(mark: CellMark, tag: str | None = None) -> str:
    lines: list[str] = []
    for d in mark.details:
        line = d.reason
        if d.decided_by:
            line += f"\n처리자: {_WHO.get(d.decided_by, d.decided_by)}"
        lines.append(line)
    return "\n---\n".join(lines)


def _rep_detail(mark: CellMark) -> MarkDetail:
    return next(d for d in mark.details if d.status == mark.status)


def _cell_content(mode: str, mark: CellMark, original: str, cleaned: str) -> str:
    """상태·보기 모드별 셀 내용. 이미 이스케이프된 HTML."""
    d = _rep_detail(mark)
    st = mark.status
    current = _esc(original if mode == "original" else cleaned)
    if mode == "original":
        return current
    after = _esc(d.after) if d.after not in (None, "") else ""
    before = _esc(d.before if d.before is not None else "")
    old = f'<span class="dm-old" style="{sty.OLD_STYLE}">{before}</span>'
    shown = original if mode == "original" else cleaned
    if (st in ("auto_pending", "approval") and d.before and d.after
            and re.sub(r"[-*\s]", "", d.before) == re.sub(r"[-*\s]", "", str(shown or "")) and d.before != str(shown)):
        current = _esc(d.before)
    if st == "auto":
        return current if mode == "cleaned" else f"{old}{current}"
    if st == "operator":
        edit = "✎" if d.edited else ""
        return f"{edit}{current}" if mode == "cleaned" else f"{old}{edit}{current}"
    if st == "auto_pending":
        return current if mode == "cleaned" else f"{current} → 예정: {after or '?'}"
    if st == "approval":
        return current if mode == "cleaned" else f"{current} → 제안: {after or '없음'}"
    if st == "undecided":
        return f"? {current}"
    if st == "rejected":
        if mode == "cleaned" or not after:
            return current
        return f'{current}<s class="dm-rejv" style="{sty.REJECTED_VALUE_STYLE}">{after}</s>'
    if st == "request":
        return f"📩 {current}"
    return current


def _row_visible(row_key, marks: ViewerMarks, cell_status_by_row: dict, opts: ViewOptions) -> bool:
    if not opts.changed_only:
        return True
    if row_key in marks.deleted_rows or row_key in marks.group_rows:
        return True
    statuses = cell_status_by_row.get(row_key, ())
    return any(s not in _HIDDEN_WITH_AUTO_TOGGLE or opts.show_auto for s in statuses)


def build_sheet_html(
    original: pd.DataFrame,
    cleaned: pd.DataFrame | None,
    marks: ViewerMarks,
    file: str,
    sheet: str,
    opts: ViewOptions | None = None,
) -> tuple[str, PageInfo]:
    """한 시트를 HTML 표로 만든다. 반환: (HTML, 페이지 정보)."""
    opts = opts or ViewOptions()
    cols = [c for c in original.columns if c != "_row"]
    cleaned_by_row = {int(r["_row"]): r for _, r in cleaned.iterrows()} if cleaned is not None else {}

    status_by_row: dict[tuple[str, str, int], list[str]] = {}
    for (f, s, r, _c), m in marks.cells.items():
        if f == file and s == sheet:
            status_by_row.setdefault((f, s, r), []).append(m.status)

    rows = []
    for _, orow in original.iterrows():
        r = int(orow["_row"])
        key = (file, sheet, r)
        deleted = key in marks.deleted_rows
        if deleted and opts.mode == "cleaned":
            continue
        if _row_visible(key, marks, status_by_row, opts):
            rows.append((r, key, orow, deleted))

    total_pages = max(1, -(-len(rows) // opts.page_size))
    page = min(max(opts.page, 0), total_pages - 1)
    chunk = rows[page * opts.page_size:(page + 1) * opts.page_size]

    json_file = is_json_file(file)
    head = "".join(f"<th>{_esc(c) if json_file else col_letter(i) + ' ' + _esc(c)}</th>" for i, c in enumerate(cols))
    body: list[str] = []
    for r, key, orow, deleted in chunk:
        crow = cleaned_by_row.get(r)
        group = marks.group_rows.get(key)
        is_ok = key in marks.ok_rows
        head_cls = "dm-rowhead" + (" dm-grp" if group else "") + (" dm-ok" if is_ok else "")
        head_style = sty.GROUP_ROWHEAD_STYLE if group else ""
        head_title = f' title="{_esc(chr(10).join(group))}"' if group else (' title="검사 통과"' if is_ok else "")
        tick = '<span class="dm-ok-mark">✓</span>' if is_ok else ""
        tds = [f'<td class="{head_cls}" style="{head_style}"{head_title}>{tick}{_esc(row_label(file, r))}</td>']
        for c in cols:
            ov = _plain(c, orow[c])
            cv = _plain(c, crow[c]) if crow is not None and c in crow.index else ov
            mark = marks.cells.get((file, sheet, r, c))
            if deleted:
                tds.append(f'<td class="dm-del" style="{sty.DELETED_ROW_STYLE}">{_esc(ov)}</td>')
                continue
            if mark is None or (mark.status in _HIDDEN_WITH_AUTO_TOGGLE and not opts.show_auto):
                tds.append(f"<td>{_esc(cv if opts.mode != 'original' else ov)}</td>")
                continue
            cls, style = sty.CELL_STYLE[mark.status]
            tds.append(f'<td class="{cls}" style="{style}" title="{_esc(_tooltip(mark))}">'
                       f"{_cell_content(opts.mode, mark, ov, cv)}</td>")
        row_cls = " class=\"dm-delrow\"" if deleted else ""
        body.append(f"<tr{row_cls}>{''.join(tds)}</tr>")

    html = (f"{sty.TABLE_CSS}\n<div class=\"dm-wrap\"><table class=\"dm-table\">"
            f"<thead><tr><th class=\"dm-rowhead\">행</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table></div>")
    return html, PageInfo(page=page, total_pages=total_pages, total_rows=len(rows), shown_rows=len(chunk))


def render_sheet_viewer(dataset, cleaned, marks: ViewerMarks, file: str, sheet: str, *, key: str = "viewer") -> None:
    """Streamlit 에 한 시트를 그린다 — 보기 모드·변경된 행만·자동 수정 표시·페이지 위젯 포함."""
    import streamlit as st

    c1, c2, c3 = st.columns([2, 1, 1])
    mode = c1.radio("보기", list(MODE_LABELS), format_func=lambda k: MODE_LABELS[k], horizontal=True, key=f"{key}_mode")
    orig = dataset.tables[(file, sheet)]
    clean = cleaned.tables.get((file, sheet)) if cleaned is not None else None
    _probe = build_sheet_html(orig, clean, marks, file, sheet, ViewOptions(mode=mode, changed_only=True))[1]
    changed_only = c2.checkbox("변경된 행만", value=_probe.total_rows > 0, key=f"{key}_changed")
    show_auto = c3.checkbox("자동 수정 표시", value=True, key=f"{key}_auto")
    opts = ViewOptions(mode=mode, changed_only=changed_only, show_auto=show_auto,
                       page=int(st.session_state.get(f"{key}_page", 0)))
    html, info = build_sheet_html(orig, clean, marks, file, sheet, opts)
    if info.total_pages > 1:
        opts.page = st.number_input(f"페이지 (1~{info.total_pages})", 1, info.total_pages, info.page + 1,
                                    key=f"{key}_pagebox") - 1
        st.session_state[f"{key}_page"] = opts.page
        html, info = build_sheet_html(orig, clean, marks, file, sheet, opts)
    st.caption(f"{info.shown_rows}행 표시 / 조건에 맞는 행 {info.total_rows}개")
    if info.total_rows == 0:
        st.info("표시할 행이 없습니다. '변경된 행만'을 끄면 모든 행이 보입니다.")
    else:
        st.html(html)


STATUS_LABELS = {
    "auto": "자동 수정", "auto_pending": "자동 수정 예정", "approval": "승인 대기", "undecided": "미결정",
    "operator": "담당자 수정", "rejected": "거절", "request": "확인 요청",
}
STATUS_ICON = {"auto": "", "auto_pending": "", "approval": "", "undecided": "?", "operator": "", "rejected": "", "request": "📩"}


def check_summary_text(checks, *, stale: bool = False) -> str:
    """시트 상단 검사 요약 한 줄 — '한도 초과 검사 통과 ✓ · 중복 수혜 1건 📩'. 집계 검사를 안 돌렸으면 빈 문자열."""
    parts: list[str] = []
    for c in checks or []:
        if c.issue_count == 0:
            parts.append(f"{c.label} 검사 통과 ✓")
            continue
        req, ok = c.status_counts.get("request", 0), c.status_counts.get("ok", 0)
        bits = ([f"{req}건 📩"] if req else []) + ([f"{ok}건 문제 없음"] if ok else [])
        parts.append(f"{c.label} " + " · ".join(bits))
    text = " · ".join(parts)
    return f"{text} (수정 후 다시 집계 필요)" if text and stale else text


def legend_html() -> str:
    """범례."""
    def chip(style: str, text: str) -> str:
        return f'<span style="{style}padding:1px 8px;margin-right:10px;border-radius:3px;white-space:nowrap">{text}</span>'
    items = [
        chip(sty.CELL_STYLE["auto"][1], "자동 수정"),
        chip(sty.CELL_STYLE["approval"][1], "승인 대기 (→ 제안값)"),
        chip(sty.CELL_STYLE["operator"][1], "담당자 수정 (✎ 직접 입력)"),
        chip("", f'원래값 <s style="{sty.REJECTED_VALUE_STYLE}">제안값</s> 거절'),
        chip("", "📩 확인 요청 · 회신 대기"),
        chip("", "? 미결정"),
        chip("", '<span class="dm-ok-mark">✓</span> 검사 통과'),
        chip("", f'<s style="{sty.DELETED_ROW_STYLE}">행</s> 삭제된 행'),
        chip(sty.GROUP_ROWHEAD_STYLE, "묶음 규칙 대상"),
    ]
    return f'<div style="font-size:0.85rem;line-height:2">{"".join(items)}</div>'


def change_rows(marks: ViewerMarks, file: str, columns_by_sheet: dict[str, list[str]]) -> list[dict]:
    """현재 파일의 모든 MarkDetail 을 변경 목록 행으로 만든다 (사업자번호·기업명은 이미 마스킹됨)."""
    out: list[dict] = []
    for (f, s, r, c), mark in sorted(marks.cells.items(), key=lambda kv: (kv[0][1], kv[0][2], kv[0][3])):
        if f != file:
            continue
        cols = columns_by_sheet.get(s, [])
        if is_json_file(file):
            where = f"{s}{row_label(file, r)} · {c}"
        else:
            where = f"{s} · {col_letter(cols.index(c)) if c in cols else c}{r}"
        for n, d in enumerate(mark.details):
            out.append({
                "위치": where, "상태": (STATUS_ICON.get(d.status, "") + " " + STATUS_LABELS[d.status]).strip(),
                "규칙": rule_label(d.rule_id), "이전": d.before or "", "이후/제안": d.after or "",
                "처리자": _WHO.get(d.decided_by or "", d.decided_by or ""), "_key": ((f, s, r, c), n),
            })
    for (f, s, r), d in sorted(marks.deleted_rows.items(), key=lambda kv: (kv[0][1], kv[0][2])):
        if f == file:
            out.append({"위치": f"{s}{row_label(f, r)}" if is_json_file(f) else f"{s} · {r}행", "상태": "삭제된 행", "규칙": rule_label(d.rule_id), "이전": d.before or "",
                        "이후/제안": "", "처리자": _WHO.get(d.decided_by or "", d.decided_by or ""),
                        "_key": ((f, s, r, ""), -1)})
    return out


_COUNT_LABELS = (("auto", "자동"), ("auto_pending", "예정"), ("operator", "담당"), ("request", "요청"),
                 ("approval", "대기"), ("undecided", "미결정"), ("rejected", "거절"), ("deleted", "삭제"))


def file_counts_text(counts: dict[str, int]) -> str:
    """파일 목록에 붙는 짧은 상태 건수 — '자동 6 · 담당 2 · 요청 1 · 대기 1' (0건은 생략)."""
    return " · ".join(f"{label} {counts[k]}" for k, label in _COUNT_LABELS if counts.get(k))
