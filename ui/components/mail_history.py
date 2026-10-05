"""과거 실행의 메일 열람 화면 — 저장된 초안과 발송 기록을 읽기 전용으로 보여 준다 (발송·수정 버튼 없음)."""
from __future__ import annotations

from pathlib import Path

from engine.models import RequestItem
from engine.run_history import load_requests
from notify import mailer


def mail_status(item: RequestItem, log: list[dict]) -> str:
    """발송 기록으로 본 상태: 발송됨 / 시험 저장 / 초안 (저장된 초안 파일은 발송 여부를 담지 않는다)."""
    rows = [r for r in log if r.get("기관") == item.org and r.get("제목") == item.subject]
    if any(r.get("모드") == "실제" and r.get("결과") == "성공" for r in rows):
        return "발송됨"
    if any(r.get("모드") == "시험" for r in rows):
        return "시험 저장"
    return "초안"


def render_mail_history(run_dir: Path) -> None:
    import pandas as pd
    import streamlit as st

    requests = load_requests(run_dir)
    log = mailer.read_log(run_dir)
    if not requests and not log:
        st.caption("이 실행에는 저장된 메일이 없습니다.")
        return
    st.subheader("📩 메일 (읽기 전용)")
    st.caption("과거 실행에서 만들거나 보낸 메일입니다. 이 화면에서는 수정·발송할 수 없습니다.")
    for i, (kind, item) in enumerate(requests):
        status = mail_status(item, log)
        icon = {"발송됨": "✅", "시험 저장": "🧪", "초안": "📧"}[status]
        with st.expander(f"{icon} [{kind}] {item.org} — {item.subject} ({status})"):
            st.text_input("수신자", value=item.to or "", disabled=True, key=f"mh_to_{run_dir.name}_{i}")
            st.text_input("제목", value=item.subject, disabled=True, key=f"mh_sj_{run_dir.name}_{i}")
            st.text_area("본문", value=item.body, height=220, disabled=True, key=f"mh_bd_{run_dir.name}_{i}")
    if log:
        with st.expander(f"📒 발송 기록 ({len(log)}건)"):
            st.dataframe(pd.DataFrame(log), hide_index=True, use_container_width=True)
