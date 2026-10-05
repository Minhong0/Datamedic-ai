"""변경 내역 뷰어 화면 — 파일 목록·검사 요약·시트·범례·변경 목록·상세·결정 버튼."""
from __future__ import annotations

from typing import Any, Iterable

from engine import fixer
from engine.models import Dataset, Decision
from engine.viewer_marks import ViewerMarks, build_marks
from ui.components.sheet_viewer import (
    STATUS_LABELS, change_rows, check_summary_text, file_counts_text, legend_html, render_sheet_viewer,
)

AGGREGATE_TEMPLATES = ("SUM_LIMIT", "MAX_COUNT", "NEAR_DUPLICATE")
DECIDABLE = ("approval", "undecided")


def explicit_decisions(decisions: dict[str, dict]) -> list[Decision]:
    """화면에서 담당자가 직접 고른 결정(아직 재개 전)을 Decision 목록으로."""
    out = []
    for iid, v in decisions.items():
        if v.get("action") == "edit" and not v.get("value"):
            continue
        out.append(Decision(issue_id=iid, action=v["action"], value=v.get("value"), decided_by="operator"))
    return out


def compute_marks(state: dict, *, finalized: bool,
                  operator_decisions: Iterable[Decision] = ()) -> tuple[ViewerMarks, Dataset]:
    """그래프 상태로부터 표시 정보와 보여 줄 정제본을 계산한다."""
    ds: Dataset = state["dataset"]
    issues = state.get("issues", [])
    if finalized:
        decisions = state.get("decisions", [])
        changes = state.get("changes", [])
        cleaned = state.get("cleaned") or ds
    else:
        decisions = list(operator_decisions)
        cleaned, changes = fixer.apply(ds, issues, decisions)
    results = [r for r in state.get("agg_results", []) if not r.get("skipped")]
    executed = {r["spec"].template for r in results
                if r.get("spec") is not None and r["spec"].template in AGGREGATE_TEMPLATES}
    groups = [(r["capability"], g) for r in results for g in r.get("group_issues", [])]
    row_index = {k: df["_row"].astype(int).tolist() for k, df in ds.tables.items()}
    marks = build_marks(
        issues, decisions, changes, executed, finalized=finalized, group_issues=groups,
        group_dispositions=state.get("agg_dispositions"), aggregate_basis=state.get("aggregate_basis"),
        row_index=row_index,
    )
    return marks, cleaned


def set_decision(issue_id: str, action: str, value: str | None = None) -> None:
    """뷰어의 결정 버튼 콜백 — ③ 검토 화면의 위젯과 같은 값을 쓰도록 세션 상태를 맞춘다."""
    import streamlit as st

    st.session_state[f"act_{issue_id}"] = action
    if value is not None:
        st.session_state[f"ev_{issue_id}"] = value
    decisions = dict(st.session_state.get("viewer_decisions", {}))
    decisions[issue_id] = {"action": action, "value": value}
    st.session_state["viewer_decisions"] = decisions
    st.session_state.pop("viewer_edit_error", None)


def apply_direct_edit(issue: Any, input_key: str) -> None:
    """'직접 수정 적용' 콜백 — 규칙으로 검증해 맞을 때만 결정으로 기록한다 (틀리면 이유를 남긴다)."""
    import streamlit as st

    value, error = fixer.validate_edit(issue, st.session_state.get(input_key, ""))
    if error:
        st.session_state["viewer_edit_error"] = error
        return
    set_decision(issue.issue_id, "edit", value)


def render_viewer(state: dict, *, finalized: bool, allow_decisions: bool = False, key: str = "viewer") -> None:
    """파일 목록 → 시트 → 검사 요약 → 표 → 범례 → 변경 목록·상세(+결정 버튼)."""
    import pandas as pd
    import streamlit as st

    from engine.json_io import unsupported_notes

    for note in unsupported_notes(state.get("input_paths", [])):
        st.info(f"표로 표시할 수 없는 구조 — {note} (변경 목록에서만 확인할 수 있습니다)")
    ds = state.get("dataset")
    if ds is None or not ds.tables:
        st.info("표시할 데이터가 없습니다.")
        return
    ops = [] if finalized else explicit_decisions(st.session_state.get("viewer_decisions", {}))
    marks, cleaned = compute_marks(state, finalized=finalized, operator_decisions=ops)

    if marks.aggregates_stale:
        st.warning("담당자 결정이 바뀌어 집계 결과가 오래되었습니다. 집계 결과는 다시 실행해야 최신입니다.")

    files = sorted({f for f, _ in ds.tables})
    left, right = st.columns([1, 4])
    with left:
        _busiest = max(files, key=lambda f: sum(marks.file_counts.get(f, {}).values()))
        file = st.radio("파일", files, index=files.index(_busiest), key=f"{key}_file",
                        format_func=lambda f: f"{f}\n{file_counts_text(marks.file_counts.get(f, {}))}".strip())
    with right:
        sheets = sorted(s for f, s in ds.tables if f == file)
        sheet = st.radio("시트", sheets, horizontal=True, key=f"{key}_sheet_{file}") if len(sheets) > 1 else sheets[0]
        summary = check_summary_text(marks.sheet_checks.get((file, sheet)), stale=marks.aggregates_stale)
        if summary:
            st.markdown(f"**이 파일의 검사 결과** · {summary}")
        render_sheet_viewer(ds, cleaned, marks, file, sheet, key=f"{key}_{file}_{sheet}")
        st.html(legend_html())

        columns = {s: [c for c in df.columns if c != "_row"] for (f, s), df in ds.tables.items() if f == file}
        rows = change_rows(marks, file, columns)
        st.markdown(f"**변경 목록** ({len(rows)}건)")
        if not rows:
            st.caption("이 파일에는 표시할 변경이 없습니다.")
            return
        event = st.dataframe(pd.DataFrame(rows).drop(columns=["_key"]), hide_index=True, use_container_width=True,
                             on_select="rerun", selection_mode="single-row", key=f"{key}_changes_{file}")
        picked = list(event.selection.rows) if event and event.selection else []
        if not picked:
            st.caption("줄을 선택하면 상세와 처리 버튼이 나옵니다.")
            return
        row = rows[picked[0]]
        (cell_key, n) = row["_key"]
        detail = marks.deleted_rows.get(cell_key[:3]) if n == -1 else marks.cells[cell_key].details[n]
        with st.container(border=True):
            st.markdown(f"**위치** {row['위치']} · **상태** {STATUS_LABELS.get(detail.status, detail.status)}")
            from engine.viewer_marks import rule_label
            st.markdown(f"**규칙** {rule_label(detail.rule_id)} · **근거** {detail.reason}")
            st.markdown(f"**이전** `{detail.before or ''}` → **이후/제안** `{detail.after or ''}`"
                        + (f" · **처리자** {row['처리자']}" if row["처리자"] else "")
                        + (f" · **시각** {detail.timestamp:%Y-%m-%d %H:%M:%S}" if detail.timestamp else ""))
            if allow_decisions and not finalized and detail.status in DECIDABLE and detail.issue_id:
                _decision_buttons(state, detail.issue_id, key)


def _decision_buttons(state: dict, issue_id: str, key: str) -> None:
    import streamlit as st

    issue = next((i for i in state.get("issues", []) if i.issue_id == issue_id), None)
    if issue is None:
        return
    st.caption("이 항목을 어떻게 처리할까요? (③ 승인 화면과 같은 결정입니다)")
    c1, c2, c3 = st.columns(3)
    c1.button("✅ 승인", key=f"{key}_ap_{issue_id}", on_click=set_decision, args=(issue_id, "apply"))
    c2.button("↩️ 거절(유지)", key=f"{key}_rj_{issue_id}", on_click=set_decision, args=(issue_id, "reject"))
    c3.button("📩 확인 요청", key=f"{key}_rq_{issue_id}", on_click=set_decision, args=(issue_id, "request"))
    input_key = f"{key}_edit_{issue_id}"
    st.text_input("직접 수정 값", key=input_key, placeholder="규칙에 맞는 값을 입력")
    st.button("✎ 직접 수정 적용", key=f"{key}_ed_{issue_id}", on_click=apply_direct_edit, args=(issue, input_key))
    if st.session_state.get("viewer_edit_error"):
        st.error(st.session_state["viewer_edit_error"])
