"""평가 스크립트: 검출률·적중률 등 지표 계산."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_answer(testset_dir: Path) -> list[dict]:
    path = testset_dir / "answer.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_issues(run_dir: Path) -> list[dict]:
    path = run_dir / "issues.csv"
    import csv
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def load_changes(run_dir: Path) -> list[dict]:
    path = run_dir / "changelog.csv"
    if not path.exists():
        return []
    import csv
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def evaluate(testset_dir: Path, run_dir: Path) -> dict:
    answer = load_answer(testset_dir)
    issues = load_issues(run_dir)
    changes = load_changes(run_dir)

    # answer lookup: (file, sheet, row, column) → answer entry
    ans_map: dict[tuple, dict] = {}
    for a in answer:
        key = (a["file"], a["sheet"], str(a["row"]), a["column"])
        ans_map[key] = a

    # 검출된 issue lookup
    detected: set[tuple] = set()
    for i in issues:
        key = (i["file"], i["sheet"], str(i["row"]), i["column"])
        if key in ans_map:
            detected.add(key)

    total_injected = len(answer)
    total_detected = len(detected)
    detection_rate = total_detected / total_injected if total_injected else 0.0

    # 규칙형 vs 이상치형
    rule_ans = [a for a in answer if a.get("rule_id") != "OUTLIER"]
    outlier_ans = [a for a in answer if a.get("rule_id") == "OUTLIER"]

    rule_det = sum(1 for a in rule_ans if (a["file"], a["sheet"], str(a["row"]), a["column"]) in detected)
    outlier_det = sum(1 for a in outlier_ans if (a["file"], a["sheet"], str(a["row"]), a["column"]) in detected)

    rule_detection = rule_det / len(rule_ans) if rule_ans else 0.0
    outlier_detection = outlier_det / len(outlier_ans) if outlier_ans else 0.0

    # 자동 수정 오수정률 (auto 변경 중 answer에 없는 것)
    auto_changes = [c for c in changes if c.get("tier") == "auto"]
    wrong_auto = sum(
        1 for c in auto_changes
        if (c["file"], c["sheet"], str(c["row"]), c["column"]) not in ans_map
    )
    wrong_auto_rate = wrong_auto / len(auto_changes) if auto_changes else 0.0

    # 수정안 적중률 (answer와 after 값 비교)
    change_map: dict[tuple, str] = {}
    for c in changes:
        key = (c["file"], c["sheet"], str(c["row"]), c["column"])
        change_map[key] = c.get("after", "")

    hit = 0
    total_fix = 0
    for a in answer:
        key = (a["file"], a["sheet"], str(a["row"]), a["column"])
        if key in change_map:
            total_fix += 1
            if change_map[key] == a["original"]:
                hit += 1
    fix_hit_rate = hit / total_fix if total_fix else 0.0

    # 자동 처리율
    auto_tier_issues = [i for i in issues if i.get("tier") == "auto"]
    auto_rate = len(auto_tier_issues) / len(issues) if issues else 0.0

    metrics = {
        "총 주입": total_injected,
        "총 검출": total_detected,
        "전체 검출률": f"{detection_rate:.1%}",
        "규칙형 검출률": f"{rule_detection:.1%}",
        "이상치형 검출률": f"{outlier_detection:.1%}",
        "자동 수정 오수정률": f"{wrong_auto_rate:.1%}",
        "수정안 적중률": f"{fix_hit_rate:.1%}",
        "자동 처리율": f"{auto_rate:.1%}",
    }

    targets = {
        "규칙형 검출률": (rule_detection, 0.95),
        "이상치형 검출률": (outlier_detection, 0.70),
        "자동 수정 오수정률": (wrong_auto_rate, 0.03, True),
        "수정안 적중률": (fix_hit_rate, 0.80),
        "자동 처리율": (auto_rate, 0.60),
    }

    print("\n=== 평가 결과 ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    print("\n=== 목표 달성 여부 ===")
    for name, tup in targets.items():
        val, tgt = tup[0], tup[1]
        lower_better = len(tup) == 3 and tup[2]
        ok = val <= tgt if lower_better else val >= tgt
        sym = "[OK]" if ok else "[X] "
        dir_str = f"≤{tgt:.0%}" if lower_better else f"≥{tgt:.0%}"
        print(f"  {sym} {name}: {val:.1%} (목표 {dir_str})")

    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--testset", required=True)
    parser.add_argument("--run", required=True)
    args = parser.parse_args()
    evaluate(Path(args.testset), Path(args.run))
