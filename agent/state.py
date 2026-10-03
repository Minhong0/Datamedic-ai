from __future__ import annotations

from typing import TypedDict

from engine.models import (
    Change, Dataset, Decision, Issue, Plan, RequestItem, RuleBinding,
    RunReport, SheetProfile,
)


# LangGraph 노드들이 공유하는 상태 객체.
# 파이프라인을 따라 아래 흐름으로 채워진다:
#   structure → rules → diagnosis → prescription → treatment → recheck
class GraphState(TypedDict, total=False):
    run_id: str                   # 실행 식별자 (YYYYMMDD-HHMMSS)
    input_paths: list[str]        # 처리할 파일 경로 목록
    goal: str                     # 사용자가 입력한 진단 목표 (LLM 컨텍스트로 전달)

    # [plan 노드가 채움]
    plan: Plan                    # 목표 해석 결과. 있으면 모든 노드가 이 범위 밖을 실행하지 않는다
    plan_choices: list[str]       # 되묻기에 대한 사용자 선택 (capability ID)

    # [aggregate 단계 — 집계 규칙 R5~R7]
    agg_queue: list[str]          # 실행 대기 중인 집계 능력 (sum_limit, count_limit, near_duplicate)
    query: dict                   # 진행 중인 집계 질문 상태 (query.QueryState)
    agg_results: list[dict]       # [{capability, skipped, spec, group_issues, response_text}]
    agg_dispositions: dict        # 집계 위반 묶음의 처리 {"<검사>:<묶음 ID>": "request"|"ok"} — 없으면 기본 request
    aggregate_basis: str          # 집계를 마지막으로 실행했을 때의 담당자 결정 버전 (engine.decisions.decision_version)

    # [레거시 — plan 이 없는 CLI 파이프라인 전용]
    rule_priorities: list[str]

    # [prescription 노드가 채움 — LLM 전체 분석]
    analysis_summary: dict        # {headline, findings, recommendation}

    # [앱이 관리 — 대화 이력]
    chat_history: list[dict]      # [{"role": "user"|"assistant", "content": str}]

    # [structure 노드가 채움]
    dataset: Dataset              # 파일에서 읽은 원본 테이블 (모든 값 str)
    profiles: list[SheetProfile]  # 각 시트의 컬럼명·시맨틱 타입 메타데이터

    # [rules 노드가 채움]
    bindings: list[RuleBinding]   # 어떤 규칙을 어떤 컬럼에 적용할지 매핑

    # [diagnosis 노드가 채움]
    issues: list[Issue]           # 발견된 오류 목록 (E-0001 형식 ID)

    # [prescription 노드가 수정]
    # issues 의 tier·suggestion 이 보강됨 (충돌 셀은 auto→approval 로 격상)

    # [treatment 노드 전에 외부(사람/시스템)가 채움]
    decisions: list[Decision]     # 각 Issue에 대한 처리 결정 (apply/reject/edit/request)

    # [treatment 노드가 채움]
    cleaned: Dataset | None       # 수정이 적용된 복사본 Dataset
    changes: list[Change]         # 변경 이력 (C-0001 형식 ID)

    # [recheck 노드가 채움]
    requests: list[RequestItem]   # 원본 제공처에 보낼 보완 요청 메일 목록
    report: RunReport | None      # 최종 실행 요약 리포트
    remaining_issue_list: list[Issue]  # 재검진 후 남은 오류 (피드백 루프용)
    loop_count: int               # 치료→재검진 반복 횟수

    # ── ReAct 에이전트 상태 (planner_node가 읽고 씀) ──────────────────────────
    agent_action: str        # planner가 결정한 다음 액션
    agent_reason: str        # LLM 판단 근거 (채팅 패널에 노출)
    agent_loop_count: int    # planner 루프 카운터 (≥ 8 → 강제 종료)
    agent_phase: str         # "init"|"analyzed"|"diagnosed"|"fixed"|"approved"|"applied"|"aggregated"|"finalized"
