"""② 실행 중 실시간 뷰어."""
from __future__ import annotations

import html as _html
from typing import Any

from ui.components.sheet_viewer import ViewOptions, build_sheet_html, file_counts_text, legend_html
from ui.components.viewer_screen import compute_marks

STAGE_TEXT: dict[str, str] = {
    "analyze": "파일을 분석했습니다 — 아래는 읽어 온 데이터입니다",
    "diagnose": "오류를 진단했습니다 — 자동 수정 예정(초록 점선)·승인 대기(노랑)·확인 요청(📩)이 표시됩니다",
    "ask_approval": "담당자 승인을 기다리는 중입니다",
    "treatment": "수정을 적용했습니다 — 자동 수정과 담당자 결정이 반영됩니다",
    "agg_execute": "집계 검사를 실행했습니다 — 묶음 규칙 대상 행에 보라 줄이 표시됩니다",
    "recheck": "재검진을 마쳤습니다",
}
LIVE_NODES = frozenset(STAGE_TEXT)
PREVIEW_ROWS = 8


def build_live_html(state: dict, stage: str, *, max_rows: int = 25, max_tables: int = 6) -> str:
    """지금까지의 상태를 표시하는 HTML (진행 문구 + 표 + 범례). 사업자번호·기업명은 마스킹된 값만 나온다."""
    ds = state["dataset"]
    finalized = state.get("cleaned") is not None
    marks, cleaned = compute_marks(state, finalized=finalized)

    blocks = [f'<div style="padding:6px 10px;margin-bottom:8px;background:#F3F0FF;border-left:3px solid #6E56CF">'
              f"⏳ <b>진행 상황</b> · {_html.escape(stage)}</div>"]
    tables = sorted(ds.tables)
    for (file, sheet) in tables[:max_tables]:
        has_marks = (any(k[0] == file and k[1] == sheet for k in marks.cells)
                     or any(k[0] == file and k[1] == sheet for k in marks.deleted_rows)
                     or any(k[0] == file and k[1] == sheet for k in marks.group_rows))
        opts = ViewOptions(mode="compare", changed_only=has_marks, page_size=max_rows if has_marks else PREVIEW_ROWS)
        table, info = build_sheet_html(ds.tables[(file, sheet)], cleaned.tables.get((file, sheet)), marks, file, sheet, opts)
        counts = file_counts_text(marks.file_counts.get(file, {}))
        note = (f"변경·표시 대상 {info.total_rows}행 중 {info.shown_rows}행" if has_marks
                else f"변경 표시 없음 — 데이터 미리보기 {info.shown_rows}행")
        blocks.append(f'<div style="margin:10px 0 2px"><b>📄 {_html.escape(file)} · {_html.escape(sheet)}</b> '
                      f'<span style="color:#777">{_html.escape(note)}'
                      f'{" · " + _html.escape(counts) if counts else ""}</span></div>{table}')
    if len(tables) > max_tables:
        blocks.append(f'<div style="color:#777">… 표 {len(tables) - max_tables}개는 완료 후 결과 화면에서 볼 수 있습니다.</div>')
    blocks.append(legend_html())
    return "\n".join(blocks)


class LiveViewer:
    """스트림 이벤트를 받아 상태를 쌓고, 의미 있는 노드가 끝날 때마다 target(st.empty() 같은 자리)을 갱신한다."""

    def __init__(self, target: Any) -> None:
        self.target = target
        self.state: dict = {}
        self.updates = 0

    def on_event(self, node: str, out: Any) -> None:
        if not isinstance(out, dict):
            return
        self.state.update(out)
        if node in LIVE_NODES and self.state.get("dataset") is not None:
            self.target.html(build_live_html(self.state, STAGE_TEXT[node]))
            self.updates += 1
