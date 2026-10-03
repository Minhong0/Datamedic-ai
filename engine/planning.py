"""목표 → 실행 계획 해석의 순수 로직.

능력 카탈로그, 의존성 표, 키워드 해석, 되묻기 판단, 범위 가드(PlanScope)를 담는다.
LLM·agent를 import하지 않는다.
"""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

from engine.models import Plan, PlanStep


@dataclass(frozen=True)
class Capability:
    id: str
    label: str
    rule_ids: tuple[str, ...]            # 실행할 REGISTRY 규칙 (비면 그래프에서 실행하지 않음)
    issue_prefixes: tuple[str, ...]      # 이 능력이 만들 수 있는 Issue.rule_id 접두어
    keywords: tuple[str, ...]
    goal_only: bool = False              # True면 목표에 해당할 때만 실행 (전체 실행에서 제외)
    evidence_required: bool = False      # True면 LLM이 골라도 키워드 근거가 없으면 버림
    aggregate: bool = False               # 집계 규칙(R5~R7) — 그래프의 aggregate 단계에서 실행


CATALOG: tuple[Capability, ...] = (
    Capability("blank_check", "필수값 공백 점검 (R1)", ("BLANK",), ("BLANK",),
               ("누락", "공백", "빈 값", "빈값", "필수", "결측")),
    Capability("blank_scoped", "대상 컬럼 공백 확인 (R1, 컬럼 한정)", ("BLANK",), ("BLANK",),
               (), evidence_required=True),
    Capability("amount_normalize", "지원금액 표기 정규화 (R2)", ("AMOUNT_NORMALIZE",), ("AMOUNT_",),
               ("금액 형식", "금액 정규", "금액 표기", "금액 단위", "금액 오류", "쉼표", "만원", "단위 통일")),
    Capability("date_normalize", "지원일자 형식 정규화 (R3)", ("DATE_NORMALIZE",), ("DATE_",),
               ("날짜", "일자")),
    Capability("bizno_format", "사업자번호 형식·체크디지트 (R4 로컬)", ("BIZNO_VERIFY",),
               ("BIZNO_VERIFY", "BIZNO_FORMAT", "BIZNO_CHECKSUM"),
               ("사업자",)),
    Capability("bizno_api", "국세청 휴·폐업 상태조회 (R4 API)", ("BIZNO_VERIFY",),
               ("BIZNO_NOT_FOUND", "BIZNO_SUSPENDED", "BIZNO_CLOSED_", "BIZNO_UNVERIFIED"),
               ("폐업", "휴업", "국세청", "상태조회", "상태 조회"), evidence_required=True),
    Capability("sum_limit", "기업당 한도 초과 확인 (R7)", (), (),
               ("한도", "상한", "초과", "넘으면", "넘는", "넘은", "넘어", "넘지", "넘기", "넘게",
                "원까지", "원 까지", "원 이하", "원이하", "원 이내", "원이내",
                "최대 지원금액", "최대 지원액", "최대지원금액"),
               goal_only=True, evidence_required=True, aggregate=True),
    Capability("count_limit", "기업당 수혜 횟수 제한 확인 (R5)", (), (),
               ("횟수", "중복 수혜", "중복수혜", "한 번만", "한번만", "한 번 가능", "한번 가능", "1회",
                "두 개 이상", "두개 이상", "두 가지 이상", "두가지 이상", "두 사업 이상", "사업 두 개", "사업이 두 개",
                "두 번 이상 받", "두번 이상 받", "여러 사업", "중복 지원",
                "회까지", "번까지", "회 까지", "번 까지", "회 이내", "번 이내", "회 이하", "번 이하",
                "번 이상", "회 이상", "여러 번", "번씩", "1사 1회", "번 넘", "회 넘"),
               goal_only=True, evidence_required=True, aggregate=True),
    Capability("near_duplicate", "이중 지급 의심 확인 (R6)", (), (),
               ("이중 지급", "이중지급", "두 번 나", "두번 나", "중복 지급", "중복지급", "두 번 지급", "두번 지급",
                "또 나간", "또 나갔", "또 지급", "반복 지급", "반복해서 지급", "중복 송금", "중복 입금",
                "두 번 입금", "두번 입금", "이중으로", "이중 입금"),
               goal_only=True, evidence_required=True, aggregate=True),
    Capability("duplicate_row", "완전 중복 행 탐지 (DUPLICATE_ROW)", ("DUPLICATE_ROW",), ("DUPLICATE_ROW",),
               ("중복",), goal_only=True, evidence_required=True),
)
BY_ID: dict[str, Capability] = {c.id: c for c in CATALOG}

# 선행 작업 표 — 이 표만이 의존성의 근거다 (LLM이 판단하지 않는다)
DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "amount_normalize": ("blank_scoped",),
    "date_normalize": ("blank_scoped",),
    "bizno_format": ("blank_scoped",),
    "bizno_api": ("bizno_format",),
    "sum_limit": ("amount_normalize",),
    "near_duplicate": ("amount_normalize",),
}

# 목표 문구에 포괄 점검 의도가 있으면 기본 실행 목록(goal_only 제외)을 명시 선택한 것으로 본다
_GENERAL_KEYWORDS = ("전체 점검", "오류 점검", "정합성", "취합 전", "전반", "일괄 정리", "형식 오류")  # 필드 지정이 없을 때만 확장
_FORMAT_KEYWORDS = ("형식", "표기", "정규화", "정리")
_DELETE_WORDS = ("제거", "삭제", "지워", "없애", "정리해")

# 모호한 용어: 용어가 있는데 구분 키워드가 없으면 되묻는다
_AMBIGUOUS = (
    ("합계", "합산", "총액"),
)
_SUM_CHOICES = ("sum_limit", "count_limit", "near_duplicate")

# 집계 능력 → 집계 규칙 템플릿 (engine.models.Template)
AGGREGATE_TEMPLATES: dict[str, str] = {
    "sum_limit": "SUM_LIMIT",
    "count_limit": "MAX_COUNT",
    "near_duplicate": "NEAR_DUPLICATE",
}

# 능력 키워드 판정에서 빼는 표현 — 다른 능력의 표현과 겹쳐 엉뚱한 능력이 같이 골라지는 것을 막는다
_KEYWORD_MASKS: dict[str, tuple[str, ...]] = {
    "duplicate_row": ("중복 수혜", "중복수혜", "중복 지급", "중복지급", "중복 지원"),
    "sum_limit": ("횟수 초과", "횟수초과", "횟수 한도", "수상한", "번 넘", "회 넘"),   # "수상한"의 "상한"이 상한 키워드로 오인된다
}


def matches_capability(goal: str, capability_id: str) -> bool:
    """목표 문구에 능력의 키워드가 있는지 (겹치는 표현은 제외하고 판정)."""
    g = goal
    for phrase in _KEYWORD_MASKS.get(capability_id, ()):
        g = g.replace(phrase, " ")
    return any(k in g for k in BY_ID[capability_id].keywords)


# 능력 → 컬럼 한정 공백 확인 대상 시맨틱 타입
SCOPED_BLANK_TYPES: dict[str, str] = {
    "amount_normalize": "amount",
    "date_normalize": "date",
    "bizno_format": "biz_no",
}
# blank_check(전체 공백 점검)가 계획에 있으면 컬럼 한정 공백 확인은 중복이므로 추가하지 않는다
_SUBSUMED_BY = {"blank_scoped": "blank_check"}


def default_capabilities() -> list[str]:
    """목표가 비어 있을 때만 쓰는 기본(전체) 실행 목록."""
    return [c.id for c in CATALOG if not c.goal_only]


_SOURCE_LABELS = {
    "llm": "LLM이 고른 검사 (키워드 근거로 검증)",
    "keyword": "키워드 해석 (LLM 응답 없음)",
    "user": "사용자가 고른 검사",
    "full": "기본 전체 점검 (목표 없음)",
}


def plan_source_label(source: str) -> str:
    """계획이 어떤 방식으로 만들어졌는지 화면에 보여 줄 문구."""
    return _SOURCE_LABELS.get(source, source)


def has_delete_intent(goal: str) -> bool:
    return "중복" in goal and any(w in goal for w in _DELETE_WORDS)


def keyword_select(goal: str) -> list[str]:
    """키워드로 능력을 고른다. 근거가 없으면 빈 목록.

    우선순위: 필드 지정 키워드(날짜·금액·사업자 …) > 포괄 표현(오류 점검·형식 오류 …).
    포괄 표현은 필드가 하나도 지정되지 않았을 때만 확장한다.
    """
    g = goal.strip()
    picked: set[str] = {c.id for c in CATALOG if matches_capability(g, c.id)}

    field_picked = picked & {"amount_normalize", "date_normalize", "bizno_format", "blank_check"}
    if not field_picked:
        if any(k in g for k in _GENERAL_KEYWORDS):
            picked.update(default_capabilities())
            picked.discard("bizno_api")
            if any(k in g for k in ("폐업", "휴업", "국세청", "상태조회")):
                picked.add("bizno_api")
        elif any(k in g for k in _FORMAT_KEYWORDS) and not picked:
            picked.update({"amount_normalize", "date_normalize", "bizno_format"})
    return [c.id for c in CATALOG if c.id in picked]


def has_evidence(goal: str, capability_id: str) -> bool:
    return matches_capability(goal, capability_id)


def sanitize_llm_selection(goal: str, ids: list[str]) -> list[str]:
    """LLM 선택은 키워드 해석 결과의 부분집합만 허용한다 (LLM이 범위를 넓히지 못하게)."""
    allowed = set(keyword_select(goal))
    return [c.id for c in CATALOG if c.id in ids and c.id in allowed]


def find_ambiguity(goal: str, selected: list[str]) -> Plan | None:
    """'합계' 같은 모호한 용어를 한도 확인/집계 보기 중 하나로 정하지 못하면 되묻는다."""
    for terms in _AMBIGUOUS:
        if not any(t in goal for t in terms):
            continue
        if any(i in selected for i in _SUM_CHOICES):
            continue   # 구분 키워드로 이미 정해짐
        options = [{"id": i, "label": BY_ID[i].label} for i in _SUM_CHOICES]
        return Plan(
            goal=goal, status="needs_clarification",
            question="'합계 검증'은 어떤 검사를 말씀하시나요? 하나를 선택해 주세요.",
            options=options, delete_intent=has_delete_intent(goal),
        )
    return None


def clarify_empty(goal: str) -> Plan:
    """목표에서 실행할 작업을 하나도 찾지 못했을 때."""
    return Plan(
        goal=goal, status="needs_clarification",
        question="목표에서 실행할 검사를 찾지 못했습니다. 실행할 검사를 선택해 주세요.",
        options=[{"id": c.id, "label": c.label} for c in CATALOG],
        delete_intent=has_delete_intent(goal),
    )


# ── 해석하지 못한 조항 ────────────────────────────────────────────────────────

_CLAUSE_SPLIT_RE = re.compile(r"[,，.。;；]|\n|하며|이며|이고|하고|인데|이지만|(?<!까)지만|그리고|및|면서|그런데")
# 검사 조건처럼 보이는 표현 (한도·범위·제한 또는 단위가 붙은 수치)
_CONSTRAINT_RE = re.compile(
    r"한도|상한|하한|이하|이상|미만|초과|이내|이전|이후|까지|부터|제한|불가|금지|넘|반드시|해야|안\s?[됨되]"
    r"|\d[\d,]*\s*(?:원|만원|억|천|백|회|번|%)"
)
_RANGE_RE = re.compile(r"이하|이상|미만|이내|이전|이후|까지|부터|최대|최소")
_DATE_RANGE_RE = re.compile(r"이전|이후|이내|부터|까지")
_FORMAT_ONLY = {"blank_check", "blank_scoped", "amount_normalize", "date_normalize", "bizno_format", "bizno_api"}


def find_unhandled_clauses(goal: str, selected: list[str]) -> list[str]:
    """목표에서 검사 조건처럼 보이지만 계획의 어떤 능력으로도 다뤄지지 않는 조항을 찾는다.

    - 조건 표현이 있는데 선택된 능력의 키워드가 하나도 없는 조항
    - 지원일자 범위(이전·이후·이내 …)처럼 형식 정규화만 하고 범위는 확인하지 않는 조항
    - 금액·횟수 상한처럼 보이는데 형식 정규화만 걸리고 집계 검사가 계획에 없는 조항
    """
    picked = [c for c in selected if c in BY_ID]
    has_checker = any(c not in _FORMAT_ONLY and c != "duplicate_row" for c in picked)
    out: list[str] = []
    for raw in _CLAUSE_SPLIT_RE.split(goal):
        clause = raw.strip()
        if len(clause) < 2:
            continue
        hit = {c for c in picked if matches_capability(clause, c)}
        if not hit:
            if _CONSTRAINT_RE.search(clause):
                out.append(clause)
            continue
        if hit <= _FORMAT_ONLY:
            if "date_normalize" in hit and _DATE_RANGE_RE.search(clause):
                out.append(clause)
            elif _RANGE_RE.search(clause):
                # 금액 조건은 한도 확인(sum_limit)이 있을 때만 거기서 다뤄진 것으로 본다 — 횟수 제한 등 다른 검사가 있다고
                # "5000만원 이상" 같은 금액 조건이 읽힌 것은 아니다.
                covered = ("sum_limit" in picked) if "amount_normalize" in hit else has_checker
                if not covered:
                    out.append(clause)
    return list(dict.fromkeys(out))


# ── 해석하지 못한 조항은 건너뛰지 않고 되묻는다 ───────────────────────────────
# 되묻기 답변의 표기: "use:<능력>::<조항>" = 그 조항을 이 검사로 처리, "skip::<조항>" = 사용자가 동의하고 제외.

_AMOUNT_RE = re.compile(r"\d[\d,]*\s*(?:원|만원|억|천|만)")
_COUNT_RE = re.compile(r"\d+\s*(?:번|회)|한\s*번|횟수")
_DUP_RE = re.compile(r"이중|반복|또\s*(?:나|지급|입금)")
_LOWER_BOUND_RE = re.compile(r"이상|최소|이후|부터")

_USE_LABELS = {
    "sum_limit": "합계 한도 확인으로 처리 — 이 금액을 기준으로 넘는 기업을 찾습니다",
    "count_limit": "횟수 제한 확인으로 처리 — 한 기업이 받은 횟수를 셉니다",
    "near_duplicate": "이중 지급 의심 확인으로 처리 — 같은 금액이 짧은 기간에 반복된 건을 찾습니다",
}


def encode_use(capability: str, clause: str) -> str:
    return f"use:{capability}::{clause}"


def encode_skip(clause: str) -> str:
    return f"skip::{clause}"


def parse_clause_choices(choices: list[str]) -> tuple[list[str], list[str], list[str]]:
    """되묻기 답변에서 (선택된 능력, 검사로 처리하기로 한 조항, 제외하기로 한 조항)을 뽑는다."""
    caps: list[str] = []
    used: list[str] = []
    skipped: list[str] = []
    for c in choices:
        if c.startswith("use:") and "::" in c:
            head, clause = c.split("::", 1)
            if head[4:] in _USE_LABELS:
                caps.append(head[4:])
                used.append(clause)
        elif c.startswith("skip::"):
            skipped.append(c[len("skip::"):])
    return caps, used, skipped


def unhandled_options(clause: str) -> list[dict]:
    """해석하지 못한 조항에 대해 사용자가 고를 처리 방법. 조항 내용으로 그럴듯한 검사를 앞에 둔다."""
    caps = []
    if _AMOUNT_RE.search(clause):
        caps.append("sum_limit")
    if _COUNT_RE.search(clause):
        caps.append("count_limit")
    if _DUP_RE.search(clause):
        caps.append("near_duplicate")
    caps = caps or list(_USE_LABELS)
    options = [{"id": encode_use(c, clause), "label": _USE_LABELS[c]} for c in caps]
    options.append({"id": encode_skip(clause),
                    "label": "이번 점검에서 제외하고 진행 — 이 조건은 검사하지 않고, 제외한 사실을 결과에 남깁니다"})
    return options


def clarify_unhandled(goal: str, clause: str, remaining: int = 1) -> Plan:
    """해석하지 못한 조항을 그냥 넘기지 않고 어떻게 처리할지 묻는 계획."""
    hint = ""
    if _AMOUNT_RE.search(clause) and _LOWER_BOUND_RE.search(clause):
        hint = (" ‘최소 금액’(하한)을 확인하라는 뜻이라면 아직 지원하지 않는 검사예요. "
                "그 경우 제외하고 진행하거나, 기준 금액을 넘는 곳을 찾는 한도 확인으로 처리할 수 있어요.")
    more = f" (확인할 조건 {remaining}개 중 첫 번째)" if remaining > 1 else ""
    return Plan(
        goal=goal, status="needs_clarification",
        question=f"“{clause}” 조건을 어떻게 처리할까요?{more}{hint}",
        options=unhandled_options(clause), delete_intent=has_delete_intent(goal),
    )


def resolve_plan(goal: str, selected: list[str], source: str) -> Plan:
    """선택된 능력 → 선행 작업 자동 추가 + 생략 사유 기록."""
    run = [i for i in selected if i in BY_ID]
    deps: list[str] = []
    queue = list(run)
    while queue:   # 선행 작업의 선행 작업까지 (전이)
        i = queue.pop(0)
        for d in DEPENDENCIES.get(i, ()):
            if d in run or d in deps or _SUBSUMED_BY.get(d) in run:
                continue
            deps.append(d)
            queue.append(d)
    steps: list[PlanStep] = []
    for c in CATALOG:
        if c.id in run:
            steps.append(PlanStep(capability=c.id, label=c.label, status="run",
                                  reason="목표에 포함" if source != "full" else "목표 없음 — 기본 전체 점검"))
        elif c.id in deps:
            who = ", ".join(BY_ID[i].label for i in [*run, *deps] if c.id in DEPENDENCIES.get(i, ()))
            steps.append(PlanStep(capability=c.id, label=c.label, status="dependency",
                                  reason=f"선행 작업 (필요: {who})"))
        else:
            why = "목표 선택형 규칙 — 목표에 해당하지 않음" if c.goal_only else "목표에 없음"
            steps.append(PlanStep(capability=c.id, label=c.label, status="skipped", reason=why))
    unhandled = [] if source == "full" else find_unhandled_clauses(goal, run)
    return Plan(goal=goal, status="ready", steps=steps, unhandled=unhandled,
                delete_intent=has_delete_intent(goal), source=source)  # type: ignore[arg-type]


# ── 범위 가드 ─────────────────────────────────────────────────────────────────

class PlanScopeError(RuntimeError):
    """계획에 없는 능력(예: 국세청 호출)을 실행하려 할 때."""


@dataclass(frozen=True)
class PlanScope:
    capabilities: frozenset[str]

    @classmethod
    def from_plan(cls, plan: Plan | None) -> "PlanScope | None":
        """plan이 없으면 None (레거시 파이프라인: 제한 없음). plan이 있으면 반드시 제한한다."""
        if plan is None:
            return None
        return cls(frozenset(s.capability for s in plan.steps if s.status != "skipped"))

    @property
    def rule_ids(self) -> set[str]:
        return {r for c in self.capabilities for r in BY_ID[c].rule_ids}

    def allows_issue(self, rule_id: str) -> bool:
        return any(
            rule_id.startswith(p)
            for c in self.capabilities for p in BY_ID[c].issue_prefixes
        )

    def filter_issues(self, issues: list):
        return [i for i in issues if self.allows_issue(i.rule_id)]


_current: ContextVar[PlanScope | None] = ContextVar("plan_scope", default=None)


@contextmanager
def active_scope(scope: PlanScope | None) -> Iterator[None]:
    token = _current.set(scope)
    try:
        yield
    finally:
        _current.reset(token)


def require_capability(capability_id: str) -> None:
    """활성 범위가 있고 해당 능력이 없으면 PlanScopeError. 범위가 없으면(레거시) 통과."""
    scope = _current.get()
    if scope is not None and capability_id not in scope.capabilities:
        raise PlanScopeError(f"계획에 '{capability_id}'가 없어 실행을 차단했습니다.")


def sanitize_goal(goal: str) -> str:
    return re.sub(r"\s+", " ", goal or "").strip()
