"""과거 실행(outputs/<run_id>/) 읽기 — 화면의 '실행 이력'에서 결과를 다시 보여줄 때 쓴다."""
from __future__ import annotations

import csv
import json
import logging
from datetime import datetime
from pathlib import Path

from engine.models import CellRef, Change, Issue, RequestItem, RunReport

logger = logging.getLogger(__name__)

_MAX_RUNS = 20


def list_runs(outputs_dir: Path, limit: int = _MAX_RUNS) -> list[str]:
    """완료된 실행(report.json 이 있는 폴더)만 최신순으로 돌려준다. 임시·미완료 폴더는 제외한다."""
    if not outputs_dir.exists():
        return []
    runs = [d.name for d in outputs_dir.iterdir()
            if d.is_dir() and not d.name.startswith((".", "_")) and (d / "report.json").exists()]
    return sorted(runs, reverse=True)[:limit]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _or_none(value: str | None) -> str | None:
    return value if value not in (None, "") else None


def _ref(row: dict[str, str]) -> CellRef | None:
    if not row.get("file"):
        return None
    return CellRef(file=row["file"], sheet=row.get("sheet", ""), row=int(row["row"]), column=row.get("column", ""))


def load_issues(run_dir: Path) -> list[Issue]:
    issues: list[Issue] = []
    for row in _read_csv(run_dir / "issues.csv"):
        try:
            issues.append(Issue(
                issue_id=row["issue_id"], ref=_ref(row), rule_id=row["rule_id"], kind=row["kind"],
                value=_or_none(row.get("value")), message=row.get("message", ""),
                explanation=_or_none(row.get("explanation")), tier=row["tier"],
                suggestion=_or_none(row.get("suggestion")),
            ))
        except (KeyError, ValueError) as e:
            logger.warning("issues.csv 행 건너뜀 (%s): %s", run_dir.name, e)
    return issues


def load_changes(run_dir: Path) -> list[Change]:
    changes: list[Change] = []
    for row in _read_csv(run_dir / "changelog.csv"):
        try:
            changes.append(Change(
                change_id=row["change_id"], issue_id=row["issue_id"], ref=_ref(row),
                before=_or_none(row.get("before")), after=_or_none(row.get("after")), tier=row["tier"],
                decided_by=row["decided_by"], timestamp=datetime.fromisoformat(row["timestamp"]),
            ))
        except (KeyError, ValueError) as e:
            logger.warning("changelog.csv 행 건너뜀 (%s): %s", run_dir.name, e)
    return changes


def load_goal(run_dir: Path) -> str | None:
    """이 실행의 목표 문장. 목표를 남기기 전에 끝난 실행이면 None."""
    path = run_dir / "goal.txt"
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def load_run(run_dir: Path) -> dict | None:
    """화면 상태(state) 모양의 dict. report.json 이 없거나 깨졌으면 None."""
    path = run_dir / "report.json"
    if not path.exists():
        return None
    try:
        report = RunReport(**json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError) as e:
        logger.warning("report.json 읽기 실패 (%s): %s", run_dir.name, e)
        return None
    return {"run_id": run_dir.name, "input_paths": report.files, "issues": load_issues(run_dir),
            "decisions": [], "changes": load_changes(run_dir), "requests": [], "report": report,
            "goal": load_goal(run_dir)}


AGGREGATE_MAIL_TAG = "집계"


def load_requests(run_dir: Path) -> list[tuple[str, RequestItem]]:
    """저장된 메일 초안 (종류, 메일). 종류는 '보완 요청' 또는 '집계 확인 요청'. 깨진 파일은 건너뛴다."""
    d = run_dir / "requests"
    if not d.is_dir():
        return []
    out: list[tuple[str, RequestItem]] = []
    for p in sorted(d.glob("*.json")):
        try:
            item = RequestItem(**json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError) as e:
            logger.warning("메일 초안 읽기 실패 (%s/%s): %s", run_dir.name, p.name, e)
            continue
        is_aggregate = p.stem.endswith(f"__{AGGREGATE_MAIL_TAG}") or "(집계 확인)" in item.org
        out.append(("집계 확인 요청" if is_aggregate else "보완 요청", item))
    return out


def cleaned_files(run_dir: Path) -> list[Path]:
    """이 실행의 정제본 파일 (새 실행의 입력으로 쓸 수 있다). 없으면 빈 목록."""
    d = run_dir / "cleaned"
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".xlsx", ".json"))
