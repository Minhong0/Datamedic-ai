"""경남 20개 시·군 사업실적 샘플 xlsx 재생성 스크립트 (RULES.md R-08).

변경 사항 (R-08):
  - 기업 풀(COMPANY_POOL) 사전 정의: 이름·사업자번호·NTS 상태가 고정
    → 같은 기업이 여러 파일에서 동일한 번호로 나타남
  - nts_mock.json을 기업 풀 기준으로 자동 재생성
  - 오류 주입을 활성 규칙(R1 BLANK / R2 AMOUNT / R3 DATE / R4 BIZNO)에 맞춤
  - 비활성 규칙(NEAR_DUPLICATE, SUM_MATCH) 오류 제거
"""
from __future__ import annotations

import json
import random
from pathlib import Path

import openpyxl
from openpyxl import Workbook

SEED = 42
random.seed(SEED)

OUT_DIR    = Path(__file__).parent
MOCK_PATH  = Path(__file__).parent.parent.parent / "tests" / "fixtures" / "nts_mock.json"
STD_DIR    = Path(__file__).parent.parent / "standard"

# ── 사업 프로그램 ────────────────────────────────────────────────────────────
PROGRAMS: list[dict] = [
    {"code": "BC001", "name": "스마트공장구축지원",   "min_amt": 30_000_000,  "max_amt": 200_000_000},
    {"code": "BC002", "name": "수출바우처지원",        "min_amt": 10_000_000,  "max_amt": 100_000_000},
    {"code": "BC003", "name": "기술개발R&D지원",       "min_amt": 50_000_000,  "max_amt": 300_000_000},
    {"code": "BC004", "name": "창업도약패키지",         "min_amt": 20_000_000,  "max_amt": 150_000_000},
    {"code": "BC005", "name": "소공인특화지원",         "min_amt": 5_000_000,   "max_amt": 50_000_000},
    {"code": "BC006", "name": "디지털전환지원",         "min_amt": 15_000_000,  "max_amt": 100_000_000},
    {"code": "BC007", "name": "탄소중립전환지원",       "min_amt": 30_000_000,  "max_amt": 200_000_000},
    {"code": "BC008", "name": "청년일자리창출지원",     "min_amt": 10_000_000,  "max_amt": 80_000_000},
    {"code": "BC009", "name": "여성기업육성지원",       "min_amt": 5_000_000,   "max_amt": 50_000_000},
    {"code": "BC010", "name": "사회적기업육성지원",     "min_amt": 10_000_000,  "max_amt": 70_000_000},
]

# ── 사업자번호 생성 유틸 ─────────────────────────────────────────────────────
_WEIGHTS = [1, 3, 7, 1, 3, 7, 1, 3, 5]


def _checksum(d9: list[int]) -> int:
    s = sum(d9[i] * _WEIGHTS[i] for i in range(9))
    s += (d9[8] * 5) // 10
    return (10 - s % 10) % 10


def _make_bizno(seed_val: int) -> str:
    """seed_val 기반으로 결정론적 유효 사업자번호 생성."""
    rng = random.Random(seed_val)
    while True:
        d = [rng.randint(0, 9) for _ in range(9)]
        c = _checksum(d)
        s = "".join(map(str, d)) + str(c)
        # 앞 3자리가 000이면 비현실적 — 재시도
        if s[:3] != "000":
            return f"{s[:3]}-{s[3:5]}-{s[5:]}"


def _corrupt_bizno(bizno: str) -> str:
    """체크디지트를 의도적으로 틀림 (BIZNO_CHECKSUM 오류 시뮬레이션)."""
    digits = bizno.replace("-", "")
    last = int(digits[-1])
    wrong = str((last + random.randint(1, 9)) % 10)
    s = digits[:-1] + wrong
    return f"{s[:3]}-{s[3:5]}-{s[5:]}"


# ── 기업 풀 정의 ──────────────────────────────────────────────────────────────
# nts: "계속" | "휴업" | "폐업전" (폐업일 < 지원일) | "폐업후" (폐업일 > 지원일) | "미등록"
# industry: 이 기업이 속한 지역 업종 (org 매핑용)

_COMPANY_RAW: list[dict] = [
    # ── 기계·자동차부품 (창원·김해·양산) ───────────────────────────────────────
    {"name": "(주)경남정밀기계",          "industry": "기계",        "nts": "계속"},
    {"name": "창원기계공업(주)",           "industry": "기계",        "nts": "계속"},
    {"name": "대한자동차부품(주)",         "industry": "자동차부품",  "nts": "계속"},
    {"name": "(주)남부드라이브텍",         "industry": "자동차부품",  "nts": "계속"},
    {"name": "한국산업기계(주)",           "industry": "기계",        "nts": "계속"},
    {"name": "(주)경남메카트로닉스",       "industry": "기계",        "nts": "계속"},
    {"name": "동방공작기계(주)",           "industry": "기계",        "nts": "계속"},
    {"name": "(주)삼원산업",              "industry": "기계",        "nts": "계속"},
    {"name": "한일금속공업(주)",           "industry": "금속",        "nts": "계속"},
    {"name": "(주)경남오토파츠",           "industry": "자동차부품",  "nts": "계속"},
    {"name": "양산모빌리티(주)",           "industry": "자동차부품",  "nts": "계속"},
    {"name": "(주)남부물류센터",           "industry": "물류",        "nts": "계속"},
    {"name": "경남로지스틱스(주)",         "industry": "물류",        "nts": "계속"},
    # ── 조선·해양 (거제·통영) ────────────────────────────────────────────────
    {"name": "거제조선기자재(주)",         "industry": "조선기자재",  "nts": "계속"},
    {"name": "(주)한국마린테크",           "industry": "조선기자재",  "nts": "계속"},
    {"name": "경남선박부품(주)",           "industry": "조선기자재",  "nts": "계속"},
    {"name": "(주)통영마린푸드",           "industry": "수산물",      "nts": "계속"},
    {"name": "거제해양플랜트(주)",         "industry": "조선기자재",  "nts": "계속"},
    # ── 항공·방산 (사천·진주) ─────────────────────────────────────────────────
    {"name": "(주)사천에어로스페이스",     "industry": "항공",        "nts": "계속"},
    {"name": "한국항공MRO(주)",            "industry": "항공",        "nts": "계속"},
    {"name": "(주)경남방산테크",           "industry": "방위산업",    "nts": "계속"},
    {"name": "코리아에비에이션(주)",       "industry": "항공부품",    "nts": "계속"},
    {"name": "(주)진주항공부품",           "industry": "항공부품",    "nts": "계속"},
    # ── 전기·전자·IT ─────────────────────────────────────────────────────────
    {"name": "경남일렉트로닉스(주)",       "industry": "전기전자",    "nts": "계속"},
    {"name": "(주)남부전기시스템",         "industry": "전기전자",    "nts": "계속"},
    {"name": "(주)경남디지털솔루션",       "industry": "전기전자",    "nts": "계속"},
    {"name": "한국전자기술(주)",           "industry": "전기전자",    "nts": "계속"},
    # ── 섬유·나노섬유 (진주·밀양) ────────────────────────────────────────────
    {"name": "진주섬유(주)",              "industry": "섬유",        "nts": "계속"},
    {"name": "경남텍스타일(주)",           "industry": "섬유",        "nts": "계속"},
    {"name": "(주)남부실크",              "industry": "섬유",        "nts": "계속"},
    {"name": "(주)밀양나노섬유",           "industry": "나노섬유",    "nts": "계속"},
    # ── 바이오·헬스 (진주·밀양) ──────────────────────────────────────────────
    {"name": "(주)진주바이오텍",           "industry": "바이오",      "nts": "계속"},
    {"name": "경남헬스케어(주)",           "industry": "바이오",      "nts": "계속"},
    {"name": "(주)남부생명과학",           "industry": "바이오",      "nts": "계속"},
    # ── 농식품·특산물 ─────────────────────────────────────────────────────────
    {"name": "영농조합법인 함안수박",       "industry": "농식품",      "nts": "계속"},
    {"name": "영농조합법인 의령한우",       "industry": "한우",        "nts": "계속"},
    {"name": "농업회사법인 남해수산(주)",   "industry": "수산물",      "nts": "계속"},
    {"name": "하동녹차(주)",              "industry": "녹차",        "nts": "계속"},
    {"name": "산청한방약초(주)",           "industry": "약초",        "nts": "계속"},
    {"name": "거창사과(주)",              "industry": "사과",        "nts": "계속"},
    {"name": "농업회사법인 고령참외(주)",   "industry": "참외",        "nts": "계속"},
    {"name": "(주)성주참외농장",           "industry": "참외",        "nts": "계속"},
    {"name": "(주)경남식품",              "industry": "농식품",      "nts": "계속"},
    {"name": "함양산삼(주)",              "industry": "산삼",        "nts": "계속"},
    {"name": "영농조합법인 창녕양파",       "industry": "농식품",      "nts": "계속"},
    {"name": "합천영농조합법인",           "industry": "농식품",      "nts": "계속"},
    # ── 관광·서비스 ──────────────────────────────────────────────────────────
    {"name": "(주)경남투어리즘",           "industry": "관광",        "nts": "계속"},
    {"name": "남해관광개발(주)",           "industry": "관광",        "nts": "계속"},
    # ── 스마트팩토리·디지털전환 ─────────────────────────────────────────────
    {"name": "(주)경남스마트팩토리",       "industry": "기계",        "nts": "계속"},
    {"name": "남부ICT솔루션(주)",          "industry": "전기전자",    "nts": "계속"},
    # ── 사회적기업·여성기업·청년창업 ─────────────────────────────────────────
    {"name": "(주)경남소셜벤처",           "industry": "농식품",      "nts": "계속"},
    {"name": "사회적협동조합 경남푸드",     "industry": "농식품",      "nts": "계속"},
    {"name": "(주)경남여성기업",           "industry": "섬유",        "nts": "계속"},
    {"name": "청년창업협동조합 경남",       "industry": "전기전자",    "nts": "계속"},
    # ── 휴업자 (6개) ──────────────────────────────────────────────────────────
    {"name": "(구)남부정밀기계(주)",       "industry": "기계",        "nts": "휴업"},
    {"name": "경남조선부품(주)",           "industry": "조선기자재",  "nts": "휴업"},
    {"name": "(구)진주직물(주)",           "industry": "섬유",        "nts": "휴업"},
    {"name": "창원전자공업(주)",           "industry": "전기전자",    "nts": "휴업"},
    {"name": "(구)거제수산(주)",           "industry": "수산물",      "nts": "휴업"},
    {"name": "함안기계부품(구)(주)",       "industry": "기계",        "nts": "휴업"},
    # ── 폐업자 — 지원일 이전 폐업 (4개, 폐업일 2025년) ──────────────────────
    {"name": "(폐)경남자동차부품(주)",     "industry": "자동차부품",  "nts": "폐업전",
     "end_dt": "20250301"},
    {"name": "(폐)통영해산물(주)",         "industry": "수산물",      "nts": "폐업전",
     "end_dt": "20250615"},
    {"name": "(폐)진주섬유기계(주)",       "industry": "섬유",        "nts": "폐업전",
     "end_dt": "20251101"},
    {"name": "(폐)사천항공부품(주)",       "industry": "항공부품",    "nts": "폐업전",
     "end_dt": "20251215"},
    # ── 폐업자 — 지원일 이후 폐업 (4개, 폐업일 2026년 하반기) ──────────────
    {"name": "(폐)경남금속가공(주)",       "industry": "금속",        "nts": "폐업후",
     "end_dt": "20261001"},
    {"name": "(폐)남부식품(주)",           "industry": "농식품",      "nts": "폐업후",
     "end_dt": "20261115"},
    {"name": "(폐)거제관광(주)",           "industry": "관광",        "nts": "폐업후",
     "end_dt": "20261201"},
    {"name": "(폐)창원전기부품(주)",       "industry": "전기전자",    "nts": "폐업후",
     "end_dt": "20261210"},
    # ── 국세청 미등록 (4개 — 체크섬 유효하나 등록이력 없음) ─────────────────
    {"name": "(미)경남가상기업(주)",       "industry": "기계",        "nts": "미등록"},
    {"name": "(미)테스트산업(주)",         "industry": "자동차부품",  "nts": "미등록"},
    {"name": "(미)가상식품(주)",           "industry": "농식품",      "nts": "미등록"},
    {"name": "(미)경남관광개발(주)",       "industry": "관광",        "nts": "미등록"},
]

# 사업자번호 할당 (결정론적 — 인덱스 기반)
COMPANY_POOL: list[dict] = []
for _idx, _c in enumerate(_COMPANY_RAW):
    _c = dict(_c)
    _c["bizno"] = _make_bizno(1000 + _idx)
    COMPANY_POOL.append(_c)

# ── 업종별 기업 풀 인덱스 ─────────────────────────────────────────────────────
from collections import defaultdict

_INDUSTRY_POOL: dict[str, list[dict]] = defaultdict(list)
for _c in COMPANY_POOL:
    _INDUSTRY_POOL[_c["industry"]].append(_c)

# ── 경남 20개 시·군 및 지역 업종 ─────────────────────────────────────────────
ORGS: list[dict] = [
    {"name": "창원시",  "industries": ["기계", "자동차부품", "방위산업", "전기전자"]},
    {"name": "진주시",  "industries": ["항공부품", "섬유", "바이오"]},
    {"name": "통영시",  "industries": ["조선기자재", "수산물", "관광"]},
    {"name": "사천시",  "industries": ["항공", "방위산업", "항공부품"]},
    {"name": "김해시",  "industries": ["금속", "자동차부품", "물류"]},
    {"name": "밀양시",  "industries": ["나노섬유", "농식품", "바이오"]},
    {"name": "거제시",  "industries": ["조선기자재", "관광"]},
    {"name": "양산시",  "industries": ["자동차부품", "물류", "전기전자"]},
    {"name": "의령군",  "industries": ["한우", "농식품"]},
    {"name": "함안군",  "industries": ["기계", "농식품"]},
    {"name": "창녕군",  "industries": ["농식품"]},
    {"name": "고성군",  "industries": ["수산물", "관광"]},
    {"name": "남해군",  "industries": ["수산물", "관광"]},
    {"name": "하동군",  "industries": ["녹차", "농식품"]},
    {"name": "산청군",  "industries": ["약초", "농식품"]},
    {"name": "함양군",  "industries": ["산삼", "약초", "농식품"]},
    {"name": "거창군",  "industries": ["사과", "농식품", "관광"]},
    {"name": "합천군",  "industries": ["농식품", "관광"]},
    {"name": "고령군",  "industries": ["참외", "농식품"]},
    {"name": "성주군",  "industries": ["참외", "농식품"]},
]


def _pick_company(org: dict, exclude: set[str]) -> dict:
    """지역 업종에 맞는 기업을 풀에서 선택. 없으면 전체 풀에서 선택."""
    candidates = []
    for ind in org["industries"]:
        candidates.extend(_INDUSTRY_POOL.get(ind, []))
    # 미등록/폐업/휴업 비율 유지를 위해 '계속' 아닌 기업도 후보에 포함
    if not candidates:
        candidates = COMPANY_POOL[:]
    available = [c for c in candidates if c["bizno"] not in exclude]
    if not available:
        available = candidates  # 풀 소진 시 중복 허용
    return random.choice(available)


# ── 날짜/금액 오류 주입 ───────────────────────────────────────────────────────

def _clean_date() -> str:
    m = random.randint(1, 9)
    d = random.randint(1, 28)
    return f"2026-{m:02d}-{d:02d}"


def _dirty_date() -> str:
    """R3 DATE_NORMALIZE 오류 케이스."""
    m = random.randint(1, 9)
    d = random.randint(1, 28)
    return random.choice([
        f"2026.{m:02d}.{d:02d}",          # 점 형식
        f"{m}/{d}/2026",                   # M/D/YYYY
        f"26-{m:02d}-{d:02d}",            # 2자리 연도
        f"2026년 {m}월 {d}일",             # 한국어
        f"2026{m:02d}{d:02d}",             # 8자리 붙임
    ])


def _clean_amount(program: dict) -> str:
    base = random.randint(
        program["min_amt"] // 10_000,
        program["max_amt"] // 10_000,
    ) * 10_000
    return str(base)


def _dirty_amount(program: dict) -> str:
    """R2 AMOUNT_NORMALIZE 오류 케이스."""
    base = random.randint(
        program["min_amt"] // 10_000,
        program["max_amt"] // 10_000,
    ) * 10_000
    style = random.choice(["comma", "won", "manwon", "mixed"])
    if style == "comma":
        return f"{base:,}"
    elif style == "won":
        return f"{base:,}원"
    elif style == "manwon":
        return f"{base // 10_000}만원"
    else:
        eok = base // 100_000_000
        rem = (base % 100_000_000) // 10_000
        if eok > 0:
            return f"{eok}억 {rem:,}만원"
        return f"{base // 10_000:,}만원"



# ── xlsx 파일 생성 ────────────────────────────────────────────────────────────

def generate_file(org: dict, out_path: Path) -> None:
    org_name = org["name"]
    wb = Workbook()
    ws = wb.active
    ws.title = "실적"

    headers = ["기관명", "사업코드", "사업명", "수혜기업명",
               "사업자번호", "지원일자", "지원금액"]
    ws.append(headers)

    n_rows = random.randint(8, 18)
    code_amounts: dict[str, int] = {}
    code_counts:  dict[str, int] = {}
    used_biznos: set[str] = set()

    for _ in range(n_rows):
        prog = random.choice(PROGRAMS)
        roll = random.random()

        # ── 수혜기업명·사업자번호 ────────────────────────────────────────────
        if roll < 0.88:
            # 정상: 풀에서 기업 선택 (중복 기업 최소화)
            company = _pick_company(org, used_biznos)
            bizno = company["bizno"]
            used_biznos.add(bizno)
            company_name = company["name"]
        else:
            # 체크섬 오류 (12%) — R4 BIZNO_CHECKSUM 유발
            company = _pick_company(org, set())
            bizno = _corrupt_bizno(company["bizno"])
            company_name = company["name"]

        # ── 지원일자 ──────────────────────────────────────────────────────────
        date_val = _dirty_date() if random.random() < 0.10 else _clean_date()

        # ── 지원금액 ──────────────────────────────────────────────────────────
        amount_str = _dirty_amount(prog) if random.random() < 0.15 else _clean_amount(prog)

        # ── 기관명/사업명: 5% 확률로 공백 주입 (R1 BLANK 유발) ──────────────
        org_cell = "" if random.random() < 0.03 else org_name
        biz_name_cell = "" if random.random() < 0.02 else prog["name"]

        ws.append([org_cell, prog["code"], biz_name_cell,
                   company_name, bizno, date_val, amount_str])

        # 합계 집계 (순수 정수만)
        try:
            amt_int = int(amount_str)
            code_amounts[prog["code"]] = code_amounts.get(prog["code"], 0) + amt_int
            code_counts[prog["code"]]  = code_counts.get(prog["code"],  0) + 1
        except ValueError:
            code_counts[prog["code"]] = code_counts.get(prog["code"], 0) + 1

    # ── 요약 시트 ─────────────────────────────────────────────────────────────
    ws_sum = wb.create_sheet("요약")
    ws_sum.append(["사업코드", "지원건수", "지원금액합계"])
    for code in sorted(code_counts):
        ws_sum.append([code, str(code_counts[code]),
                       str(code_amounts.get(code, 0))])

    try:
        wb.save(out_path)
        print(f"  생성: {out_path.name}  ({n_rows}행)")
    except PermissionError:
        print(f"  [건너뜀] {out_path.name} — Excel에서 열려 있음, 닫고 재실행하세요")


# ── nts_mock.json 재생성 ──────────────────────────────────────────────────────

def rebuild_nts_mock() -> None:
    """기업 풀의 사업자번호 전체를 nts_mock.json에 등록."""
    data: dict[str, dict] = {}
    for c in COMPANY_POOL:
        digits = c["bizno"].replace("-", "")
        nts_type = c["nts"]

        if nts_type == "계속":
            data[digits] = {
                "b_no": digits, "b_stt": "계속사업자", "b_stt_cd": "01",
                "tax_type": "일반과세자", "tax_type_cd": "1", "end_dt": "",
            }
        elif nts_type == "휴업":
            data[digits] = {
                "b_no": digits, "b_stt": "휴업자", "b_stt_cd": "02",
                "tax_type": "", "tax_type_cd": "", "end_dt": "",
            }
        elif nts_type in ("폐업전", "폐업후"):
            data[digits] = {
                "b_no": digits, "b_stt": "폐업자", "b_stt_cd": "03",
                "tax_type": "", "tax_type_cd": "",
                "end_dt": c.get("end_dt", ""),
            }
        elif nts_type == "미등록":
            data[digits] = {
                "b_no": digits, "b_stt": "국세청 미등록", "b_stt_cd": None,
                "tax_type": "", "tax_type_cd": "", "end_dt": "",
            }

    payload = {
        "comment": (
            "기업 풀 기반 nts_mock (RULES.md R-08). "
            f"총 {len(data)}개 사업자번호. "
            "자동 재생성 — 직접 편집 금지."
        ),
        "data": data,
    }
    MOCK_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    counts = {}
    for c in COMPANY_POOL:
        counts[c["nts"]] = counts.get(c["nts"], 0) + 1
    print(f"\n  nts_mock.json 재생성: {len(data)}개 번호")
    for k, v in sorted(counts.items()):
        print(f"    {k}: {v}개")


# ── 메인 ─────────────────────────────────────────────────────────────────────

def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("경남 20개 시·군 사업실적 샘플 생성 중...\n")
    for org in ORGS:
        fname = f"{org['name']}_사업실적_2026.xlsx"
        generate_file(org, OUT_DIR / fname)
    print(f"\n[완료] 총 {len(ORGS)}개 파일 생성.")

    rebuild_nts_mock()

    print("\n[주입된 오류 유형 — RULES.md R1~R4]")
    print("  R1 BLANK            : 기관명/사업명 공백 (약 3~2%)")
    print("  R2 AMOUNT_NORMALIZE : 쉼표·원·만원·억만원 혼용 (15%)")
    print("  R3 DATE_NORMALIZE   : 점/슬래시/2자리연도/한국어 형식 (10%)")
    print("  R4 BIZNO_CHECKSUM   : 체크디지트 오류 (12%)")
    print("  R4 BIZNO_SUSPENDED  : 휴업 기업 선택 시 자연 발생")
    print("  R4 BIZNO_CLOSED_*   : 폐업 기업 선택 시 자연 발생")
    print("  R4 BIZNO_NOT_FOUND  : 미등록 기업 선택 시 자연 발생")


if __name__ == "__main__":
    for old in OUT_DIR.glob("*.xlsx"):
        if old.name.startswith("~$"):
            continue   # Excel 잠금 파일은 건너뜀
        try:
            old.unlink()
        except PermissionError:
            print(f"  [경고] {old.name} 이 열려 있어 건너뜀 — Excel을 닫고 재실행하세요")
    main()
