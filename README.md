# DataMedic AI — 1일차 작업 내역

> **팀원:** 김홍민 (A)  
> **범위:** T01 ~ T07 — 프로젝트 골격 · 데이터 모델 · 파서 · 규칙 엔진 · 검증 · 수정 · 내보내기

---

## 1. 1일차 목표 및 달성 현황

| ID | 작업 | 상태 |
|---|---|---|
| T01 | 프로젝트 골격, config, requirements, `.env.example` | ✅ 완료 |
| T02 | `engine/models.py` — Pydantic 데이터 모델 전체 | ✅ 완료 |
| T03 | 표준 서식 yaml, 코드표 csv, 샘플 20개 생성 스크립트 | ✅ 완료 |
| T04 | `engine/parser.py` + `Dataset` | ✅ 완료 |
| T05 | 규칙 base + format_rules (DATE_FORMAT, PHONE_FORMAT, TEXT_NORMALIZE) | ✅ 완료 |
| T06 | value / row / table / json 규칙 (총 14종) | ✅ 완료 |
| T07 | `validator`, `fixer`, `exporter`, `semantic.py`, `codebook.py` | ✅ 완료 |

**테스트 결과: 105개 통과 / 0개 실패**

```
tests/test_models.py          16 passed
tests/test_parser.py          11 passed
tests/test_rules_format.py    27 passed
tests/test_rules_value.py     21 passed
tests/test_rules_row.py        7 passed
tests/test_rules_table.py     10 passed
tests/test_rules_json.py       6 passed
tests/test_t07_integration.py  7 passed
─────────────────────────────────────
합계                          105 passed  in 1.33s
```

---

## 2. 구현 파일 목록

```
제4회 AI-SW/
├── config.py                          # 경로·임계값·LLM 설정 (.env 로드)
├── requirements.txt                   # 의존 패키지
├── .env.example                       # 환경변수 템플릿
│
├── engine/
│   ├── models.py                      # Pydantic 데이터 모델 전체
│   ├── parser.py                      # xlsx/json → Dataset (모든 값 str)
│   ├── semantic.py                    # 시맨틱 타입 → 기본 규칙 매핑
│   ├── codebook.py                    # 코드표 로드 · 유사 코드 검색
│   ├── validator.py                   # RuleBinding 실행 → Issue 목록
│   ├── fixer.py                       # Decision 적용 → Dataset 수정 + Change 기록
│   ├── exporter.py                    # 정제본·이력·리포트 파일 저장
│   └── rules/
│       ├── base.py                    # Rule 추상 클래스, REGISTRY
│       ├── format_rules.py            # DATE_FORMAT, PHONE_FORMAT, TEXT_NORMALIZE
│       ├── value_rules.py             # REQUIRED, DATE_RANGE, BIZNO_CHECKSUM,
│       │                              #   CODE_VALID, NUMERIC_UNIT
│       ├── row_rules.py               # DUPLICATE_ROW, NEAR_DUPLICATE
│       ├── table_rules.py             # SUM_MATCH, OUTLIER, TYPE_CONSISTENCY
│       └── json_rules.py             # JSON_SCHEMA
│
├── data/
│   ├── standard/
│   │   ├── performance.yaml           # 표준 서식 정의 (시군 사업실적)
│   │   └── business_codes.csv         # 사업코드 코드표
│   └── samples/
│       ├── generate_samples.py        # 샘플 20개 생성 스크립트
│       └── <시군명>_사업실적.xlsx      # 샘플 20개
│
└── tests/
    ├── fixtures/                      # 테스트용 소형 xlsx/json
    ├── test_models.py
    ├── test_parser.py
    ├── test_rules_format.py
    ├── test_rules_value.py
    ├── test_rules_row.py
    ├── test_rules_table.py
    ├── test_rules_json.py
    └── test_t07_integration.py        # 통합 테스트 (validator → fixer → exporter)
```

---

## 3. 핵심 구현 설명

### 3-1. 데이터 모델 (`engine/models.py`)

파이프라인 전체에서 공유하는 Pydantic v2 모델.

```python
SemanticType = Literal["org_name", "biz_no", "date", "amount", "count",
                        "phone", "email", "code", "text", "unknown"]
Tier = Literal["auto", "approval", "request"]
```

| 모델 | 역할 |
|---|---|
| `CellRef` | 특정 셀 좌표 (파일·시트·행·컬럼) |
| `Issue` | 발견된 오류 (`E-0001` 형식 ID) |
| `Decision` | 이슈 처리 결정 (apply/reject/edit/request) |
| `Change` | 적용된 변경 이력 (`C-0001` 형식 ID) |
| `Dataset` | `(file, sheet) → DataFrame` (모든 값 str, `_row` 컬럼으로 원본 행 번호 유지) |

### 3-2. 파서 (`engine/parser.py`)

- xlsx: `dtype=str`로 전체 읽기, 엑셀 날짜 일련번호(40000~55000) → ISO 문자열 자동 변환
- json: 배열/중첩 dict 모두 지원, `pd.json_normalize`
- 공통: `_row` 컬럼 삽입 (헤더=1, 첫 데이터=2)

### 3-3. 규칙 엔진 (`engine/rules/`)

```python
class Rule(ABC):
    rule_id: ClassVar[str]
    default_tier: ClassVar[Tier]
    scope: ClassVar[Literal["cell", "row", "table", "cross_sheet"]]

    def check(self, ds, binding, ctx) -> list[Issue]: ...
    def fix(self, value, issue) -> str | None: ...  # 불가능하면 NotFixable
```

`@register` 데코레이터로 `REGISTRY`에 자동 등록, `validator.py`가 `run()`으로 일괄 실행.

| rule_id | scope | tier | 설명 |
|---|---|---|---|
| DATE_FORMAT | cell | auto | ISO 외 날짜 → 자동 변환 |
| PHONE_FORMAT | cell | auto | 하이픈 형식 통일 |
| TEXT_NORMALIZE | cell | auto | 앞뒤 공백·전각 문자 제거 |
| REQUIRED | cell | request | 필수값 공백 감지 |
| DATE_RANGE | cell | request | 허용 범위 밖 날짜 |
| BIZNO_CHECKSUM | cell | approval | 사업자번호 체크디지트 검증 |
| CODE_VALID | cell | approval | 코드표 미등록 코드 |
| NUMERIC_UNIT | cell | approval | 금액 단위 혼용 |
| DUPLICATE_ROW | row | auto | 완전 동일 행 삭제 |
| NEAR_DUPLICATE | row | approval | 핵심 컬럼 일치 유사 중복 |
| SUM_MATCH | cross_sheet | request | 시트 간 합계 불일치 |
| OUTLIER | table | request | IQR 기반 이상치 |
| TYPE_CONSISTENCY | cell | approval | 컬럼 다수 타입과 다른 값 |
| JSON_SCHEMA | cell | approval | jsonschema 위반 |

### 3-4. 검증·수정·내보내기

```
validator.run(ds, bindings)          # Issue 목록 반환
  └─ fixer.apply(ds, issues, decisions)  # cleaned Dataset + Change 목록
        └─ exporter.save_*(...)           # outputs/<run_id>/ 저장
```

`fixer.py`는 유일하게 `Dataset.set_cell()` / `drop_row()`를 호출하는 모듈 — LLM이 셀 값을 직접 바꾸는 경로 없음.

---

## 4. 표준 서식 (`data/standard/performance.yaml`)

경남 시군 사업실적 서식 기준:

```yaml
sheets:
  실적:
    columns:
      - {name: 기관명,       type: org_name, required: true}
      - {name: 사업코드,     type: code,     required: true, codebook: business_codes.csv}
      - {name: 지원일자,     type: date,     required: true, range: [2026-01-01, 2026-12-31]}
      - {name: 지원금액,     type: amount,   required: true, unit: 원}
      ...
  요약:
    ...
relations:
  - {rule: SUM_MATCH, source: 실적.지원금액, group_by: 사업코드, agg: sum, target: 요약.지원금액합계}
```

---

## 5. 실행 방법

```bash
# 가상환경 생성 및 패키지 설치
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 테스트 실행
pytest tests/test_models.py tests/test_parser.py tests/test_rules_format.py \
       tests/test_rules_value.py tests/test_rules_row.py tests/test_rules_table.py \
       tests/test_rules_json.py tests/test_t07_integration.py -v

# 샘플 생성 (선택)
python data/samples/generate_samples.py
```

---

## 6. 2일차 예정 작업

| ID | 작업 |
|---|---|
| T08 | `eval/inject_errors.py`, `evaluate.py` |
| T09 | `llm/client.py` + 프롬프트 4종 |
| T10 | `agent/` LangGraph 그래프 6개 노드 |
| T11 | `app.py` Streamlit 4개 탭 |
