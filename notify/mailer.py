from __future__ import annotations

import json
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import settings
from engine.models import RequestItem

logger = logging.getLogger(__name__)


def send(request: RequestItem, run_dir: Path) -> RequestItem:
    """보완 요청 메일 발송. MAIL_DRY_RUN=true이면 파일로만 저장."""
    req_dir = run_dir / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)

    out_path = req_dir / f"{request.org}.json"
    out_path.write_text(
        json.dumps(request.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if settings.mail_dry_run:
        logger.info("[DRY-RUN] 메일 저장: %s", out_path)
        return request.model_copy(update={"sent": False})

    if not request.to:
        logger.warning("수신 주소 없음 — 발송 건너뜀: %s", request.org)
        return request

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = request.subject
        msg["From"] = settings.smtp_user
        msg["To"] = request.to
        msg.attach(MIMEText(request.body, "plain", "utf-8"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, [request.to], msg.as_string())

        logger.info("메일 발송 완료: %s → %s", request.org, request.to)
        return request.model_copy(update={"sent": True})
    except Exception as e:
        logger.error("메일 발송 실패: %s — %s", request.org, e)
        return request


def send_all(requests: list[RequestItem], run_dir: Path) -> list[RequestItem]:
    return [send(r, run_dir) for r in requests]


# send의 별칭 — app.py에서 단일 발송 시 명시적으로 사용
send_request = send
