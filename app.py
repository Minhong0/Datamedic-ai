"""DataMedic AI — Streamlit 앱."""
from __future__ import annotations

import io
import json
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

from agent.state import GraphState
from config import settings
from engine.models import Decision, Issue

st.set_page_config(page_title="DataMedic AI", page_icon="🏥", layout="wide")
st.title("DataMedic AI — 업무 데이터 진단·복구 Agent")

# ── 세션 상태 초기화 ──────────────────────────────────────────────────────────
_DEFAULTS: dict = {
    "run_id": None,
    "state": None,
    "phase": "upload",   # upload | diagnosed | prescription | treated
    "log_messages": [],
    "goal": "",
    "sample_mode": False,
    "manual_standard": None,
    "chat_history": [],   # ④ 대화 이력
}
for _k, _v in _DEFAULTS.items():
    if _k not in st.session_state:
        st.session_state[_k] = list(_v) if isinstance(_v, list) else _v


# ── 유틸 ──────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.log_messages.append(f"[{ts}] {msg}")


def _auto_treat() -> None:
    """approval 없이 자동으로 치료+재검진 실행."""
    from agent.nodes.treatment import treatment_node
    from agent.nodes.recheck import recheck_node

    state = st.session_state.state
    issues: list[Issue] = state.get("issues", [])
    auto_dec = [Decision(issue_id=i.issue_id, action="apply",   decided_by="system") for i in issues if i.tier == "auto"]
    req_dec  = [Decision(issue_id=i.issue_id, action="request", decided_by="system") for i in issues if i.tier == "request"]
    info_dec = [Decision(issue_id=i.issue_id, action="reject",  decided_by="system") for i in issues if i.tier == "info"]
    updated = {**state, "decisions": auto_dec + req_dec + info_dec}
    _log(f"자동 치료 시작: 결정 {len(updated['decisions'])}건")
    with st.spinner("치료 중..."):
        treated = treatment_node(updated)
        final   = recheck_node(treated)
    st.session_state.state = final
    st.session_state.phase = "treated"
    _log(f"치료 완료: 변경 {len(final.get('changes', []))}건")


# ── 전역 데이터 ───────────────────────────────────────────────────────────────

_outputs_dir = settings.outputs_dir
_past_runs: list[str] = []
if _outputs_dir.exists():
    _past_runs = sorted(
        [d.name for d in _outputs_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
        reverse=True,
    )[:20]

_sample_files: list[Path] = []
if settings.samples_dir.exists():
    _sample_files = (
        sorted(settings.samples_dir.glob("*.xlsx")) +
        sorted(settings.samples_dir.glob("*.json"))
    )


# ── 단계 표시줄 ───────────────────────────────────────────────────────────────

_STEPS = [
    ("upload",       "① 업로드"),
    ("diagnosed",    "② 진단 완료"),
    ("prescription", "③ 처방전"),
    ("treated",      "④ 결과"),
]
_STEP_ORDER = [s[0] for s in _STEPS]


def _render_steps() -> None:
    phase = st.session_state.phase
    cur = _STEP_ORDER.index(phase) if phase in _STEP_ORDER else 0
    parts: list[str] = []
    for i, (_, label) in enumerate(_STEPS):
        if i < cur:
            parts.append(f"<span style='color:#28a745'>✓ {label}</span>")
        elif i == cur:
            parts.append(f"<span style='font-weight:700;color:#1f77b4'>▶ {label}</span>")
        else:
            parts.append(f"<span style='color:#aaa'>{label}</span>")
    st.markdown(" &nbsp;→&nbsp; ".join(parts), unsafe_allow_html=True)


_render_steps()
st.divider()


# ── 사이드바 ──────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("🏥 DataMedic AI")
    if st.session_state.run_id:
        st.info(f"run_id: `{st.session_state.run_id}`")

    if st.button("🔄 초기화", use_container_width=True):
        for _k, _v in _DEFAULTS.items():
            st.session_state[_k] = list(_v) if isinstance(_v, list) else _v
        st.rerun()

    # 실행 로그
    st.divider()
    st.subheader("📋 실행 로그")
    _logs = st.session_state.log_messages
    if _logs:
        st.code("\n".join(reversed(_logs[-30:])), language=None)
    else:
        st.caption("로그 없음")

    # 실행 이력
    st.divider()
    st.subheader("🗂 실행 이력")
    if _past_runs:
        _sel = st.selectbox("과거 실행 불러오기", ["(현재 세션)"] + _past_runs, key="hist_sel")
        if _sel != "(현재 세션)" and st.button("불러오기"):
            _rpath = _outputs_dir / _sel / "report.json"
            if _rpath.exists():
                from engine.models import RunReport
                _rpt = RunReport(**json.loads(_rpath.read_text(encoding="utf-8")))
                st.session_state.run_id = _sel
                st.session_state.phase  = "treated"
                st.session_state.state  = {
                    "run_id": _sel, "input_paths": _rpt.files,
                    "issues": [], "decisions": [], "changes": [],
                    "requests": [], "report": _rpt,
                }
                _log(f"이력 불러옴: {_sel}")
                st.rerun()
    else:
        st.caption("이전 실행 없음")

    # ④ 자연어 대화 패널
    if st.session_state.phase == "upload":
        st.divider()
        st.subheader("💬 Agent에게 물어보기")
        st.caption("진단을 완료하면 결과에 대해 질문할 수 있습니다.")
    elif st.session_state.phase in ("diagnosed", "prescription", "treated"):
        st.divider()
        st.subheader("💬 Agent에게 물어보기")
        _chat_state = st.session_state.state or {}
        _chat_issues: list = _chat_state.get("issues", [])
        _analysis: dict = _chat_state.get("analysis_summary", {})

        # 이전 대화 표시
        for _msg in st.session_state.chat_history[-6:]:
            _role_icon = "🧑" if _msg["role"] == "user" else "🤖"
            st.markdown(f"**{_role_icon}** {_msg['content']}")

        _chat_input = st.text_input(
            "질문 입력",
            placeholder="예: 어떤 파일이 가장 문제가 많나요?",
            key="chat_input_box",
            label_visibility="collapsed",
        )
        if st.button("전송", key="chat_send") and _chat_input.strip():
            try:
                from pydantic import BaseModel as _BM
                from llm.client import complete_json as _cj
                import pandas as _pd

                class _ChatResp(_BM):
                    answer: str

                _top_rules = list(
                    _pd.Series([i.rule_id for i in _chat_issues]).value_counts().head(5).index
                ) if _chat_issues else []
                _history_text = "\n".join(
                    f"{'사용자' if m['role']=='user' else '어시스턴트'}: {m['content']}"
                    for m in st.session_state.chat_history[-4:]
                ) or "(없음)"

                _resp = _cj(
                    "chat_qa",
                    {
                        "user_goal": st.session_state.goal or "데이터 정합성 검증",
                        "files": ", ".join({Path(i.ref.file).name for i in _chat_issues if i.ref}) or "(없음)",
                        "total_issues": len(_chat_issues),
                        "auto_count": sum(1 for i in _chat_issues if i.tier == "auto"),
                        "approval_count": sum(1 for i in _chat_issues if i.tier == "approval"),
                        "request_count": sum(1 for i in _chat_issues if i.tier == "request"),
                        "top_rules": ", ".join(_top_rules) or "(없음)",
                        "analysis_summary": _analysis.get("headline", "(없음)"),
                        "chat_history": _history_text,
                        "question": _chat_input.strip(),
                    },
                    _ChatResp,
                )
                st.session_state.chat_history.append({"role": "user",      "content": _chat_input.strip()})
                st.session_state.chat_history.append({"role": "assistant", "content": _resp.answer})
                _log(f"대화: Q={_chat_input.strip()[:30]}...")
            except Exception as _e:
                st.error(f"답변 생성 실패: {_e}")
            st.rerun()

        if st.session_state.chat_history and st.button("대화 초기화", key="chat_clear"):
            st.session_state.chat_history = []
            st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# ① 업로드
# ════════════════════════════════════════════════════════════════════════════
if st.session_state.phase == "upload":
    left, right = st.columns([3, 2], gap="large")

    with left:
        st.subheader("진단 목표")
        _EXAMPLE_GOALS = [
            "경남 20개 시군 사업실적 취합 전 오류 점검",
            "분기 지원금액 합계 검증 및 중복 행 제거",
            "기관별 엑셀 형식 오류·누락값 일괄 정리",
        ]
        _goal = st.text_area(
            "목표",
            value=st.session_state.goal,
            height=80,
            placeholder="예: 경남 20개 시군 사업실적 취합 전 오류 점검",
            label_visibility="collapsed",
        )
        st.session_state.goal = _goal

        _eg_cols = st.columns(3)
        for _idx, _eg in enumerate(_EXAMPLE_GOALS):
            if _eg_cols[_idx].button(_eg, key=f"eg_{_idx}", use_container_width=True):
                st.session_state.goal = _eg
                st.rerun()

        st.subheader("파일 업로드")
        _uploaded = st.file_uploader(
            "엑셀(.xlsx) 또는 JSON 파일 (여러 개 가능)",
            type=["xlsx", "json"],
            accept_multiple_files=True,
        )

        if _sample_files:
            if st.session_state.sample_mode:
                _sel_n = st.session_state.get("sample_count", 5)
                st.success(f"샘플 파일 {_sel_n}개 선택됨")
                if st.button("샘플 선택 취소"):
                    st.session_state.sample_mode = False
                    st.rerun()
            else:
                _sc1, _sc2 = st.columns([2, 1])
                if _sc1.button("📂 샘플 데이터 불러오기", use_container_width=True):
                    st.session_state.sample_mode = True
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
            st.caption("파일과 컬럼명이 80% 이상 일치하면 자동 인식됩니다.")

        _has_input = bool(_uploaded) or st.session_state.sample_mode
        if st.button("🔍 진단 시작", disabled=not _has_input, type="primary", use_container_width=True):
            _run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
            st.session_state.run_id = _run_id
            _log(f"진단 시작: run_id={_run_id}, 목표={st.session_state.goal or '(없음)'}")

            _tmp = Path(tempfile.mkdtemp())
            if st.session_state.sample_mode:
                _n = st.session_state.get("sample_count", 5)
                _paths = [str(f) for f in _sample_files[:_n] if not f.name.startswith("~$")]
                _log(f"샘플 파일 {len(_paths)}개 사용")
            else:
                _paths = []
                for _f in _uploaded:
                    _dest = _tmp / _f.name
                    _dest.write_bytes(_f.read())
                    _paths.append(str(_dest))
                _log(f"업로드 파일 {len(_paths)}개 저장")

            from agent.nodes.structure    import structure_node
            from agent.nodes.rules        import rules_node
            from agent.nodes.diagnosis    import diagnosis_node
            from agent.nodes.prescription import prescription_node

            _cur: GraphState = {"run_id": _run_id, "input_paths": _paths,
                                "decisions": [], "goal": st.session_state.goal}
            _NODE_STEPS = [
                (structure_node,    "파일 파싱 · 컬럼 분석"),
                (rules_node,        "규칙 바인딩"),
                (diagnosis_node,    "오류 진단"),
                (prescription_node, "처방 (tier · suggestion)"),
            ]
            with st.status("진단 진행 중...", expanded=True) as _st_status:
                for _i, (_fn, _label) in enumerate(_NODE_STEPS, 1):
                    st.write(f"⏳ {_i}/{len(_NODE_STEPS)}  {_label}")
                    _cur = _fn(_cur)
                    _log(f"✓ {_label}")
                    st.write(f"✅ {_i}/{len(_NODE_STEPS)}  {_label}")
                _st_status.update(label="진단 완료 ✅", state="complete")

            st.session_state.state       = _cur
            st.session_state.phase       = "diagnosed"
            st.session_state.sample_mode = False
            _log(f"진단 완료: 오류 {len(_cur.get('issues', []))}건")
            st.rerun()

    with right:
        st.subheader("Agent 상태")
        st.info("대기 중 — 파일을 업로드하고 진단을 시작하세요.")

        st.subheader("최근 실행 기억")
        if _past_runs:
            _lpath = _outputs_dir / _past_runs[0] / "report.json"
            if _lpath.exists():
                try:
                    from engine.models import RunReport as _RR
                    _lrpt = _RR(**json.loads(_lpath.read_text(encoding="utf-8")))
                    st.caption(f"마지막 실행: `{_past_runs[0]}`")
                    _rc1, _rc2 = st.columns(2)
                    _rc1.metric("처리 파일",  len(_lrpt.files))
                    _rc2.metric("재검진",     "통과" if _lrpt.recheck_passed else "미통과")
                    _rc3, _rc4 = st.columns(2)
                    _rc3.metric("총 이슈",    sum(_lrpt.issue_counts.values()))
                    _rc4.metric("잔여 오류",  len(_lrpt.remaining_issues))
                except Exception:
                    st.caption(f"마지막 실행: `{_past_runs[0]}`")
        else:
            st.caption("이전 실행 없음")


# ════════════════════════════════════════════════════════════════════════════
# ② 진단 완료 — 계획 카드
# ════════════════════════════════════════════════════════════════════════════
elif st.session_state.phase == "diagnosed":
    _state: GraphState = st.session_state.state
    _issues: list[Issue] = _state.get("issues", [])
    _auto_cnt = sum(1 for i in _issues if i.tier == "auto")
    _appr_cnt = sum(1 for i in _issues if i.tier == "approval")
    _req_cnt  = sum(1 for i in _issues if i.tier == "request")

    st.subheader("진단 결과 — 실행 계획")
    if st.session_state.goal:
        st.caption(f"목표: {st.session_state.goal}")

    # ① Agent 사고 과정 카드
    _analysis: dict = _state.get("analysis_summary", {})
    if _analysis and _analysis.get("headline"):
        with st.container(border=True):
            st.markdown(f"### 🤖 {_analysis.get('headline', '')}")
            for _f in _analysis.get("findings", []):
                st.markdown(f"- {_f}")
            if _analysis.get("recommendation"):
                st.info(f"💡 **권고**: {_analysis['recommendation']}")
        _priorities = _state.get("rule_priorities", [])
        if _priorities:
            st.caption(f"Agent 우선 검사 규칙: {' → '.join(_priorities[:5])}")
    else:
        with st.container(border=True):
            st.markdown("### 🤖 Agent 분석")
            _auto_c = sum(1 for i in _issues if i.tier == "auto")
            _appr_c = sum(1 for i in _issues if i.tier == "approval")
            _req_c  = sum(1 for i in _issues if i.tier == "request")
            _actionable = [i for i in _issues if i.tier != "info"]
            _info_c = sum(1 for i in _issues if i.tier == "info")
            if _issues:
                from collections import Counter as _Ctr
                _top = _Ctr(i.rule_id for i in _actionable).most_common(1)
                _top_rule = f"{_top[0][0]} ({_top[0][1]}건)" if _top else ""
                st.markdown(
                    f"- 총 **{len(_actionable)}건** 오류 감지 — "
                    f"자동 수정 {_auto_c}건 / 승인 필요 {_appr_c}건 / 보완 요청 {_req_c}건"
                    + (f" · 참고 {_info_c}건" if _info_c else "")
                )
                if _top_rule:
                    st.markdown(f"- 가장 많은 오류 규칙: **{_top_rule}**")
                _files_with_err = sorted({i.ref.file for i in _actionable if i.ref})
                st.markdown(f"- 오류 발생 파일: {', '.join(Path(f).name for f in _files_with_err)}")
            else:
                st.success("오류가 발견되지 않았습니다.")
            if not _analysis:
                st.caption("💡 LLM 분석 요약을 보려면 .env에 API 키를 설정하세요.")

    _cc1, _cc2, _cc3, _cc4 = st.columns(4)
    _cc1.metric("총 오류",    len(_issues))
    _cc2.metric("🤖 자동 수정", _auto_cnt, help="Agent가 즉시 처리합니다")
    _cc3.metric("👤 승인 필요", _appr_cnt, help="사용자 확인 후 수정합니다")
    _cc4.metric("📩 보완 요청", _req_cnt,  help="기관에 재제출을 요청합니다")

    # ── 파일별 오류 현황 ─────────────────────────────────────────────────────
    _file_names = sorted({i.ref.file for i in _issues if i.ref})
    if len(_file_names) > 1:
        st.subheader("파일별 오류 현황")
        _file_rows = []
        for _fn in _file_names:
            _fi = [i for i in _issues if i.ref and i.ref.file == _fn]
            _file_rows.append({
                "파일": Path(_fn).name,
                "총 오류": len(_fi),
                "🤖 자동": sum(1 for i in _fi if i.tier == "auto"),
                "👤 승인필요": sum(1 for i in _fi if i.tier == "approval"),
                "📩 보완요청": sum(1 for i in _fi if i.tier == "request"),
            })
        st.dataframe(pd.DataFrame(_file_rows), use_container_width=True, hide_index=True)

    # ── LLM 분석 결과 ────────────────────────────────────────────────────────
    _profiles: list = _state.get("profiles", [])
    _llm_profiles = [p for p in _profiles if any(c.confidence < 1.0 for c in p.columns)]
    _std_profiles  = [p for p in _profiles if all(c.confidence >= 1.0 for c in p.columns)]
    with st.expander(
        f"🤖 LLM 분석 결과 — {len(_llm_profiles)}개 시트 추론 / {len(_std_profiles)}개 표준 서식 매칭"
    ):
        if not _profiles:
            st.caption("프로필 정보 없음")
        else:
            if _std_profiles:
                st.success(
                    "표준 서식 자동 매칭: "
                    + ", ".join(f"{Path(p.file).name}/{p.sheet}" for p in _std_profiles)
                    + " — LLM 호출 불필요"
                )
            if _llm_profiles:
                st.info("아래 시트는 표준 서식 미매칭 → LLM이 컬럼 의미를 추론했습니다.")
                for _p in _llm_profiles:
                    st.markdown(f"**{Path(_p.file).name} / {_p.sheet}**")
                    st.dataframe(pd.DataFrame([
                        {"컬럼": c.name, "추론 타입": c.semantic_type,
                         "신뢰도": f"{c.confidence:.0%}",
                         "출처": "표준서식" if c.confidence >= 1.0 else "LLM 추론"}
                        for c in _p.columns
                    ]), use_container_width=True, hide_index=True)
            st.caption(
                "LLM 역할 요약 — "
                "① 컬럼 의미 추론(표준서식 없을 때) "
                "② 오류 설명 생성(규칙별 대표 1건만 호출 후 복사) "
                "③ 보완 요청 메일 작성. "
                "값 변경은 규칙 코드만 수행합니다."
            )

    if _issues:
        with st.expander("오류 상세 보기"):
            _rule_counts = pd.Series([i.rule_id for i in _issues if i.tier != "info"]).value_counts()
            st.bar_chart(_rule_counts)

            _all_tiers = sorted({i.tier    for i in _issues})
            _all_rules = sorted({i.rule_id for i in _issues})
            _all_files = sorted({i.ref.file for i in _issues if i.ref})

            _fc1, _fc2, _fc3 = st.columns(3)
            _sel_tiers = _fc1.multiselect("tier", _all_tiers, default=_all_tiers, key="f_tier")
            _sel_rules = _fc2.multiselect("규칙",  _all_rules, default=_all_rules, key="f_rule")
            _sel_files = _fc3.multiselect("파일",  _all_files, default=_all_files, key="f_file")

            _filtered = [
                i for i in _issues
                if i.tier in _sel_tiers and i.rule_id in _sel_rules
                and (i.ref.file if i.ref else "") in (_sel_files or [""])
            ]
            _PAGE = 50
            _tp = max(1, (len(_filtered) + _PAGE - 1) // _PAGE)
            _pg = st.number_input("페이지", min_value=1, max_value=_tp, value=1, step=1, key="diag_pg")
            _s  = (_pg - 1) * _PAGE
            st.caption(f"총 {len(_filtered)}건 · {_pg}/{_tp} 페이지")
            st.dataframe(pd.DataFrame([
                {"issue_id": i.issue_id, "rule_id": i.rule_id, "tier": i.tier,
                 "파일": Path(i.ref.file).name if i.ref else "-",
                 "위치": i.ref.a1 if i.ref else "-",
                 "값": i.value if i.value is not None else "(공백)",
                 "메시지": i.message, "제안": i.suggestion or ""}
                for i in _filtered[_s: _s + _PAGE]
            ]), use_container_width=True)

    st.divider()
    _btn_label = (
        "▶ 계획대로 실행"
        if _appr_cnt == 0
        else f"▶ 계획대로 실행  (승인 필요 {_appr_cnt}건 검토)"
    )
    if st.button(_btn_label, type="primary"):
        if _appr_cnt > 0:
            st.session_state.phase = "prescription"
            st.rerun()
        else:
            _auto_treat()
            st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# ③ 처방전 — 승인 필요 항목
# ════════════════════════════════════════════════════════════════════════════
elif st.session_state.phase == "prescription":
    _state = st.session_state.state
    _issues = _state.get("issues", [])
    _appr_issues = [i for i in _issues if i.tier == "approval"]

    st.subheader("처방전 — 승인 필요 항목")
    if not _appr_issues:
        st.info("승인이 필요한 항목이 없습니다. 아래 버튼을 눌러 자동 치료를 실행하세요.")
    else:
        st.caption(f"총 {len(_appr_issues)}건을 검토하세요. 나머지 auto/request 건은 자동 처리됩니다.")

    _dec_map: dict[str, dict] = {}
    for _i in _appr_issues:
        _loc = f"{Path(_i.ref.file).name} / {_i.ref.a1}" if _i.ref else "-"
        with st.expander(f"{_i.issue_id} — {_i.rule_id} @ {_loc}"):
            st.write(f"**현재 값**: `{_i.value if _i.value is not None else '(공백)'}`")
            st.write(f"**메시지**: {_i.message}")
            if _i.explanation:
                st.write(f"**설명**: {_i.explanation}")
            if _i.suggestion:
                st.write(f"**제안 값**: `{_i.suggestion}`")
            _act = st.radio(
                "처리", ["apply", "reject", "edit"],
                key=f"act_{_i.issue_id}", horizontal=True,
            )
            _ev = st.text_input("직접 입력", key=f"ev_{_i.issue_id}") if _act == "edit" else None
            _dec_map[_i.issue_id] = {"action": _act, "value": _ev}

    st.divider()
    if st.button("✅ 승인 후 치료", type="primary"):
        from agent.nodes.treatment import treatment_node
        from agent.nodes.recheck   import recheck_node

        _all_dec = (
            [Decision(issue_id=i.issue_id, action="apply",   decided_by="system") for i in _issues if i.tier == "auto"] +
            [Decision(issue_id=i.issue_id, action="request", decided_by="system") for i in _issues if i.tier == "request"] +
            [Decision(issue_id=i.issue_id, action="reject",  decided_by="system") for i in _issues if i.tier == "info"] +
            [Decision(issue_id=i.issue_id,
                      action=_dec_map.get(i.issue_id, {}).get("action", "reject"),
                      value=_dec_map.get(i.issue_id, {}).get("value"),
                      decided_by="operator")
             for i in _appr_issues]
        )
        _log(f"승인 결정 {len(_all_dec)}건 제출")
        _updated = {**_state, "decisions": _all_dec}
        with st.spinner("치료 중..."):
            _treated = treatment_node(_updated)
            _final   = recheck_node(_treated)
        st.session_state.state = _final
        st.session_state.phase = "treated"
        _log(f"치료 완료: 변경 {len(_final.get('changes', []))}건")
        st.rerun()


# ════════════════════════════════════════════════════════════════════════════
# ④ 결과
# ════════════════════════════════════════════════════════════════════════════
elif st.session_state.phase == "treated":
    _state = st.session_state.state
    if not _state:
        st.info("치료를 완료한 후 결과를 확인할 수 있습니다.")
    else:
        st.subheader("결과")

        _report = _state.get("report")
        if _report:
            _r1, _r2, _r3, _r4 = st.columns(4)
            _r1.metric("재검진",   "✅ 통과" if _report.recheck_passed else "❌ 미통과")
            _r2.metric("잔여 오류", len(_report.remaining_issues))
            _r3.metric("처리 파일", len(_report.files))
            _r4.metric("총 변경",   sum(_report.tier_counts.values()))

        _changes = _state.get("changes", [])
        if _changes:
            st.subheader("변경 이력")
            st.dataframe(pd.DataFrame([
                {"change_id": c.change_id, "issue_id": c.issue_id,
                 "위치": c.ref.a1, "이전": c.before, "이후": c.after,
                 "tier": c.tier, "처리자": c.decided_by}
                for c in _changes
            ]), use_container_width=True)

        _requests = _state.get("requests", [])
        if _requests:
            st.subheader("보완 요청 메일")
            for _ridx, _req in enumerate(_requests):
                with st.expander(
                    f"{'✅' if _req.sent else '📧'} {_req.org} — {_req.subject}",
                    expanded=not _req.sent,
                ):
                    _new_to   = st.text_input("수신자 이메일", value=_req.to or "", key=f"rto_{_ridx}")
                    _new_subj = st.text_input("제목", value=_req.subject, key=f"rsj_{_ridx}")
                    _new_body = st.text_area("본문", value=_req.body, height=200, key=f"rbd_{_ridx}")
                    if _req.sent:
                        st.success("발송 완료")
                    else:
                        if st.button(f"✉️ {_req.org} 발송", key=f"send_{_ridx}"):
                            from notify.mailer import send_request
                            _run_dir = settings.outputs_dir / (_state.get("run_id") or "unknown")
                            _sent = send_request(
                                _req.model_copy(update={
                                    "to": _new_to or None,
                                    "subject": _new_subj,
                                    "body": _new_body,
                                }),
                                _run_dir,
                            )
                            _new_reqs = list(_requests)
                            _new_reqs[_ridx] = _sent
                            st.session_state.state = {**_state, "requests": _new_reqs}
                            st.rerun()

        # 결과 다운로드
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
