"""변경 내역 뷰어의 색·스타일 상수. 상태 색은 여기서만 정의한다."""
from __future__ import annotations

AUTO_BG = "#EEF8F1"
AUTO_BORDER = "#7BBF8E"
APPROVAL_BG, APPROVAL_FG = "#FFF6D6", "#7A5B00"
OPERATOR_BG, OPERATOR_FG = "#E3EEFB", "#1F4E9A"
MUTED = "#9A9A9A"
GROUP_BAR = "#6E56CF"
REQUEST_BG = "#FDF0DC"

CELL_STYLE: dict[str, tuple[str, str]] = {
    "auto": ("dm-auto", f"background:{AUTO_BG};"),
    "auto_pending": ("dm-autop", f"border:1px dashed {AUTO_BORDER};"),
    "approval": ("dm-appr", f"background:{APPROVAL_BG};color:{APPROVAL_FG};"),
    "undecided": ("dm-und", f"border:1px dashed {MUTED};color:#555;"),
    "operator": ("dm-op", f"background:{OPERATOR_BG};color:{OPERATOR_FG};"),
    "rejected": ("dm-rej", ""),
    "request": ("dm-req", ""),
}

OLD_STYLE = f"color:{MUTED};font-size:0.82em;margin-right:4px;"
REJECTED_VALUE_STYLE = f"color:{MUTED};text-decoration:line-through;margin-left:4px;"
GROUP_ROWHEAD_STYLE = f"box-shadow:inset 3px 0 0 {GROUP_BAR};"
DELETED_ROW_STYLE = f"color:{MUTED};text-decoration:line-through;"

TABLE_CSS = """
<style>
.dm-wrap{overflow-x:auto;max-width:100%}
.dm-table{border-collapse:collapse;font-size:0.9rem;width:100%}
.dm-table th,.dm-table td{border:1px solid #E3E3E3;padding:3px 8px;white-space:nowrap;text-align:left;vertical-align:top}
.dm-table th{background:#F6F6F6;font-weight:600;position:sticky;top:0}
.dm-table td.dm-rowhead,.dm-table th.dm-rowhead{background:#FAFAFA;color:#666;text-align:right;min-width:3.2em}
.dm-ok-mark{color:#2E8B57;font-weight:700;margin-right:3px}
</style>
""".strip()
