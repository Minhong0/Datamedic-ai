from __future__ import annotations

import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from config import settings
from engine import exporter, validator
from engine.planning import PlanScope, active_scope
from engine.models import RequestItem, RunReport
from engine.viewer_marks import rule_label
from agent.nodes.aggregate_requests import MAIL_WORKERS
from agent.state import GraphState

logger = logging.getLogger(__name__)


def recheck_node(state: GraphState) -> GraphState:
    """파이프라인 6단계(마지막): 수정 후 재검진 → 보완 요청 생성 → 결과 파일 저장."""
    cleaned = state.get("cleaned") or state["dataset"]
    bindings = state["bindings"]
    run_id = state["run_id"]
    issues_orig = state["issues"]

    scope = PlanScope.from_plan(state.get("plan"))
    with active_scope(scope):
        remaining = validator.run(
            cleaned, bindings, allowed_rule_ids=scope.rule_ids if scope else None,
        )
    if scope is not None:
        remaining = scope.filter_issues(remaining)
    passed = len(remaining) == 0

    from engine.masking import mask_issues
    request_cells = {(i.ref.file, i.ref.sheet, i.ref.row, i.ref.column) for i in issues_orig
                     if i.ref and any(d.issue_id == i.issue_id and d.action == "request"
                                      for d in state.get("decisions", []))}
    request_issues = mask_issues([
        i for i in remaining
        if i.tier == "request" or (i.ref and (i.ref.file, i.ref.sheet, i.ref.row, i.ref.column) in request_cells)
    ])
    org_issues: dict[str, list] = defaultdict(list)
    for i in request_issues:
        org = i.ref.file if i.ref else "unknown"
        org_issues[org].append(i)

    from notify.contacts import load_contacts, resolve_recipient
    contacts = load_contacts()
    recipients = {org: resolve_recipient(org, state.get("dataset"), contacts) for org in org_issues}

    def _where(i) -> str:
        return f"{i.ref.row}행 {i.ref.column}" if i.ref else "-"

    def _draft(org: str, org_i: list) -> RequestItem:
        to = recipients[org]
        try:
            from llm.client import complete_json
            from pydantic import BaseModel

            class MailResp(BaseModel):
                subject: str
                body: str

            issue_list = "\n".join(
                f"- {_where(i)} — {rule_label(i.rule_id)}: {i.message}" for i in org_i[:10]
            )
            resp = complete_json(
                "request_mail",
                {"org_name": org, "issues": issue_list},
                MailResp,
            )
            return RequestItem(
                org=org, issues=[i.issue_id for i in org_i],
                subject=resp.subject, body=resp.body, to=to,
            )
        except Exception:
            return RequestItem(
                org=org, issues=[i.issue_id for i in org_i],
                subject=f"[데이터 보완 요청] {org}",
                body="\n".join(f"- {_where(i)}: {i.message}" for i in org_i), to=to,
            )

    with ThreadPoolExecutor(max_workers=min(MAIL_WORKERS, max(len(org_issues), 1))) as pool:
        requests: list[RequestItem] = list(pool.map(lambda kv: _draft(*kv), org_issues.items()))

    issue_counts: dict[str, int] = defaultdict(int)
    tier_counts: dict[str, int] = defaultdict(int)
    for i in issues_orig:
        issue_counts[i.rule_id] += 1
        tier_counts[i.tier] += 1

    report = RunReport(
        run_id=run_id,
        files=list({i.ref.file for i in issues_orig if i.ref}),
        issue_counts=dict(issue_counts),
        tier_counts=dict(tier_counts),
        recheck_passed=passed,
        remaining_issues=[i.issue_id for i in remaining],
    )

    run_dir = settings.outputs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    changes_list = state.get("changes", [])
    exporter.save_cleaned(cleaned, run_dir, original=state.get("dataset"), input_paths=state.get("input_paths"))
    exporter.save_highlighted(cleaned, issues_orig, changes_list, run_dir)
    exporter.save_changelog(changes_list, run_dir)
    exporter.save_issues(issues_orig, run_dir)
    exporter.save_report(report, run_dir)
    exporter.save_goal(state.get("goal", ""), run_dir)
    exporter.save_diagnosis_report(issues_orig, run_dir)
    exporter.save_diagnosis_xlsx(issues_orig, run_dir)
    from agent.aggregate_store import save_aggregate_results
    save_aggregate_results(state.get("agg_results", []), state.get("agg_dispositions"), run_dir)

    req_dir = run_dir / "requests"
    req_dir.mkdir(exist_ok=True)
    import json
    for req in requests:
        (req_dir / f"{req.org}.json").write_text(
            json.dumps(req.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    from notify.mailer import save_drafts
    requests = save_drafts(requests, run_dir)

    loop_count = state.get("loop_count", 0) + 1
    logger.info("재검진 완료 — 잔여 오류: %d건, passed=%s, loop=%d", len(remaining), passed, loop_count)
    return {**state, "requests": requests, "report": report,
            "remaining_issue_list": remaining, "loop_count": loop_count}
