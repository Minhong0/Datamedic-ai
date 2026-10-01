"""국세청 사업자등록정보 상태조회 클라이언트 (RULES.md R4-3 ~ R4-5).

NTS_MODE 환경변수(config.nts_mode) 에 따라 동작:
  mock — tests/fixtures/nts_mock.json 의 사전 정의 응답 사용
  off  — API 호출 없이 BIZNO_UNVERIFIED 반환
  live — 실제 ODCLOUD API 호출 (odcloud_api_key 필요)

engine/ 은 이 모듈을 직접 import 하지 않는다.
bizno.py 는 StatusProvider 프로토콜을 받아 사용하고,
실제 구현은 이 파일이 제공한다.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from engine.planning import require_capability

logger = logging.getLogger(__name__)

# ── 응답 타입 ──────────────────────────────────────────────────────────────────

class NtsRecord:
    """API 한 건의 응답 파싱 결과."""
    __slots__ = ("b_no", "b_stt", "b_stt_cd", "tax_type", "end_dt")

    def __init__(self, raw: dict) -> None:
        self.b_no: str = raw.get("b_no", "")
        self.b_stt: str = raw.get("b_stt", "") or ""
        self.b_stt_cd: str | None = raw.get("b_stt_cd")
        self.tax_type: str = raw.get("tax_type", "") or ""
        self.end_dt: str = raw.get("end_dt", "") or ""   # "YYYYMMDD" or ""


# ── StatusProvider 프로토콜 ────────────────────────────────────────────────────

@runtime_checkable
class StatusProvider(Protocol):
    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        """사업자번호(숫자 10자리) 목록 → 번호별 NtsRecord 딕셔너리."""
        ...


# ── MockProvider ───────────────────────────────────────────────────────────────

class MockProvider:
    """nts_mock.json 기반 테스트용 provider."""

    def __init__(self, mock_path: Path | None = None) -> None:
        if mock_path is None:
            mock_path = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "nts_mock.json"
        data = json.loads(mock_path.read_text(encoding="utf-8"))
        self._db: dict[str, NtsRecord] = {
            k: NtsRecord(v) for k, v in data["data"].items()
        }

    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        require_capability("bizno_api")   # 계획에 없으면 호출 전에 차단
        result: dict[str, NtsRecord] = {}
        for b in bizno_list:
            if b in self._db:
                result[b] = self._db[b]
        return result


# ── OffProvider ────────────────────────────────────────────────────────────────

class OffProvider:
    """NTS_MODE=off: API 조회 건너뜀. bizno.py는 NTS 관련 Issue를 생성하지 않는다 (RULES.md R4-5)."""
    disabled: bool = True

    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        return {}


# ── LiveProvider ───────────────────────────────────────────────────────────────

class LiveProvider:
    """실제 ODCLOUD API 호출 (최대 100건 배치, 24h 캐시)."""

    def __init__(self, api_key: str, cache_path: Path | None = None) -> None:
        self._api_key = api_key
        self._cache_path = cache_path
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
        return age < 86400   # 24시간

    def query(self, bizno_list: list[str]) -> dict[str, NtsRecord]:
        require_capability("bizno_api")   # 계획에 없으면 호출 전에 차단
        result: dict[str, NtsRecord] = {}
        to_fetch: list[str] = []

        for b in bizno_list:
            if b in self._cache and self._is_fresh(self._cache[b]):
                result[b] = NtsRecord(self._cache[b])
            else:
                to_fetch.append(b)

        if not to_fetch:
            return result

        try:
            import httpx
        except ImportError:
            logger.warning("httpx 없음 — 국세청 API 호출 불가 (pip install httpx)")
            return result

        # 최대 100건씩 배치
        for i in range(0, len(to_fetch), 100):
            batch = to_fetch[i: i + 100]
            try:
                resp = httpx.post(
                    "https://api.odcloud.kr/api/nts-businessman/v1/status",
                    params={"serviceKey": self._api_key},
                    json={"b_no": batch},
                    timeout=10.0,
                )
                resp.raise_for_status()
                items: list[dict] = resp.json().get("data", [])
                ts = datetime.now(timezone.utc).timestamp()
                for item in items:
                    b = item.get("b_no", "").replace("-", "")
                    item["_ts"] = ts
                    self._cache[b] = item
                    result[b] = NtsRecord(item)
            except Exception as exc:
                logger.warning("국세청 API 호출 실패 (batch %d): %s", i, exc)

        self._save_cache()
        return result


# ── 팩토리 ────────────────────────────────────────────────────────────────────

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
    # mock (기본)
    return MockProvider()
