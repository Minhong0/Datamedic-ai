"""사업자번호 마스킹 유틸리티."""
from __future__ import annotations

import re

_DIGITS_RE = re.compile(r"[^0-9]")


def mask_bizno(bizno: str) -> str:
    """사업자번호를 '123-45-*****' 형식으로 마스킹한다."""
    if not bizno:
        return bizno
    digits = _DIGITS_RE.sub("", str(bizno))
    if len(digits) != 10:
        return bizno
    return f"{digits[:3]}-{digits[3:5]}-*****"


_HYPHEN_BIZNO_RE = re.compile(r"(?<!\d)(\d{3})-(\d{2})-\d{4,6}(?!\d)")
BIZNO_COLUMN = "사업자번호"


def mask_text(text: str | None) -> str | None:
    """자유 텍스트 안의 하이픈 사업자번호(123-45-67890)를 가린다."""
    if not text:
        return text
    return _HYPHEN_BIZNO_RE.sub(lambda m: f"{m.group(1)}-{m.group(2)}-*****", text)


def _mask_cell_value(value: str | None) -> str | None:
    """사업자번호 컬럼의 셀 값 — 자릿수가 틀린 오입력(9자리 등)도 앞 5자리만 남기고 가린다."""
    if value is None:
        return value
    masked = mask_bizno(value)
    if masked != value:
        return masked
    digits = _DIGITS_RE.sub("", str(value))
    if len(digits) < 4:
        return mask_text(value)
    head = f"{digits[:3]}-{digits[3:5]}-" if len(digits) > 5 else ""
    return head + "*" * max(len(digits) - 5, 1) if head else "*" * len(digits)


def _is_bizno_issue(issue) -> bool:
    return issue.rule_id.startswith("BIZNO") or (
        issue.ref is not None and issue.ref.column == BIZNO_COLUMN
    )


def mask_issue(issue):
    """Issue 의 value·message·suggestion·explanation 에서 사업자번호를 가린 복사본."""
    raw = [v for v in (issue.value, issue.suggestion) if v]
    biz = _is_bizno_issue(issue)

    def fix(text: str | None) -> str | None:
        if not text:
            return text
        if biz:
            for r in raw:
                m = _mask_cell_value(r)
                if m != r:
                    text = text.replace(r, m)
        return mask_text(text)

    return issue.model_copy(update={
        "value": _mask_cell_value(issue.value) if biz else mask_text(issue.value),
        "suggestion": _mask_cell_value(issue.suggestion) if biz else mask_text(issue.suggestion),
        "message": fix(issue.message),
        "explanation": fix(issue.explanation),
    })


def mask_issues(issues: list) -> list:
    return [mask_issue(i) for i in issues]


def mask_change(change):
    """Change 의 before/after 에서 사업자번호를 가린 복사본 (사업자번호 컬럼만 전체 마스킹)."""
    if change.ref.column == BIZNO_COLUMN:
        return change.model_copy(update={
            "before": _mask_cell_value(change.before),
            "after": _mask_cell_value(change.after),
        })
    return change.model_copy(update={
        "before": mask_text(change.before), "after": mask_text(change.after),
    })


def mask_changes(changes: list) -> list:
    return [mask_change(c) for c in changes]


COMPANY_COLUMN = "수혜기업명"
_CORP_MARKS = ("주식회사", "유한회사", "(주)", "(유)", "㈜", "㈲")


def mask_company(name: str | None) -> str | None:
    """기업명을 가린다 — 법인 표기((주)·주식회사 등)는 두고 상호는 첫 글자만 남긴다."""
    if not name:
        return name
    text = str(name)
    marks = [m for m in _CORP_MARKS if m in text]
    core = text
    for m in marks:
        core = core.replace(m, "\0")
    parts = core.split("\0")
    out = []
    for p in parts:
        s = p.strip()
        if not s:
            out.append(p)
            continue
        lead = p[: len(p) - len(p.lstrip())]
        trail = p[len(p.rstrip()):]
        out.append(lead + s[0] + "○" * (len(s) - 1) + trail if len(s) > 1 else lead + "○" + trail)
    merged = out[0]
    for m, rest in zip(marks, out[1:]):
        merged += m + rest
    return merged


def mask_cell_value(column: str, value: str | None) -> str | None:
    """열 이름에 맞는 마스킹 — 사업자번호 열·기업명 열·그 밖의 자유 텍스트. 화면·메모·툴팁용."""
    if value is None:
        return value
    if column == BIZNO_COLUMN:
        return _mask_cell_value(value)
    if column == COMPANY_COLUMN:
        return mask_company(value)
    return mask_text(value)
