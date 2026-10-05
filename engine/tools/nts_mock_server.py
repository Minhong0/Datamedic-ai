"""국세청(ODCLOUD) 상태조회 API 모의 서버 — httpx.MockTransport 핸들러."""
from __future__ import annotations

import json
from typing import Any

import httpx

MOCK_SERVICE_KEY = "mock-service-key"
MAX_BATCH = 100

JSON_CT = "application/json; charset=UTF-8"
TEXT_CT = "text/plain; charset=utf-8"

UNREGISTERED_MSG = "국세청에 등록되지 않은 사업자등록번호입니다."

_RECORD_FIELDS = (
    "b_no", "b_stt", "b_stt_cd", "tax_type", "tax_type_cd", "end_dt",
    "utcc_yn", "tax_type_change_dt", "invoice_apply_dt", "rbf_tax_type", "rbf_tax_type_cd",
)


def unregistered_record(b_no: str) -> dict[str, str]:
    """실제 서버가 미등록 번호에 돌려주는 레코드."""
    return {
        "b_no": b_no, "b_stt": "", "b_stt_cd": "",
        "tax_type": UNREGISTERED_MSG, "tax_type_cd": "",
        "end_dt": "", "utcc_yn": "", "tax_type_change_dt": "",
        "invoice_apply_dt": "", "rbf_tax_type": "", "rbf_tax_type_cd": "",
    }


def make_record(b_no: str, status: str, end_dt: str = "") -> dict[str, str]:
    """상태별 레코드. 세 상태 모두 실제 서버에서 관찰한 응답을 기준으로 한다."""
    if status == "계속":
        stt, cd = "계속사업자", "01"
    elif status == "휴업":
        stt, cd = "휴업자", "02"
    elif status == "폐업":
        stt, cd = "폐업자", "03"
    else:
        raise ValueError(f"알 수 없는 상태: {status}")
    return {
        "b_no": b_no, "b_stt": stt, "b_stt_cd": cd,
        "tax_type": "부가가치세 일반과세자", "tax_type_cd": "01",
        "end_dt": end_dt if status == "폐업" else "",
        "utcc_yn": "N", "tax_type_change_dt": "", "invoice_apply_dt": "",
        "rbf_tax_type": "해당없음", "rbf_tax_type_cd": "99",
    }


def _text(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": TEXT_CT}, content=json.dumps(payload))


def _json(status: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": JSON_CT},
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def build_handler(db: dict[str, dict[str, str]], service_key: str = MOCK_SERVICE_KEY):
    """db: {숫자 10자리: 11개 필드 레코드} → httpx.MockTransport 핸들러."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("serviceKey") != service_key:
            return _json(401, {"code": -4, "msg": "등록되지 않은 인증키 입니다."})

        try:
            body = json.loads(request.content or b"{}")
        except json.JSONDecodeError:
            return _text(411, {"status_code": "REQUEST_DATA_MALFORMED"})

        b_nos = body.get("b_no") if isinstance(body, dict) else None
        if not isinstance(b_nos, list) or not b_nos:
            return _text(411, {"status_code": "REQUEST_DATA_MALFORMED"})
        if len(b_nos) > MAX_BATCH:
            return _text(413, {"status_code": "TOO_LARGE_REQUEST"})

        data: list[dict[str, str]] = []
        matched = 0
        for b in b_nos:
            b = str(b)
            rec = db.get(b)
            if rec is None:
                data.append(unregistered_record(b))
            else:
                matched += 1
                data.append({k: rec.get(k, "") for k in _RECORD_FIELDS} | {"b_no": b})

        payload: dict[str, Any] = {"request_cnt": len(b_nos)}
        if matched:
            payload["match_cnt"] = matched
        payload["status_code"] = "OK"
        payload["data"] = data
        return _json(200, payload)

    return handler


def build_transport(db: dict[str, dict[str, str]]) -> httpx.MockTransport:
    return httpx.MockTransport(build_handler(db))
