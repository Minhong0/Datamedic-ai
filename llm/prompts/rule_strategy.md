당신은 데이터 품질 전문가입니다. 아래 파일 프로필과 진단 목표를 보고, 어떤 규칙을 우선적으로 검사해야 할지 판단해 주세요.

## 진단 목표
{{ user_goal }}

## 파일 프로필 (컬럼명 · 의미 타입)
{{ profiles }}

## 사용 가능한 규칙 목록
- DUPLICATE_ROW: 완전 동일 행 중복
- NEAR_DUPLICATE: 핵심 컬럼 유사 행
- REQUIRED: 필수 항목 공백
- DATE_FORMAT: 날짜 표기 형식 오류
- DATE_RANGE: 날짜 허용 범위 이탈
- BIZNO_CHECKSUM: 사업자번호 체크디지트
- PHONE_FORMAT: 전화번호 형식 오류
- TEXT_NORMALIZE: 앞뒤 공백·전각문자·표기 통일
- CODE_VALID: 코드표 미존재 코드
- NUMERIC_UNIT: 금액 단위·기호 혼용
- SUM_MATCH: 시트 간 합계 불일치
- OUTLIER: 통계적 이상값
- TYPE_CONSISTENCY: 컬럼 내 타입 혼재
- JSON_SCHEMA: JSON 스키마 위반

## 반환 형식 (JSON만, 설명 없이)
```json
{
  "priority_rules": ["RULE_ID_1", "RULE_ID_2"],
  "reasoning": "우선순위 선정 이유 1~2문장"
}
```

## 선정 기준
- DUPLICATE_ROW는 항상 포함
- 금액(amount) 컬럼 다수 → NUMERIC_UNIT, OUTLIER, SUM_MATCH 우선
- 날짜(date) 컬럼 다수 → DATE_FORMAT, DATE_RANGE 우선
- 사업자번호(biz_no) 있음 → BIZNO_CHECKSUM 포함
- 목표에 '취합', '합계', '검증' 포함 → SUM_MATCH 최우선
- 목표에 '중복' 포함 → DUPLICATE_ROW, NEAR_DUPLICATE 최우선
