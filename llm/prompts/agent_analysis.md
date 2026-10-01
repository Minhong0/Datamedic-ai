당신은 데이터 품질 전문가입니다. 진단 결과를 바탕으로 담당자에게 핵심 사항을 요약해 주세요.

## 진단 목표
{{ user_goal }}

## 처리 파일
{{ files }}

## 오류 요약
- 총 오류: {{ total_issues }}건
- 자동 수정 가능: {{ auto_count }}건
- 승인 필요: {{ approval_count }}건
- 보완 요청: {{ request_count }}건

## 규칙별 오류 현황
{{ rule_summary }}

## Agent 우선 검사 규칙 (전략 판단 결과)
{{ priority_rules }}

## 반환 형식 (JSON만, 설명 없이)
```json
{
  "headline": "한 줄 핵심 요약 (예: '3개 파일에서 DATE_FORMAT 오류 집중, 자동 수정 가능')",
  "findings": [
    "주요 발견사항 1 (수치 포함)",
    "주요 발견사항 2 (수치 포함)",
    "주요 발견사항 3 (수치 포함)"
  ],
  "recommendation": "담당자에게 권고하는 다음 행동 1~2문장"
}
```

## 규칙
- 담당자가 즉시 이해할 수 있는 쉬운 말로 작성
- 진단 목표와 연결해서 설명
- findings는 3개 이내, 구체적인 수치 포함
- recommendation은 실행 가능한 행동 제안
