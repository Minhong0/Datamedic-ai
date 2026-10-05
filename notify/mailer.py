from __future__ import annotations

import csv
import json
import logging
import smtplib
import socket
from contextlib import ExitStack
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
    mode: str
    message: str
    error: str | None = None


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


def handled_orgs(run_dir: Path) -> set[str]:
    """메일을 실제로 보냈거나(성공) 시험 모드로 저장한(저장만 됨) 기관 이름. 발송 실패는 포함하지 않는다."""
    return {row.get("기관", "") for row in read_log(run_dir)
            if row.get("결과") in ("성공", "저장만 됨") and row.get("기관")}


def save_draft(request: RequestItem, run_dir: Path, tag: str = "") -> Path:
    """메일 내용을 파일로 저장한다 (발송하지 않는다). tag 가 있으면 `<기관명>__<tag>.json` — 같은 기관의"""
    req_dir = run_dir / "requests"
    req_dir.mkdir(parents=True, exist_ok=True)
    out_path = req_dir / (f"{request.org}__{tag}.json" if tag else f"{request.org}.json")
    out_path.write_text(json.dumps(request.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def save_drafts(requests: list[RequestItem], run_dir: Path) -> list[RequestItem]:
    """에이전트가 자동으로 만드는 초안은 저장만 한다 — 실제 발송은 담당자가 화면에서 직접 누를 때만 일어난다."""
    for r in requests:
        save_draft(r, run_dir)
    return [r.model_copy(update={"sent": False}) for r in requests]


class _Session:
    """여러 통을 보낼 때 SMTP 연결·로그인을 한 번만 한다 (통마다 새로 연결하면 통당 2~3초가 더 든다)."""

    def __init__(self) -> None:
        self._stack: ExitStack | None = None
        self._server = None

    def server(self):
        if self._server is None:
            stack = ExitStack()
            try:
                srv = stack.enter_context(smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=SMTP_TIMEOUT))
                srv.starttls()
                srv.login(settings.smtp_user, settings.smtp_password)
            except BaseException:
                stack.close()
                raise
            self._stack, self._server = stack, srv
        return self._server

    def close(self) -> None:
        """연결을 닫는다. 오류가 난 뒤에도 쓰여서, 다음 메일은 새 연결로 시작한다."""
        stack, self._stack, self._server = self._stack, None, None
        if stack is not None:
            try:
                stack.close()
            except Exception:
                pass


def send_with_result(request: RequestItem, run_dir: Path, tag: str = "", session: _Session | None = None) -> SendResult:
    """보완 요청 메일 발송. MAIL_DRY_RUN=true 이면 파일로만 저장한다. 메일 제목·본문은 건드리지 않는다."""
    out_path = save_draft(request, run_dir, tag)

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

    own = session is None
    sess = session or _Session()
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = request.subject
        msg["From"] = settings.smtp_user
        msg["To"] = request.to
        msg.attach(MIMEText(request.body, "plain", "utf-8"))

        sess.server().sendmail(settings.smtp_user, [request.to], msg.as_string())

        logger.info("메일 발송 완료: %s → %s", request.org, request.to)
        _log(run_dir, "실제", request, "성공")
        return SendResult(request.model_copy(update={"sent": True}), True, "실제", f"{request.to} 로 발송했습니다.")
    except Exception as e:
        sess.close()
        reason = explain_error(e)
        logger.error("메일 발송 실패: %s — %s", request.org, e)
        _log(run_dir, "실제", request, "실패", reason)
        return SendResult(request, False, "실제", reason, reason)
    finally:
        if own:
            sess.close()


def send_batch(requests: list[RequestItem], run_dir: Path, tag: str = "") -> list[SendResult]:
    """여러 통을 한 번의 SMTP 연결로 보낸다. 통마다 검사·기록은 send_with_result 와 같다."""
    session = _Session()
    try:
        return [send_with_result(r, run_dir, tag, session) for r in requests]
    finally:
        session.close()


def send(request: RequestItem, run_dir: Path) -> RequestItem:
    """send_with_result 의 결과 중 갱신된 RequestItem 만 돌려준다 (기존 호출부 호환)."""
    return send_with_result(request, run_dir).item


def send_all(requests: list[RequestItem], run_dir: Path) -> list[RequestItem]:
    return [r.item for r in send_batch(requests, run_dir)]


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


test_connection.__test__ = False

send_request = send
