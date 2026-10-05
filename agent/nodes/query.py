"""8장 자연어 질문 → 집계 규칙 서브그래프 노드."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import BaseModel

from config import settings
from engine.models import Dataset, GroupIssue, Period, RuleSpec, Template
from engine.planning import AGGREGATE_TEMPLATES
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

MAX_CLARIFY_ROUNDS = 4


class QueryState(TypedDict, total=False):
    query_text: str
    template_hint: str
    business_names: list[str]
    answers: list
    slot_tries: dict
    defaults_used: list
    clarify_count: int
    translation: dict
    missing_slots: list[str]
    intent: Literal["query", "out_of_scope", ""]
    confirmed_spec: RuleSpec | None
    confirm_card: dict
    clarify_question: str
    group_issues: list[GroupIssue]
    response_text: str
    saved: bool


class TranslationResponse(BaseModel):
    intent: Literal["query", "out_of_scope"] = "query"
    template: Template | None = None
    group_by: list[str] = []
    params: dict = {}
    amount_text: str | None = None
    period: Period | None = None
    filter_business: str | list[str] | None = None
    missing_slots: list[str] = []
    description: str = ""
    evidence: dict[str, str] = {}
    unreadable: list[str] = []
    interpreted_by: str = ""


_REQUIRED_SLOTS: dict[str, list[str]] = {
    "MAX_COUNT":       ["period", "group_by", "count_distinct"],
    "NEAR_DUPLICATE":  ["period", "date_window_days"],
    "SUM_LIMIT":       ["period", "scope"],
}

_CLARIFY_QUESTIONS: dict[str, str] = {
    "period":          "올해 기준으로 볼까요, 전체 기간으로 볼까요? (year / all)",
    "group_by":        "같은 시군 안에서만 볼까요, 도 전체로 볼까요?",
    "count_distinct":  "서로 다른 사업 개수를 셀까요, 같은 사업의 횟수를 셀까요?",
    "max":             "한 기업이 최대 몇 번까지 받을 수 있나요? (예: 1)",
    "business":        "이번 지원 사업이 파일의 어느 사업인가요? (여러 개 선택 가능)",
    "date_window_days": "며칠 이내를 같은 건으로 볼까요? (기본 30일)",
    "scope":           "한도를 어떻게 셀까요? 사업별 합계 / 모든 사업 합산 / 건별(지원금 한 건)",
    "max_amount":      "한도 금액을 정확히 알려주세요 (예: 5천만원)",
}


def _collect_missing(t: TranslationResponse) -> list[str]:
    """필수 슬롯 중 비어 있는 것을 수집한다."""
    missing: list[str] = list(t.missing_slots)

    if t.template is None:
        return missing

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


_UNIT_NUM = r"\d[\d,]*(?:\.\d+)?\s*[억만천백]+"
_AMOUNT_RE = re.compile(rf"{_UNIT_NUM}(?:\s*{_UNIT_NUM})*(?:\s*\d[\d,]*(?=\s*원))?\s*원?|\d[\d,]*(?:\.\d+)?\s*원?")
_INJECTION_RE = re.compile(r"이전\s*지시|지시\s*무시|bypass|ignore all|결과.*정상|모두\s*삭제")


def _extract_amount_text(text: str) -> str | None:
    """텍스트에서 한국어 금액 표현을 추출한다 (normalize_amount에 넘길 원문)."""
    matches = [m.group(0).strip() for m in _AMOUNT_RE.finditer(text)]
    if not matches:
        return None
    with_unit = [m for m in matches if re.search(r"[억만천원]", m)]
    return with_unit[-1] if with_unit else matches[-1]


_THIS_BUSINESS_RE = re.compile(r"이번\s*(?:지원\s*)?사업|이\s*사업|해당\s*사업|같은\s*사업|이번\s*지원(?![금액])")
_PER_COMPANY_RE = re.compile(r"한\s*(?:회사|기업|업체)\s*당|(?:회사|기업|업체)\s*(?:당|별)|1\s*사\b")
_SPECIFIC_BUSINESS_RE = re.compile(r"이번\s*(?:지원\s*)?사업|이\s*사업|해당\s*사업|이번\s*지원(?![금액])")
_MANY_BUSINESS_RE = re.compile(r"두\s*(?:개|가지)\s*이상|두\s*사업\s*이상|사업\s*(?:이\s*)?두\s*개|여러\s*사업|서로\s*다른\s*사업|다른\s*사업")
_KO_NUM = {"한": 1, "두": 2, "세": 3, "네": 4}
_NUM = r"(\d+|한|두|세|네)"
_AT_LEAST_RE = re.compile(_NUM + r"\s*(?:개|건|회|번|가지|사업)\s*이상")
_UP_TO_RE = re.compile(_NUM + r"\s*(?:번|회)\s*(?:만\s*)?(?:가능|까지|이내|이하|만|씩|원칙)")
_BARE_COUNT_RE = re.compile(_NUM + r"\s*(?:번|회)(?!\s*이상)")
_SCOPE_TOTAL = ("합산", "total", "전체 합산", "모든 사업", "총합", "전체 기준")
_SCOPE_PER_BUSINESS = ("per_business", "사업별", "각 사업", "사업 별", "사업당", "사업 기준", "사업마다", "사업 마다")
_SCOPE_PER_PAYMENT = ("per_payment", "건별", "건당", "한 건", "1건당", "개별 지원")
_SCOPE_CUES = (*_SCOPE_TOTAL, *_SCOPE_PER_BUSINESS, *_SCOPE_PER_PAYMENT,
               "합쳐", "합계", "다 더", "모두 더", "한 번에 받은", "한번에 받은", "건 하나", "한건")


def _has_scope_cue(query: str) -> bool:
    """문장에 한도의 '범위'(사업별·합산·건별)를 가리키는 말이 있는가. 없으면 범위는 문장에서 읽은 값이 아니다."""
    ql = _squash(query).lower()
    return any(_squash(c).lower() in ql for c in _SCOPE_CUES) or bool(_THIS_BUSINESS_RE.search(query))


def _num(v: str) -> int:
    return _KO_NUM.get(v) or int(v)


def _allowed_count(ql: str) -> tuple[str, str] | None:
    """허용 횟수와 근거 구절. 'N번/회 이상'은 N-1회 허용, '한 번 가능'·'2회까지'는 그대로, 그 밖의 'N회'는 최후 수단."""
    for rx, minus in ((_AT_LEAST_RE, 1), (_UP_TO_RE, 0), (_BARE_COUNT_RE, 0)):
        ms = list(rx.finditer(ql))
        if ms:
            m = ms[-1]
            return str(_num(m.group(1)) - minus), m.group(0)
    return None


def _business_in(ql: str, business_names: list[str] | None) -> str | None:
    """문장에 파일의 사업명이 하나로 정해지게 나오면 그 사업."""
    hits = [n for n in (business_names or []) if n and n.lower() in ql]
    hits = [n for n in hits if not any(n != o and n.lower() in o.lower() for o in hits)]
    return hits[0] if len(hits) == 1 else None


def _businesses_in(ql: str, business_names: list[str] | None) -> list[str]:
    """문장에 나온 파일의 사업명 전부 (다른 사업명 안에 든 짧은 이름은 긴 이름이 함께 나오면 뺀다)."""
    hits = [n for n in (business_names or []) if n and n.lower() in ql]
    return [n for n in hits if not any(n != o and n.lower() in o.lower() for o in hits)]


def _business_filter(ql: str, business_names: list[str] | None) -> str | list[str] | None:
    """하나면 문자열, 둘 이상이면 목록, 없으면 None."""
    hits = _businesses_in(ql, business_names)
    return hits[0] if len(hits) == 1 else (hits or None)


def _build_sum_limit(q: str, ql: str, business_names: list[str] | None = None) -> TranslationResponse:
    amount = _extract_amount_text(q)
    params: dict = {"sum_column": "지원금액"}
    missing: list[str] = []
    evidence: dict[str, str] = {}
    if amount:
        evidence["max_amount"] = amount
    else:
        missing.append("max_amount")
    last = lambda words: max(((ql.rfind(w), w) for w in words if w in ql), default=(-1, None))
    (t_pos, total), (p_pos, per), (g_pos, pay) = last(_SCOPE_TOTAL), last(_SCOPE_PER_BUSINESS), last(_SCOPE_PER_PAYMENT)
    this = _THIS_BUSINESS_RE.search(ql)
    best = max(t_pos, p_pos, g_pos)
    if best >= 0 and best == g_pos:
        params["scope"], evidence["scope"] = "per_payment", pay
    elif best >= 0 and best == t_pos:
        params["scope"], evidence["scope"] = "total", total
    elif per or this:
        params["scope"], evidence["scope"] = "per_business", per or this.group(0)
    else:
        missing.append("scope")
    period: Period = "all" if ("전체" in ql and "기간" in ql) else "year"
    return TranslationResponse(
        intent="query", template="SUM_LIMIT", group_by=["사업자번호"], params=params,
        amount_text=amount, period=period, missing_slots=missing, evidence=evidence,
        filter_business=_business_filter(ql, business_names),
        description="지원금 합계 한도 초과 검사",
    )


def _build_near_duplicate(ql: str) -> TranslationResponse:
    day_m = re.search(r"(\d+)\s*일", ql)
    window = day_m.group(1) if day_m else "30"
    return TranslationResponse(
        intent="query", template="NEAR_DUPLICATE", group_by=["사업자번호", "사업명"],
        params={"amount_tolerance_pct": "0", "date_window_days": window},
        period="year", missing_slots=[], description="이중 지급 의심 검사",
        evidence={"date_window_days": day_m.group(0)} if day_m else {},
    )


def _build_max_count(ql: str, *, strict: bool, business_names: list[str] | None = None) -> TranslationResponse:
    """strict=True(계획이 지정한 검사)이면 읽지 못한 슬롯을 기본값으로 채우지 않고 되묻는다."""
    evidence: dict[str, str] = {}
    org = next((w for w in ("시군", "기관") if w in ql), None)
    group_by = ["기관명", "사업자번호"] if org else ["사업자번호"]
    if org:
        evidence["group_by"] = org
    missing: list[str] = []
    allowed = _allowed_count(ql)
    if allowed:
        max_val, evidence["max"] = allowed
    else:
        if strict:
            missing.append("max")
        max_val = "1"
    many, per_company, this = _MANY_BUSINESS_RE.search(ql), _PER_COMPANY_RE.search(ql), _THIS_BUSINESS_RE.search(ql)
    if many:
        count_distinct, evidence["count_distinct"] = "사업명", many.group(0)
    elif this or per_company:
        count_distinct, evidence["count_distinct"] = "행", (this or per_company).group(0)
    elif strict:
        count_distinct, missing = "사업명", missing + ["count_distinct"]
    else:
        count_distinct = "사업명"
    params = {"max": max_val}
    if "count_distinct" not in missing:
        params["count_distinct"] = count_distinct
    return TranslationResponse(
        intent="query", template="MAX_COUNT", group_by=group_by, params=params,
        period="all" if ("전체" in ql and "기간" in ql) else "year",
        filter_business=_business_filter(ql, business_names),
        missing_slots=missing, evidence=evidence, description="중복 수혜(허용 횟수 초과) 검사",
    )


def _keyword_translate(query: str, template: str | None = None,
                       business_names: list[str] | None = None) -> TranslationResponse | None:
    """LLM 없이 키워드 기반으로 번역을 시도한다."""
    if _INJECTION_RE.search(query):
        return TranslationResponse(intent="out_of_scope")

    q = query
    ql = query.lower()

    if template == "SUM_LIMIT":
        return _build_sum_limit(q, ql, business_names)
    if template == "NEAR_DUPLICATE":
        return _build_near_duplicate(ql)
    if template == "MAX_COUNT":
        return _build_max_count(ql, strict=True, business_names=business_names)

    from engine.planning import matches_capability
    if matches_capability(ql, "sum_limit"):
        return _build_sum_limit(q, ql, business_names)
    if matches_capability(ql, "near_duplicate"):
        return _build_near_duplicate(ql)
    if matches_capability(ql, "count_limit") or "횟수" in ql:
        return _build_max_count(ql, strict=False, business_names=business_names)
    return None


class _Condition(BaseModel):
    template: str
    evidence: str = ""
    slots: dict = {}


class GoalConditions(BaseModel):
    conditions: list[_Condition] = []
    unreadable: list[str] = []


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _llm_condition(query: str, template: str, business_names: list[str]) -> TranslationResponse | None:
    """LLM이 문장에서 뽑은 조건 중 지정 템플릿의 것을 검증해 돌려준다. 실패·검증 탈락이면 None."""
    from llm.client import complete_json

    try:
        resp = complete_json(
            "goal_conditions",
            {"user_query": query, "business_names": ", ".join(business_names) or "없음", "target_template": template},
            GoalConditions,
        )
    except Exception as e:
        logger.warning("조건 추출 LLM 실패 → 키워드 해석만 사용: %s", e)
        return None
    unreadable = [u for u in resp.unreadable if u.strip() and _squash(u) in _squash(query)]
    for cond in resp.conditions:
        if cond.template != template:
            continue
        if not cond.evidence or _squash(cond.evidence) not in _squash(query):
            logger.warning("LLM 조건의 근거 구절이 원문에 없어 버림: %r", cond.evidence)
            continue
        return _verified_condition(cond, query, business_names, unreadable)
    return None


def _verified_condition(cond: _Condition, query: str, business_names: list[str],
                        unreadable: list[str]) -> TranslationResponse:
    s, ev = cond.slots, cond.evidence
    out = TranslationResponse(intent="query", template=cond.template, unreadable=unreadable)
    out.period = s.get("period") if s.get("period") in ("year", "all") else None
    biz = s.get("filter_business")
    wanted = [biz] if isinstance(biz, str) else (list(biz) if isinstance(biz, list) else [])
    valid = [b for b in wanted if b in business_names]
    out.filter_business = valid[0] if len(valid) == 1 else (valid or None)
    if cond.template == "MAX_COUNT":
        out.group_by = ["기관명", "사업자번호"] if s.get("group_scope") == "org" else ["사업자번호"]
        mx = str(s.get("max", "")).strip()
        if mx.isdigit() and 0 <= int(mx) <= 20:
            out.params["max"], out.evidence["max"] = mx, ev
        if s.get("count_distinct") in ("행", "사업명"):
            out.params["count_distinct"], out.evidence["count_distinct"] = s["count_distinct"], ev
        out.missing_slots = [k for k in ("max", "count_distinct") if k not in out.params]
    elif cond.template == "SUM_LIMIT":
        out.group_by = ["사업자번호"]
        out.params["sum_column"] = "지원금액"
        amount = str(s.get("max_amount_text", "")).strip()
        if amount and _squash(amount) in _squash(query):
            out.amount_text, out.evidence["max_amount"] = amount, ev
        if s.get("scope") in ("per_business", "total", "per_payment") and _has_scope_cue(query):
            out.params["scope"], out.evidence["scope"] = s["scope"], ev
        out.missing_slots = (["max_amount"] if not out.amount_text else []) + \
                            (["scope"] if "scope" not in out.params else [])
    elif cond.template == "NEAR_DUPLICATE":
        out.group_by = ["사업자번호", "사업명"]
        out.params["amount_tolerance_pct"] = "0"
        days = str(s.get("date_window_days", "")).strip()
        if days.isdigit() and 0 <= int(days) <= 365:
            out.params["date_window_days"], out.evidence["date_window_days"] = days, ev
    return out


def _amount_value(text: str | None) -> str | None:
    if not text:
        return None
    from engine.rules.amount import normalize_amount
    return normalize_amount(text).value


def _explicit_slots(t: TranslationResponse) -> dict[str, str]:
    """번역 결과에서 '문장에서 실제로 읽은' 슬롯만 뽑는다 (기본값으로 채운 것은 제외)."""
    miss = set(t.missing_slots)
    if t.template == "MAX_COUNT":
        return {k: str(t.params[k]) for k in ("max", "count_distinct") if k in t.params and k not in miss}
    if t.template == "SUM_LIMIT":
        out = {}
        if "scope" in t.params and "scope" not in miss:
            out["scope"] = str(t.params["scope"])
        if _amount_value(t.amount_text):
            out["max_amount"] = _amount_value(t.amount_text)
        return out
    if t.template == "NEAR_DUPLICATE" and "date_window_days" in t.evidence:
        return {"date_window_days": str(t.params["date_window_days"])}
    return {}


def _merge_translations(kw: TranslationResponse, llm: TranslationResponse) -> TranslationResponse:
    """키워드 해석과 LLM 해석을 슬롯별로 교차 확인한다."""
    a, b = _explicit_slots(kw), _explicit_slots(llm)
    out = kw.model_copy(deep=True)
    out.unreadable = list(dict.fromkeys([*kw.unreadable, *llm.unreadable]))
    if not out.filter_business:
        out.filter_business = llm.filter_business
    names = {"MAX_COUNT": ("max", "count_distinct"), "SUM_LIMIT": ("max_amount", "scope"),
             "NEAR_DUPLICATE": ("date_window_days",)}.get(kw.template or "", ())
    missing = [m for m in kw.missing_slots if m not in names]
    for slot in names:
        va, vb = a.get(slot), b.get(slot)
        if va and vb and va != vb:
            logger.warning("키워드(%s)와 LLM(%s)의 %s 해석이 달라 되묻는다", va, vb, slot)
            value = None
        else:
            value = va or vb
        if slot == "max_amount":
            if value is None:
                out.amount_text = None
                missing.append(slot)
            else:
                out.amount_text = kw.amount_text if va else llm.amount_text
                if not va and slot in llm.evidence:
                    out.evidence[slot] = llm.evidence[slot]
            continue
        if value is None:
            out.params.pop(slot, None)
            if slot != "date_window_days":
                missing.append(slot)
            else:
                out.params["date_window_days"] = "30"
            continue
        out.params[slot] = value
        if slot not in out.evidence and slot in llm.evidence:
            out.evidence[slot] = llm.evidence[slot]
    out.missing_slots = list(dict.fromkeys(missing))
    return out


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

    if t.amount_text and "max_amount" not in params:
        result = normalize_amount(t.amount_text)
        if result.value is not None:
            params["max_amount"] = result.value

    return RuleSpec(
        spec_id=spec_id or _next_spec_id(),
        template=t.template,
        description=t.description or f"{t.template} 규칙",
        group_by=t.group_by,
        params=params,
        period=t.period or "year",
        filter_business=t.filter_business,
        tier="request",
        source="user_query",
    )


def translate_node(state: QueryState) -> QueryState:
    """LLM으로 자연어 질문을 TranslationResponse JSON으로 번역한다."""
    from llm.client import complete_json

    query = state.get("query_text", "")
    if not query:
        return {**state, "intent": "out_of_scope"}

    names: list[str] = list(state.get("business_names") or [])
    if not names:
        names = sorted({s.filter_business for s in load_business_master(_MASTER_FILE) if s.filter_business})
    biz_names = ", ".join(names) or "없음"

    hint = state.get("template_hint") or None
    resp: TranslationResponse | None = None

    if hint:
        kw = _keyword_translate(query, hint, names)
        if kw is not None and kw.intent == "out_of_scope":
            resp = kw
        else:
            llm = _llm_condition(query, hint, names)
            resp = _merge_translations(kw, llm) if (kw and llm) else (kw or llm)
            if resp is not None:
                resp.interpreted_by = ("LLM + 키워드 교차 확인" if (kw and llm) else
                                       "LLM만 (키워드로 읽지 못함)" if llm else
                                       "키워드만 (LLM 응답 없음 또는 검증 탈락)")
    else:
        try:
            resp = complete_json(
                "rule_request",
                {"user_query": query, "business_names": biz_names, "target_template": "지정 없음"},
                TranslationResponse,
            )
        except Exception as e:
            logger.warning("번역 LLM 호출 실패 → 키워드 fallback 시도: %s", e)

        if resp is None or resp.intent == "out_of_scope":
            kw = _keyword_translate(query, None, names)
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
            state = {**state, "defaults_used": [_SLOT_LABELS.get(m, m) for m in missing]}
            t = _apply_defaults(t)
            missing = []
        else:
            first_missing = missing[0]
            question = _CLARIFY_QUESTIONS.get(first_missing, f"'{first_missing}'을 알려주세요")
            return {
                **state,
                "missing_slots": missing,
                "clarify_question": question,
                "translation": t.model_dump(),
            }

    spec = _translation_to_spec(t)
    errors = validate_spec(spec)
    if errors:
        return {
            **state,
            "missing_slots": errors,
            "clarify_question": errors[0],
            "translation": t.model_dump(),
        }

    card = _make_confirm_card(spec, t.evidence, t.unreadable, t.interpreted_by, state.get("defaults_used"))
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


_SLOT_LABELS = {"business": "사업 한정", "max": "허용 횟수", "count_distinct": "집계 대상", "max_amount": "한도", "scope": "범위",
                "date_window_days": "날짜 간격", "group_by": "묶는 기준"}


def _make_confirm_card(spec: RuleSpec, evidence: dict[str, str] | None = None,
                       unreadable: list[str] | None = None, interpreted_by: str = "",
                       defaults: list[str] | None = None) -> dict:
    """확인 카드 dict 생성. evidence 가 있으면 값마다 원문 근거를 덧붙인다 ("이렇게 이해했어요")."""
    template_kr = {"MAX_COUNT": "중복 수혜 검사", "NEAR_DUPLICATE": "이중 지급 의심 검사", "SUM_LIMIT": "한도 초과 검사"}
    per_payment = spec.template == "SUM_LIMIT" and spec.params.get("scope") == "per_payment"
    card: dict = {
        "검사 종류": template_kr.get(spec.template, spec.template),
        "묶는 기준": "건별 (묶지 않고 지원금 한 건씩 본다)" if per_payment else ", ".join(spec.group_by),
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
        fb = spec.filter_business
        card["사업 필터"] = fb if isinstance(fb, str) else ", ".join(fb)
    for slot, quote in (evidence or {}).items():
        if quote and slot in _SLOT_LABELS:
            card[f"└ 근거({_SLOT_LABELS[slot]})"] = f"“{quote}”"
    if unreadable:
        card["해석하지 못한 구절"] = "; ".join(unreadable)
    if interpreted_by:
        card["해석 방식"] = interpreted_by
    if defaults:
        card["기본값 적용"] = ", ".join(defaults) + " — 답변을 읽지 못해 기본값으로 채웠습니다. 다르면 아래에서 고쳐 주세요."
    return card


def execute_node(state: QueryState, dataset: Dataset, *, id_offset: int = 0) -> QueryState:
    """confirmed_spec에 따라 집계 규칙을 실행한다. id_offset: 같은 검사를 범위만 바꿔 다시 돌릴 때"""
    spec = state.get("confirmed_spec")
    if spec is None:
        return {**state, "group_issues": []}

    ctx = RuleContext(issue_counter=[id_offset])
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
    """LLM으로 실행 결과 요약 문장을 생성한다."""
    from engine.masking import mask_bizno
    from llm.client import complete_json

    spec = state.get("confirmed_spec")
    issues = state.get("group_issues", [])

    if not issues:
        response = "조건에 해당하는 건이 없습니다."
        if spec:
            response += f" (검사 조건: {spec.description})"
        return {**state, "response_text": response}

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


def _spec_signature(spec: RuleSpec) -> str:
    """같은 검사 기준인지 비교하는 키 — spec_id·확인자는 무시하고 내용만 본다."""
    fb = spec.filter_business
    return json.dumps([spec.template, sorted(spec.group_by), spec.period, spec.params,
                       sorted(fb) if isinstance(fb, list) else fb], ensure_ascii=False, sort_keys=True)


def memory_rules_for(template: str) -> list[RuleSpec]:
    """저장된 기준 중 이 템플릿에 쓸 수 있는 것 (내용이 같으면 한 번만, 검증을 통과한 것만)."""
    seen: set[str] = set()
    out: list[RuleSpec] = []
    for spec in load_memory_rules():
        if spec.template != template or validate_spec(spec):
            continue
        sig = _spec_signature(spec)
        if sig not in seen:
            seen.add(sig)
            out.append(spec)
    return out


_OUT_OF_SCOPE_BUTTONS = [
    {"label": "횟수 초과(중복 수혜) 검사", "template": "MAX_COUNT"},
    {"label": "이중 지급 의심 검사",       "template": "NEAR_DUPLICATE"},
    {"label": "합계 한도 초과 검사",       "template": "SUM_LIMIT"},
]


def out_of_scope_response(user_query: str) -> dict:
    """범위 밖 질문에 대한 안내 응답 dict를 반환한다."""
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


def _answer_value(slot: str, text: str, names: list[str]) -> tuple[str, object] | None:
    """답변 문장에서 슬롯 값을 읽는다. 읽지 못하면 None. 반환: (적용 대상, 값)"""
    tl = text.lower()
    if slot == "max":
        got = _allowed_count(tl)
        return ("params", got[0]) if got else None
    if slot == "count_distinct":
        if _MANY_BUSINESS_RE.search(tl):
            return "params", "사업명"
        if _THIS_BUSINESS_RE.search(tl) or _PER_COMPANY_RE.search(tl):
            return "params", "행"
        return None
    if slot == "scope":
        found = {"total": max((tl.rfind(w) for w in _SCOPE_TOTAL if w in tl), default=-1),
                 "per_business": max((tl.rfind(w) for w in _SCOPE_PER_BUSINESS if w in tl), default=-1),
                 "per_payment": max((tl.rfind(w) for w in _SCOPE_PER_PAYMENT if w in tl), default=-1)}
        value, pos = max(found.items(), key=lambda kv: kv[1])
        return ("params", value) if pos >= 0 else None
    if slot == "period":
        if "전체" in tl and "기간" in tl:
            return "period", "all"
        return ("period", "year") if ("올해" in tl or "금년" in tl) else None
    if slot == "business":
        if "전체 사업" in text:
            return "filter_business", None
        picked = _businesses_in(tl, names)
        if not picked:
            return None
        return "filter_business", picked[0] if len(picked) == 1 else picked
    if slot == "max_amount":
        amount = _extract_amount_text(text)
        return ("amount_text", amount) if amount and _amount_value(amount) else None
    if slot == "date_window_days":
        m = re.search(r"(\d+)\s*일", tl)
        return ("params", m.group(1)) if m else None
    return None


_ANSWER_SLOTS = ("max", "count_distinct", "scope", "period", "business", "max_amount", "date_window_days")

SCOPE_ORDER = ("per_business", "total", "per_payment")
SCOPE_NAMES = {"per_business": "사업별 합계", "total": "모든 사업 합산", "per_payment": "건별"}


def scopes_in_text(text: str) -> list[str]:
    """답변에 나온 한도 범위를 모두 읽는다 (사업별 → 합산 → 건별 순서). 하나도 없으면 빈 목록."""
    tl = (text or "").lower()
    words = {"per_business": _SCOPE_PER_BUSINESS, "total": _SCOPE_TOTAL, "per_payment": _SCOPE_PER_PAYMENT}
    return [s for s in SCOPE_ORDER if any(w in tl for w in words[s])]


def apply_user_answers(translation: dict, answers: list, names: list[str]) -> tuple[dict, list[str]]:
    """사용자 답변을 번역 결과에 덮어쓴다. 반환: (번역, 답을 읽지 못한 슬롯 목록)."""
    t = dict(translation)
    t["params"] = dict(t.get("params", {}))
    t["evidence"] = dict(t.get("evidence", {}))
    missing = list(t.get("missing_slots", []))
    unread: list[str] = []
    for slot, text in answers:
        for s in (_ANSWER_SLOTS if slot == "*" else (slot,)):
            got = _answer_value(s, str(text), names)
            if got is None:
                if slot != "*":
                    unread.append(s)
                continue
            target, value = got
            if target == "params":
                t["params"][s] = value
            else:
                t[target] = value
            t["evidence"][s] = str(text)
            missing = [m for m in missing if m != s]
            unread = [u for u in unread if u != s]
    t["missing_slots"] = missing
    return t, unread


_VALUE_TEXT = {
    "count_distinct": {"행": "같은 사업을 받은 횟수", "사업명": "서로 다른 사업의 수"},
    "scope": {"per_business": "사업별로 따로", "total": "모든 사업 합산", "per_payment": "건별(지원금 한 건)"},
}


def _show_value(slot: str, value: str) -> str:
    if slot == "max_amount":
        return f"{int(value):,}원"
    if slot == "max":
        return f"{value}회"
    if slot == "date_window_days":
        return f"{value}일 이내"
    return _VALUE_TEXT.get(slot, {}).get(value, value)


def interpret_goal(goal: str, plan, business_names: list[str] | None = None) -> list[dict]:
    """계획의 집계 검사마다 목표 문장에서 읽은 값·근거와 되물을 항목을 정리한다 (키워드 해석 기준 — LLM 호출 없음)."""
    out: list[dict] = []
    for step in plan.steps:
        template = AGGREGATE_TEMPLATES.get(step.capability)
        if step.status != "run" or template is None:
            continue
        t = _keyword_translate(goal, template, business_names)
        if t is None or t.intent != "query":
            continue
        read = [(_SLOT_LABELS[k], _show_value(k, v), t.evidence.get(k, ""))
                for k, v in _explicit_slots(t).items() if k in _SLOT_LABELS]
        out.append({"capability": step.capability, "label": step.label, "read": read,
                    "ask": [_SLOT_LABELS.get(m, m) for m in t.missing_slots]})
    return out
