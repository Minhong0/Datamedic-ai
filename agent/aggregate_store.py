"""집계 검사 결과의 저장·복원 — 과거 실행(outputs/<run_id>/aggregate_results.json)을 다시 열 때 쓴다."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from engine.masking import mask_bizno, mask_issue, mask_text
from engine.models import GroupIssue, RuleSpec

logger = logging.getLogger(__name__)

FILE_NAME = "aggregate_results.json"


def _mask_group(g: GroupIssue) -> GroupIssue:
    masked = mask_issue(g)
    return masked.model_copy(update={
        "group_key": {k: (mask_bizno(v) if k == "사업자번호" else mask_text(v)) for k, v in g.group_key.items()},
        "metric": mask_text(g.metric),
    })


def save_aggregate_results(agg_results: list[dict], dispositions: dict | None, run_dir: Path) -> Path | None:
    """집계 결과가 없으면 쓰지 않는다. 쓴 경로를 돌려준다."""
    if not agg_results:
        return None
    results = []
    for res in agg_results:
        spec = res.get("spec")
        results.append({
            "capability": res.get("capability", ""),
            "skipped": bool(res.get("skipped")),
            "response_text": mask_text(res.get("response_text", "")),
            "spec": spec.model_dump(mode="json") if spec is not None else None,
            "group_issues": [_mask_group(g).model_dump(mode="json") for g in res.get("group_issues", [])],
        })
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / FILE_NAME
    path.write_text(json.dumps({"results": results, "dispositions": dict(dispositions or {})},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def update_dispositions(run_dir: Path, dispositions: dict) -> bool:
    """담당자가 바꾼 '처리'(확인 요청/문제 없음)만 파일에 반영한다. 저장된 결과가 없으면 아무것도 하지 않는다."""
    path = run_dir / FILE_NAME
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        data["dispositions"] = dict(dispositions)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except (ValueError, OSError) as e:
        logger.warning("집계 처리 결과 저장 실패 (%s): %s", run_dir.name, e)
        return False


def load_aggregate_results(run_dir: Path) -> dict | None:
    """{"agg_results": [...], "agg_dispositions": {...}} — 저장된 것이 없거나 깨졌으면 None."""
    path = run_dir / FILE_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        results = [{
            "capability": r["capability"], "skipped": bool(r.get("skipped")),
            "response_text": r.get("response_text", ""),
            "spec": RuleSpec(**r["spec"]) if r.get("spec") else None,
            "group_issues": [GroupIssue(**g) for g in r.get("group_issues", [])],
        } for r in data.get("results", [])]
        return {"agg_results": results, "agg_dispositions": dict(data.get("dispositions", {}))}
    except (KeyError, ValueError, OSError) as e:
        logger.warning("집계 결과 읽기 실패 (%s): %s", run_dir.name, e)
        return None
