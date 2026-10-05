"""국세청 사업자등록정보 상태조회 클라이언트."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from engine.planning import require_capability
from engine.tools import nts_mock_server

logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

NTS_STATUS_URL = "https://api.odcloud.kr/api/nts-businessman/v1/status"
MAX_BATCH = nts_mock_server.MAX_BATCH
DEFAULT_MOCK_PATH = Path(__file__).parent.parent.parent / "data" / "mock" / "nts_mock.json"

STT_UNREGISTERED = "국세청 미등록"
_UNREGISTERED_HINT = "등록되지 않은"


class NtsRecord:
    """API 한 건의 응답 파싱 결과."""
    __slots__ = ("b_no", "b_stt", "b_stt_cd", "tax_type", "tax_type_cd", "end_dt", "raw")

    def __init__(self, raw: dict) -> None:
        self.raw: dict = dict(raw)
        self.b_no: str = re.sub(r"\D", "", raw.get("b_no", "") or "")
        stt: str = raw.get("b_stt", "") or ""
        tax_type: str = raw.get("tax_type", "") or ""
        if not stt and _UNREGISTERED_HINT in tax_type:
            stt = STT_UNREGISTERED
        self.b_stt: str = stt
        self.b_stt_cd: str | None = raw.get("b_stt_cd")
        self.tax_type: str = tax_type
        self.tax_type_cd: str = raw.get("tax_type_cd", "") or ""
        self.end_dt: str = raw.get("end_dt", "") or ""


@runtime_checkable
class StatusProvider(Protocol):
    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        """사업자번호(숫자 10자리) 목록 → 번호별 NtsRecord 딕셔너리."""
        ...


class OffProvider:
    """NTS_MODE=off: API 조회 건너뜀. bizno.py는 NTS 관련 Issue를 생성하지 않는다."""
    disabled: bool = True

    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        return {}


def _error_detail(resp: httpx.Response) -> str:
    """오류 응답 본문에서 사람이 읽을 요약. 401은 {code,msg}, 411/413은 {status_code}."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:100]
    if isinstance(body, dict):
        return str(body.get("msg") or body.get("status_code") or body)[:100]
    return str(body)[:100]


class LiveProvider:
    """ODCLOUD 상태조회 API 호출 (최대 100건 배치, 24h 캐시)."""

    def __init__(
        self,
        api_key: str,
        cache_path: Path | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._cache_path = cache_path
        self._transport = transport
        self._cache: dict[str, dict] = self._load_cache()

    def _load_cache(self) -> dict[str, dict]:
        if self._cache_path and self._cache_path.exists():
            try:
                return json.loads(self._cache_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_cache(self) -> None:
        if self._cache_path:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _is_fresh(self, entry: dict) -> bool:
        ts = entry.get("_ts")
        if not ts:
            return False
        age = (datetime.now(timezone.utc).timestamp() - ts)
        return age < 86400

    @staticmethod
    def _digits(bizno_list: list[str]) -> list[str]:
        """하이픈을 제거한 10자리 숫자만 남긴다. 실제 서버는 하이픈이 있으면 미등록으로 응답한다."""
        seen: dict[str, None] = {}
        for b in bizno_list:
            d = re.sub(r"\D", "", b or "")
            if len(d) == 10:
                seen[d] = None
        return list(seen)

    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        require_capability("bizno_api")
        result: dict[str, NtsRecord] = {}
        to_fetch: list[str] = []

        for b in self._digits(bizno_list):
            if b in self._cache and self._is_fresh(self._cache[b]):
                result[b] = NtsRecord(self._cache[b])
            else:
                to_fetch.append(b)

        if not to_fetch:
            return result

        with httpx.Client(transport=self._transport, timeout=10.0) as client:
            for i in range(0, len(to_fetch), MAX_BATCH):
                batch = to_fetch[i: i + MAX_BATCH]
                try:
                    resp = client.post(
                        NTS_STATUS_URL,
                        params={"serviceKey": self._api_key},
                        json={"b_no": batch},
                    )
                    if resp.status_code != 200:
                        logger.warning("국세청 API 오류 (batch %d): HTTP %d %s",
                                       i, resp.status_code, _error_detail(resp))
                        continue
                    body = resp.json()
                    if body.get("status_code") != "OK":
                        logger.warning("국세청 API 비정상 응답 (batch %d): %s", i, body.get("status_code"))
                        continue
                    ts = datetime.now(timezone.utc).timestamp()
                    for item in body.get("data", []):
                        b = re.sub(r"\D", "", item.get("b_no", "") or "")
                        if not b:
                            continue
                        item["_ts"] = ts
                        self._cache[b] = item
                        result[b] = NtsRecord(item)
                except Exception as exc:
                    logger.warning("국세청 API 호출 실패 (batch %d): %s", i, exc)

        self._save_cache()
        return result


class MockProvider(LiveProvider):
    """모의 서버를 호출하는 LiveProvider. 요청 생성·응답 파싱·배치 분할은 live와 같은 코드다."""

    def __init__(
        self,
        mock_path: Path | None = None,
        db: dict[str, dict] | None = None,
    ) -> None:
        if db is None:
            path = mock_path or DEFAULT_MOCK_PATH
            db = json.loads(path.read_text(encoding="utf-8"))["data"]
        super().__init__(
            api_key=nts_mock_server.MOCK_SERVICE_KEY,
            cache_path=None,
            transport=nts_mock_server.build_transport(db),
        )


def get_provider() -> StatusProvider:
    """config.nts_mode 에 따라 적절한 provider 반환."""
    from config import settings
    require_capability("bizno_api")
    mode = settings.nts_mode.strip().lower()
    if mode == "live":
        return LiveProvider(
            api_key=settings.odcloud_api_key,
            cache_path=settings.cache_dir / "nts_status.json",
        )
    if mode == "off":
        return OffProvider()
    return MockProvider()
