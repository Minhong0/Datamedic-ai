당신은 데이터 전문가입니다. 아래 엑셀/JSON 컬럼 목록과 샘플 데이터를 보고, 각 컬럼의 의미 타입을 추정해 주세요.

## 컬럼 목록
{{ headers }}

## 샘플 데이터 (최대 5행, 민감정보 마스킹됨)
{{ sample_rows }}

## 반환 형식 (JSON만, 설명 없이)
```json
[
  {"column": "컬럼명", "semantic_type": "타입", "confidence": 0.0~1.0}
]
```

## 허용 타입
org_name, biz_no, date, amount, count, phone, email, code, text, unknown

## 규칙
- 확신이 없으면 confidence를 낮게, 0.6 미만이면 unknown으로 설정
- 날짜처럼 보이는 컬럼은 date로 분류
- 금액/건수는 amount/count로 구분
