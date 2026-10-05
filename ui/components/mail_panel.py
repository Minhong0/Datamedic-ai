"""메일 초안 목록·발송 화면 — 보완 요청 메일과 집계 확인 요청 메일이 함께 쓴다."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from engine.models import RequestItem
from notify import mailer


def render_mail_panel(
    drafts: list[RequestItem],
    *,
    run_dir: Path,
    key: str,
    on_update: Callable[[list[RequestItem]], None],
    title: str,
    file_tag: str = "",
) -> None:
    import pandas as pd
    import streamlit as st

    if not drafts:
        return
    st.subheader(title)
    live = not mailer.settings.mail_dry_run

    msgs: dict[str, tuple[bool, str]] = st.session_state.setdefault(f"{key}_msgs", {})
    edited: list[RequestItem] = []
    for i, d in enumerate(drafts):
        with st.expander(f"{'✅' if d.sent else '📧'} {d.org} — {d.subject}", expanded=not d.sent):
            to = st.text_input("수신자", value=d.to or "", key=f"{key}_to_{i}")
            subject = st.text_input("제목", value=d.subject, key=f"{key}_sj_{i}")
            body = st.text_area("본문", value=d.body, height=220, key=f"{key}_bd_{i}")
            cur = d.model_copy(update={"to": to.strip() or None, "subject": subject, "body": body})
            edited.append(cur)
            if d.sent:
                st.success("발송 완료")
            elif st.button(f"✉️ {d.org} {'발송' if live else '저장(시험)'}", key=f"{key}_send_{i}"):
                with st.spinner("메일 처리 중..."):
                    res = mailer.send_with_result(cur, run_dir, file_tag)
                msgs[d.org] = (res.ok, res.message)
                all_ = list(drafts)
                all_[i] = res.item
                on_update(all_)
                st.rerun()
            if d.org in msgs:
                ok, message = msgs[d.org]
                (st.success if ok else st.error)(message)

    pending = [(i, c) for i, c in enumerate(edited) if not drafts[i].sent]
    if len(pending) > 1:
        confirmed = True
        if live:
            confirmed = st.checkbox(f"{len(pending)}통을 실제로 발송합니다 (되돌릴 수 없습니다)", key=f"{key}_confirm_all")
        if st.button(f"✉️ 전체 {'발송' if live else '저장(시험)'} ({len(pending)}통)", disabled=not confirmed,
                     key=f"{key}_send_all"):
            all_ = list(drafts)
            with st.spinner(f"메일 {len(pending)}통 처리 중..."):
                results = mailer.send_batch([cur for _, cur in pending], run_dir, file_tag)
            for (i, cur), res in zip(pending, results):
                msgs[cur.org] = (res.ok, res.message)
                all_[i] = res.item
            on_update(all_)
            st.rerun()

    log = mailer.read_log(run_dir)
    if log:
        with st.expander(f"📒 발송 기록 ({len(log)}건)"):
            st.dataframe(pd.DataFrame(log), hide_index=True, use_container_width=True)
