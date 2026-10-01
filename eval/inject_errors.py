"""오류 주입 스크립트 (블라인드 평가용 — B 담당자는 열람 금지)."""
from __future__ import annotations

import argparse
import json
import random
import shutil
from pathlib import Path

import openpyxl
import pandas as pd

SEED = 42


def _rand_bad_date(correct: str) -> str:
    """ISO 날짜를 다양한 잘못된 형식으로."""
    fmts = [
        lambda d: d.replace("-", ""),          # YYYYMMDD
        lambda d: d.replace("-", "."),          # YYYY.MM.DD
        lambda d: d.replace("-", "/"),          # YYYY/MM/DD
        lambda d: d[2:],                        # YY-MM-DD (모호)
    ]
    return random.choice(fmts)(correct)


def _rand_bad_phone(correct: str) -> str:
    return correct.replace("-", "")


def _rand_bad_amount(correct: str) -> str:
    try:
        n = int(correct)
        return f"{n:,}"  # 쉼표 추가
    except ValueError:
        return correct


def _rand_bad_bizno(correct: str) -> str:
    digits = correct.replace("-", "")
    wrong = list(digits)
    idx = random.randint(0, 8)
    wrong[idx] = str((int(wrong[idx]) + 1) % 10)
    return "".join(wrong)


def inject_to_df(df: pd.DataFrame, answer: list, file: str, sheet: str, rng: random.Random) -> pd.DataFrame:
    df = df.copy()
    cols = [c for c in df.columns if c != "_row"]

    for i, row in df.iterrows():
        row_num = int(row["_row"])
        for col in cols:
            val = str(row[col]).strip()
            if not val or val.lower() in ("nan", "none", ""):
                continue

            roll = rng.random()
            injected = None
            rule_id = None

            # 날짜 컬럼 추정
            if "일자" in col or "날짜" in col or "date" in col.lower():
                from datetime import date
                try:
                    date.fromisoformat(val)
                    if roll < 0.15:
                        injected = _rand_bad_date(val)
                        rule_id = "DATE_FORMAT"
                except ValueError:
                    pass

            # 전화 컬럼
            elif "연락처" in col or "전화" in col or "phone" in col.lower():
                import re
                if re.match(r"^0\d{1,2}-\d{3,4}-\d{4}$", val) and roll < 0.20:
                    injected = _rand_bad_phone(val)
                    rule_id = "PHONE_FORMAT"

            # 금액 컬럼
            elif "금액" in col or "amount" in col.lower():
                try:
                    int(val)
                    if roll < 0.15:
                        injected = _rand_bad_amount(val)
                        rule_id = "NUMERIC_UNIT"
                except ValueError:
                    pass

            # 사업자번호: 절반은 형식 오류(하이픈 제거 → auto), 절반은 체크섬 오류(approval)
            elif "사업자" in col or "biz" in col.lower():
                if roll < 0.15:
                    if rng.random() < 0.5:
                        injected = val.replace("-", "")  # 형식 오류만 (auto)
                    else:
                        injected = _rand_bad_bizno(val)   # 체크섬 오류 (approval)
                    rule_id = "BIZNO_CHECKSUM"

            if injected and injected != val:
                answer.append({
                    "file": file, "sheet": sheet, "row": row_num,
                    "column": col, "rule_id": rule_id,
                    "original": val, "injected": injected,
                })
                df.at[i, col] = injected

    # 이상치 주입: 금액 컬럼에서 랜덤 1행을 10배 증폭
    amount_cols = [c for c in cols if "금액" in c]
    for acol in amount_cols:
        nums = []
        for i, row in df.iterrows():
            try:
                nums.append((i, int(str(row[acol]).replace(",", ""))))
            except ValueError:
                pass
        if len(nums) >= 4 and rng.random() < 0.3:
            idx, val_int = rng.choice(nums)
            row_num = int(df.at[idx, "_row"])
            original = str(val_int)
            outlier = str(val_int * 20)
            answer.append({
                "file": file, "sheet": sheet, "row": row_num,
                "column": acol, "rule_id": "OUTLIER",
                "original": original, "injected": outlier,
            })
            df.at[idx, acol] = outlier

    return df


def inject_file(src: Path, dst: Path, answer: list, rng: random.Random) -> None:
    wb_src = openpyxl.load_workbook(src, data_only=True)
    wb_dst = openpyxl.Workbook()
    wb_dst.remove(wb_dst.active)

    for sheet_name in wb_src.sheetnames:
        ws = wb_src[sheet_name]
        rows = list(ws.values)
        if not rows:
            continue
        headers = [str(h) if h is not None else "" for h in rows[0]]
        data = [[str(c) if c is not None else "" for c in r] for r in rows[1:]]
        df = pd.DataFrame(data, columns=headers)
        df.insert(0, "_row", range(2, 2 + len(df)))

        df_injected = inject_to_df(df, answer, src.name, sheet_name, rng)

        ws_dst = wb_dst.create_sheet(sheet_name)
        ws_dst.append(headers)
        for _, row in df_injected.iterrows():
            ws_dst.append([row[c] for c in headers])

    wb_dst.save(dst)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", default="data/samples")
    parser.add_argument("--out", default="data/testset")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()

    src_dir = Path(args.src)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    answer: list = []

    for xlsx in sorted(src_dir.glob("*.xlsx")):
        dst = out_dir / xlsx.name
        inject_file(xlsx, dst, answer, rng)
        print(f"주입: {xlsx.name}")

    answer_path = out_dir / "answer.json"
    with open(answer_path, "w", encoding="utf-8") as f:
        json.dump(answer, f, ensure_ascii=False, indent=2)

    rule_counts: dict[str, int] = {}
    for a in answer:
        rule_counts[a["rule_id"]] = rule_counts.get(a["rule_id"], 0) + 1

    print(f"\n주입 완료: {len(answer)}건")
    for k, v in sorted(rule_counts.items()):
        print(f"  {k}: {v}건")


if __name__ == "__main__":
    main()
