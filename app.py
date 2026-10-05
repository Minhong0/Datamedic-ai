"""DataMedic AI — Streamlit 앱 (ReAct Agent)."""
from __future__ import annotations

import json
import logging
import re
import tempfile
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
from engine.planning import BY_ID, approval_reason, strip_rule_codes
from engine.run_history import cleaned_files, list_runs, load_goal, load_run
from engine.viewer_marks import rule_label, tier_label

logger = logging.getLogger(__name__)

st.set_page_config(page_title="DataMedic AI", page_icon="🏥", layout="wide")

st.markdown(
    "<span style='background:#fff3cd;color:#856404;padding:4px 10px;"
    "border-radius:4px;font-size:0.8rem;font-weight:600'>"
    "⚠️ 시연용 가상 데이터</span>",
    unsafe_allow_html=True,
)
st.title("DataMedic AI — 업무 데이터 진단·복구 Agent")


_DEFAULTS: dict = {
    "run_id":          None,
    "state":           None,
    "phase":           "upload",
    "log_messages":    [],
    "goal":            "",
    "approval_log":    [],
    "previous_runs":   [],
    "activity_log":    [],
    "compiled_graph":  None,
    "run_config":      None,
    "uploaded_paths":  [],
    "pending_rerun":   False,
    "agg_requests":    [],
    "export_zips":     None,
    "viewer_decisions": {},
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = list(_v) if isinstance(_v, list) else (dict(_v) if isinstance(_v, dict) else _v)


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
    """새 목표로 넘어가기 전에 현재 목표의 전체 결과를 보관한다."""
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


_STEP_TEXT = {
    "analyze_files": "업로드한 파일의 구조를 분석합니다.",
    "run_diagnosis": "규칙으로 오류를 진단합니다.",
    "ask_approval": "담당자 승인이 필요한 항목을 확인합니다.",
    "fix_auto": "자동으로 고칠 수 있는 오류를 수정합니다.",
    "apply_decisions": "담당자의 결정을 적용합니다.",
    "run_aggregate": "집계 검사(횟수·한도·이중 지급)를 실행합니다.",
    "finalize": "재검진하고 결과를 저장합니다.",
    "done": "처리를 마칩니다.",
}
_CARD_VALUE_LABELS = {
    "기간": {"year": "연도별 (해마다 따로)", "all": "전체 기간", "business_period": "사업 기간 안에서"},
    "범위": {"per_business": "사업별 합계 — 회사별로 사업마다 따로 더한다",
             "total": "모든 사업 합산 — 회사가 받은 모든 사업을 합친다",
             "per_payment": "건별 — 지원금 한 건씩 본다"},
}
_plain = strip_rule_codes


def _issue_rows(issues: list[Issue]) -> list[dict]:
    return [{"ID": i.issue_id, "규칙": rule_label(i.rule_id), "처리 방식": tier_label(i.tier),
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
                _r1.metric("재검진", "✅ 통과" if _rep.recheck_passed else "⚠️ 남은 항목 있음")
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
    except Exception as _e:
        _log(f"집계 처리 결과 저장 실패: {_e}")
    st.session_state.state = {**_state, "agg_dispositions": dispositions}
    from agent.aggregate_store import update_dispositions
    update_dispositions(settings.outputs_dir / (st.session_state.run_id or "unknown"), dispositions)


def _note_review_decision(issue_id: str) -> None:
    """승인 화면에서 담당자가 직접 고른 결정을 기록한다 — 뷰어 미리보기가 같은 선택을 보여 주도록."""
    _act = st.session_state.get(f"act_{issue_id}")
    _val = (st.session_state.get(f"ev_{issue_id}") or None) if _act == "edit" else None
    _d = dict(st.session_state.get("viewer_decisions", {}))
    _d[issue_id] = {"action": _act, "value": _val}
    st.session_state["viewer_decisions"] = _d


EXAMPLE_GOALS = [
    "날짜 형식, 금액 표기, 필수값 누락, 중복 행 점검",
    "사업자번호 형식 체크디지트 확인, 국세청 폐업 휴업 확인",
    "이번 지원 사업은 한 회사당 한번만 가능",
    "이번 지원금은 최대 1억인데 초과하는 회사를 찾아줘",
    "같은 금액이 짧은 기간에 두 번 나간 곳 찾아줘",
]

_ACT_LABEL = {"apply": "승인(적용)", "reject": "거절(유지)", "request": "확인 요청", "edit": "직접 수정"}
_DUP_ACT_LABEL = {**_ACT_LABEL, "apply": "행 삭제 (승인)", "reject": "삭제하지 않고 유지"}


def _act_label(issue: Issue, action: str) -> str:
    """결정 이름 — 중복 행은 '승인(적용)'이 삭제라서 무엇이 일어나는지 그대로 적는다."""
    return (_DUP_ACT_LABEL if issue.rule_id == "DUPLICATE_ROW" else _ACT_LABEL).get(action, "")


def _dup_short(issue: Issue) -> str:
    """중복 행 항목의 표 칸용 짧은 말 — '5행과 같음' (자세한 문장은 항목을 펼치면 나온다)."""
    m = re.search(r"(\d+)행과", issue.message or "")
    if not m:
        return issue.message or "중복 행"
    return ("다른 파일 " if "다른 파일" in (issue.message or "") else "") + f"{m.group(1)}행과 같음"


def _default_action(issue: Issue) -> str:
    """항목별 처리의 기본값 — 중복 행은 삭제이므로 거절(유지)이 기본이다."""
    return "reject" if issue.rule_id == "DUPLICATE_ROW" else "apply"


def _set_all(action: str, issue_ids: tuple[str, ...]) -> None:
    """승인 항목의 처리를 한꺼번에 정한다 (버튼 콜백). 항목마다 다시 바꿀 수 있다."""
    for iid in issue_ids:
        st.session_state[f"act_{iid}"] = action
        _note_review_decision(iid)


def _proactive_summary(issues: list[Issue], n_files: int) -> str:
    n      = len(issues)
    auto_c = sum(1 for i in issues if i.tier == "auto")
    appr_c = sum(1 for i in issues if i.tier == "approval")
    req_c  = sum(1 for i in issues if i.tier == "request")
    top    = Counter(i.rule_id for i in issues).most_common(1)
    if n == 0:
        return f"파일 {n_files}개 분석 완료 — 오류가 발견되지 않았습니다. 🎉"
    top_str = f" (가장 많은 오류: {rule_label(top[0][0])} {top[0][1]}건)" if top else ""
    return f"📊 파일 {n_files}개 분석 완료 — 총 {n}건 발견: 자동 {auto_c} · 승인 {appr_c} · 요청 {req_c}{top_str}"


def _plan_card_md(plan) -> str:
    """계획 카드: 실행·선행·생략(이유)을 보여준다."""
    from agent.nodes.query import interpret_goal

    if plan.status == "needs_clarification":
        return f"❓ **계획 확인 필요** — {plan.question}"
    _icon = {"run": "▶️ 실행", "dependency": "➕ 선행 작업", "skipped": "⏭️ 생략"}
    _lines = [f"📋 **실행 계획** — 목표: *{plan.goal or '(없음: 기본 전체 점검)'}*", ""]
    for _s in plan.steps:
        if _s.status == "skipped":
            continue
        _lines.append(f"- {_icon[_s.status]} **{_plain(_s.label)}** — {_plain(_s.reason)}")
        if _s.status == "run" and _s.capability in ("sum_limit", "count_limit", "near_duplicate"):
            _lines[-1] += " (실행 중 기준을 확인합니다)"
    _skipped = [_plain(_s.label) for _s in plan.steps if _s.status == "skipped"]
    if _skipped:
        _lines.append(f"- ⏭️ **생략 {len(_skipped)}개** (목표에 해당하지 않음) — " + " · ".join(_skipped))
    _reads = interpret_goal(plan.goal, plan) if plan.goal else []
    if _reads:
        _lines += ["", "🔎 **이렇게 이해했어요** (문장에서 읽은 값 · 근거 — 실행 전 확인 카드에서 다시 확인합니다)"]
        for _r in _reads:
            _vals = " · ".join(f"{_k} {_v}" + (f" (“{_q}”)" if _q else "") for _k, _v, _q in _r["read"]) or "읽은 값 없음"
            _ask = f" — 확인할 것: {', '.join(_r['ask'])}" if _r["ask"] else ""
            _lines.append(f"- {_plain(_r['label'])}: {_vals}{_ask}")
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
    _live.state.update(st.session_state.state or {})
    with st.status(label, expanded=True) as _st:
        _stream_events(
            _compiled.stream(Command(resume=resume_value), _cfg, stream_mode="updates"),
            status_widget=_st, live=_live,
        )
    _after_run(_compiled.get_state(_cfg), st.session_state.uploaded_paths)


def _after_run(_snapshot, paths: list[str]) -> None:
    """그래프 실행/재개 후 phase 를 결정한다."""
    st.session_state.state       = dict(_snapshot.values)
    _pending = _pending_interrupt() if _snapshot.next else None
    _ptype = (_pending or {}).get("type")
    if _ptype in _CLARIFY_TYPES:
        st.session_state.phase = "clarify"
    elif _ptype == "aggregate_confirm":
        st.session_state.phase = "agg_confirm"
    elif _snapshot.next:
        _issues  = _snapshot.values.get("issues", [])
        _n_files = len({Path(p).name for p in paths})
        _add_activity(_proactive_summary(_issues, _n_files))
        _appr_n  = sum(1 for i in _issues if i.tier == "approval")
        _add_activity(
            f"⏸️ **승인 필요 항목 {_appr_n}건**이 있습니다. "
            "아래에서 각 항목을 검토한 뒤 '승인 후 재개'를 눌러주세요."
        )
        st.session_state.phase = "diagnosed"
    else:
        _rep_done = _snapshot.values.get("report")
        if _rep_done is not None and not _rep_done.recheck_passed:
            _add_activity(f"✅ 처리를 마쳤습니다. 남은 항목 {len(_rep_done.remaining_issues)}건은 아래 결과에서 확인하세요.")
        else:
            _add_activity("🎉 Agent가 모든 처리를 완료했습니다. 아래 결과를 확인하세요.")
        st.session_state.phase = "done"


def _stream_events(event_iter, status_widget=None, live=None) -> None:
    """LangGraph 스트림 이벤트를 처리하며 화면과 채팅 히스토리에 기록한다. live 가 있으면 실시간 뷰어도 갱신한다."""
    for _event in event_iter:
        for _node, _out in _event.items():
            if live is not None:
                try:
                    live.on_event(_node, _out)
                except Exception as _e:
                    _log(f"실시간 뷰어 갱신 실패: {_e}")
            if _node == "planner":
                _reason = _out.get("agent_reason", "")
                _action = _out.get("agent_action", "")
                _step = _STEP_TEXT.get(_action) or _reason
                if _step:
                    st.markdown(f"🤔 **{_step}**")
                    _add_activity(f"🤔 {_step}")
                    _log(_step)
                    logger.debug("planner → %s: %s", _action, _reason)
            elif _node == "plan":
                _plan = _out.get("plan")
                if _plan is not None:
                    _msg = _plan_card_md(_plan)
                    st.markdown(_msg); _log(f"plan → {_plan.status}")
            elif _node == "analyze":
                _profiles = _out.get("profiles", [])
                _nf  = len({Path(p.file).name for p in _profiles})
                _msg = f"✅ 파일 분석 완료 — 파일 {_nf}개 · 시트 {len(_profiles)}개"
                st.markdown(_msg); _add_activity(_msg); _log(_msg)
            elif _node == "diagnose":
                _n   = len(_out.get("issues", []))
                _msg = f"✅ 진단 완료 — {_n}건 발견"
                st.markdown(_msg); _add_activity(_msg); _log(_msg)
                for _w in _out.get("warnings", []):
                    st.warning(_w); _add_activity(f"⚠️ {_w}"); _log("⚠️ 국세청 조회 실패")
            elif _node == "treatment":
                _n   = len(_out.get("changes", []))
                _msg = f"✅ 수정 완료 — {_n}건 변경"
                st.markdown(_msg); _add_activity(_msg); _log(_msg)
            elif _node == "recheck":
                _rpt = _out.get("report")
                if _rpt:
                    _left = len(_rpt.remaining_issues)
                    _msg = ("✅ 재검진 통과 — 남은 오류가 없습니다" if _rpt.recheck_passed else
                            f"⚠️ 재검진 결과 남은 항목 {_left}건 — 담당자 결정이 없었거나 기관 확인이 필요한 항목입니다")
                    st.markdown(_msg); _add_activity(_msg); _log(_msg)
    if status_widget:
        status_widget.update(label="완료 ✅", state="complete")


def _run_agent(paths: list[str], *, keep_uploaded: bool = False, note: str | None = None) -> None:
    """주어진 파일 목록과 현재 세션 goal로 Agent를 실행하고 상태를 갱신한다."""
    _run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    _config  = {"configurable": {"thread_id": _run_id}}
    st.session_state["_history_view"] = None
    st.session_state["_live_backup"]  = None
    st.session_state.run_id         = _run_id
    st.session_state.run_config     = _config
    if not keep_uploaded:
        st.session_state.uploaded_paths = paths
    st.session_state.activity_log   = []
    if note:
        _add_activity(note)
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


_outputs_dir = settings.outputs_dir
_past_runs: list[str] = list_runs(_outputs_dir)


def _restore_live() -> None:
    """과거 실행을 보다가 원래 작업하던 세션으로 돌아간다."""
    backup = st.session_state.get("_live_backup")
    if backup is not None:
        st.session_state.update(backup)
    st.session_state["_live_backup"] = None
    st.session_state["_history_view"] = None
    st.session_state["hist_sel"] = "(현재 세션)"


def _load_history(run_name: str) -> None:
    """과거 실행 결과를 읽기 전용으로 연다. 열기 전에 지금 세션을 보관해 둔다."""
    run_state = load_run(_outputs_dir / run_name)
    if run_state is None:
        st.session_state["_history_error"] = f"{run_name} 실행 결과를 읽지 못했습니다."
        return
    st.session_state["_history_error"] = None
    if st.session_state.get("_live_backup") is None:
        st.session_state["_live_backup"] = {k: st.session_state.get(k) for k in _DEFAULTS}
    from agent.aggregate_store import load_aggregate_results
    run_state.update(load_aggregate_results(_outputs_dir / run_name) or {})
    st.session_state.update({
        "run_id": run_name, "phase": "done", "state": run_state, "uploaded_paths": [], "approval_log": [],
        "activity_log": [], "agg_requests": [], "export_zips": None, "viewer_decisions": {},
        "pending_rerun": False, "previous_runs": [], "_history_view": run_name,
    })
    _log(f"이력 불러옴: {run_name}")


def _run_label(name: str) -> str:
    """이력 목록 표시: 실행 ID + 목표 앞부분 (목표 기록이 없는 실행은 ID만)."""
    if name == "(현재 세션)":
        return name
    goal = load_goal(_outputs_dir / name)
    return f"{name} · {goal[:22]}{'…' if len(goal) > 22 else ''}" if goal else name


def _use_cleaned(run_name: str) -> None:
    """과거 실행의 정제본을 새 실행의 입력으로 삼는다 (아래 '다음 목표로 재실행'에서 새 목표를 입력)."""
    st.session_state["uploaded_paths"] = [str(f) for f in cleaned_files(_outputs_dir / run_name)]


with st.sidebar:
    st.header("🏥 DataMedic AI")
    _nts = (settings.nts_mode or "mock").strip().lower()
    if _nts == "live":
        st.warning("국세청 조회: **실제 API**. 가상 데이터의 사업자번호는 모두 '미등록'으로 나옵니다 (시연은 모의 응답으로).")
    else:
        st.caption("국세청 조회: " + {"mock": "모의 응답 (시연용)", "off": "사용 안 함"}.get(_nts, _nts))
    if st.session_state.run_id:
        st.info(f"run_id: `{st.session_state.run_id}`")
    if st.button("🔄 초기화", use_container_width=True):
        st.session_state["_history_view"] = None
        st.session_state["_live_backup"] = None
        st.session_state["hist_sel"] = "(현재 세션)"
        for _k, _v in _DEFAULTS.items():
            st.session_state[_k] = list(_v) if isinstance(_v, list) else (dict(_v) if isinstance(_v, dict) else _v)
        st.rerun()
    st.divider()
    st.subheader("📋 실행 로그")
    _logs = st.session_state.log_messages
    st.code("\n".join(reversed(_logs[-30:])) if _logs else "로그 없음", language=None, wrap_lines=True)
    st.divider()
    st.subheader("🗂 실행 이력")
    if _past_runs:
        _sel = st.selectbox("과거 실행 불러오기", ["(현재 세션)"] + _past_runs, key="hist_sel", format_func=_run_label,
                            on_change=lambda: _restore_live() if st.session_state.get("hist_sel") == "(현재 세션)" else None)
        if _sel != "(현재 세션)":
            st.button("불러오기", on_click=_load_history, args=(_sel,), key="hist_load")
        if st.session_state.get("_history_error"):
            st.error(st.session_state["_history_error"])
    else:
        st.caption("이전 실행 없음")


with st.container():

    if st.session_state.get("_history_view"):
        _hid = st.session_state["_history_view"]
        with st.container(border=True):
            st.info(f"📁 과거 실행 **{_hid}** 의 결과입니다 (읽기 전용). 저장된 파일은 사업자번호가 마스킹돼 있어 "
                    "이 화면에서 값을 고치거나 다시 처리할 수는 없습니다. 이어서 작업하려면 정제본으로 새 실행을 시작하세요.")
            _hgoal = (st.session_state.state or {}).get("goal")
            st.markdown(f"🎯 **목표**: {_hgoal}" if _hgoal else "🎯 **목표**: 기록 없음 (목표를 저장하기 전에 끝난 실행)")
            _hb1, _hb2 = st.columns(2)
            _hb1.button("← 현재 세션으로 돌아가기", on_click=_restore_live, key="hist_back", use_container_width=True)
            _hfiles = cleaned_files(_outputs_dir / _hid)
            _hb2.button(f"🔁 정제본 {len(_hfiles)}개로 새 목표 실행", on_click=_use_cleaned, args=(_hid,),
                        disabled=not _hfiles, key="hist_reuse", use_container_width=True,
                        help="이 실행의 정제본(cleaned/)을 입력으로 새 실행을 만든다. 원본·과거 결과는 바뀌지 않는다.")
            if st.session_state.uploaded_paths:
                st.success("정제본을 불러왔습니다. 아래 '🎯 다음 목표로 재실행'에서 새 목표를 입력하세요.")

    _render_past_turns()


    if st.session_state.phase == "upload":
        if st.session_state.get("pending_rerun") and st.session_state.uploaded_paths:
            st.session_state.pending_rerun = False
            _chain_paths = st.session_state.pop("rerun_paths", None)
            _note = (f"🧠 이전 목표에서 수정한 결과(정제본 {len(_chain_paths)}개)를 이어서 사용합니다"
                     if _chain_paths else None)
            _add_activity(f"🎯 새 목표로 재실행합니다: **{st.session_state.goal}**")
            _run_agent(_chain_paths or st.session_state.uploaded_paths, keep_uploaded=bool(_chain_paths), note=_note)
            st.rerun()

        with st.container(border=True):
            st.subheader("① 파일 업로드 · 목표 설정")

            _EXAMPLE_GOALS = EXAMPLE_GOALS
            if st.session_state.goal != st.session_state.get("goal_input_last", ""):
                st.session_state["goal_input"] = st.session_state.goal
            st.session_state.setdefault("goal_input", st.session_state.goal)
            _goal = st.text_area(
                "진단 목표", key="goal_input", height=72,
                placeholder="아래 예시를 누르거나 목표를 직접 입력하세요",
                label_visibility="visible",
            )
            st.session_state.goal = _goal
            st.session_state["goal_input_last"] = _goal

            st.caption("예시 목표 (누르면 위 입력칸에 채워집니다)")
            _eg_cols = st.columns(2)
            for _idx, _eg in enumerate(_EXAMPLE_GOALS):
                _eg_cols[_idx % 2].button(_eg, key=f"eg_{_idx}", use_container_width=True,
                                          on_click=lambda _t=_eg: st.session_state.__setitem__("goal_input", _t))

            _uploaded = st.file_uploader(
                "엑셀(.xlsx) 또는 JSON (여러 개 가능)",
                type=["xlsx", "json"], accept_multiple_files=True,
            )
            if st.button("🚀 Agent 실행", disabled=not _uploaded, type="primary",
                         use_container_width=True):
                _tmp = Path(tempfile.mkdtemp())
                _paths = []
                for _f in _uploaded:
                    _dest = _tmp / _f.name
                    _dest.write_bytes(_f.read())
                    _paths.append(str(_dest))
                _log(f"업로드 파일 {len(_paths)}개")
                _run_agent(_paths)
                st.rerun()

    else:
        if st.session_state.previous_runs:
            st.markdown(f"### 🎯 목표 {len(st.session_state.previous_runs) + 1}: "
                        f"{st.session_state.goal or '(목표 없음)'}")
        with st.container(border=True):
            st.markdown(
                f"**① 분석 완료** &nbsp;|&nbsp; "
                f"목표: *{st.session_state.goal or '(없음)'}* &nbsp;|&nbsp; "
                f"`{st.session_state.run_id}`"
            )


    if st.session_state.phase != "upload":
        _plan_now = (st.session_state.state or {}).get("plan")
        if _plan_now is not None and _plan_now.status == "ready":
            with st.container(border=True):
                st.markdown(_plan_card_md(_plan_now))

        _agent_msgs = st.session_state.activity_log
        if _agent_msgs:
            with st.expander("② 🤖 Agent 실행 기록", expanded=True):
                st.markdown("\n".join(f"- {_msg}" for _msg in _agent_msgs))


    if st.session_state.phase == "clarify":
        _clar = _pending_interrupt() or {}
        with st.container(border=True):
            if _clar.get("type") == "aggregate_clarify":
                st.subheader("❓ 집계 규칙 조건 확인")
                st.markdown(_clar.get("question", "조건을 알려주세요."))
                _opts = _clar.get("options", [])
                _ans = None
                if _opts and _clar.get("slot") == "business":
                    _labels = {o["id"]: o["label"] for o in _opts}
                    _picked_biz = st.multiselect("지원 사업 (여러 개 선택 가능)", list(_labels),
                                                 format_func=lambda k: _labels[k], key="agg_clarify_multi")
                    _ans = ", ".join(_picked_biz) if _picked_biz else None
                elif _opts and _clar.get("slot") == "scope":
                    _labels = {o["id"]: o["label"] for o in _opts}
                    _picked_scope = st.multiselect("한도 범위 (여러 개 선택 가능)", list(_labels),
                                                   format_func=lambda k: _labels[k], key="agg_clarify_scope")
                    _ans = ", ".join(_picked_scope) if _picked_scope else None
                elif _opts:
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
                if _k != "해석 방식":
                    st.markdown(f"**{_k}**: {_CARD_VALUE_LABELS.get(_k, {}).get(str(_v), _v)}")
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


    if st.session_state.phase in ("diagnosed", "done"):
        _state: dict  = st.session_state.state or {}
        _issues: list[Issue] = mask_issues(_state.get("issues", []))
        for _w in _state.get("warnings", []):
            st.warning(_w)
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

        if _issues:
            with st.expander("오류 상세 보기"):
                _rule_c = pd.Series([rule_label(i.rule_id) for i in _issues]).value_counts()
                if not _rule_c.empty:
                    st.bar_chart(_rule_c)
                _all_t = sorted({i.tier    for i in _issues})
                _all_r = sorted({i.rule_id for i in _issues})
                _f1, _f2 = st.columns(2)
                _st_f = _f1.multiselect("처리 방식", _all_t, default=_all_t, key="f_tier", format_func=tier_label)
                _sr_f = _f2.multiselect("규칙",  _all_r, default=_all_r, key="f_rule", format_func=rule_label)
                _flt = [i for i in _issues if i.tier in _st_f and i.rule_id in _sr_f]
                _PAGE = 50
                _tp = max(1, (len(_flt) + _PAGE - 1) // _PAGE)
                _pg = st.number_input("페이지", 1, _tp, 1, key="diag_pg")
                _s2 = (_pg - 1) * _PAGE
                st.caption(f"총 {len(_flt)}건 · {_pg}/{_tp} 페이지")
                st.dataframe(pd.DataFrame([
                    {"ID": i.issue_id, "규칙": rule_label(i.rule_id), "처리 방식": tier_label(i.tier),
                     "파일": Path(i.ref.file).name if i.ref else "-",
                     "위치": i.ref.a1 if i.ref else "-",
                     "값": i.value or "(공백)",
                     "오류 내용": i.message,
                     "제안": i.suggestion or ""}
                    for i in _flt[_s2: _s2 + _PAGE]
                ]), use_container_width=True)

        _appr_issues = [i for i in _issues if i.tier == "approval"]
        _plan = _state.get("plan")
        _del_intent = bool(_plan and _plan.delete_intent)
        _bulk_dups = [
            i for i in _appr_issues
            if i.rule_id == "DUPLICATE_ROW" and i.value == "(완전중복)"
        ] if _del_intent else []
        _bulk_ids = {i.issue_id for i in _bulk_dups}
        if _appr_issues and st.session_state.phase == "diagnosed":
            with st.container(border=True):
                st.subheader(f"👤 승인 필요 항목 — {len(_appr_issues)}건")
                st.caption("Agent가 스스로 처리하기 어려운 항목입니다. 한꺼번에 정하거나, 항목마다 골라도 됩니다.")
                _next_checks = [strip_rule_codes(BY_ID[_st.capability].label) for _st in (_plan.steps if _plan else [])
                                if _st.status == "run" and BY_ID[_st.capability].aggregate]
                if _next_checks:
                    st.caption(f"➡️ 승인 뒤에 이어서 '{', '.join(_next_checks)}'을(를) 실행합니다. 그 결과는 아래 '집계 규칙 결과'에 나옵니다.")
                _reasons = {_i.issue_id: approval_reason(_plan, _i.rule_id) for _i in _appr_issues}
                _same = list(dict.fromkeys(_reasons.values()))
                if len(_same) == 1:
                    st.info(f"ℹ️ {_same[0]}")

                _todo = [_i for _i in _appr_issues if _i.issue_id not in _bulk_ids]
                if len(_todo) > 1:
                    _no_dup = tuple(_i.issue_id for _i in _todo if _i.rule_id != "DUPLICATE_ROW")
                    _all_ids = tuple(_i.issue_id for _i in _todo)
                    _b1, _b2, _b3 = st.columns(3)
                    _b1.button("✅ 모두 제안대로 적용", on_click=_set_all, args=("apply", _no_dup), key="bulk_apply",
                               use_container_width=True, disabled=not _no_dup,
                               help="중복 행은 삭제가 되므로 이 버튼에서는 제외합니다 (항목에서 직접 고르세요).")
                    _b2.button("🚫 모두 거절(유지)", on_click=_set_all, args=("reject", _all_ids), key="bulk_reject",
                               use_container_width=True)
                    _b3.button("📩 모두 기관에 확인 요청", on_click=_set_all, args=("request", _all_ids), key="bulk_request",
                               use_container_width=True)

                    st.dataframe(pd.DataFrame([{
                        "ID": _i.issue_id, "규칙": rule_label(_i.rule_id),
                        "위치": f"{Path(_i.ref.file).name} · {_i.ref.row}행 {_i.ref.column}" if _i.ref else "-",
                        "현재 값": _dup_short(_i) if _i.rule_id == "DUPLICATE_ROW" else (_i.value if _i.value is not None else "(공백)"),
                        "제안": _i.suggestion or "",
                        "선택": _act_label(_i, st.session_state.get(f"act_{_i.issue_id}", _default_action(_i))),
                    } for _i in _todo]), hide_index=True, use_container_width=True)

                _dec_map: dict[str, dict] = {}
                for _i in _appr_issues:
                    if _i.issue_id in _bulk_ids:
                        continue
                    _loc = f"{Path(_i.ref.file).name} · {_i.ref.row}행 {_i.ref.column}" if _i.ref else "-"
                    with st.expander(f"{_i.issue_id} · {rule_label(_i.rule_id)} — {_loc}", expanded=len(_todo) <= 3):
                        if _i.rule_id != "DUPLICATE_ROW":
                            st.write(f"**현재 값**: `{_i.value if _i.value is not None else '(공백)'}`")
                        st.write(f"**오류 내용**: {_i.message}")
                        if _i.rule_id == "DUPLICATE_ROW":
                            st.caption("삭제해도 원본 파일은 그대로이고, 정제본에서만 빠집니다. 기본값은 '삭제하지 않고 유지'입니다.")
                        if _i.suggestion:
                            st.write(f"**제안**: `{_i.suggestion}`")
                        if len(_same) != 1:
                            st.caption(f"왜 확인이 필요한가요? {_reasons[_i.issue_id]}")
                        _act = st.radio(
                            "처리", ["apply", "reject", "request", "edit"],
                            format_func=lambda k, _iss=_i: _act_label(_iss, k),
                            index=["apply", "reject", "request", "edit"].index(_default_action(_i)),
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
                        "ID": d.issue_id, "규칙": rule_label(_by[d.issue_id].rule_id),
                        "위치": _by[d.issue_id].ref.a1 if _by[d.issue_id].ref else "-",
                        "값": _by[d.issue_id].value or "(공백)",
                        "오류 내용": _by[d.issue_id].message,
                        "결정": ({"apply": "행 삭제 (승인)", "reject": "삭제하지 않고 유지", "edit": "직접 수정", "request": "보완 요청"}
                                 if _by[d.issue_id].rule_id == "DUPLICATE_ROW" else
                                 {"apply": "승인(적용)", "reject": "거절(유지)", "edit": "직접 수정",
                                  "request": "보완 요청"}).get(d.action, d.action),
                        "입력값": d.value or "",
                    } for d in ds if d.issue_id in _by]

                def _human_decisions() -> list[Decision]:
                    _ds = [
                        Decision(issue_id=iid, action=v["action"],
                                 value=v.get("value"), decided_by="operator")
                        for iid, v in _dec_map.items()
                    ]
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
                        if st.session_state.phase != "done":
                            st.rerun()

        if st.session_state.phase == "diagnosed" and _state.get("dataset") is not None:
            with st.expander("📂 파일로 보기 (엑셀처럼 셀 단위로 확인하고 바로 처리)", expanded=False):
                from ui.components.viewer_screen import render_viewer
                render_viewer(_state, finalized=False, allow_decisions=True, key="viewer_review")

        if _appr_issues and st.session_state.phase == "done":
            _changes = mask_changes((st.session_state.state or {}).get("changes", []))
            _op_n = sum(1 for c in _changes if c.decided_by == "operator")
            st.success(f"✅ 승인 완료 — 담당자 결정 {_op_n}건 적용됨")
            if st.session_state.approval_log:
                with st.expander(f"👤 검토한 승인 항목 ({len(st.session_state.approval_log)}건)", expanded=True):
                    st.dataframe(pd.DataFrame(st.session_state.approval_log),
                                 use_container_width=True, hide_index=True)


    if st.session_state.phase == "done" and not st.session_state.get("_history_view"):
        _n_cell_mail = len((st.session_state.state or {}).get("requests", []))
        _n_agg_found = sum(len(_r.get("group_issues", [])) for _r in (st.session_state.state or {}).get("agg_results", [])
                           if not _r.get("skipped"))
        if _n_cell_mail or _n_agg_found:
            _bits = ([f"보완 요청 메일 {_n_cell_mail}통"] if _n_cell_mail else []) + \
                    ([f"집계 위반 {_n_agg_found}건(확인 요청 메일 초안은 집계 결과 아래)"] if _n_agg_found else [])
            st.info("📬 기관에 확인을 요청할 항목이 있습니다 — " + ", ".join(_bits) + ". 아래에서 메일을 확인하고 발송하세요.")

    if st.session_state.phase == "done":
        _agg_results = (st.session_state.state or {}).get("agg_results", [])
        if _agg_results:
            with st.container(border=True):
                st.subheader("📊 집계 규칙 결과")
                _ro = bool(st.session_state.get("_history_view"))
                if _ro:
                    st.caption("과거 실행의 결과입니다. 처리 선택·기준 저장·메일 초안은 이 화면에서 바꿀 수 없습니다.")
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
                        _df = pd.DataFrame(_agg_rows(_res))
                        _df.insert(0, "처리", [disposition_to_label(_stored_disp.get(issue_key(_res["capability"], _g)))
                                              for _g in _gi])
                        _edited = st.data_editor(
                            _df, hide_index=True, use_container_width=True, key=f"agg_edit_{_ri}",
                            column_config={"처리": st.column_config.SelectboxColumn(
                                "처리", options=[REQUEST, OK], required=True,
                                help="확인 요청: 해당 기관에 확인(소명)을 요청 / 문제 없음: 확인해 보니 정상")},
                            disabled=list(_df.columns) if _ro else [c for c in _df.columns if c != "처리"],
                        )
                        for _pos, _g in enumerate(_gi):
                            _new_disp[issue_key(_res["capability"], _g)] = label_to_disposition(_edited.iloc[_pos]["처리"])
                    if _spec and not _ro and st.button("💾 이 기준 저장", key=f"agg_save_{_ri}"):
                        from agent.nodes.query import save_rule_node
                        _saved = save_rule_node({"confirmed_spec": _spec})
                        _add_activity("✅ 집계 기준 저장 완료." if _saved.get("saved") else "기준 저장 실패")
                        st.success("기준을 저장했습니다." if _saved.get("saved") else "저장 실패")
                    st.divider()

                if not _ro and _new_disp != {_k: _stored_disp.get(_k, DISP_REQUEST) for _k in _new_disp}:
                    _persist_dispositions(_new_disp)
                _ok_keys = ok_keys(_new_disp)
                if collect_group_issues(_agg_results):
                    _n_req = sum(1 for _k, _, _ in collect_group_issues(_agg_results) if _k not in _ok_keys)
                    st.caption(f"확인 요청 {_n_req}건 · 문제 없음 {len(collect_group_issues(_agg_results)) - _n_req}건")
                    def _make_agg_drafts() -> None:
                        from notify import mailer
                        from notify.contacts import resolve_recipient
                        _run_dir = settings.outputs_dir / (st.session_state.run_id or "unknown")
                        with st.spinner("확인 요청 메일 초안을 만드는 중..."):
                            _drafts = build_aggregate_requests(
                                _agg_results, _ok_keys,
                                recipient_for=lambda _f: resolve_recipient(_f, (st.session_state.state or {}).get("dataset")))
                            for _d in _drafts:
                                mailer.save_draft(_d, _run_dir, "집계")
                        st.session_state.agg_requests = _drafts

                    if (not _ro and _n_req > 0 and not st.session_state.agg_requests
                            and st.session_state.get("_agg_mail_auto") != st.session_state.run_id):
                        st.session_state["_agg_mail_auto"] = st.session_state.run_id
                        _make_agg_drafts()
                    if st.session_state.agg_requests and not _ro:
                        st.caption("확인 요청 메일 초안을 아래에 만들어 두었습니다. 위 표에서 '처리'를 바꿨다면 '다시 만들기'로 갱신하세요.")
                    if not _ro and st.button(
                            "📩 확인 요청 메일 초안 다시 만들기" if st.session_state.agg_requests else "📩 확인 요청 메일 초안 만들기",
                            disabled=_n_req == 0, key="agg_mail_make"):
                        _make_agg_drafts()
                        st.rerun()
                from ui.components.mail_panel import render_mail_panel
                render_mail_panel(
                    st.session_state.agg_requests,
                    run_dir=settings.outputs_dir / (st.session_state.run_id or "unknown"),
                    key="aggmail", title="📩 집계 확인 요청 메일", file_tag="집계",
                    on_update=lambda _all: st.session_state.__setitem__("agg_requests", _all),
                )


    if st.session_state.phase == "done" and not st.session_state.get("_history_view"):
        _req_mails = (st.session_state.state or {}).get("requests", [])
        if _req_mails:
            with st.container(border=True):
                from ui.components.mail_panel import render_mail_panel
                render_mail_panel(
                    _req_mails,
                    run_dir=settings.outputs_dir / ((st.session_state.state or {}).get("run_id") or "unknown"),
                    key="reqmail", title="📩 보완 요청 메일",
                    on_update=lambda _all: st.session_state.__setitem__(
                        "state", {**(st.session_state.state or {}), "requests": _all}),
                )


    if st.session_state.phase == "done":
        _state = st.session_state.state or {}
        _report = _state.get("report")

        with st.container(border=True):
            st.subheader("④ 결과")

            if _state.get("dataset") is not None:
                st.markdown("#### 📂 변경 내역 (파일로 보기)")
                from ui.components.viewer_screen import render_viewer
                render_viewer(_state, finalized=True, allow_decisions=False, key="viewer_result")

                with st.expander("📥 엑셀로 내려받기", expanded=True):
                    from engine.clean_export import (
                        MERGED_NAME, aggregate_notes, build_clean_files, build_merged_workbook, deleted_rows, pending_cells,
                        zip_clean_workbooks,
                    )
                    from notify.mailer import handled_orgs
                    _cdir = settings.outputs_dir / (_state.get("run_id") or "unknown")
                    _cfiles = cleaned_files(_cdir)
                    _mask = st.checkbox("발표용: 사업자번호·기업명 가리기", key="dl_mask",
                                        help="화면 공유나 영상에 쓸 때 켜세요. 끄면 정제된 실제 값이 그대로 들어갑니다.")
                    _pend = pending_cells(_state.get("remaining_issue_list", []), _state.get("requests", []), handled_orgs(_cdir))
                    _gone = deleted_rows(_state.get("changes", []))
                    _notes = aggregate_notes(_state.get("agg_results", []), st.session_state.get("agg_requests", []),
                                             handled_orgs(_cdir))
                    st.caption("수정이 반영된 최종 값만 깔끔하게 정리해 드립니다. 원본 파일은 바뀌지 않습니다.")
                    if _pend:
                        st.caption(f"📨 보완 요청 메일을 처리한 오류 {len(_pend)}칸은 엑셀에서 **(회신 대기)** 로 바뀌어 내려받아집니다. "
                                   "원래 값은 저장된 정제본(outputs 폴더의 cleaned)에 그대로 있습니다.")
                    if _notes:
                        st.caption(f"📨 집계 확인 요청 메일을 처리한 {len(_notes)}개 행에는 엑셀 맨 끝 **확인 요청** 열에 사유가 적힙니다 (값은 그대로).")
                    if not _cfiles:
                        st.info("내려받을 정제된 파일이 아직 없습니다.")
                    else:
                        if _mask and any(f.suffix.lower() == ".json" for f in _cfiles):
                            st.caption("JSON 파일은 가리기를 지원하지 않아 발표용 내려받기에서 제외됩니다.")
                        _each = build_clean_files(_cfiles, masked=_mask, pending=_pend, deleted=_gone, notes=_notes)
                        _merged = build_merged_workbook(_cfiles, masked=_mask, pending=_pend, deleted=_gone, notes=_notes)
                        _rid = _state.get("run_id", "")
                        _d1, _d2 = st.columns(2)
                        _kind = "엑셀" if all(n.endswith(".xlsx") for n in _each) else "파일"
                        _d1.download_button(f"📦 기관별 {_kind} {len(_each)}개 (ZIP)", data=zip_clean_workbooks(_each),
                                            file_name=f"datamedic_기관별_{_rid}.zip", mime="application/zip",
                                            key="dl_clean_each", use_container_width=True)
                        if _merged:
                            _d2.download_button("📄 합친 엑셀 1개", data=_merged, file_name=f"{Path(MERGED_NAME).stem}_{_rid}.xlsx",
                                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                                key="dl_clean_merged", use_container_width=True)
                st.divider()

            if _report:
                _r1, _r2, _r3, _r4 = st.columns(4)
                _r1.metric("재검진",   "✅ 통과" if _report.recheck_passed else "⚠️ 남은 항목 있음")
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

            if st.session_state.get("_history_view"):
                from ui.components.mail_history import render_mail_history
                render_mail_history(settings.outputs_dir / st.session_state["_history_view"])

        if not _past_runs:
            st.caption("새 분석을 시작하려면 좌측 사이드바의 🔄 초기화를 누르세요.")

        if st.session_state.uploaded_paths:
            with st.container(border=True):
                st.subheader("🎯 다음 목표로 재실행")
                st.caption("동일한 파일에 새 목표를 설정하고 Agent를 다시 실행합니다.")
                st.caption("예시 목표 (누르면 아래 입력칸에 채워집니다)")
                _ng_cols = st.columns(2)
                for _k, _eg in enumerate(EXAMPLE_GOALS):
                    _ng_cols[_k % 2].button(_eg, key=f"eg_next_{_k}", use_container_width=True,
                                            on_click=lambda _t=_eg: st.session_state.__setitem__("next_goal_input", _t))
                _ng = st.text_input(
                    "새 진단 목표",
                    key="next_goal_input",
                    placeholder="예: 날짜 형식 오류 및 필수값 누락 점검",
                )
                _prev_cleaned = cleaned_files(settings.outputs_dir / (st.session_state.state or {}).get("run_id", "_"))
                _n_changes = len((st.session_state.state or {}).get("changes", []))
                _chain = st.checkbox(
                    "이전 수정 결과를 이어서 사용", key="chain_prev", value=False,
                    disabled=not _prev_cleaned,
                    help="켜면 방금 목표에서 고친 결과(정제본)를 입력으로 새 목표를 실행합니다. "
                         "끄면 업로드한 원본 파일에서 다시 시작합니다. 어느 쪽이든 원본 파일은 바뀌지 않습니다.")
                if _chain and _prev_cleaned:
                    st.caption(f"🧠 이번 목표에서 적용된 수정 {_n_changes}건이 반영된 정제본 {len(_prev_cleaned)}개에서 이어 갑니다 "
                               f"(이전 목표 {len(st.session_state.previous_runs) + 1}개 기억 중).")
                if st.button(
                    "▶ 재실행", type="primary",
                    disabled=not _ng.strip(),
                    use_container_width=True,
                    key="next_goal_btn",
                ):
                    _archive_current_run()
                    st.session_state["rerun_paths"] = [str(f) for f in _prev_cleaned] if (_chain and _prev_cleaned) else None
                    st.session_state.goal          = _ng.strip()
                    st.session_state.state         = None
                    st.session_state.log_messages  = []
                    st.session_state.activity_log  = []
                    st.session_state.phase         = "upload"
                    st.session_state.pending_rerun = True
                    st.rerun()
