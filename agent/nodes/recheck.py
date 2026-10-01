from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

from config import settings
from engine import exporter, validator
from engine.planning import PlanScope, active_scope
from engine.models import RequestItem, RunReport
from agent.state import GraphState

logger = logging.getLogger(__name__)


def recheck_node(state: GraphState) -> GraphState:
    """파이프라인 6단계(마지막): 수정 후 재검진 → 보완 요청 생성 → 결과 파일 저장.

    처리 순서:
      1. 수정된 Dataset(cleaned)에 validator를 다시 실행해 잔여 오류 확인
      2. 잔여 오류 중 request 티어를 모아 기관별로 보완 요청 메일 생성 (LLM)
      3. 통계 집계 → RunReport 생성
      4. outputs/<run_id>/ 하위에 cleaned xlsx, changelog, issues, report 저장
      5. notify.mailer로 보완 요청 메일 발송 (dry-run 포함)
    """
    cleaned = state.get("cleaned") or state["dataset"]
    bindings = state["bindings"]
    run_id = state["run_id"]
    issues_orig = state["issues"]

    # 수정 후 재검진 — 잔여 오류가 0건이면 통과
    scope = PlanScope.from_plan(state.get("plan"))
    with active_scope(scope):
        remaining = validator.run(
            cleaned, bindings, allowed_rule_ids=scope.rule_ids if scope else None,
        )
    if scope is not None:
        remaining = scope.filter_issues(remaining)
    passed = len(remaining) == 0

    # 잔여 오류 중 request 티어 → 기관별로 보완 요청 생성
    from engine.masking import mask_issues
    request_issues = mask_issues([i for i in remaining if i.tier == "request"])   # 메일·LLM 프롬프트용
    org_issues: dict[str, list] = defaultdict(list)
    for i in request_issues:
        org = i.ref.file if i.ref else "unknown"
        org_issues[org].append(i)

    requests: list[RequestItem] = []
    for org, org_i in org_issues.items():
        mail_body = ""
        try:
            from llm.client import complete_json
            from pydantic import BaseModel

            class MailResp(BaseModel):
                subject: str
                body: str

            issue_list = "\n".join(
                f"- [{i.issue_id}] {i.rule_id}: {i.message}" for i in org_i[:10]
            )
            resp = complete_json(
                "request_mail",
                {"org_name": org, "issues": issue_list},
                MailResp,
            )
            requests.append(RequestItem(
                org=org, issues=[i.issue_id for i in org_i],
                subject=resp.subject, body=resp.body,
            ))
        except Exception:
            requests.append(RequestItem(
                org=org, issues=[i.issue_id for i in org_i],
                subject=f"[데이터 보완 요청] {org}",
                body="\n".join(f"- {i.message}" for i in org_i),
            ))

    # 통계
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

    # 파일 저장
    run_dir = settings.outputs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    changes_list = state.get("changes", [])
    exporter.save_cleaned(cleaned, run_dir)
    exporter.save_highlighted(cleaned, issues_orig, changes_list, run_dir)
    exporter.save_changelog(changes_list, run_dir)
    exporter.save_issues(issues_orig, run_dir)
    exporter.save_report(report, run_dir)
    exporter.save_diagnosis_report(issues_orig, run_dir)
    exporter.save_diagnosis_xlsx(issues_orig, run_dir)

    # 보완 요청 저장
    req_dir = run_dir / "requests"
    req_dir.mkdir(exist_ok=True)
    import json
    for req in requests:
        (req_dir / f"{req.org}.json").write_text(
            json.dumps(req.model_dump(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # 메일 발송 (dry-run 포함)
    from notify.mailer import send_all
    requests = send_all(requests, run_dir)

    loop_count = state.get("loop_count", 0) + 1
    logger.info("재검진 완료 — 잔여 오류: %d건, passed=%s, loop=%d", len(remaining), passed, loop_count)
    return {**state, "requests": requests, "report": report,
            "remaining_issue_list": remaining, "loop_count": loop_count}
