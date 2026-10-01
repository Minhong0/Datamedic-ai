# Planner 설계 — 목표(Plan) 범위 가드 (T30)

## 1. 문제
목표를 "분기 지원금액 합계 검증 및 중복 행 제거"로 설정해도 금액·날짜·사업자번호 규칙까지 함께 실행됐다.

원인:
1. 목표 → 규칙 선택을 LLM에 맡기고, LLM 실패·빈 응답이면 **전체 규칙 실행(fail-open)**.
2. 범위 제한이 `rules_node` 한 곳에만 있고 diagnosis·treatment·recheck에는 없었다.
3. 목표 문구가 가리키는 DUPLICATE_ROW·SUM_MATCH는 validator가 로드하지 않아 실행되지 않았다.

## 2. 구조 (Plan-and-Execute + 범위 가드)

```
plan ──ready──────────────► planner → analyze → diagnose → … (기존 ReAct 루프)
  │  └─needs_clarification─► ask_clarify (interrupt) ─► plan
```

| 구성요소 | 위치 | 역할 |
|---|---|---|
| 능력 카탈로그·의존성 표·키워드·되묻기 규칙 | `engine/planning.py` | 순수 데이터·순수 함수 (llm/agent import 금지) |
| `Plan`, `PlanStep` | `engine/models.py` | 계획 모델 (README 7장) |
| plan 노드 | `agent/nodes/plan.py` | LLM 해석 → 키워드 해석 → 되묻기 |
| 범위 가드 | `engine/planning.py` `PlanScope` | rules·validator·diagnosis·treatment·recheck·국세청 클라이언트에 적용 |
| 프롬프트 | `llm/prompts/plan_goal.md` | 카탈로그 ID 선택만 요청 |

## 3. 능력 카탈로그

| capability | 규칙 | 선행 작업 | 비고 |
|---|---|---|---|
| `blank_check` | BLANK | - | |
| `blank_scoped` | BLANK (컬럼 한정) | - | 대상 컬럼의 빈 값만 점검(request). 공백 정규화·빈 행 삭제 없음. `blank_check`가 있으면 추가 안 함 |
| `amount_normalize` | AMOUNT_NORMALIZE | `blank_scoped` | |
| `date_normalize` | DATE_NORMALIZE | `blank_scoped` | |
| `bizno_format` | BIZNO_VERIFY (로컬) | `blank_scoped` | 형식·체크디지트·이름충돌 |
| `bizno_api` | BIZNO_VERIFY (국세청) | `bizno_format` | 폐업·휴업·국세청 키워드 필요 |
| `sum_match` | SUM_MATCH | `amount_normalize` | 목표 선택형, 요약표 대조 |
| `sum_limit` | 집계 규칙 R7 (RuleSpec) | `amount_normalize` | 그래프 `aggregate` 단계에서 실행 |
| `aggregate_view` | 집계 규칙 R5~R7 (RuleSpec) | `amount_normalize` | 그래프 `aggregate` 단계, 검사 종류는 되묻기로 확정 |
| `duplicate_row` | DUPLICATE_ROW | - | 목표 선택형, tier=approval 고정 |

- R1은 R2~R4가 공백 셀을 건너뛰므로(RULES 0절), 금액·날짜·사업자번호 점검은 **해당 컬럼의 공백 확인**을 선행 작업으로 자동 추가한다. 선행 작업의 선행 작업도 전이적으로 추가된다.
- 선행 작업은 `DEPENDENCIES`(코드 dict) 한 곳에서만 정의한다. LLM이 판단하지 않는다.
- "목표 선택형"(`goal_only`)은 목표에 해당할 때만 실행하고 기본(전체) 실행 목록에는 넣지 않는다.

## 3-1. 집계 단계 (R5~R7 통합)
`sum_limit`·`aggregate_view`는 채팅이 아니라 그래프 안에서 실행한다. `agent/nodes/aggregate.py`가 `query.py`의 번역·검증·실행·요약 함수를 재사용한다.

```
treatment → planner → run_aggregate → agg_translate ─┬─ 슬롯 부족/범위 밖 → agg_clarify(interrupt) ─┐
                                                       ├─ 완전 → agg_confirm(interrupt) → agg_execute │ (agg_translate 복귀)
                                                       └─ 포기 ───────────────────────► agg_execute
agg_execute ─┬─ 대기열(`agg_queue`) 남음 → agg_translate
             └─ 끝 → planner(phase=aggregated) → finalize
```
- 정규화·수정이 끝난 데이터(`cleaned`)에 실행한다. 되묻기·확인 카드는 목표 되묻기와 같은 `interrupt` 흐름이다.
- 한 노드에 interrupt를 1회만 둔다 (재개 시 LLM 호출이 반복되지 않게).
- 결과는 `agg_results`에 쌓이고 본문 "집계 규칙 결과"에 표시된다. 별도 채팅 패널은 두지 않는다 (단일 화면, 사용 가능한 Agent에 집중).
- 대기열이 남아 있으면 planner가 `finalize`로 건너뛸 수 없다.

## 4. 해석 순서 (fail-open 금지)
1. 목표가 비어 있으면 → 기본 실행 목록(goal_only 제외) 전체. **전체 실행은 이 경우뿐.**
2. LLM이 카탈로그 ID를 선택. 카탈로그 밖 ID는 버린다. `bizno_api`·`duplicate_row`는 목표에 근거 키워드가 없으면 버린다.
3. LLM 실패·빈 결과 → 키워드 해석.
4. 그래도 0개 → 되묻기 (`needs_clarification`). 실행하지 않는다.
5. 모호한 용어("합계")는 요약표 대조 / 한도 확인 / 집계 보기 중 하나로 정해지지 않으면 되묻기.

## 5. 범위 가드
허용 규칙 = 계획의 run·dependency 단계 규칙 ID 합집합. 모든 단계가 이 집합 밖을 통과시키지 않는다.

| 단계 | 동작 |
|---|---|
| rules_node | 계획 밖 binding 미생성. standard 소스 예외 없음 |
| validator.run | `allowed_rule_ids` 밖 binding 스킵 |
| diagnosis / recheck | 허용 밖 issue 폐기 |
| treatment | 허용 밖 issue의 decision 폐기, DUPLICATE_ROW는 operator 결정만 허용 |
| 국세청 클라이언트 | 계획에 `bizno_api`가 없으면 `PlanScopeError` (호출 0회) |

## 6. DUPLICATE_ROW 정책
- tier는 approval로 잠근다. 자동 수정(`fix_auto`)·시스템 결정으로 삭제하지 않는다.
- 목표에 제거·삭제 의도가 있으면 검토 화면에 "중복 행 N건 일괄 삭제" 버튼만 노출한다. 누를 때 행마다 `Change`를 남긴다.
- 파일 간 중복(`value="(파일간중복)"`)은 일괄 대상에서 제외하고 개별 확인한다. 같은 파일 내 중복은 `value="(완전중복)"`.

## 7. 생략 표시
목표에 없는 능력은 `skipped(이유)`로 계획 카드와 로그에 남긴다.
