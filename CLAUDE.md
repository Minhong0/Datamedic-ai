# CLAUDE.md

DataMedic AI — Excel·JSON 업무 데이터 정합성 자동 진단·복구·검증 Agent (LangGraph + Streamlit).
**설계의 기준은 `docs/DESIGN.md`다.** 작업 전 해당 장을 먼저 읽고, 문서와 다르게 구현해야 하면 멈추고 사용자에게 물어본다.

## 명령어

```bash
pip install -r requirements.txt
pytest -q                                   # 전체 테스트
pytest tests/test_rules_format.py -q        # 부분 테스트
python -m agent.graph --inputs "data/samples/*.xlsx" --auto-approve
streamlit run app.py
python eval/evaluate.py --testset data/testset --run outputs/<run_id>
```

## 절대 규칙

1. **LLM 출력으로 셀 값을 직접 바꾸지 않는다.** 값 변경은 `engine/fixer.py`만, 반드시 `Change`를 남긴다.
2. **입력 파일을 수정하지 않는다.** 결과는 `outputs/<run_id>/`에만 쓴다.
3. `engine/`은 `llm/`·`agent/`를 import하지 않는다. (역방향 의존 금지)
4. `engine/models.py`의 필드명·타입을 바꾸면 docs/DESIGN.md 7장도 같이 수정하고 사용자에게 알린다.
5. 프롬프트는 `llm/prompts/*.md`에만 둔다. 코드에 긴 프롬프트 문자열 금지.
6. 테스트에서 실제 LLM API를 호출하지 않는다. `FakeLLM`을 쓴다.
7. `.env`, API 키, `outputs/`는 커밋하지 않는다.
8. 사용자가 B(우대원)로 작업 중이라고 하면 `data/testset/`, `eval/inject_errors.py`를 열거나 읽지 않는다. (블라인드 평가)

## 작업 방식

- 한 번에 docs/DESIGN.md 14장의 작업 하나(T01~T14)만 한다. 범위 밖 파일은 건드리지 않는다.
- 순서: 관련 docs/DESIGN.md 장 읽기 → 짧은 구현 계획 제시 → 구현 → 테스트 작성 → `pytest` 통과 확인 → docs/DESIGN.md 14장 체크박스 갱신.
- 완료 기준을 통과하지 못하면 체크하지 말고 남은 문제를 보고한다.
- 새 규칙 추가 시 docs/DESIGN.md 9장의 "규칙 추가 방법"을 따른다.

## 코드 스타일

- Python 3.11, 타입 힌트 필수, pydantic v2
- 함수는 작게, 모듈 간 데이터는 `engine/models.py` 모델로 주고받기
- 사용자에게 보이는 문자열(진단 메시지, 화면, 메일)은 한국어
- 로그는 `logging`, `print` 금지 (CLI 출력 제외)
- ID 형식: Issue `E-0001`, Change `C-0001`, run_id `YYYYMMDD-HHMMSS`

## 자주 틀리는 부분

- 엑셀 행 번호 = DataFrame 원본 인덱스 + 2. 행 삭제 후에도 `_row` 컬럼 기준으로 원본 번호를 유지한다.
- 파서는 모든 값을 `str`로 읽는다. 엑셀 날짜 일련번호(예: 45930)는 ISO 문자열로 변환한다.
- `auto`로 분류할지 애매하면 `approval`로 올린다.
- 같은 원인의 Issue는 LLM 설명을 대표 1건만 호출하고 복사한다.
