"""담당자 결정의 버전 — 집계 결과가 최신 결정을 반영하고 있는지 판단하는 데 쓴다."""
from __future__ import annotations

import hashlib
from typing import Iterable

from engine.models import Decision


def decision_version(decisions: Iterable[Decision]) -> str:
    """담당자(operator)가 내린 결정 목록의 해시. 시스템 결정은 규칙으로 정해지므로 포함하지 않는다."""
    ops = sorted((d.issue_id, d.action, d.value or "") for d in decisions if d.decided_by == "operator")
    return hashlib.sha1(repr(ops).encode("utf-8")).hexdigest()[:12]


def aggregates_stale(aggregate_basis: str | None, decisions: Iterable[Decision], *, has_results: bool) -> bool:
    """집계를 실행한 뒤 담당자 결정이 바뀌었는가 (집계 결과가 있을 때만 의미가 있다)."""
    if not has_results or aggregate_basis is None:
        return False
    return aggregate_basis != decision_version(decisions)
