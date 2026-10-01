"""T12: notify/mailer.py 테스트 (dry-run)."""
import json
import tempfile
from pathlib import Path

from engine.models import RequestItem
from notify.mailer import send, send_all


def make_req(**kw):
    defaults = dict(
        org="창원시", issues=["E-0001"],
        subject="데이터 보완 요청", body="오류가 있습니다.",
        to="test@example.com", sent=False,
    )
    defaults.update(kw)
    return RequestItem(**defaults)


def test_dry_run_saves_file(monkeypatch):
    monkeypatch.setattr("notify.mailer.settings.mail_dry_run", True)
    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp)
        req = make_req()
        result = send(req, run_dir)
        saved = run_dir / "requests" / "창원시.json"
        assert saved.exists()
        data = json.loads(saved.read_text(encoding="utf-8"))
        assert data["org"] == "창원시"
        assert result.sent is False


def test_dry_run_no_smtp(monkeypatch):
    monkeypatch.setattr("notify.mailer.settings.mail_dry_run", True)
    import smtplib
    original = smtplib.SMTP
    smtp_called = [False]

    class FakeSMTP:
        def __init__(self, *a, **k):
            smtp_called[0] = True

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    with tempfile.TemporaryDirectory() as tmp:
        send(make_req(), Path(tmp))
    assert not smtp_called[0]


def test_send_all(monkeypatch):
    monkeypatch.setattr("notify.mailer.settings.mail_dry_run", True)
    with tempfile.TemporaryDirectory() as tmp:
        reqs = [make_req(org="창원시"), make_req(org="진주시")]
        results = send_all(reqs, Path(tmp))
        assert len(results) == 2
        for org in ("창원시", "진주시"):
            assert (Path(tmp) / "requests" / f"{org}.json").exists()


def test_no_recipient_skips_send(monkeypatch):
    monkeypatch.setattr("notify.mailer.settings.mail_dry_run", False)
    with tempfile.TemporaryDirectory() as tmp:
        req = make_req(to=None)
        result = send(req, Path(tmp))
        assert result.sent is False
