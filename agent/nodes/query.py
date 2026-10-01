"""8장 자연어 질문 → 집계 규칙 서브그래프 노드.

흐름:
  query_input → translate(LLM) → validate(code)
    ├─ 슬롯 부족 → clarify_question 생성 → (UI interrupt) → translate 재시도
    ├─ 범위 밖   → out_of_scope 응답 → 종료
    └─ 완전     → confirm_card 생성 → (UI interrupt) → execute → respond(LLM) → [save_as_rule]

노드 함수들은 QueryState를 받아 QueryState를 반환한다.
UI(Streamlit)가 interrupt 지점에서 사용자 입력을 받아 상태를 업데이트한 뒤 재호출한다.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel

from config import settings
from engine.models import Dataset, GroupIssue, Period, RuleSpec, Template
from engine.rules.aggregate import (
    STANDARD_COLUMNS,
    _next_spec_id,
    load_business_master,
    run_max_count,
    run_near_duplicate,
    run_sum_limit,
    validate_spec,
)
from engine.rules.base import RuleContext

logger = logging.getLogger(__name__)

_MEMORY_FILE = settings.data_dir / "memory" / "rules.json"
_MASTER_FILE = settings.standard_dir / "business_master.csv"

MAX_CLARIFY_ROUNDS = 2


# ── 상태 ──────────────────────────────────────────────────────────────────────

class QueryState(TypedDict, total=False):
    query_text: str                      # 사용자 원문 질문
    clarify_count: int                   # 되묻기 횟수 (최대 MAX_CLARIFY_ROUNDS)
    translation: dict                    # LLM 번역 결과 (TranslationResponse.model_dump())
    missing_slots: list[str]             # 미충족 필수 슬롯 이름
    intent: Literal["query", "out_of_scope", ""]
    confirmed_spec: RuleSpec | None      # 사용자 확인 완료된 RuleSpec
    confirm_card: dict                   # UI에 보여줄 확인 카드
    clarify_question: str                # 되묻기 질문 문자열
    group_issues: list[GroupIssue]       # 실행 결과
    response_text: str                   # LLM 생성 요약 문장
    saved: bool                          # memory/rules.json 저장 여부


# ── LLM 번역 스키마 ────────────────────────────────────────────────────────────

class TranslationResponse(BaseModel):
    intent: Literal["query", "out_of_scope"] = "query"
    template: Template | None = None
    group_by: list[str] = []
    params: dict = {}
    amount_text: str | None = None
    period: Period | None = None
    filter_business: str | None = None
    missing_slots: list[str] = []
    description: str = ""


# ── 필수 슬롯 정의 ─────────────────────────────────────────────────────────────

_REQUIRED_SLOTS: dict[str, list[str]] = {
    "MAX_COUNT":       ["period", "group_by", "count_distinct"],
    "NEAR_DUPLICATE":  ["period", "date_window_days"],
    "SUM_LIMIT":       ["period", "scope"],
}

_CLARIFY_QUESTIONS: dict[str, str] = {
    "period":          "올해 기준으로 볼까요, 전체 기간으로 볼까요? (year / all)",
    "group_by":        "같은 시군 안에서만 볼까요, 도 전체로 볼까요?",
    "count_distinct":  "서로 다른 사업 개수를 셀까요, 같은 사업의 횟수를 셀까요?",
    "date_window_days": "며칠 이내를 같은 건으로 볼까요? (기본 30일)",
    "scope":           "사업별 한도인가요, 모든 사업 합산 한도인가요? (per_business / total)",
    "max_amount":      "한도 금액을 정확히 알려주세요 (예: 5천만원)",
}


def _collect_missing(t: TranslationResponse) -> list[str]:
    """필수 슬롯 중 비어 있는 것을 수집한다."""
    missing: list[str] = list(t.missing_slots)  # LLM이 이미 표시한 것 포함

    if t.template is None:
        return missing

    # period
    if t.period is None and "period" not in missing:
        missing.append("period")

    if t.template == "MAX_COUNT":
        if not t.group_by and "group_by" not in missing:
            missing.append("group_by")
        if "count_distinct" not in t.params and "count_distinct" not in missing:
            missing.append("count_distinct")

    elif t.template == "NEAR_DUPLICATE":
        if "date_window_days" not in t.params and "date_window_days" not in missing:
            missing.append("date_window_days")

    elif t.template == "SUM_LIMIT":
        if "scope" not in t.params and "scope" not in missing:
            missing.append("scope")
        if t.amount_text is None and "max_amount" not in t.params and "max_amount" not in missing:
            missing.append("max_amount")

    return missing


_AMOUNT_RE = re.compile(r"[\d,]+(?:\.\d+)?(?:\s*(?:억|만|천))*\s*원?")
_INJECTION_RE = re.compile(r"이전\s*지시|지시\s*무시|bypass|ignore all|결과.*정상|모두\s*삭제")


def _extract_amount_text(text: str) -> str | None:
    """텍스트에서 한국어 금액 표현을 추출한다 (normalize_amount에 넘길 원문)."""
    m = _AMOUNT_RE.search(text)
    return m.group(0).strip() if m else None


def _keyword_translate(query: str) -> TranslationResponse | None:
    """LLM 없이 키워드 기반으로 번역을 시도한다.

    LLM API 키가 없는 데모 환경에서 예시 질문 3종이 동작하도록 보장한다.
    확신할 수 없는 슬롯은 missing_slots에 넣어 되묻기로 처리한다.
    """
    if _INJECTION_RE.search(query):
        return TranslationResponse(intent="out_of_scope")

    q = query  # 금액 추출 등 원문 필요
    ql = query.lower()

    # ── SUM_LIMIT: 한도·초과·넘다 ───────────────────────────────────────────
    sum_kw = ["한도", "초과", "넘는", "넘은", "넘다", "넘어"]
    if any(w in ql for w in sum_kw):
        amount = _extract_amount_text(q)
        params: dict = {"sum_column": "지원금액"}
        missing: list[str] = []

        if not amount:
            missing.append("max_amount")

        if any(w in ql for w in ["합산", "total", "전체 합산", "모든 사업", "총합", "전체 기준"]):
            params["scope"] = "total"
        elif any(w in ql for w in ["per_business", "사업별", "각 사업", "사업 별", "사업당", "사업 기준"]):
            params["scope"] = "per_business"
        else:
            missing.append("scope")

        period: Period = "all" if ("전체" in ql and "기간" in ql) else "year"

        return TranslationResponse(
            intent="query",
            template="SUM_LIMIT",
            group_by=["사업자번호"],
            params=params,
            amount_text=amount,
            period=period,
            missing_slots=missing,
            description="지원금 합계 한도 초과 검사",
        )

    # ── NEAR_DUPLICATE: 이중 지급·두 번 나감 (MAX_COUNT보다 먼저 체크) ──────
    dup_kw = ["이중 지급", "이중지급", "두 번 나", "두번 나", "두 번 출금",
              "중복 지급", "중복지급", "두 번 이체", "두 번 나간", "두번 나간",
              "같은 건이 두", "같은건이 두", "두 번 지급", "두번 지급"]
    if any(w in ql for w in dup_kw):
        day_m = re.search(r"(\d+)\s*일", ql)
        window = day_m.group(1) if day_m else "30"

        return TranslationResponse(
            intent="query",
            template="NEAR_DUPLICATE",
            group_by=["사업자번호", "사업명"],
            params={"amount_tolerance_pct": "0", "date_window_days": window},
            period="year",
            missing_slots=[],
            description="이중 지급 의심 검사",
        )

    # ── MAX_COUNT: 중복 수혜·두 개 이상 ────────────────────────────────────
    # "두 번 나간" 같은 NEAR_DUPLICATE 문구가 걸리지 않도록 명시적 키워드만 사용
    max_kw = ["두 개 이상", "두개 이상", "두 가지 이상", "두가지 이상",
              "중복 수혜", "두 사업 이상", "사업 두 개", "사업이 두 개",
              "여러 사업", "두 번 이상 받", "두번 이상 받"]
    if any(w in ql for w in max_kw):
        if "시군" in ql or "기관" in ql:
            group_by = ["기관명", "사업자번호"]
        else:
            group_by = ["사업자번호"]

        m = re.search(r"(\d+)\s*(?:개|건|회|가지|사업)\s*이상", ql)
        max_val = str(int(m.group(1)) - 1) if m else "1"

        return TranslationResponse(
            intent="query",
            template="MAX_COUNT",
            group_by=group_by,
            params={"count_distinct": "사업명", "max": max_val},
            period="year",
            missing_slots=[],
            description="중복 수혜(허용 횟수 초과) 검사",
        )

    return None  # 키워드 미탐지 → LLM 결과에 맡김


def _apply_defaults(t: TranslationResponse) -> TranslationResponse:
    """최대 되묻기 횟수 초과 시 기본값으로 채운다."""
    data = t.model_dump()

    if data["period"] is None:
        data["period"] = "year"

    p = data["params"]
    if t.template == "MAX_COUNT":
        if "count_distinct" not in p:
            p["count_distinct"] = "사업명"
        if "max" not in p:
            p["max"] = "1"
        if not data["group_by"]:
            data["group_by"] = ["사업자번호"]
        data["_defaults_applied"] = True

    elif t.template == "NEAR_DUPLICATE":
        p.setdefault("date_window_days", "30")
        p.setdefault("amount_tolerance_pct", "0")
        if not data["group_by"]:
            data["group_by"] = ["사업자번호", "사업명"]
        data["_defaults_applied"] = True

    elif t.template == "SUM_LIMIT":
        p.setdefault("scope", "per_business")
        p.setdefault("sum_column", "지원금액")
        data["_defaults_applied"] = True

    return TranslationResponse(**{k: v for k, v in data.items() if k in TranslationResponse.model_fields})


def _translation_to_spec(t: TranslationResponse, spec_id: str | None = None) -> RuleSpec:
    """TranslationResponse → RuleSpec (검증 전)."""
    from engine.rules.amount import normalize_amount

    params = dict(t.params)

    # 금액 텍스트를 R2 함수로 변환
    if t.amount_text and "max_amount" not in params:
        result = normalize_amount(t.amount_text)
        if result.value is not None:
            params["max_amount"] = result.value
        # 변환 실패면 max_amount 없이 진행 (validate_spec에서 처리)

    return RuleSpec(
        spec_id=spec_id or _next_spec_id(),
        template=t.template,            # type: ignore[arg-type]
        description=t.description or f"{t.template} 규칙",
        group_by=t.group_by,
        params=params,
        period=t.period or "year",
        filter_business=t.filter_business,
        tier={"MAX_COUNT": "approval", "NEAR_DUPLICATE": "approval", "SUM_LIMIT": "request"}.get(
            t.template or "", "approval"
        ),
        source="user_query",
    )


# ── 노드 함수 ─────────────────────────────────────────────────────────────────

def translate_node(state: QueryState) -> QueryState:
    """LLM으로 자연어 질문을 TranslationResponse JSON으로 번역한다."""
    from llm.client import complete_json

    query = state.get("query_text", "")
    if not query:
        return {**state, "intent": "out_of_scope"}

    # 사업명 목록 수집
    biz_specs = load_business_master(_MASTER_FILE)
    biz_names = ", ".join({s.filter_business for s in biz_specs if s.filter_business}) or "없음"

    resp: TranslationResponse | None = None
    try:
        resp = complete_json(
            "rule_request",
            {"user_query": query, "business_names": biz_names},
            TranslationResponse,
        )
    except Exception as e:
        logger.warning("번역 LLM 호출 실패 → 키워드 fallback 시도: %s", e)

    # LLM 실패 또는 out_of_scope → 키워드 기반 재시도
    if resp is None or resp.intent == "out_of_scope":
        kw = _keyword_translate(query)
        if kw is not None:
            resp = kw
            logger.info("키워드 번역 성공: template=%s", kw.template)

    if resp is None:
        resp = TranslationResponse(intent="out_of_scope")

    return {
        **state,
        "translation": resp.model_dump(),
        "intent": resp.intent,
        "missing_slots": _collect_missing(resp),
    }


def validate_node(state: QueryState) -> QueryState:
    """번역 결과를 검증해 complete / missing / out_of_scope 상태를 확정한다."""
    intent = state.get("intent", "")
    if intent == "out_of_scope":
        return state

    raw = state.get("translation", {})
    t = TranslationResponse(**raw)

    if t.template is None:
        return {**state, "intent": "out_of_scope"}

    missing = _collect_missing(t)

    if missing:
        clarify_count = state.get("clarify_count", 0)
        if clarify_count >= MAX_CLARIFY_ROUNDS:
            # 기본값으로 채우고 진행
            t = _apply_defaults(t)
            missing = []
            question = "(기본값 적용됨) 다음 조건으로 검사합니다."
        else:
            first_missing = missing[0]
            question = _CLARIFY_QUESTIONS.get(first_missing, f"'{first_missing}'을 알려주세요")
            return {
                **state,
                "missing_slots": missing,
                "clarify_question": question,
                "translation": t.model_dump(),
            }

    # 완전한 번역 → RuleSpec 생성 후 validate_spec
    spec = _translation_to_spec(t)
    errors = validate_spec(spec)
    if errors:
        return {
            **state,
            "missing_slots": errors,
            "clarify_question": errors[0],
            "translation": t.model_dump(),
        }

    # 확인 카드 생성
    card = _make_confirm_card(spec)
    return {
        **state,
        "missing_slots": [],
        "clarify_question": "",
        "confirmed_spec": spec,
        "confirm_card": card,
        "translation": t.model_dump(),
    }


def clarify_node(state: QueryState, user_answer: str) -> QueryState:
    """되묻기 답변을 기존 query_text에 병합해 재번역 준비를 한다."""
    clarify_count = state.get("clarify_count", 0) + 1
    combined_query = f"{state.get('query_text', '')} / {user_answer}"
    return {
        **state,
        "query_text": combined_query,
        "clarify_count": clarify_count,
    }


def _make_confirm_card(spec: RuleSpec) -> dict:
    """확인 카드 dict 생성."""
    template_kr = {"MAX_COUNT": "중복 수혜 검사", "NEAR_DUPLICATE": "이중 지급 의심 검사", "SUM_LIMIT": "한도 초과 검사"}
    card: dict = {
        "검사 종류": template_kr.get(spec.template, spec.template),
        "묶는 기준": ", ".join(spec.group_by),
        "기간": spec.period,
        "설명": spec.description,
    }
    p = spec.params
    if spec.template == "MAX_COUNT":
        card["허용 횟수"] = p.get("max", "1")
        card["집계 대상"] = p.get("count_distinct", "사업명")
    elif spec.template == "NEAR_DUPLICATE":
        card["날짜 간격"] = f"{p.get('date_window_days', 30)}일"
        card["금액 허용 오차"] = f"{p.get('amount_tolerance_pct', 0)}%"
    elif spec.template == "SUM_LIMIT":
        card["한도"] = f"{int(p.get('max_amount', 0)):,}원"
        card["범위"] = p.get("scope", "per_business")
    if spec.filter_business:
        card["사업 필터"] = spec.filter_business
    return card


def execute_node(state: QueryState, dataset: Dataset) -> QueryState:
    """confirmed_spec에 따라 집계 규칙을 실행한다."""
    spec = state.get("confirmed_spec")
    if spec is None:
        return {**state, "group_issues": []}

    ctx = RuleContext()
    try:
        if spec.template == "MAX_COUNT":
            issues = run_max_count(dataset, spec, ctx)
        elif spec.template == "NEAR_DUPLICATE":
            issues = run_near_duplicate(dataset, spec, ctx)
        elif spec.template == "SUM_LIMIT":
            issues = run_sum_limit(dataset, spec, ctx)
        else:
            issues = []
    except Exception as e:
        logger.error("집계 규칙 실행 오류: %s", e)
        issues = []

    return {**state, "group_issues": issues}


def respond_node(state: QueryState) -> QueryState:
    """LLM으로 실행 결과 요약 문장을 생성한다.

    숫자·건수·기업명은 execute 결과에서 주입하고, LLM이 직접 계산하지 않는다.
    사업자번호는 마스킹된 값으로 전달한다.
    """
    from engine.masking import mask_bizno
    from llm.client import complete_json

    spec = state.get("confirmed_spec")
    issues = state.get("group_issues", [])

    if not issues:
        response = "조건에 해당하는 건이 없습니다."
        if spec:
            response += f" (검사 조건: {spec.description})"
        return {**state, "response_text": response}

    # 결과 요약 데이터 구성 (LLM에 넘길 통계, 사업자번호 마스킹)
    issue_count = len(issues)
    sample_groups = []
    for iss in issues[:3]:
        masked_key = {
            k: (mask_bizno(v) if k == "사업자번호" else v)
            for k, v in iss.group_key.items()
        }
        sample_groups.append({"group": masked_key, "metric": iss.metric})

    try:
        class SummaryResp(BaseModel):
            summary: str

        resp = complete_json(
            "explain_issue",
            {
                "rule_id": spec.template if spec else "AGGREGATE",
                "value": f"{issue_count}건 탐지",
                "message": f"집계 규칙 실행 결과: {issue_count}건 탐지. 대표 그룹: {sample_groups}",
                "user_goal": spec.description if spec else "집계 규칙 검사",
            },
            SummaryResp,
        )
        response = resp.summary if hasattr(resp, "summary") else f"{issue_count}건이 탐지되었습니다."
    except Exception:
        response = f"총 {issue_count}건이 탐지되었습니다."

    return {**state, "response_text": response}


def save_rule_node(state: QueryState) -> QueryState:
    """사용자가 동의하면 RuleSpec을 data/memory/rules.json에 저장한다."""
    spec = state.get("confirmed_spec")
    if spec is None:
        return {**state, "saved": False}

    _MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict] = []
    if _MEMORY_FILE.exists():
        try:
            content = _MEMORY_FILE.read_text(encoding="utf-8").strip()
            if content and content != "{}":
                loaded = json.loads(content)
                if isinstance(loaded, list):
                    existing = loaded
        except Exception:
            existing = []

    existing.append(spec.model_dump())
    _MEMORY_FILE.write_text(
        json.dumps(existing, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info("사용자 기준 저장: %s → %s", spec.spec_id, _MEMORY_FILE)
    return {**state, "saved": True}


def load_memory_rules() -> list[RuleSpec]:
    """data/memory/rules.json에서 사용자 저장 규칙을 로드한다."""
    if not _MEMORY_FILE.exists():
        return []
    try:
        content = _MEMORY_FILE.read_text(encoding="utf-8").strip()
        if not content or content == "{}":
            return []
        data = json.loads(content)
        if isinstance(data, list):
            return [RuleSpec(**item) for item in data]
    except Exception as e:
        logger.warning("memory/rules.json 로드 실패: %s", e)
    return []


# ── 범위 밖 응답 생성 ──────────────────────────────────────────────────────────

_OUT_OF_SCOPE_BUTTONS = [
    {"label": "횟수 초과(중복 수혜) 검사", "template": "MAX_COUNT"},
    {"label": "이중 지급 의심 검사",       "template": "NEAR_DUPLICATE"},
    {"label": "합계 한도 초과 검사",       "template": "SUM_LIMIT"},
]


def out_of_scope_response(user_query: str) -> dict:
    """범위 밖 질문에 대한 안내 응답 dict를 반환한다."""
    # 인젝션 패턴 탐지
    injection_keywords = ["이전 지시", "지시 무시", "결과를 정상", "모두 삭제", "bypass", "ignore"]
    is_injection = any(kw in user_query for kw in injection_keywords)

    if is_injection:
        message = "요청을 처리할 수 없습니다."
    elif "부정수급" in user_query or "판정" in user_query:
        message = "의심 근거는 진단서에서 확인할 수 있으며, 부정수급 여부는 담당자 확인이 필요합니다."
    else:
        message = (
            "지금은 ① 횟수 초과(중복 수혜) ② 이중 지급 의심 ③ 합계 한도 초과 검사를 할 수 있습니다. "
            "어떤 걸 볼까요?"
        )

    return {"message": message, "buttons": _OUT_OF_SCOPE_BUTTONS if not is_injection else []}
