"""집계 위반(한도 초과·횟수 초과·이중 지급 의심) → 기관별 확인 요청 메일 초안."""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from engine.masking import mask_bizno
from engine.models import GroupIssue, RequestItem

logger = logging.getLogger(__name__)

MAIL_WORKERS = 5

REQUEST = "확인 요청"
OK = "문제 없음"

DISP_REQUEST = "request"
DISP_OK = "ok"


def label_to_disposition(label: str) -> str:
    return DISP_OK if label == OK else DISP_REQUEST


def disposition_to_label(disposition: str | None) -> str:
    return OK if disposition == DISP_OK else REQUEST


def ok_keys(dispositions: dict[str, str] | None) -> set[str]:
    """'문제 없음'으로 처리된 묶음 키 집합."""
    return {k for k, v in (dispositions or {}).items() if v == DISP_OK}

_RAW_BIZNO_RE = re.compile(r"(?<!\d)\d{3}-?\d{2}-?\d{5}(?!\d)")

_RULE_TEXT = {
    "SUM_LIMIT": "지원금 합계 한도 초과 의심",
    "MAX_COUNT": "허용 횟수 초과 수혜 의심",
    "NEAR_DUPLICATE": "이중 지급 의심",
}


def issue_key(capability: str, g: GroupIssue) -> str:
    """집계 결과 사이에서 겹치지 않는 묶음 식별자 (Issue ID 는 검사마다 E-0001부터 다시 시작한다)."""
    return f"{capability}:{g.issue_id}"


def collect_group_issues(agg_results: list[dict]) -> list[tuple[str, dict, GroupIssue]]:
    """(키, 집계 결과, 묶음) 목록 — 건너뛴 검사는 제외."""
    out = []
    for res in agg_results:
        if res.get("skipped"):
            continue
        for g in res.get("group_issues", []):
            out.append((issue_key(res["capability"], g), res, g))
    return out


def _group_text(g: GroupIssue) -> str:
    return " / ".join(f"{k}: {mask_bizno(v) if k == '사업자번호' else v}" for k, v in g.group_key.items())


def _rows_by_file(g: GroupIssue) -> dict[str, dict[str, list[int]]]:
    by: dict[str, dict[str, set[int]]] = defaultdict(lambda: defaultdict(set))
    for r in g.rows:
        by[Path(r.file).name][r.sheet].add(r.row)
    return {f: {s: sorted(rows) for s, rows in sheets.items()} for f, sheets in by.items()}


def _lines_for_file(items: list[tuple[str, GroupIssue]], file: str) -> list[str]:
    lines = []
    for n, (rule_id, g) in enumerate(items, 1):
        by = _rows_by_file(g)
        where = ", ".join(f"{s} {', '.join(str(r) for r in rows)}행" for s, rows in by[file].items())
        others = [f for f in by if f != file]
        line = (f"{n}. {_RULE_TEXT.get(rule_id, rule_id)} — {g.metric}\n"
                f"   - 대상: {_group_text(g)}\n   - 위치: {file} {where}")
        if others:
            line += f"\n   - 같은 건이 다른 파일에도 걸쳐 있음: {', '.join(others)}"
        lines.append(line)
    return lines


def _fallback_body(org: str, lines: list[str]) -> str:
    return (f"{org} 담당자님께,\n\n"
            "집계 점검 결과 아래 사항에 대한 확인을 요청드립니다. "
            "위반으로 확정된 것이 아니며, 사실관계 확인 및 소명을 부탁드립니다.\n\n"
            + "\n".join(lines) + "\n\n확인 후 회신 부탁드립니다.\n\n[담당자명]")


def build_aggregate_requests(agg_results: list[dict], ok_keys: set[str] | frozenset[str] = frozenset(),
                             *, use_llm: bool = True, recipient_for=None) -> list[RequestItem]:
    """`문제 없음`으로 표시되지 않은 위반 묶음을 파일(기관)별 확인 요청 메일 초안으로 만든다."""
    per_file: dict[str, list[tuple[str, GroupIssue]]] = defaultdict(list)
    keys: dict[str, list[str]] = defaultdict(list)
    for key, res, g in collect_group_issues(agg_results):
        if key in ok_keys:
            continue
        for file in _rows_by_file(g):
            per_file[file].append((g.rule_id, g))
            keys[file].append(key)

    def _draft(file: str, items: list) -> RequestItem:
        org = f"{file} (집계 확인)"
        lines = _lines_for_file(items, file)
        subject, body = f"[확인 요청] {file} — 집계 점검 {len(items)}건", _fallback_body(file, lines)
        if use_llm:
            try:
                from pydantic import BaseModel

                from llm.client import complete_json

                class MailResp(BaseModel):
                    subject: str
                    body: str

                resp = complete_json("request_mail_aggregate", {"org_name": file, "issues": "\n".join(lines)}, MailResp)
                if _RAW_BIZNO_RE.search(resp.body) or _RAW_BIZNO_RE.search(resp.subject):
                    logger.warning("LLM 메일에 마스킹되지 않은 사업자번호가 있어 기본 문안으로 대체")
                elif resp.subject.strip() and resp.body.strip():
                    subject, body = resp.subject.strip(), resp.body.strip()
            except Exception as e:
                logger.info("확인 요청 메일 LLM 작성 실패 → 기본 문안 사용: %s", e)
        return RequestItem(org=org, issues=keys[file], subject=subject, body=body, to=to_map[file])

    if not per_file:
        return []
    to_map = {f: (recipient_for(f) if recipient_for else None) for f in per_file}
    with ThreadPoolExecutor(max_workers=min(MAIL_WORKERS, len(per_file))) as pool:
        return list(pool.map(lambda kv: _draft(*kv), per_file.items()))
