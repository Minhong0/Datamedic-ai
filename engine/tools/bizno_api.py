"""국세청 사업자번호 상태 조회 — data.go.kr REST API."""
from __future__ import annotations

import logging
from typing import TypedDict

logger = logging.getLogger(__name__)


class BiznoStatus(TypedDict):
    valid: bool
    status: str
    tax_type: str


def lookup(bizno: str, api_key: str) -> BiznoStatus | None:
    """사업자번호로 국세청 상태를 조회한다."""
    if not api_key:
        return None
    try:
        import httpx
        digits = "".join(c for c in bizno if c.isdigit())
        if len(digits) != 10:
            return None
        resp = httpx.post(
            "https://api.odcloud.kr/api/nts-businessman/v1/status",
            params={"serviceKey": api_key},
            json={"b_no": [digits]},
            timeout=5.0,
        )
        resp.raise_for_status()
        items = resp.json().get("data", [])
        if not items:
            return None
        item = items[0]
        b_stt = item.get("b_stt", "") or "조회불가"
        return BiznoStatus(
            valid=b_stt == "계속사업자",
            status=b_stt,
            tax_type=item.get("tax_type", ""),
        )
    except Exception as exc:
        logger.debug("사업자번호 API 오류 (%s): %s", bizno, exc)
        return None
