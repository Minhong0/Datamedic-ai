"""DataMedic AI — Streamlit 앱 (ReAct Agent).

레이아웃: 단일 화면 — 섹션이 아래로 쌓이는 흐름
  ① 업로드·목표 → 계획 카드 → ② Agent 실행 기록 → ③ 진단·처방·집계 결과 → ④ 결과
  - 되묻기·승인·집계 조건 확인은 모두 본문에서 처리한다 (별도 채팅 패널 없음)
  - 섹션은 사라지지 않고 완료 후 요약으로 축소됨
"""
from __future__ import annotations

import io
import json
import tempfile
import zipfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from langgraph.types import Command

from config import settings
from engine import fixer
from engine.masking import mask_changes, mask_issues
from engine.models import Decision, Issue

st.set_page_config(page_title="DataMedic AI", page_icon="🏥", layout="wide")

st.markdown(
    "<span style='background:#fff3cd;color:#856404;padding:4px 10px;"
    "border-radius:4px;font-size:0.8rem;font-weight:600'>"
    "⚠️ 시연용 가상 데이터</span>",
    unsafe_allow_html=True,
)
st.title("DataMedic AI — 업무 데이터 진단·복구 Agent")


# ── 세션 상태 초기화 ──────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "run_id":          None,
    "state":           None,
    "phase":           "upload",
    "log_messages":    [],
    "goal":            "",
    "sample_mode":     False,
    "manual_standard": None,
    "approval_log":    [],   # 담당자가 검토·결정한 승인 항목 (재개 후에도 유지)
    "previous_runs":   [],   # 이전 목표의 결과 요약 (새 목표 재실행 시 보관)
    "activity_log":    [],   # Agent 진행·판단 기록 (② 실행 기록 영역에만 표시)
    "compiled_graph":  None,
    "run_config":      None,
    "uploaded_paths":  [],   # 이번 실행에 사용된 파일 경로 목록
    "pending_rerun":   False, # 다음 목표 재실행 플래그
    "agg_requests":    [],   # 집계 위반 확인 요청 메일 초안 (RequestItem 목록)
    "export_zips":     None, # 내려받기용 엑셀 ZIP (표시된 것·발표용)
    "viewer_decisions": {},  # 뷰어·승인 화면에서 담당자가 고른 결정 (재개 전, 미리보기용) {issue_id: {action, value}}
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = list(_v) if isinstance(_v, list) else (dict(_v) if isinstance(_v, dict) else _v)


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log_messages.append(f"[{ts}] {msg}")


def _get_dataset():
    s = st.session_state.state
    return s.get("dataset") if s else None


def _agg_rows(res: dict) -> list[dict]:
    """집계 결과의 묶음 목록을 표시용 행으로 바꾼다 (파일·위치 포함, 사업자번호 마스킹)."""
    from agent.nodes.aggregate import group_issue_rows
    return group_issue_rows(res)


def _archive_current_run() -> None:
    """새 목표로 넘어가기 전에 현재 목표의 전체 결과를 보관한다.

    보관본은 화면에 그대로(읽기 전용) 남아, 새 작업이 그 아래로 이어진다.
    표시용 데이터이므로 사업자번호는 마스킹본을 저장한다.
    """
    _s = st.session_state.state or {}
    _plan = _s.get("plan")
    if not _s or _plan is None:
        return
    _rep = _s.get("report")
    st.session_state.previous_runs.append({
        "goal": st.session_state.goal or "(목표 없음)",
        "run_id": st.session_state.run_id,
        "plan": _plan if _plan.status == "ready" else None,
        "issues": mask_issues(_s.get("issues", [])),
        "changes": mask_changes(_s.get("changes", [])),
        "report": _rep,
        "requests": list(_s.get("requests", [])),
        "agg": [{"title": (r["spec"].description if r.get("spec") else "집계 검사"),
                 "text": r.get("response_text", ""), "rows": _agg_rows(r)}
                for r in _s.get("agg_results", [])],
        "approvals": list(st.session_state.approval_log),
        "activity": list(st.session_state.activity_log),
    })


def _issue_rows(issues: list[Issue]) -> list[dict]:
    return [{"ID": i.issue_id, "규칙": i.rule_id, "tier": i.tier,
             "파일": Path(i.ref.file).name if i.ref else "-",
             "위치": i.ref.a1 if i.ref else "-", "값": i.value or "(공백)",
             "오류 내용": i.message, "제안": i.suggestion or ""} for i in issues]


def _render_past_turns() -> None:
    """이전 목표들을 시간순으로 전부 보여준다 (위 = 오래된 것). 새 작업은 그 아래에 이어진다."""
    for _n, _p in enumerate(st.session_state.previous_runs, 1):
        _iss = _p["issues"]
        _rep = _p["report"]
        with st.container(border=True):
            st.markdown(f"### 🎯 목표 {_n}: {_p['goal']}")
            st.caption(f"run_id `{_p['run_id']}` · 완료된 작업 (읽기 전용)")
            if _p["plan"] is not None:
                st.markdown(_plan_card_md(_p["plan"]))
            if _p["activity"]:
                with st.expander("🤖 Agent 실행 기록", expanded=True):
                    for _a in _p["activity"]:
                        st.markdown(_a)
            _m1, _m2, _m3, _m4 = st.columns(4)
            _m1.metric("총 오류", len(_iss))
            _m2.metric("🤖 자동", sum(1 for i in _iss if i.tier == "auto"))
            _m3.metric("👤 승인", sum(1 for i in _iss if i.tier == "approval"))
            _m4.metric("📩 요청", sum(1 for i in _iss if i.tier == "request"))
            _files = sorted({i.ref.file for i in _iss if i.ref})
            if len(_files) > 1:
                with st.expander("파일별 오류 현황", expanded=True):
                    st.dataframe(pd.DataFrame([{
                        "파일": Path(f).name,
                        "총": sum(1 for i in _iss if i.ref and i.ref.file == f),
                        "🤖": sum(1 for i in _iss if i.ref and i.ref.file == f and i.tier == "auto"),
                        "👤": sum(1 for i in _iss if i.ref and i.ref.file == f and i.tier == "approval"),
                        "📩": sum(1 for i in _iss if i.ref and i.ref.file == f and i.tier == "request"),
                    } for f in _files]), use_container_width=True, hide_index=True)
            if _iss:
                with st.expander(f"오류 상세 ({len(_iss)}건)", expanded=False):
                    st.dataframe(pd.DataFrame(_issue_rows(_iss[:300])),
                                 use_container_width=True, hide_index=True)
            if _p["approvals"]:
                with st.expander(f"👤 검토한 승인 항목 ({len(_p['approvals'])}건)", expanded=True):
                    st.dataframe(pd.DataFrame(_p["approvals"]), use_container_width=True, hide_index=True)
            if _p["agg"]:
                st.markdown("**📊 집계 규칙 결과**")
                for _g in _p["agg"]:
                    st.markdown(f"{_g['title']} — {_g['text']}")
                    if _g["rows"]:
                        st.dataframe(pd.DataFrame(_g["rows"]), use_container_width=True, hide_index=True)
            if _rep:
                _r1, _r2, _r3, _r4 = st.columns(4)
                _r1.metric("재검진", "✅ 통과" if _rep.recheck_passed else "❌ 미통과")
                _r2.metric("잔여 오류", len(_rep.remaining_issues))
                _r3.metric("처리 파일", len(_rep.files))
                _r4.metric("총 변경", len(_p["changes"]))
            if _p["changes"]:
                with st.expander(f"변경 이력 ({len(_p['changes'])}건)", expanded=False):
                    st.dataframe(pd.DataFrame([
                        {"ID": c.change_id, "이슈": c.issue_id, "위치": c.ref.a1,
                         "이전": c.before, "이후": c.after, "tier": c.tier, "처리자": c.decided_by}
                        for c in _p["changes"]]), use_container_width=True, hide_index=True)
            for _req in _p["requests"]:
                with st.expander(f"{'✅' if _req.sent else '📧'} 보완 요청 — {_req.org}: {_req.subject}",
                                 expanded=False):
                    st.write(_req.body)
        st.markdown("")


def _add_activity(content: str) -> None:
    """Agent 진행·판단 기록. 채팅 패널이 아닌 ② 실행 기록에만 쌓는다."""
    st.session_state.activity_log.append(content)


def _get_compiled_graph():
    if st.session_state.compiled_graph is None:
        from agent.graph import build_react_graph
        st.session_state.compiled_graph = build_react_graph()
    return st.session_state.compiled_graph


def _persist_dispositions(dispositions: dict[str, str]) -> None:
    """집계 위반 처리 결과(확인 요청/문제 없음)를 그래프 상태에 저장한다 — 뷰어·내보내기가 같은 값을 읽도록."""
    _state = st.session_state.state or {}
    try:
        _get_compiled_graph().update_state(st.session_state.run_config, {"agg_dispositions": dispositions})
    except Exception as _e:   # 화면 동작은 계속하되, 저장 실패는 기록한다
        _log(f"집계 처리 결과 저장 실패: {_e}")
    st.session_state.state = {**_state, "agg_dispositions": dispositions}


def _note_review_decision(issue_id: str) -> None:
    """승인 화면에서 담당자가 직접 고른 결정을 기록한다 — 뷰어 미리보기가 같은 선택을 보여 주도록."""
    _act = st.session_state.get(f"act_{issue_id}")
    _val = (st.session_state.get(f"ev_{issue_id}") or None) if _act == "edit" else None
    _d = dict(st.session_state.get("viewer_decisions", {}))
    _d[issue_id] = {"action": _act, "value": _val}
    st.session_state["viewer_decisions"] = _d


def _proactive_summary(issues: list[Issue], n_files: int) -> str:
    n      = len(issues)
    auto_c = sum(1 for i in issues if i.tier == "auto")
    appr_c = sum(1 for i in issues if i.tier == "approval")
    req_c  = sum(1 for i in issues if i.tier == "request")
    top    = Counter(i.rule_id for i in issues).most_common(1)
    top_str = f"\n주요 규칙: **{top[0][0]}** ({top[0][1]}건)" if top else ""
    if n == 0:
        return f"파일 **{n_files}개** 분석 완료 — 오류가 발견되지 않았습니다. 🎉"
    return (
        f"파일 **{n_files}개** 분석 완료 — 총 **{n}건** 발견했습니다.\n\n"
        f"| 처리 방식 | 건수 |\n|---|---|\n"
        f"| 🤖 자동 수정 | {auto_c} |\n"
        f"| 👤 승인 필요 | {appr_c} |\n"
        f"| 📩 보완 요청 | {req_c} |"
        f"{top_str}"
    )


def _plan_card_md(plan) -> str:
    """계획 카드: 실행·선행·생략(이유)을 보여준다."""
    from agent.nodes.query import interpret_goal
    from engine.planning import plan_source_label

    if plan.status == "needs_clarification":
        return f"❓ **계획 확인 필요** — {plan.question}"
    _icon = {"run": "▶️ 실행", "dependency": "➕ 선행 작업", "skipped": "⏭️ 생략"}
    _lines = [f"📋 **실행 계획** — 목표: *{plan.goal or '(없음: 기본 전체 점검)'}*",
              f"🧭 계획 해석 방식: {plan_source_label(plan.source)}", ""]
    for _s in plan.steps:
        _tail = f"생략({_s.reason})" if _s.status == "skipped" else _s.reason
        _lines.append(f"- {_icon[_s.status]} **{_s.label}** — {_tail}")
        if _s.status == "run" and _s.capability in ("sum_limit", "count_limit", "near_duplicate"):
            _lines[-1] += " (실행 중 기준을 확인합니다)"
    _reads = interpret_goal(plan.goal, plan) if plan.goal else []
    if _reads:
        _lines += ["", "🔎 **이렇게 이해했어요** (문장에서 읽은 값 · 근거 — 실행 전 확인 카드에서 다시 확인합니다)"]
        for _r in _reads:
            _vals = " · ".join(f"{_k} {_v}" + (f" (“{_q}”)" if _q else "") for _k, _v, _q in _r["read"]) or "읽은 값 없음"
            _ask = f" — 확인할 것: {', '.join(_r['ask'])}" if _r["ask"] else ""
            _lines.append(f"- {_r['label']}: {_vals}{_ask}")
    if plan.unhandled:
        _lines += ["", "⚠️ **이번 점검에서 제외된 조건** (해석할 수 없어 확인을 거쳐 제외했습니다)"]
        _lines += [f"- “{_c}”" for _c in plan.unhandled]
        _lines.append("(위 조건은 검사하지 않습니다. 필요하면 목표를 다시 써 주세요.)")
    return "\n".join(_lines)


_CLARIFY_TYPES = ("clarification_required", "aggregate_clarify")


def _pending_interrupt() -> dict | None:
    """그래프가 멈춰 있는 interrupt payload (되묻기·집계 확인·승인)를 반환한다."""
    _cfg = st.session_state.run_config
    if not _cfg:
        return None
    _snap = _get_compiled_graph().get_state(_cfg)
    for _t in getattr(_snap, "tasks", ()) or ():
        for _it in getattr(_t, "interrupts", ()) or ():
            _v = getattr(_it, "value", None)
            if isinstance(_v, dict) and "type" in _v:
                return _v
    return None


def _resume_agent(resume_value, label: str) -> None:
    """interrupt 로 멈춘 그래프를 재개하고 세션 상태를 갱신한다."""
    _compiled = _get_compiled_graph()
    _cfg = st.session_state.run_config
    from ui.components.live_viewer import LiveViewer
    _live = LiveViewer(st.empty())
    _live.state.update(st.session_state.state or {})     # 재개 전까지의 상태에서 이어서 보여 준다
    with st.status(label, expanded=True) as _st:
        _stream_events(
            _compiled.stream(Command(resume=resume_value), _cfg, stream_mode="updates"),
            status_widget=_st, live=_live,
        )
    _after_run(_compiled.get_state(_cfg), st.session_state.uploaded_paths)


def _after_run(_snapshot, paths: list[str]) -> None:
    """그래프 실행/재개 후 phase 를 결정한다."""
    st.session_state.state       = dict(_snapshot.values)
    st.session_state.sample_mode = False
    _pending = _pending_interrupt() if _snapshot.next else None
    _ptype = (_pending or {}).get("type")
    if _ptype in _CLARIFY_TYPES:
        st.session_state.phase = "clarify"
    elif _ptype == "aggregate_confirm":
        st.session_state.phase = "agg_confirm"
    elif _snapshot.next:
        _issues  = _snapshot.values.get("issues", [])
        _n_files = len(set(i.ref.file for i in _issues if i.ref) | set(paths))
        _add_activity(_proactive_summary(_issues, _n_files))
        _appr_n  = sum(1 for i in _issues if i.tier == "approval")
        _add_activity(
            f"⏸️ **승인 필요 항목 {_appr_n}건**이 있습니다. "
            "아래에서 각 항목을 검토한 뒤 '승인 후 재개'를 눌러주세요."
        )
        st.session_state.phase = "diagnosed"
    else:
        _add_activity("🎉 Agent가 모든 처리를 완료했습니다. 아래 결과를 확인하세요.")
        st.session_state.phase = "done"


def _stream_events(event_iter, status_widget=None, live=None) -> None:
    """LangGraph 스트림 이벤트를 처리하며 화면과 채팅 히스토리에 기록한다. live 가 있으면 실시간 뷰어도 갱신한다."""
    for _event in event_iter:
        for _node, _out in _event.items():
            if live is not None:
                try:
                    live.on_event(_node, _out)       # 노드가 끝날 때마다 파일 현황 표 갱신
                except Exception as _e:               # 표시 실패가 에이전트 실행을 막으면 안 된다
                    _log(f"실시간 뷰어 갱신 실패: {_e}")
            if _node == "planner":
                _reason = _out.get("agent_reason", "")
                _action = _out.get("agent_action", "")
                if _reason:
                    st.markdown(f"🤔 **{_reason}**")
                    _add_activity(f"🤔 {_reason}")
                    _log(f"planner → {_action}")
            elif _node == "plan":
                _plan = _out.get("plan")
                if _plan is not None:
                    _msg = _plan_card_md(_plan)
                    st.markdown(_msg); _log(f"plan → {_plan.status}")
            elif _node == "analyze":
                _n   = len(_out.get("profiles", []))
                _msg = f"✅ 파일 분석 완료 — {_n}개 시트"
                st.markdown(_msg); _add_activity(_msg); _log(_msg)
            elif _node == "diagnose":
                _n   = len(_out.get("issues", []))
                _msg = f"✅ 진단 완료 — {_n}건 발견"
                st.markdown(_msg); _add_activity(_msg); _log(_msg)
            elif _node == "treatment":
                _n   = len(_out.get("changes", []))
                _msg = f"✅ 수정 완료 — {_n}건 변경"
                st.markdown(_msg); _add_activity(_msg); _log(_msg)
            elif _node == "recheck":
                _rpt = _out.get("report")
                if _rpt:
                    _msg = (
                        f"✅ 재검진 {'통과' if _rpt.recheck_passed else '미통과'}"
                        f" — 잔여 오류 {len(_rpt.remaining_issues)}건"
                    )
                    st.markdown(_msg); _add_activity(_msg); _log(_msg)
    if status_widget:
        status_widget.update(label="완료 ✅", state="complete")


def _run_agent(paths: list[str]) -> None:
    """주어진 파일 목록과 현재 세션 goal로 Agent를 실행하고 상태를 갱신한다."""
    _run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    _config  = {"configurable": {"thread_id": _run_id}}
    st.session_state.run_id         = _run_id
    st.session_state.run_config     = _config
    st.session_state.uploaded_paths = paths
    st.session_state.activity_log   = []
    st.session_state.approval_log   = []
    st.session_state.agg_requests   = []
    st.session_state.viewer_decisions = {}
    st.session_state.export_zips      = None
    _log(f"Agent 시작: run_id={_run_id}, 목표={st.session_state.goal!r}")

    _initial = {
        "run_id":           _run_id,
        "input_paths":      paths,
        "goal":             st.session_state.goal,
        "decisions":        [],
        "agent_phase":      "init",
        "agent_loop_count": 0,
    }
    _compiled = _get_compiled_graph()
    from ui.components.live_viewer import LiveViewer
    _live = LiveViewer(st.empty())
    with st.status("🤖 Agent 실행 중...", expanded=True) as _st:
        _stream_events(
            _compiled.stream(_initial, _config, stream_mode="updates"),
            status_widget=_st, live=_live,
        )

    _after_run(_compiled.get_state(_config), paths)


# ── 글로벌 데이터 ─────────────────────────────────────────────────────────────

_outputs_dir = settings.outputs_dir
_past_runs: list[str] = []
if _outputs_dir.exists():
    _past_runs = sorted(
        [d.name for d in _outputs_dir.iterdir()
         if d.is_dir() and not d.name.startswith(".")],
        reverse=True,
    )[:20]

_sample_files: list[Path] = []
if settings.samples_dir.exists():
    _sample_files = (
        sorted(settings.samples_dir.glob("*.xlsx")) +
        sorted(settings.samples_dir.glob("*.json"))
    )


# ── 사이드바 ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("🏥 DataMedic AI")
    if st.session_state.run_id:
        st.info(f"run_id: `{st.session_state.run_id}`")
    if st.button("🔄 초기화", use_container_width=True):
        for _k, _v in _DEFAULTS.items():
            st.session_state[_k] = list(_v) if isinstance(_v, list) else (dict(_v) if isinstance(_v, dict) else _v)
        st.rerun()
    st.divider()
    st.subheader("📋 실행 로그")
    _logs = st.session_state.log_messages
    st.code("\n".join(reversed(_logs[-30:])) if _logs else "로그 없음", language=None)
    st.divider()
    st.subheader("🗂 실행 이력")
    if _past_runs:
        _sel = st.selectbox("과거 실행 불러오기", ["(현재 세션)"] + _past_runs, key="hist_sel")
        if _sel != "(현재 세션)" and st.button("불러오기"):
            _rpath = _outputs_dir / _sel / "report.json"
            if _rpath.exists():
                from engine.models import RunReport
                _rpt = RunReport(**json.loads(_rpath.read_text(encoding="utf-8")))
                st.session_state.update({
                    "run_id": _sel, "phase": "done",
                    "state": {"run_id": _sel, "input_paths": _rpt.files,
                              "issues": [], "decisions": [], "changes": [],
                              "requests": [], "report": _rpt},
                })
                _log(f"이력 불러옴: {_sel}")
                st.rerun()
    else:
        st.caption("이전 실행 없음")


# ════════════════════════════════════════════════════════════════════════════
# 전체 레이아웃 — 단일 화면
# ════════════════════════════════════════════════════════════════════════════

with st.container():

    _render_past_turns()   # 이전 목표는 위에 그대로 두고, 새 작업은 아래로 이어진다

    # ═══════════════════════════════════════════════════════════════════════
    # ① 업로드 / 완료 요약  — 항상 표시, 실행 후 요약으로 전환
    # ═══════════════════════════════════════════════════════════════════════

    if st.session_state.phase == "upload":
        # 이전 실행 완료 후 새 목표로 즉시 재실행
        if st.session_state.get("pending_rerun") and st.session_state.uploaded_paths:
            st.session_state.pending_rerun = False
            _add_activity(f"🎯 새 목표로 재실행합니다: **{st.session_state.goal}**")
            _run_agent(st.session_state.uploaded_paths)
            st.rerun()

        with st.container(border=True):
            st.subheader("① 파일 업로드 · 목표 설정")

            _EXAMPLE_GOALS = [
                "경남 20개 시군 사업실적 취합 전 오류 점검",
                "분기 지원금액 합계 검증 및 중복 행 제거",
                "기관별 엑셀 형식 오류·누락값 일괄 정리",
            ]
            _goal = st.text_area(
                "진단 목표", value=st.session_state.goal, height=72,
                placeholder="예: 경남 20개 시군 사업실적 취합 전 오류 점검",
                label_visibility="visible",
            )
            st.session_state.goal = _goal

            _eg_cols = st.columns(3)
            for _idx, _eg in enumerate(_EXAMPLE_GOALS):
                if _eg_cols[_idx].button(_eg, key=f"eg_{_idx}", use_container_width=True):
                    st.session_state.goal = _eg
                    # st.rerun() 제거: 버튼 클릭이 이미 rerun을 유발하므로
                    # 명시적 st.rerun() 은 file_uploader 상태를 초기화시켜 버튼을 비활성화함

            _uploaded = st.file_uploader(
                "엑셀(.xlsx) 또는 JSON (여러 개 가능)",
                type=["xlsx", "json"], accept_multiple_files=True,
            )
            if _sample_files:
                if st.session_state.sample_mode:
                    st.success(f"샘플 파일 {st.session_state.get('sample_count', 5)}개 선택됨")
                    if st.button("샘플 선택 취소"):
                        st.session_state.sample_mode = False; st.rerun()
                else:
                    _sc1, _sc2 = st.columns([2, 1])
                    if _sc1.button("📂 샘플 데이터 불러오기", use_container_width=True):
                        st.session_state.sample_mode  = True
                        st.session_state.sample_count = st.session_state.get("sample_n_sel", 5)
                        st.rerun()
                    _sc2.number_input(
                        "개수", min_value=1, max_value=len(_sample_files),
                        value=min(5, len(_sample_files)), key="sample_n_sel",
                        label_visibility="collapsed",
                    )

            with st.expander("⚙️ 고급 설정"):
                _std_opts = ["자동 인식"]
                if settings.standard_dir.exists():
                    _std_opts += [f.stem for f in settings.standard_dir.glob("*.yaml")]
                _manual = st.selectbox("표준 서식 수동 선택", _std_opts)
                st.session_state.manual_standard = None if _manual == "자동 인식" else _manual

            _has_input = bool(_uploaded) or st.session_state.sample_mode
            if st.button("🚀 Agent 실행", disabled=not _has_input, type="primary",
                         use_container_width=True):
                _tmp = Path(tempfile.mkdtemp())
                if st.session_state.sample_mode:
                    _n     = st.session_state.get("sample_count", 5)
                    _paths = [str(f) for f in _sample_files[:_n] if not f.name.startswith("~$")]
                    _log(f"샘플 파일 {len(_paths)}개")
                else:
                    _paths = []
                    for _f in _uploaded:
                        _dest = _tmp / _f.name
                        _dest.write_bytes(_f.read())
                        _paths.append(str(_dest))
                    _log(f"업로드 파일 {len(_paths)}개")
                _run_agent(_paths)
                # 채팅 패널(계획 연동 예시 질문)은 본문보다 먼저 그려지므로 실행 후 다시 그린다
                st.rerun()

    else:
        if st.session_state.previous_runs:
            st.markdown(f"### 🎯 목표 {len(st.session_state.previous_runs) + 1}: "
                        f"{st.session_state.goal or '(목표 없음)'}")
        # 실행 완료 후 컴팩트 요약
        _s = st.session_state.state or {}
        _issues_all: list[Issue] = mask_issues(_s.get("issues", []))
        _auto_n  = sum(1 for i in _issues_all if i.tier == "auto")
        _appr_n  = sum(1 for i in _issues_all if i.tier == "approval")
        _req_n   = sum(1 for i in _issues_all if i.tier == "request")
        with st.container(border=True):
            _ca, _cb, _cc, _cd = st.columns([3, 1, 1, 1])
            _ca.markdown(
                f"**① 분석 완료** &nbsp;|&nbsp; "
                f"목표: *{st.session_state.goal or '(없음)'}* &nbsp;|&nbsp; "
                f"`{st.session_state.run_id}`"
            )
            _cb.metric("총 오류",    len(_issues_all))
            _cc.metric("🤖 자동",    _auto_n)
            _cd.metric("👤 승인",    _appr_n)

    # ═══════════════════════════════════════════════════════════════════════
    # ② Agent 실행 기록 — 스트리밍 후 쌓인 메시지 표시
    # ═══════════════════════════════════════════════════════════════════════

    if st.session_state.phase != "upload":
        # 계획 카드 — 실행·선행·생략(이유)을 본문에서 한 번만 보여준다
        _plan_now = (st.session_state.state or {}).get("plan")
        if _plan_now is not None and _plan_now.status == "ready":
            with st.container(border=True):
                st.markdown(_plan_card_md(_plan_now))

        _agent_msgs = st.session_state.activity_log
        if _agent_msgs:
            with st.expander("② 🤖 Agent 실행 기록", expanded=True):
                for _msg in _agent_msgs:
                    st.markdown(_msg)
                    st.divider()

    # ═══════════════════════════════════════════════════════════════════════
    # ②-b 되묻기 — 계획이 모호하거나 비어 있을 때 (interrupt)
    # ═══════════════════════════════════════════════════════════════════════

    if st.session_state.phase == "clarify":
        _clar = _pending_interrupt() or {}
        with st.container(border=True):
            if _clar.get("type") == "aggregate_clarify":
                st.subheader("❓ 집계 규칙 조건 확인")
                st.markdown(_clar.get("question", "조건을 알려주세요."))
                _opts = _clar.get("options", [])
                _ans = None
                if _opts:
                    _labels = {o["id"]: o["label"] for o in _opts}
                    _ans = st.radio("선택", list(_labels), format_func=lambda k: _labels[k],
                                    key="agg_clarify_radio")
                else:
                    _ans = st.text_input("답변", key="agg_clarify_text",
                                         placeholder="예: 사업별로, 5천만원")
                if st.button("✅ 답변 전송", type="primary", use_container_width=True,
                             disabled=not (_ans and str(_ans).strip()), key="agg_clarify_send"):
                    _resume_agent(str(_ans).strip(), "🤖 집계 조건 반영 중...")
                    st.rerun()
            else:
                st.subheader("❓ 계획 확인")
                st.markdown(_clar.get("question", "실행할 검사를 선택해 주세요."))
                _opts = _clar.get("options", [])
                _labels = {o["id"]: o["label"] for o in _opts}
                if any(k.startswith("skip::") for k in _labels):
                    # 해석하지 못한 조건 — 건너뛰지 않고 하나를 골라 처리 방법을 정한다
                    _one = st.radio("처리 방법", list(_labels), format_func=lambda k: _labels[k],
                                    key="clarify_unhandled_pick")
                    _picked = [_one] if _one else []
                else:
                    _picked = st.multiselect(
                        "실행할 검사", list(_labels), format_func=lambda k: _labels[k], key="clarify_pick",
                    )
                if st.button("✅ 선택 완료 — 계획 확정", type="primary",
                             disabled=not _picked, use_container_width=True):
                    _resume_agent(_picked, "🤖 계획 확정 후 Agent 실행 중...")
                    st.rerun()

    if st.session_state.phase == "agg_confirm":
        _conf = _pending_interrupt() or {}
        with st.container(border=True):
            st.subheader("✅ 이 조건으로 집계 검사를 실행할까요?")
            for _k, _v in _conf.get("card", {}).items():
                st.markdown(f"**{_k}**: {_v}")
            _fix = st.text_input("해석이 다르면 고칠 내용을 적어 주세요", key="agg_edit_text",
                                 placeholder="예: 한도는 7천만원, 합산으로 / 시군 안에서만")
            if st.button("✏️ 수정 반영", disabled=not (_fix and _fix.strip()), key="agg_edit"):
                _resume_agent(f"edit:{_fix.strip()}", "🤖 수정한 조건으로 다시 해석 중...")
                st.rerun()
            _c1, _c2 = st.columns(2)
            if _c1.button("🔍 실행", type="primary", use_container_width=True, key="agg_run"):
                _resume_agent("run", "🤖 집계 규칙 실행 중...")
                st.rerun()
            if _c2.button("⏭️ 건너뛰기", use_container_width=True, key="agg_skip"):
                _resume_agent("skip", "🤖 집계 검사를 건너뜁니다...")
                st.rerun()

    # ═══════════════════════════════════════════════════════════════════════
    # ③ 진단 결과 + 승인 처방  — approval interrupt 발생 시
    # ═══════════════════════════════════════════════════════════════════════

    if st.session_state.phase in ("diagnosed", "done"):
        _state: dict  = st.session_state.state or {}
        _issues: list[Issue] = mask_issues(_state.get("issues", []))   # 화면 표시용 (사업자번호 마스킹)
        _auto_cnt = sum(1 for i in _issues if i.tier == "auto")
        _appr_cnt = sum(1 for i in _issues if i.tier == "approval")
        _req_cnt  = sum(1 for i in _issues if i.tier == "request")

        with st.container(border=True):
            _analysis: dict = _state.get("analysis_summary", {})
            if _analysis and _analysis.get("headline"):
                st.markdown(f"### ③ 🤖 {_analysis['headline']}")
                for _f in _analysis.get("findings", []):
                    st.markdown(f"- {_f}")
                if _analysis.get("recommendation"):
                    st.info(f"💡 **권고**: {_analysis['recommendation']}")
            else:
                st.markdown("### ③ Agent 진단 요약")
                if _issues:
                    st.markdown(
                        f"총 **{len(_issues)}건** — "
                        f"자동 {_auto_cnt} / 승인 {_appr_cnt} / 요청 {_req_cnt}"
                    )
                else:
                    st.success("오류가 발견되지 않았습니다.")

            _cc1, _cc2, _cc3, _cc4 = st.columns(4)
            _cc1.metric("총 오류",     len(_issues))
            _cc2.metric("🤖 자동",     _auto_cnt)
            _cc3.metric("👤 승인 필요", _appr_cnt)
            _cc4.metric("📩 보완 요청", _req_cnt)

        # 파일별 현황
        _file_names = sorted({i.ref.file for i in _issues if i.ref})
        if len(_file_names) > 1:
            with st.expander("파일별 오류 현황", expanded=True):
                _rows = []
                for _fn in _file_names:
                    _fi = [i for i in _issues if i.ref and i.ref.file == _fn]
                    _rows.append({
                        "파일": Path(_fn).name, "총": len(_fi),
                        "🤖": sum(1 for i in _fi if i.tier == "auto"),
                        "👤": sum(1 for i in _fi if i.tier == "approval"),
                        "📩": sum(1 for i in _fi if i.tier == "request"),
                    })
                st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)

        # 오류 상세
        if _issues:
            with st.expander("오류 상세 보기"):
                _rule_c = pd.Series([i.rule_id for i in _issues]).value_counts()
                if not _rule_c.empty:
                    st.bar_chart(_rule_c)
                _all_t = sorted({i.tier    for i in _issues})
                _all_r = sorted({i.rule_id for i in _issues})
                _f1, _f2 = st.columns(2)
                _st_f = _f1.multiselect("tier", _all_t, default=_all_t, key="f_tier")
                _sr_f = _f2.multiselect("규칙",  _all_r, default=_all_r, key="f_rule")
                _flt = [i for i in _issues if i.tier in _st_f and i.rule_id in _sr_f]
                _PAGE = 50
                _tp = max(1, (len(_flt) + _PAGE - 1) // _PAGE)
                _pg = st.number_input("페이지", 1, _tp, 1, key="diag_pg")
                _s2 = (_pg - 1) * _PAGE
                st.caption(f"총 {len(_flt)}건 · {_pg}/{_tp} 페이지")
                st.dataframe(pd.DataFrame([
                    {"ID": i.issue_id, "규칙": i.rule_id, "tier": i.tier,
                     "파일": Path(i.ref.file).name if i.ref else "-",
                     "위치": i.ref.a1 if i.ref else "-",
                     "값": i.value or "(공백)",
                     "오류 내용": i.message,
                     "제안": i.suggestion or ""}
                    for i in _flt[_s2: _s2 + _PAGE]
                ]), use_container_width=True)

        # 승인 요청 UI (diagnosed 상태일 때만 활성)
        _appr_issues = [i for i in _issues if i.tier == "approval"]
        _plan = _state.get("plan")
        _del_intent = bool(_plan and _plan.delete_intent)
        # 같은 파일 안 완전 중복 행 — 목표에 삭제 의도가 있을 때만 일괄 삭제 버튼 대상
        _bulk_dups = [
            i for i in _appr_issues
            if i.rule_id == "DUPLICATE_ROW" and i.value == "(완전중복)"
        ] if _del_intent else []
        _bulk_ids = {i.issue_id for i in _bulk_dups}
        if _appr_issues and st.session_state.phase == "diagnosed":
            with st.container(border=True):
                st.subheader(f"👤 승인 필요 항목 — {len(_appr_issues)}건")
                st.caption("Agent가 스스로 처리하기 어려운 항목입니다. 각 항목을 검토해 주세요.")

                _dec_map: dict[str, dict] = {}
                for _i in _appr_issues:
                    if _i.issue_id in _bulk_ids:
                        continue
                    _loc = f"{Path(_i.ref.file).name} / {_i.ref.a1}" if _i.ref else "-"
                    with st.expander(f"{_i.issue_id} — {_i.rule_id} @ {_loc}"):
                        st.write(f"**현재 값**: `{_i.value if _i.value is not None else '(공백)'}`")
                        st.write(f"**오류 내용**: {_i.message}")
                        if _i.suggestion:
                            st.write(f"**제안**: `{_i.suggestion}`")
                        # 중복 행은 삭제이므로 기본값을 reject(유지)로 둔다
                        _act = st.radio(
                            "처리", ["apply", "reject", "request", "edit"],
                            format_func=lambda k: {"apply": "승인(적용)", "reject": "거절(유지)",
                                                   "request": "확인 요청", "edit": "직접 수정"}[k],
                            index=1 if _i.rule_id == "DUPLICATE_ROW" else 0,
                            key=f"act_{_i.issue_id}", horizontal=True,
                            on_change=_note_review_decision, args=(_i.issue_id,),
                        )
                        _ev = (st.text_input("직접 입력", key=f"ev_{_i.issue_id}",
                                             on_change=_note_review_decision, args=(_i.issue_id,))
                               if _act == "edit" else None)
                        _dec_map[_i.issue_id] = {"action": _act, "value": _ev}

                def _remember_approvals(ds: list[Decision]) -> None:
                    """재개 후에도 검토 내용이 남도록 결정 내역을 보관한다 (화면용 마스킹본)."""
                    _by = {i.issue_id: i for i in _appr_issues}
                    st.session_state.approval_log = [{
                        "ID": d.issue_id, "규칙": _by[d.issue_id].rule_id,
                        "위치": _by[d.issue_id].ref.a1 if _by[d.issue_id].ref else "-",
                        "값": _by[d.issue_id].value or "(공백)",
                        "오류 내용": _by[d.issue_id].message,
                        "결정": {"apply": "승인(적용)", "reject": "거절(유지)", "edit": "직접 수정",
                                 "request": "보완 요청"}.get(d.action, d.action),
                        "입력값": d.value or "",
                    } for d in ds if d.issue_id in _by]

                def _human_decisions() -> list[Decision]:
                    _ds = [
                        Decision(issue_id=iid, action=v["action"],
                                 value=v.get("value"), decided_by="operator")
                        for iid, v in _dec_map.items()
                    ]
                    # 직접 수정 값은 해당 규칙으로 검증하고 표준 형식으로 저장한다 (틀리면 아래에서 막는다)
                    _by_id = {i.issue_id: i for i in _state.get("issues", [])}
                    for _d in _ds:
                        if _d.action == "edit" and _d.issue_id in _by_id:
                            _norm, _err = fixer.validate_edit(_by_id[_d.issue_id], _d.value or "")
                            if not _err:
                                _d.value = _norm
                    if _bulk_ids and st.session_state.get("_bulk_delete_pressed"):
                        _ds += [Decision(issue_id=iid, action="apply", decided_by="operator")
                                for iid in _bulk_ids]
                    return _ds

                if _bulk_dups:
                    st.warning(
                        f"같은 파일 안 완전 중복 행 **{len(_bulk_dups)}건** — 파일 간 중복은 "
                        "위에서 개별 확인합니다. 삭제는 행마다 변경 이력(Change)이 남습니다."
                    )
                    if st.button(f"🗑 중복 행 {len(_bulk_dups)}건 일괄 삭제", type="primary",
                                 use_container_width=True, key="bulk_dup_delete"):
                        st.session_state["_bulk_delete_pressed"] = True
                        _remember_approvals(_human_decisions())
                        _resume_agent([d.model_dump() for d in _human_decisions()],
                                      "🤖 중복 행 삭제 후 Agent 재개 중...")
                        st.session_state["_bulk_delete_pressed"] = False
                        st.rerun()

                def _edit_errors(ds: list[Decision]) -> list[str]:
                    """직접 수정 값이 규칙에 맞지 않으면 이유를 모아 돌려준다 — 틀린 값으로 재개하지 않는다."""
                    _by = {i.issue_id: i for i in _appr_issues}
                    _out = []
                    for d in ds:
                        if d.action == "edit" and d.issue_id in _by:
                            _e = fixer.validate_edit(_by[d.issue_id], d.value or "")[1]
                            if _e:
                                _out.append(f"{d.issue_id}: {_e}")
                    return _out

                if st.button("✅ 승인 후 Agent 실행 재개", type="primary", use_container_width=True):
                    _now = _human_decisions()
                    _errs = _edit_errors(_now)
                    if _errs:
                        for _e in _errs:
                            st.error(_e)
                    else:
                        _remember_approvals(_now)
                        _resume_agent([d.model_dump() for d in _now], "🤖 Agent 재개 중...")
                    # st.rerun() 없이 계속 진행 → ④ 결과 섹션이 바로 렌더링됨

        # 파일로 보기 — 변경 내역 뷰어. 고른 결정은 아직 적용 전이라 미리보기로 보여 준다.
        if st.session_state.phase == "diagnosed" and _state.get("dataset") is not None:
            with st.expander("📂 파일로 보기 (엑셀처럼 셀 단위로 확인하고 바로 처리)", expanded=False):
                from ui.components.viewer_screen import render_viewer
                render_viewer(_state, finalized=False, allow_decisions=True, key="viewer_review")

        if _appr_issues and st.session_state.phase == "done":
            # 승인 완료 요약
            _changes = mask_changes((st.session_state.state or {}).get("changes", []))
            _op_n = sum(1 for c in _changes if c.decided_by == "operator")
            st.success(f"✅ 승인 완료 — 담당자 결정 {_op_n}건 적용됨")
            if st.session_state.approval_log:
                with st.expander(f"👤 검토한 승인 항목 ({len(st.session_state.approval_log)}건)", expanded=True):
                    st.dataframe(pd.DataFrame(st.session_state.approval_log),
                                 use_container_width=True, hide_index=True)

    # ═══════════════════════════════════════════════════════════════════════
    # ③-b 집계 규칙 결과 (R5~R7) — Agent 가 그래프 안에서 실행한 결과
    # ═══════════════════════════════════════════════════════════════════════

    if st.session_state.phase == "done":
        _agg_results = (st.session_state.state or {}).get("agg_results", [])
        if _agg_results:
            with st.container(border=True):
                st.subheader("📊 집계 규칙 결과")
                from agent.nodes.aggregate_requests import (
                    DISP_REQUEST, OK, REQUEST, build_aggregate_requests, collect_group_issues,
                    disposition_to_label, issue_key, label_to_disposition, ok_keys,
                )
                _stored_disp = (st.session_state.state or {}).get("agg_dispositions") or {}
                _new_disp: dict[str, str] = {}
                for _ri, _res in enumerate(_agg_results):
                    _spec = _res.get("spec")
                    _title = _spec.description if _spec else "집계 검사"
                    st.markdown(f"**{_title}**")
                    st.write(_res.get("response_text", ""))
                    _gi = _res.get("group_issues", [])
                    if _gi:
                        # 위반 묶음마다 처리 선택: 기본은 기관에 확인 요청, 확인해 보니 정상이면 '문제 없음'
                        _df = pd.DataFrame(_agg_rows(_res))
                        _df.insert(0, "처리", [disposition_to_label(_stored_disp.get(issue_key(_res["capability"], _g)))
                                              for _g in _gi])
                        _edited = st.data_editor(
                            _df, hide_index=True, use_container_width=True, key=f"agg_edit_{_ri}",
                            column_config={"처리": st.column_config.SelectboxColumn(
                                "처리", options=[REQUEST, OK], required=True,
                                help="확인 요청: 해당 기관에 확인(소명)을 요청 / 문제 없음: 확인해 보니 정상")},
                            disabled=[c for c in _df.columns if c != "처리"],
                        )
                        for _pos, _g in enumerate(_gi):
                            _new_disp[issue_key(_res["capability"], _g)] = label_to_disposition(_edited.iloc[_pos]["처리"])
                    if _spec and st.button("💾 이 기준 저장", key=f"agg_save_{_ri}"):
                        from agent.nodes.query import save_rule_node
                        _saved = save_rule_node({"confirmed_spec": _spec})
                        _add_activity("✅ 집계 기준 저장 완료." if _saved.get("saved") else "기준 저장 실패")
                        st.success("기준을 저장했습니다." if _saved.get("saved") else "저장 실패")
                    st.divider()

                if _new_disp != {_k: _stored_disp.get(_k, DISP_REQUEST) for _k in _new_disp}:
                    _persist_dispositions(_new_disp)             # 바뀐 처리 결과만 그래프 상태에 저장
                _ok_keys = ok_keys(_new_disp)
                if collect_group_issues(_agg_results):
                    _n_req = sum(1 for _k, _, _ in collect_group_issues(_agg_results) if _k not in _ok_keys)
                    st.caption(f"확인 요청 {_n_req}건 · 문제 없음 {len(collect_group_issues(_agg_results)) - _n_req}건")
                    if st.button("📩 확인 요청 메일 초안 만들기", disabled=_n_req == 0, key="agg_mail_make"):
                        from notify.contacts import resolve_recipient
                        st.session_state.agg_requests = build_aggregate_requests(
                            _agg_results, _ok_keys,
                            recipient_for=lambda _f: resolve_recipient(_f, (st.session_state.state or {}).get("dataset")))
                        st.rerun()
                from ui.components.mail_panel import render_mail_panel
                render_mail_panel(
                    st.session_state.agg_requests,
                    run_dir=settings.outputs_dir / (st.session_state.run_id or "unknown"),
                    key="aggmail", title="📩 집계 확인 요청 메일",
                    on_update=lambda _all: st.session_state.__setitem__("agg_requests", _all),
                )

    # ═══════════════════════════════════════════════════════════════════════
    # ④ 결과  — 완료 후 표시
    # ═══════════════════════════════════════════════════════════════════════

    if st.session_state.phase == "done":
        _state = st.session_state.state or {}
        _report = _state.get("report")

        with st.container(border=True):
            st.subheader("④ 결과")

            if _state.get("dataset") is not None:
                st.markdown("#### 📂 변경 내역 (파일로 보기)")
                from ui.components.viewer_screen import render_viewer
                render_viewer(_state, finalized=True, allow_decisions=False, key="viewer_result")

                with st.expander("📥 엑셀로 내려받기 (상태 색·메모 포함)"):
                    st.caption("엑셀은 셀에 최종 값만 들어가고 상태는 배경색과 메모(마우스를 올리면 보임)로 표시됩니다. "
                               "JSON 파일은 원래 JSON 형식으로, 변경 이력은 CSV로 내려받습니다. "
                               "‘발표용’은 사업자번호·기업명 열까지 가린 파일입니다. 원본 엑셀의 서식은 유지하지 않습니다.")
                    if st.button("📄 엑셀 파일 만들기", key="export_marked"):
                        from engine.marked_export import (
                            amount_columns_from_profiles, build_export_bundle, save_marked_workbooks, zip_workbooks,
                        )
                        from ui.components.viewer_screen import compute_marks
                        _marks, _cleaned = compute_marks(_state, finalized=True)
                        _amt = amount_columns_from_profiles(_state.get("profiles"))
                        _dir = settings.outputs_dir / (_state.get("run_id") or "unknown")
                        _made = {}
                        for _masked in (False, True):
                            _files = build_export_bundle(_state["dataset"], _cleaned, _marks,
                                                         input_paths=_state.get("input_paths"),
                                                         masked=_masked, amount_columns=_amt)
                            save_marked_workbooks(_files, _dir, masked=_masked)
                            _made["masked" if _masked else "normal"] = zip_workbooks(_files)
                        st.session_state["export_zips"] = _made
                    _zips = st.session_state.get("export_zips")
                    if _zips:
                        _e1, _e2 = st.columns(2)
                        _e1.download_button("📦 표시된 엑셀 (ZIP)", data=_zips["normal"],
                                            file_name=f"datamedic_marked_{_state.get('run_id', '')}.zip",
                                            mime="application/zip", key="dl_marked")
                        _e2.download_button("🎤 발표용 마스킹 엑셀 (ZIP)", data=_zips["masked"],
                                            file_name=f"datamedic_marked_masked_{_state.get('run_id', '')}.zip",
                                            mime="application/zip", key="dl_marked_masked")
                st.divider()

            if _report:
                _r1, _r2, _r3, _r4 = st.columns(4)
                _r1.metric("재검진",   "✅ 통과" if _report.recheck_passed else "❌ 미통과")
                _r2.metric("잔여 오류", len(_report.remaining_issues))
                _r3.metric("처리 파일", len(_report.files))
                _r4.metric("총 변경",   sum(_report.tier_counts.values()))

            _changes = mask_changes(_state.get("changes", []))
            if _changes:
                with st.expander(f"변경 이력 ({len(_changes)}건)", expanded=True):
                    st.dataframe(pd.DataFrame([
                        {"ID": c.change_id, "이슈": c.issue_id, "위치": c.ref.a1,
                         "이전": c.before, "이후": c.after,
                         "tier": c.tier, "처리자": c.decided_by}
                        for c in _changes
                    ]), use_container_width=True)

            _requests = _state.get("requests", [])
            if _requests:
                from ui.components.mail_panel import render_mail_panel
                render_mail_panel(
                    _requests,
                    run_dir=settings.outputs_dir / (_state.get("run_id") or "unknown"),
                    key="reqmail", title="보완 요청 메일",
                    on_update=lambda _all: st.session_state.__setitem__(
                        "state", {**(st.session_state.state or {}), "requests": _all}),
                )

            _run_id  = _state.get("run_id", "")
            _run_dir = settings.outputs_dir / _run_id
            if _run_dir.exists():
                st.subheader("결과 다운로드")
                _buf = io.BytesIO()
                with zipfile.ZipFile(_buf, "w", zipfile.ZIP_DEFLATED) as _zf:
                    for _fp in _run_dir.rglob("*"):
                        if _fp.is_file():
                            _zf.write(_fp, _fp.relative_to(_run_dir))
                st.download_button(
                    "📦 결과 ZIP 다운로드",
                    data=_buf.getvalue(),
                    file_name=f"datamedic_{_run_id}.zip",
                    mime="application/zip",
                )

        if not _past_runs:
            st.caption("새 분석을 시작하려면 좌측 사이드바의 🔄 초기화를 누르세요.")

        # 다음 목표 설정 — Agent 재실행 (파일 재업로드 없이)
        if st.session_state.uploaded_paths:
            with st.container(border=True):
                st.subheader("🎯 다음 목표로 재실행")
                st.caption("동일한 파일에 새 목표를 설정하고 Agent를 다시 실행합니다.")
                _ng = st.text_input(
                    "새 진단 목표",
                    key="next_goal_input",
                    placeholder="예: 날짜 형식 오류 및 필수값 누락 점검",
                )
                if st.button(
                    "▶ 재실행", type="primary",
                    disabled=not _ng.strip(),
                    use_container_width=True,
                    key="next_goal_btn",
                ):
                    _archive_current_run()
                    st.session_state.goal          = _ng.strip()
                    st.session_state.state         = None
                    st.session_state.log_messages  = []
                    st.session_state.activity_log  = []
                    st.session_state.phase         = "upload"
                    st.session_state.pending_rerun = True
                    st.rerun()
