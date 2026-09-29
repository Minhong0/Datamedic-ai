"""깨끗한 샘플 xlsx 20개 생성 스크립트."""
from __future__ import annotations

import random
from pathlib import Path

import openpyxl
from openpyxl import Workbook

SEED = 42
random.seed(SEED)

ORGS = [
    "창원시", "진주시", "통영시", "사천시", "김해시",
    "밀양시", "거제시", "양산시", "의령군", "함안군",
    "창녕군", "고성군", "남해군", "하동군", "산청군",
    "함양군", "거창군", "합천군", "고령군", "성주군",
]

CODES = ["BC001", "BC002", "BC003", "BC004", "BC005",
         "BC006", "BC007", "BC008", "BC009", "BC010"]

CODE_NAMES = {
    "BC001": "스마트공장구축", "BC002": "수출바우처", "BC003": "기술개발R&D",
    "BC004": "창업도약패키지", "BC005": "소공인특화지원", "BC006": "디지털전환",
    "BC007": "탄소중립전환", "BC008": "청년일자리", "BC009": "여성기업육성",
    "BC010": "사회적기업지원",
}

COMPANY_SUFFIXES = ["(주)", "주식회사", "협동조합", "영농조합법인", ""]

OUT_DIR = Path(__file__).parent


def rand_biz_no() -> str:
    """10자리 사업자번호 (체크디지트 정확하게)."""
    while True:
        digits = [random.randint(0, 9) for _ in range(9)]
        weights = [1, 3, 7, 1, 3, 7, 1, 3, 5]
        total = sum(d * w for d, w in zip(digits, weights))
        check = (10 - (total % 10)) % 10
        all_digits = digits + [check]
        num = "".join(map(str, all_digits))
        # 형식: XXX-XX-XXXXX
        return f"{num[:3]}-{num[3:5]}-{num[5:]}"


def rand_phone() -> str:
    mid = random.randint(1000, 9999)
    last = random.randint(1000, 9999)
    prefix = random.choice(["010", "011", "016", "017", "018", "019"])
    return f"{prefix}-{mid}-{last}"


def rand_date() -> str:
    month = random.randint(1, 9)
    day = random.randint(1, 28)
    return f"2026-{month:02d}-{day:02d}"


def rand_company(org: str) -> str:
    suffix = random.choice(COMPANY_SUFFIXES)
    base = f"{org[:2]}산업기술"
    idx = random.randint(1, 99)
    name = f"{base}{idx}"
    return f"{name}{suffix}" if suffix else name


def generate_file(org: str, out_path: Path) -> None:
    wb = Workbook()
    ws_perf = wb.active
    ws_perf.title = "실적"

    headers = ["기관명", "사업코드", "사업명", "수혜기업명", "사업자번호", "지원일자", "지원금액", "담당자연락처"]
    ws_perf.append(headers)

    n_rows = random.randint(5, 15)
    code_amounts: dict[str, int] = {}
    code_counts: dict[str, int] = {}

    for _ in range(n_rows):
        code = random.choice(CODES)
        amount = random.randint(5, 200) * 100_000
        row = [
            org,
            code,
            CODE_NAMES[code],
            rand_company(org),
            rand_biz_no(),
            rand_date(),
            str(amount),
            rand_phone() if random.random() > 0.1 else "",
        ]
        ws_perf.append(row)
        code_amounts[code] = code_amounts.get(code, 0) + amount
        code_counts[code] = code_counts.get(code, 0) + 1

    ws_sum = wb.create_sheet("요약")
    ws_sum.append(["사업코드", "지원건수", "지원금액합계"])
    for code in sorted(code_amounts):
        ws_sum.append([code, str(code_counts[code]), str(code_amounts[code])])

    wb.save(out_path)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for org in ORGS:
        fname = f"{org}_사업실적.xlsx"
        fpath = OUT_DIR / fname
        generate_file(org, fpath)
        print(f"생성: {fpath}")
    print(f"\n총 {len(ORGS)}개 파일 생성 완료.")


if __name__ == "__main__":
    main()
