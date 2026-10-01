"""국세청(ODCLOUD) 상태조회 API 모의 서버 — httpx.MockTransport 핸들러.

실제 서버를 2026-10-01에 live 호출해 관찰한 동작만 재현한다
(실제 서버 응답을 캡처해 비교했다).

  관찰한 동작
  - 200: {request_cnt, match_cnt, status_code:"OK", data:[11개 필드]} (JSON)
  - 조회된 건이 0건이면 match_cnt 필드가 응답에서 빠진다
  - DB에 없는 번호는 오류가 아니라 "국세청에 등록되지 않은 사업자등록번호입니다." 레코드로 응답
  - 하이픈이 들어간 번호는 형식 오류가 아니라 미등록으로 응답 (HTTP 200)
  - 401: {"code": -4, "msg": "등록되지 않은 인증키 입니다."} (JSON)
  - 411: {"status_code": "REQUEST_DATA_MALFORMED"} (text/plain) — b_no 누락·빈 목록
  - 413: {"status_code": "TOO_LARGE_REQUEST"} (text/plain) — 101건 이상

  - 폐업자: b_stt="폐업자", b_stt_cd="03", end_dt에 폐업일 YYYYMMDD. 실제 폐업자 2건을 관찰했다
    (과세유형은 "일반과세자"(01)와 "과세특례자"(03)가 모두 있었다). 나머지 필드는 계속사업자와 같다.

  - 휴업자: b_stt="휴업자", b_stt_cd="02", end_dt는 비어 있다(폐업일만 end_dt에 들어온다). 실제 2건을 관찰했다.

  관찰하지 못한 동작
  - 5xx 서버 오류 응답, 호출 제한.

이 모듈은 네트워크를 쓰지 않는다. 엔진은 같은 LiveProvider 코드로 실제 서버와 이 모의 서버를 모두 호출한다.
"""
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
            rec = db.get(b)            # 하이픈이 섞인 번호는 여기서 걸러져 미등록이 된다
            if rec is None:
                data.append(unregistered_record(b))
            else:
                matched += 1
                data.append({k: rec.get(k, "") for k in _RECORD_FIELDS} | {"b_no": b})

        payload: dict[str, Any] = {"request_cnt": len(b_nos)}
        if matched:                    # 0건이면 match_cnt가 응답에서 빠진다 (live 관찰)
            payload["match_cnt"] = matched
        payload["status_code"] = "OK"
        payload["data"] = data
        return _json(200, payload)

    return handler


def build_transport(db: dict[str, dict[str, str]]) -> httpx.MockTransport:
    return httpx.MockTransport(build_handler(db))
