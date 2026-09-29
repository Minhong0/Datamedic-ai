"""테스트용 픽스처 파일 생성 스크립트 (1회 실행)."""
from pathlib import Path

import openpyxl
import json

FIXTURES = Path(__file__).parent


def make_simple_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "실적"
    ws.append(["기관명", "지원일자", "지원금액"])
    ws.append(["창원시", "2026-03-15", "1000000"])
    ws.append(["진주시", "2026-04-20", "2000000"])
    ws.append(["통영시", "2026-05-10", "1500000"])
    wb.save(FIXTURES / "sample.xlsx")


def make_serial_date_xlsx():
    """엑셀 날짜 일련번호가 들어있는 파일."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "실적"
    ws.append(["기관명", "지원일자", "지원금액"])
    # 2026-03-15 = 45930 (엑셀 일련번호)
    ws.append(["창원시", "45930", "1000000"])
    ws.append(["진주시", "45931", "2000000"])
    wb.save(FIXTURES / "serial_date.xlsx")


def make_json_fixture():
    data = [
        {"org": "창원시", "code": "BC001", "amount": "1000000"},
        {"org": "진주시", "code": "BC002", "amount": "2000000"},
        {"org": "통영시", "code": "BC003", "amount": "1500000"},
    ]
    with open(FIXTURES / "sample.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def make_multisheet_xlsx():
    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "실적"
    ws1.append(["기관명", "사업코드", "지원금액"])
    ws1.append(["창원시", "BC001", "500000"])
    ws1.append(["창원시", "BC001", "500000"])

    ws2 = wb.create_sheet("요약")
    ws2.append(["사업코드", "지원건수", "지원금액합계"])
    ws2.append(["BC001", "2", "1000000"])
    wb.save(FIXTURES / "multisheet.xlsx")


if __name__ == "__main__":
    FIXTURES.mkdir(parents=True, exist_ok=True)
    make_simple_xlsx()
    make_serial_date_xlsx()
    make_json_fixture()
    make_multisheet_xlsx()
    print("픽스처 생성 완료.")
