from __future__ import annotations

import csv
import json
import logging
import smtplib
import socket
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from config import settings
from engine.models import RequestItem

logger = logging.getLogger(__name__)

SMTP_TIMEOUT = 20
LOG_NAME = "mail_log.csv"
LOG_HEADER = ["시각", "모드", "기관", "수신자", "제목", "결과", "오류"]


@dataclass
class SendResult:
    """발송 한 건의 결과 — 화면에 보여 줄 한국어 메시지와 함께."""
    item: RequestItem
    ok: bool
    mode: str            # "시험" | "실제"
    message: str
    error: str | None = None


# ── 모드 · 설정 ──────────────────────────────────────────────────────────────

def smtp_configured() -> bool:
    return bool(settings.smtp_user and settings.smtp_password)


def _masked_user() -> str:
    user = settings.smtp_user or ""
    name, _, domain = user.partition("@")
    return f"{name[:1]}***@{domain}" if domain else "(미설정)"


def mode_label() -> tuple[str, str]:
    """화면 상단에 보여 줄 발송 모드 — (수준, 문구). 수준: info | warning | error."""
    if settings.mail_dry_run:
        return "info", "📪 시험 모드 — 실제로 발송되지 않고 메일 내용이 파일로만 저장됩니다. (실제 발송: .env 의 MAIL_DRY_RUN=false)"
    if not smtp_configured():
        return "error", "📪 실제 발송 모드이지만 SMTP 계정이 설정되지 않았습니다 (.env 의 SMTP_USER, SMTP_PASSWORD)."
    return "warning", f"📬 실제 발송 모드 — 발송 버튼을 누르면 {_masked_user()} 계정으로 메일이 나갑니다."


# ── 오류 설명 ────────────────────────────────────────────────────────────────

def explain_error(exc: BaseException) -> str:
    """SMTP 예외를 사용자가 고칠 수 있는 한국어 안내로 바꾼다 (비밀번호 등 비밀값은 넣지 않는다)."""
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return ("SMTP 로그인에 실패했습니다. Gmail은 일반 비밀번호가 아니라 '앱 비밀번호'(2단계 인증 필요)를 써야 합니다. "
                "SMTP_USER, SMTP_PASSWORD 를 확인하세요.")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return f"수신 주소가 거부되었습니다: {', '.join(exc.recipients) or '(없음)'}"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "보내는 주소가 거부되었습니다. SMTP_USER 가 SMTP 계정과 같은 주소인지 확인하세요."
    if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, socket.timeout, TimeoutError, OSError)):
        return (f"SMTP 서버({settings.smtp_host}:{settings.smtp_port})에 연결하지 못했습니다. "
                f"네트워크·방화벽·SMTP_HOST, SMTP_PORT 를 확인하세요. ({type(exc).__name__})")
    return f"발송 중 오류가 발생했습니다: {type(exc).__name__}: {exc}"


# ── 기록 ─────────────────────────────────────────────────────────────────────

def _log_path(run_dir: Path) -> Path:
    return run_dir / LOG_NAME


def _log(run_dir: Path, mode: str, req: RequestItem, result: str, error: str = "") -> None:
    path = _log_path(run_dir)
    new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(LOG_HEADER)
        w.writerow([datetime.now().strftime("%Y-%m-%d %H:%M:%S"), mode, req.org, req.to or "", req.subject, result, error])


def already_sent(run_dir: Path, req: RequestItem) -> bool:
    """같은 기관·수신자·제목의 메일이 이미 실제로 발송됐는가 (중복 발송 방지)."""
    path = _log_path(run_dir)
    if not path.exists():
        return False
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if (row.get("모드") == "실제" and row.get("결과") == "성공" and row.get("기관") == req.org
                    and row.get("수신자") == (req.to or "") and row.get("제목") == req.subject):
                return True
    return False


def read_log(run_dir: Path) -> list[dict]:
    path = _log_path(run_dir)
    if not path.exists():
        return []
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


# ── 초안 저장 · 발송 ──────────────────────────────────────────────────────────

def save_draft(request: RequestItem, run_dir: Path) -> Path:
    """메일 내용을 파일로 저장한다 (발송하지 않는다)."""
    req_dir = run_dir / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    out_path = req_dir / f"{request.org}.json"
    out_path.write_text(json.dumps(request.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def save_drafts(requests: list[RequestItem], run_dir: Path) -> list[RequestItem]:
    """에이전트가 자동으로 만드는 초안은 저장만 한다 — 실제 발송은 담당자가 화면에서 직접 누를 때만 일어난다."""
    for r in requests:
        save_draft(r, run_dir)
    return [r.model_copy(update={"sent": False}) for r in requests]


def send_with_result(request: RequestItem, run_dir: Path) -> SendResult:
    """보완 요청 메일 발송. MAIL_DRY_RUN=true 이면 파일로만 저장한다. 메일 제목·본문은 건드리지 않는다."""
    out_path = save_draft(request, run_dir)

    if settings.mail_dry_run:
        logger.info("[DRY-RUN] 메일 저장: %s", out_path)
        _log(run_dir, "시험", request, "저장만 됨")
        return SendResult(request.model_copy(update={"sent": False}), True, "시험",
                          f"시험 모드 — 실제로 발송되지 않았습니다. 메일 내용을 저장했습니다: {out_path}")

    if request.sent or already_sent(run_dir, request):
        return SendResult(request, False, "실제", "이미 발송된 메일입니다 (중복 발송 방지).", "중복")
    if not request.to:
        logger.warning("수신 주소 없음 — 발송 건너뜀: %s", request.org)
        _log(run_dir, "실제", request, "실패", "수신자 없음")
        return SendResult(request, False, "실제", "수신자 주소가 없습니다. 주소를 입력해 주세요.", "수신자 없음")
    if not smtp_configured():
        _log(run_dir, "실제", request, "실패", "SMTP 계정 미설정")
        return SendResult(request, False, "실제", "SMTP 계정이 설정되지 않았습니다 (.env 의 SMTP_USER, SMTP_PASSWORD).",
                          "SMTP 계정 미설정")

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = request.subject
        msg["From"] = settings.smtp_user
        msg["To"] = request.to
        msg.attach(MIMEText(request.body, "plain", "utf-8"))

        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
            server.sendmail(settings.smtp_user, [request.to], msg.as_string())

        logger.info("메일 발송 완료: %s → %s", request.org, request.to)
        _log(run_dir, "실제", request, "성공")
        return SendResult(request.model_copy(update={"sent": True}), True, "실제", f"{request.to} 로 발송했습니다.")
    except Exception as e:
        reason = explain_error(e)
        logger.error("메일 발송 실패: %s — %s", request.org, e)
        _log(run_dir, "실제", request, "실패", reason)
        return SendResult(request, False, "실제", reason, reason)


def send(request: RequestItem, run_dir: Path) -> RequestItem:
    """send_with_result 의 결과 중 갱신된 RequestItem 만 돌려준다."""
    return send_with_result(request, run_dir).item


def send_all(requests: list[RequestItem], run_dir: Path) -> list[RequestItem]:
    return [send(r, run_dir) for r in requests]


def test_connection() -> tuple[bool, str]:
    """메일을 보내지 않고 SMTP 서버 연결·로그인만 확인한다 → (성공 여부, 한국어 안내)."""
    if not smtp_configured():
        return False, "SMTP 계정이 설정되지 않았습니다 (.env 의 SMTP_USER, SMTP_PASSWORD)."
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT) as server:
            server.starttls()
            server.login(settings.smtp_user, settings.smtp_password)
        return True, f"연결과 로그인에 성공했습니다 ({settings.smtp_host}:{settings.smtp_port}, {_masked_user()})."
    except Exception as e:
        return False, explain_error(e)


test_connection.__test__ = False   # pytest 가 테스트 함수로 수집하지 않게 한다

# send의 별칭 — app.py에서 단일 발송 시 명시적으로 사용
send_request = send
