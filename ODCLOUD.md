국세청_사업자등록정보 진위확인 및 상태조회 서비스
 1.1.0 
[ Base URL: api.odcloud.kr/api/nts-businessman/v1 ]
https://infuser.odcloud.kr/api/stages/28493/api-docs?1728017570963
CHANGELOG
v1.1 (05.31):
요청항목 (request body):
진위확인: 선택 항목 사업장주소(b_adr) 추가
페이지 하단의 Model -> 사업자등록정보 진위확인 API Request 참고
상태확인: 변경사항 없음.
응답항목 (response):
진위확인&상태조회: 직전과세유형메세지 명칭 및 코드 (rbf_tax_type, rbf_tax_type_cd) 추가
페이지 하단의 Model -> 사업자등록 상태조회 결과 참고
설명
국세청에서 제공하는 사업자등록정보 진위확인 및 사업자등록 상태조회 API 서비스입니다.

1회 호출 시 최대 100개에 해당하는 사업자등록정보의 진위확인 또는 사업자등록 상태조회가 가능합니다.

100개 초과 시에는 Too Large Request Error 가 발생합니다. (* 하단의 Too Large Request Error 부분을 참고해주세요)
삭제된 사업자등록정보는 조회되지 않으며, 해당 정보를 요청시에는 아래와 같은 메세지가 return됩니다:

진위확인: '확인할 수 없습니다'
상태조회: '국세청에 등록되지 않은 사업자등록번호입니다'
Response Return Type: JSON/XML 두가지 형태로 제공되며, 아래와 같이 Request Param 형태로 호출하면
됩니다:

JSON : returnType=JSON (Default: 넣지 않을 경우, 자동으로 JSON 으로 설정됩니다)
XML : returnType=XML
예) 진위확인 서비스 response를 XML로 받을 경우: http://api.odcloud.kr/api/nts-businessman/v1/validate?serviceKey=[서비스키]&returnType=XML
API 호출 시의 주의사항
GET METHOD가 아닌 POST METHOD 로만 제공됩니다.
serviceKey 및 returnType 은 URL 에 querystring 으로 넣어주셔야 합니다.
그 외 b_no 등의 JSON string은 반드시 body로 넣어 호출해주세요.
호출 시도중 에러 발생 시, 에러 종류에 따라 status code가 400,411,413,500 으로 반환되며, 에러 발생시에도 response에 오류 메세지가 return됩니다. 자세한 내용은 각 API를 클릭하여 하단의 response 부분을 참고해주세요.
API 별 request 및 response 정보는 하단의 API 별 request/response 샘플 또는 페이지 최하단의
Models 부분을 확인하시면 됩니다.
API 요청항목별 유의사항
API	항목	내용
진위확인	공통	요청항목이 전부 일치하는 경우, 해당 사업자에 대해 상태조회 API 호출 시의 결과가 return
필수가 아닌 항목을 빈값으로 검색해야할 경우, 아래와 같이 empty string("")으로 JSON에 포함되어야 합니다.
예) 대표자성명2(p_nm2)를 빈값으로 호출해야할 경우,
{
    ...,
    "p_nm2": "",
    ...
}
사업자등록번호(필수)	숫자로 이루어진 10자리 값만 가능 ('-' 등의 기호 반드시 제거 후 호출)
대표자성명(필수)	외국인 사업자의 경우에는 영문명 입력
개업일자(필수)	1) YYYYMMDD 포맷의 날짜로 입력('-' 등의 기호 반드시 제거 후 호출)
2) 사업자등록증에 표기된 개업연월일 날짜로 입력
대표자성명2	외국인 사업자의 경우에만 한글명칭으로 입력(공동대표자명 입력 x)
*외국인 사업자가 아닌 경우 해당 항목 및 값을 추가하시면 진위확인이 되지 않습니다.
상호	1) 상호가 주식회사인 경우, 아래의 단어에 대해서는 상호의 맨 앞 또는 맨 뒤에 붙어도 동일하게 검색 가능
 - (주)
 - 주식회사
 - （주）--> 'ㄴ' 으로 한자키 입력을 통한 특수문자 괄호
2) 앞뒤 공백(empty space) 무시하고 검색
법인등록번호	숫자로 이루어진 13자리 값만 가능 ('-' 등의 기호 반드시 제거 후 호출)
주업태명	1) 모든 공백(앞뒤 포함)에 대해 무시하고 검색됨
예) '서 비 스' -> '서비스' 로 검색됨"
주종목명	1) 모든 공백(앞뒤 포함)에 대해 무시하고 검색됨 (주업태명과 동일)
사업장주소	모든 공백(앞뒤 포함)에 대해 무시하고 검색됨
(예시: 서울특별시 강남구, 경기도 부천시)
상태조회	사업자등록번호(필수)	숫자로 이루어진 10자리 값만 가능 ('-' 등의 기호 반드시 제거 후 호출)	

상태조회 샘플코드 (jquery ajax method 사용)
var data = {
    "b_no": ["xxxxxxx"] // 사업자번호 "xxxxxxx" 로 조회 시,
   }; 
   
$.ajax({
  url: "https://api.odcloud.kr/api/nts-businessman/v1/status?serviceKey=xxxxxx",  // serviceKey 값을 xxxxxx에 입력
  type: "POST",
  data: JSON.stringify(data), // json 을 string으로 변환하여 전송
  dataType: "JSON",
  contentType: "application/json",
  accept: "application/json",
  success: function(result) {
      console.log(result);
  },
  error: function(result) {
      console.log(result.responseText); //responseText의 에러메세지 확인
  }
});