"""집계 규칙(R5~R7) 그래프 노드 — 목표(Plan)의 sum_limit·count_limit·near_duplicate 실행."""
from __future__ import annotations

import re

import logging
from pathlib import Path

from langgraph.types import interrupt

from agent.nodes.query import (
    _SLOT_LABELS,
    _SPECIFIC_BUSINESS_RE,
    MAX_CLARIFY_ROUNDS,
    SCOPE_NAMES,
    apply_user_answers,
    clarify_node,
    execute_node,
    respond_node,
    scopes_in_text,
    translate_node,
    validate_node,
)
from agent.state import GraphState
from engine.decisions import decision_version
from engine.planning import AGGREGATE_TEMPLATES

logger = logging.getLogger(__name__)

TEMPLATE_CHOICES: dict[str, tuple[str, str]] = {
    "MAX_COUNT": ("횟수 초과(중복 수혜) 검사", "한 기업이 사업 두 개 이상 받은 기업 찾아줘"),
    "NEAR_DUPLICATE": ("이중 지급 의심 검사", "같은 사업에서 돈이 두 번 나간 것 같은 거"),
    "SUM_LIMIT": ("합계 한도 초과 검사", "한도 초과"),
}


def _rows_text(rows: list[int], limit: int = 8) -> str:
    shown = ", ".join(str(r) for r in rows[:limit])
    return f"{shown}행" + (f" 외 {len(rows) - limit}행" if len(rows) > limit else "")


def group_issue_rows(res: dict) -> list[dict]:
    """집계 결과의 묶음 목록을 표시용 행으로 바꾼다 — 어느 엑셀의 어느 시트·몇 행에서 나왔는지 함께 보여 준다."""
    from engine.masking import mask_bizno

    out: list[dict] = []
    for g in res.get("group_issues", []):
        by_file: dict[str, dict[str, set[int]]] = {}
        for r in g.rows:
            by_file.setdefault(Path(r.file).name, {}).setdefault(r.sheet, set()).add(r.row)
        locations = " / ".join(
            f"{fname} · {sheet} {_rows_text(sorted(rows))}"
            for fname, sheets in by_file.items() for sheet, rows in sheets.items()
        )
        out.append({
            "파일": ", ".join(by_file) or "-",
            "위치": locations or "-",
            "그룹": " / ".join(f"{k}: {mask_bizno(v) if k == '사업자번호' else v}"
                              for k, v in g.group_key.items()),
            "행": len(g.rows), "지표": g.metric,
        })
    return out


def aggregate_capabilities(plan) -> list[str]:
    """계획에서 직접 요청된 집계 능력 (그래프 안에서 실행)."""
    from engine.planning import BY_ID
    return [s.capability for s in plan.steps
            if s.status == "run" and BY_ID[s.capability].aggregate]


_SLOT_CHOICES: dict[str, list[dict]] = {
    "scope": [
        {"id": "사업별 한도", "label": "사업별 합계 — 회사별로 사업마다 따로 더해서 센다"},
        {"id": "모든 사업 합산 한도", "label": "합산 — 회사가 받은 모든 사업을 합쳐서 센다"},
        {"id": "건별 한도", "label": "건별 — 지원금 한 건이 한도를 넘는지 본다 (합계 아님)"},
    ],
    "count_distinct": [
        {"id": "같은 사업을 여러 번 받은 경우", "label": "같은 사업을 몇 번 받았는지 센다"},
        {"id": "서로 다른 사업 개수", "label": "서로 다른 사업을 몇 개 받았는지 센다"},
    ],
    "period": [
        {"id": "올해 기준", "label": "올해만"},
        {"id": "전체 기간", "label": "전체 기간"},
    ],
}


def _business_names(state: GraphState) -> list[str]:
    """집계 대상 파일(실적 시트)의 사업명 목록."""
    ds = state.get("cleaned") or state.get("dataset")
    names: set[str] = set()
    for (_, sheet), df in (ds.tables.items() if ds is not None else []):
        if sheet == "실적" and "사업명" in df.columns:
            names |= {str(v).strip() for v in df["사업명"] if str(v).strip() and str(v).lower() != "nan"}
    return sorted(names)


def _slot_options(slot: str | None, q: dict) -> list[dict]:
    if slot == "business":
        return [{"id": "전체 사업", "label": "전체 사업 (사업을 한정하지 않음)"},
                *[{"id": n, "label": n} for n in q.get("business_names", [])]]
    return _SLOT_CHOICES.get(slot or "", [])


def _normalize_answer(slot: str | None, text: str) -> str:
    """숫자만 답한 경우 번역기가 읽을 수 있는 문장으로 바꾼다 ("1" → "1회까지 가능")."""
    if text.isdigit():
        if slot == "max":
            return f"{text}회까지 가능"
        if slot == "date_window_days":
            return f"{text}일 이내"
    return text


def _resolve_business_scope(q: dict) -> dict:
    """"이번 지원 사업"처럼 특정 사업을 가리키는데 사업이 정해지지 않았으면 파일에서 정한다."""
    t = dict(q.get("translation") or {})
    if (q.get("intent") == "out_of_scope" or t.get("template") not in ("MAX_COUNT", "SUM_LIMIT")
            or t.get("filter_business") or (t.get("evidence") or {}).get("business")
            or not _SPECIFIC_BUSINESS_RE.search(q.get("query_text", ""))):
        return q
    names = q.get("business_names") or []
    if len(names) == 1:
        t["filter_business"] = names[0]
        t["evidence"] = {**t.get("evidence", {}), "business": "파일의 사업이 하나뿐"}
    elif len(names) > 1:
        t["missing_slots"] = list(dict.fromkeys([*t.get("missing_slots", []), "business"]))
        q["missing_slots"] = list(dict.fromkeys([*q.get("missing_slots", []), "business"]))
    return {**q, "translation": t}


_GIVE_UP_DEFAULTS: dict[str, object] = {
    "max": "1", "count_distinct": "사업명", "scope": "per_business", "period": "year", "date_window_days": "30",
}
_MAX_TRIES_PER_SLOT = 2

_RETRY_HINTS = {
    "max": "숫자만 적어 주세요 (예: 1)",
    "max_amount": "금액을 단위와 함께 적어 주세요 (예: 5천만원 또는 50000000원)",
    "date_window_days": "일 수를 숫자로 적어 주세요 (예: 30)",
}


def _give_up_slot(t: dict, slot: str) -> bool:
    """읽지 못한 슬롯을 기본값(또는 '한정 없음')으로 닫는다. 기본값이 없는 슬롯이면 False."""
    if slot == "business":
        pass
    elif slot in _GIVE_UP_DEFAULTS:
        if slot == "period":
            t["period"] = _GIVE_UP_DEFAULTS[slot]
        else:
            t.setdefault("params", {})[slot] = _GIVE_UP_DEFAULTS[slot]
    else:
        return False
    t["missing_slots"] = [m for m in t.get("missing_slots", []) if m != slot]
    return True


def _apply_answers(q: dict) -> dict:
    """사용자 답변을 번역 결과에 덮어쓰고, 두 번 물어도 읽지 못한 슬롯은 기본값으로 닫는다 (같은 질문 반복 방지)."""
    t = q.get("translation")
    if not t or q.get("intent") == "out_of_scope":
        return q
    t, _ = apply_user_answers(t, q.get("answers", []), q.get("business_names", []))
    q = {k: v for k, v in q.items() if k != "extra_scopes"}
    if t.get("template") == "SUM_LIMIT":
        picked = [s for slot, text in q.get("answers", []) if slot == "scope" for s in [scopes_in_text(str(text))]][-1:]
        scopes = picked[0] if picked else []
        if len(scopes) > 1:
            t["params"]["scope"] = scopes[0]
            q["extra_scopes"] = scopes[1:]
    answered = [re.sub(r"\s+", "", str(text)).lower() for slot, text in q.get("answers", []) if slot != "*" and str(text).strip()]
    if t.get("unreadable") and answered:
        t["unreadable"] = [u for u in t["unreadable"]
                           if not any(re.sub(r"\s+", "", u).lower() in a or a in re.sub(r"\s+", "", u).lower() for a in answered)]
    tries = q.get("slot_tries", {})
    defaults = list(q.get("defaults_used", []))
    for slot in list(t.get("missing_slots", [])):
        if tries.get(slot, 0) < _MAX_TRIES_PER_SLOT:
            continue
        if slot == "max_amount":
            logger.warning("한도 금액을 읽지 못해 이 검사를 건너뜁니다")
            return {**q, "translation": t, "skipped": True,
                    "skip_reason": "한도 금액을 읽지 못해 실행하지 않았습니다. 목표에 금액을 적어 다시 실행해 주세요 (예: 5천만원)."}
        if _give_up_slot(t, slot):
            defaults.append(_SLOT_LABELS.get(slot, slot))
    q = {**q, "translation": t, "missing_slots": t.get("missing_slots", [])}
    if defaults:
        q["defaults_used"] = list(dict.fromkeys(defaults))
    return q


_MEMORY_NEW = "memory:new"


def _saved_label(spec) -> str:
    fb = spec.filter_business
    biz = f" · 사업: {fb if isinstance(fb, str) else ', '.join(fb)}" if fb else ""
    return f"{spec.description}{biz}"


def _offer_saved_criteria(state: GraphState, q: dict, cap: str) -> dict | None:
    """이전 실행에서 확인해 저장한 기준이 있으면 적용할지 묻는다. 적용하면 확인 카드 단계로 바로 간다."""
    from agent.nodes.query import _make_confirm_card, memory_rules_for
    saved = memory_rules_for(AGGREGATE_TEMPLATES[cap])
    if not saved:
        return None
    options = [{"id": f"memory:{i}", "label": f"저장된 기준 — {_saved_label(s)}"} for i, s in enumerate(saved)]
    options.append({"id": _MEMORY_NEW, "label": "새로 설정 — 목표 문장을 다시 해석한다"})
    answer = str(interrupt({
        "type": "aggregate_clarify", "capability": cap, "slot": "memory",
        "question": "이전에 확인해 저장한 검사 기준이 있습니다. 이번 검사에 적용할까요?",
        "options": options,
    })).strip()
    if not answer.startswith("memory:") or answer == _MEMORY_NEW:
        return None
    try:
        spec = saved[int(answer.split(":", 1)[1])]
    except (ValueError, IndexError):
        return None
    card = _make_confirm_card(spec)
    card["기준 출처"] = "이전 실행에서 확인해 저장한 기준"
    names = _business_names(state)
    wanted = [spec.filter_business] if isinstance(spec.filter_business, str) else list(spec.filter_business or [])
    missing = [n for n in wanted if names and n not in names]
    if missing:
        card["확인 필요"] = f"저장된 사업 필터({', '.join(missing)})가 이번 파일에 없습니다. 결과가 비면 '수정'으로 고쳐 주세요."
    return {**q, "confirmed_spec": spec, "confirm_card": card, "missing_slots": [], "clarify_question": "",
            "memory_used": True}


def agg_translate_node(state: GraphState) -> GraphState:
    queue = state.get("agg_queue", [])
    q = dict(state.get("query") or {})
    if not q.get("query_text"):
        q["query_text"] = state.get("goal", "")
        q["template_hint"] = AGGREGATE_TEMPLATES[queue[0]]
        q["clarify_count"] = 0
        q["business_names"] = _business_names(state)
        reused = _offer_saved_criteria(state, q, queue[0])
        if reused is not None:
            return {**state, "query": reused}
    q.pop("clarify_question", None)
    q.pop("reedit", None)
    q = translate_node(q)
    q = _apply_answers(q)
    if q.get("skipped"):
        return {**state, "query": q}
    q = _resolve_business_scope(q)
    if q.get("intent") != "out_of_scope":
        q = validate_node(q)
        card = q.get("confirm_card")
        if card:
            plan = state.get("plan")
            extra = {}
            if plan is not None and plan.unhandled:
                extra["반영되지 않은 조항"] = "; ".join(plan.unhandled)
            if q.get("extra_scopes"):
                extra["함께 실행할 범위"] = ", ".join(SCOPE_NAMES[s] for s in q["extra_scopes"])
            if q.get("defaults_used") and "기본값 적용" not in card:
                extra["기본값 적용"] = ", ".join(q["defaults_used"]) +                     " — 답변을 읽지 못해 기본값으로 채웠습니다. 다르면 아래에서 고쳐 주세요."
            q["confirm_card"] = {**card, **extra}
    if q.get("intent") == "out_of_scope" and q.get("clarify_count", 0) >= MAX_CLARIFY_ROUNDS:
        q["skipped"] = True
    return {**state, "query": q}


def route_after_translate(state: GraphState) -> str:
    q = state.get("query", {})
    if q.get("skipped"):
        return "agg_execute"
    if q.get("confirmed_spec") is not None and not q.get("clarify_question"):
        return "agg_confirm"
    return "agg_clarify"


def agg_clarify_node(state: GraphState) -> GraphState:
    q = dict(state["query"])
    out_of_scope = q.get("intent") == "out_of_scope"
    slot = (q.get("missing_slots") or [None])[0]
    tries = dict(q.get("slot_tries", {}))
    question = ("어떤 집계 검사를 실행할까요?" if out_of_scope
                else q.get("clarify_question", "조건을 알려주세요."))
    if slot and tries.get(slot, 0) >= 1 and not out_of_scope:
        question = f"답변을 이해하지 못했어요. {question} {_RETRY_HINTS.get(slot, '아래에서 골라 주세요.')}".strip()
    answer = interrupt({
        "type": "aggregate_clarify",
        "capability": state["agg_queue"][0],
        "slot": slot,
        "question": question,
        "options": ([{"id": k, "label": v[0]} for k, v in TEMPLATE_CHOICES.items()]
                    if out_of_scope else _slot_options(slot, q)),
    })
    text = str(answer).strip()
    if text in TEMPLATE_CHOICES:
        text = TEMPLATE_CHOICES[text][1]
    text = _normalize_answer(None if out_of_scope else slot, text)
    q = clarify_node(q, text)
    if slot and not out_of_scope:
        q["answers"] = [*q.get("answers", []), (slot, text)]
        q["slot_tries"] = {**tries, slot: tries.get(slot, 0) + 1}
    q.pop("clarify_question", None)
    q.pop("intent", None)
    return {**state, "query": q}


def agg_confirm_node(state: GraphState) -> GraphState:
    q = dict(state["query"])
    decision = interrupt({
        "type": "aggregate_confirm",
        "capability": state["agg_queue"][0],
        "card": q.get("confirm_card", {}),
    })
    if isinstance(decision, str) and decision.startswith("edit:"):
        edit = decision[5:].strip()
        q = clarify_node(q, edit)
        q["answers"] = [*q.get("answers", []), ("*", edit)]
        for key in ("confirmed_spec", "confirm_card", "clarify_question"):
            q.pop(key, None)
        q["reedit"] = True
    elif decision != "run":
        q["skipped"] = True
    return {**state, "query": q}


def route_after_confirm(state: GraphState) -> str:
    return "agg_translate" if state.get("query", {}).get("reedit") else "agg_execute"


def agg_execute_node(state: GraphState) -> GraphState:
    queue = list(state.get("agg_queue", []))
    cap = queue.pop(0)
    q = dict(state.get("query") or {})
    results = list(state.get("agg_results", []))

    if q.get("skipped") or q.get("confirmed_spec") is None:
        results.append({"capability": cap, "skipped": True, "spec": None,
                        "group_issues": [],
                        "response_text": q.get("skip_reason") or "실행하지 않았습니다 (취소 또는 조건 미확정)."})
    else:
        dataset = state.get("cleaned") or state["dataset"]
        extras = list(q.get("extra_scopes") or [])
        base = q["confirmed_spec"]
        if extras:
            first = base.params.get("scope", "per_business")
            q = {**q, "confirmed_spec": base.model_copy(update={"description": f"{base.description} — {SCOPE_NAMES.get(first, first)}"})}
        q = respond_node(execute_node(q, dataset))
        results.append({"capability": cap, "skipped": False, "spec": q["confirmed_spec"],
                        "group_issues": q.get("group_issues", []),
                        "response_text": q.get("response_text", "")})
        logger.info("집계 규칙 실행: %s → %d건", q["confirmed_spec"].template,
                    len(q.get("group_issues", [])))
        for n, scope in enumerate(extras, 1):
            spec2 = base.model_copy(update={
                "params": {**base.params, "scope": scope},
                "description": f"{base.description} — {SCOPE_NAMES.get(scope, scope)}"})
            q2 = respond_node(execute_node({**q, "confirmed_spec": spec2}, dataset, id_offset=1000 * n))
            results.append({"capability": cap, "skipped": False, "spec": q2["confirmed_spec"],
                            "group_issues": q2.get("group_issues", []),
                            "response_text": q2.get("response_text", "")})
            logger.info("집계 규칙 실행(추가 범위 %s): %d건", scope, len(q2.get("group_issues", [])))

    out: GraphState = {**state, "agg_queue": queue, "agg_results": results, "query": {},
                       "aggregate_basis": decision_version(state.get("decisions", []))}
    if not queue:
        out["agent_phase"] = "aggregated"
    return out


def route_after_execute(state: GraphState) -> str:
    return "agg_translate" if state.get("agg_queue") else "planner"
