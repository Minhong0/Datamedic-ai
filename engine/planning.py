"""목표 → 실행 계획 해석의 순수 로직 (docs/planner_design.md).

능력 카탈로그, 의존성 표, 키워드 해석, 되묻기 판단, 범위 가드(PlanScope)를 담는다.
LLM·agent를 import하지 않는다 (CLAUDE.md 규칙 3).
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
    Capability("sum_match", "요약표 합계 대조 (SUM_MATCH)", ("SUM_MATCH",), ("SUM_MATCH",),
               ("요약표", "요약 시트", "합계 대조", "합계 불일치", "요약 대조"), goal_only=True, evidence_required=True),
    Capability("sum_limit", "기업당 한도 초과 확인 (R7)", (), (),
               ("한도", "상한", "초과"), goal_only=True, evidence_required=True, aggregate=True),
    Capability("aggregate_view", "집계 보기", (), (),
               ("집계", "통계", "현황"), goal_only=True, evidence_required=True, aggregate=True),
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
    "sum_match": ("amount_normalize",),
    "sum_limit": ("amount_normalize",),
    "aggregate_view": ("amount_normalize",),
}

# 목표 문구에 포괄 점검 의도가 있으면 기본 실행 목록(goal_only 제외)을 명시 선택한 것으로 본다
_GENERAL_KEYWORDS = ("전체 점검", "오류 점검", "정합성", "취합 전", "전반", "일괄 정리", "형식 오류")  # 필드 지정이 없을 때만 확장
_FORMAT_KEYWORDS = ("형식", "표기", "정규화", "정리")
_DELETE_WORDS = ("제거", "삭제", "지워", "없애", "정리해")

# 모호한 용어: 용어가 있는데 구분 키워드가 없으면 되묻는다
_AMBIGUOUS = (
    ("합계", "합산", "총액"),
)
_SUM_CHOICES = ("sum_match", "sum_limit", "aggregate_view")


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


def has_delete_intent(goal: str) -> bool:
    return "중복" in goal and any(w in goal for w in _DELETE_WORDS)


def keyword_select(goal: str) -> list[str]:
    """키워드로 능력을 고른다. 근거가 없으면 빈 목록.

    우선순위: 필드 지정 키워드(날짜·금액·사업자 …) > 포괄 표현(오류 점검·형식 오류 …).
    포괄 표현은 필드가 하나도 지정되지 않았을 때만 확장한다.
    """
    g = goal.strip()
    picked: set[str] = {c.id for c in CATALOG if any(k in g for k in c.keywords)}

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
    return any(k in goal for k in BY_ID[capability_id].keywords)


def sanitize_llm_selection(goal: str, ids: list[str]) -> list[str]:
    """LLM 선택은 키워드 해석 결과의 부분집합만 허용한다 (LLM이 범위를 넓히지 못하게)."""
    allowed = set(keyword_select(goal))
    return [c.id for c in CATALOG if c.id in ids and c.id in allowed]


def find_ambiguity(goal: str, selected: list[str]) -> Plan | None:
    """'합계' 같은 모호한 용어를 요약표 대조/한도 확인/집계 보기 중 하나로 정하지 못하면 되묻는다."""
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
    return Plan(goal=goal, status="ready", steps=steps,
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
