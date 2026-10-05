"""R4. BIZNO_VERIFY — 사업자번호 형식·체크디지트·국세청 상태조회."""
from __future__ import annotations

import re
from datetime import date

from engine.models import CellRef, Dataset, Issue, RuleBinding
from engine.rules.base import NormResult, NotFixable, Rule, RuleContext, register
from engine.rules.blank import is_blank

_WEIGHTS = [1, 3, 7, 1, 3, 7, 1, 3, 5]


def extract_digits(bizno: str) -> str:
    return re.sub(r"\D", "", bizno)


def bizno_checksum_ok(d: str) -> bool:
    """10자리 숫자 문자열의 체크디지트 검증."""
    if len(d) != 10 or not d.isdigit():
        return False
    s = sum(int(d[i]) * _WEIGHTS[i] for i in range(9))
    s += (int(d[8]) * 5) // 10
    return (10 - s % 10) % 10 == int(d[9])


def normalize_bizno(raw: str) -> NormResult:
    """형식 정규화만. 체크디지트·API 는 Rule.check 에서 처리."""
    digits = extract_digits(raw)
    if len(digits) != 10:
        return NormResult(None, True,
                          f"숫자 10자리가 아닙니다 (추출된 숫자: {len(digits)}자리)")
    formatted = f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if raw.strip() == formatted:
        return NormResult(formatted, True, "")
    return NormResult(formatted, True, "사업자번호 형식 통일 (XXX-XX-XXXXX)")


def _iter_column(ds: Dataset, target: str):
    parts = target.split(".")
    if len(parts) != 2:
        return
    sheet, column = parts
    for (file, s), df in ds.tables.items():
        if s == sheet and column in df.columns:
            yield file, sheet, df, column


def _parse_end_dt(end_dt: str) -> date | None:
    """'YYYYMMDD' 문자열 → date. 실패 시 None."""
    try:
        return date(int(end_dt[:4]), int(end_dt[4:6]), int(end_dt[6:8]))
    except (ValueError, IndexError):
        return None


NTS_OUTAGE_MESSAGE = ("국세청 조회에 실패해 사업자번호의 휴·폐업 상태는 확인하지 못했고 형식·체크디지트만 검증했습니다 "
                      "(국세청 서버 오류 또는 네트워크 문제). 잠시 후 다시 실행해 주세요.")

@register
class BiznoVerifyRule(Rule):
    """R4: 사업자번호 형식 → 체크디지트 → 국세청 상태조회."""

    rule_id = "BIZNO_VERIFY"
    default_tier = "approval"
    scope = "cell"

    def check(self, ds: Dataset, binding: RuleBinding, ctx: RuleContext) -> list[Issue]:
        issues: list[Issue] = []

        date_col: str | None = binding.params.get("date_col")

        provider = binding.params.get("_provider")
        if provider is None and binding.params.get("use_nts", True):
            try:
                from engine.tools.nts_client import get_provider
                provider = get_provider()
            except Exception:
                provider = None
        nts_available = provider is not None and not getattr(provider, "disabled", False)

        for target in binding.targets:
            for file, sheet, df, column in _iter_column(ds, target):
                valid_digits: dict[int, str] = {}
                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if is_blank(val):
                        continue
                    digits = extract_digits(val)
                    if len(digits) == 10 and bizno_checksum_ok(digits):
                        valid_digits[int(row["_row"])] = digits

                nts_results: dict[str, object] = {}
                if nts_available and provider and valid_digits:
                    unique = list(set(valid_digits.values()))
                    try:
                        nts_results = provider.query(unique)
                    except Exception:
                        pass
                nts_failed = bool(nts_available and provider and valid_digits and not nts_results)
                if nts_failed:
                    ctx.warn(NTS_OUTAGE_MESSAGE)

                for _, row in df.iterrows():
                    val = str(row[column]).strip()
                    if is_blank(val):
                        continue

                    ref = CellRef(file=file, sheet=sheet, row=int(row["_row"]), column=column)
                    digits = extract_digits(val)

                    norm = normalize_bizno(val)
                    if norm.value is None:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id="BIZNO_FORMAT", kind="rule", value=val,
                            message=f"사업자번호 형식이 올바르지 않습니다: '{val}' ({norm.reason})",
                            tier="request",
                        ))
                        continue

                    if norm.value != val.strip():
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id=self.rule_id, kind="rule", value=val,
                            message=f"사업자번호 표시 형식을 통일합니다: '{val}' → '{norm.value}'",
                            tier="auto",
                            suggestion=norm.value,
                        ))

                    if not bizno_checksum_ok(digits):
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id="BIZNO_CHECKSUM", kind="rule", value=val,
                            message=f"사업자번호 체크디지트가 맞지 않습니다: '{val}'",
                            tier="approval",
                        ))
                        continue

                    if not nts_available:
                        continue

                    rec = nts_results.get(digits)
                    if rec is None and nts_failed:
                        continue
                    if rec is None:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id="BIZNO_UNVERIFIED", kind="rule", value=val,
                            message=f"국세청 조회 실패 — 형식·체크디지트만 검증됨: '{val}'",
                            tier="approval",
                        ))
                        continue

                    stt: str = rec.b_stt

                    if stt == "계속사업자":
                        pass

                    elif stt == "국세청 미등록":
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id="BIZNO_NOT_FOUND", kind="rule", value=val,
                            message=f"국세청에 등록되지 않은 사업자번호: '{val}'",
                            tier="request",
                        ))

                    elif stt == "휴업자":
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id="BIZNO_SUSPENDED", kind="rule", value=val,
                            message=f"휴업 상태 사업자: '{val}'",
                            tier="approval",
                        ))

                    elif stt == "폐업자":
                        end_dt = rec.end_dt
                        close = _parse_end_dt(end_dt)
                        support_date = _get_support_date(df, row["_row"], date_col)

                        if close and support_date and close < support_date:
                            issues.append(Issue(
                                issue_id=ctx.next_issue_id(), ref=ref,
                                rule_id="BIZNO_CLOSED_BEFORE_SUPPORT", kind="rule", value=val,
                                message=(f"폐업 이후 지원 — 부적정 집행 의심: "
                                         f"'{val}' (폐업일 {close}, 지원일 {support_date})"),
                                tier="request",
                            ))
                        else:
                            close_str = close.isoformat() if close else "미상"
                            issues.append(Issue(
                                issue_id=ctx.next_issue_id(), ref=ref,
                                rule_id="BIZNO_CLOSED_AFTER_SUPPORT", kind="rule", value=val,
                                message=f"지원 이후 폐업 — 확인 필요: '{val}' (폐업일 {close_str})",
                                tier="approval",
                            ))

                    else:
                        issues.append(Issue(
                            issue_id=ctx.next_issue_id(), ref=ref,
                            rule_id="BIZNO_UNVERIFIED", kind="rule", value=val,
                            message=f"국세청 응답을 해석할 수 없습니다: '{val}' (상태: {stt})",
                            tier="approval",
                        ))

        return issues

    def fix(self, value: str | None, issue: Issue) -> str | None:
        if not value:
            raise NotFixable
        norm = normalize_bizno(value)
        if norm.value is None:
            raise NotFixable
        return norm.value


def _get_support_date(df, row_key: int, date_col: str | None) -> date | None:
    if not date_col:
        return None
    if date_col not in df.columns:
        return None
    rows = df[df["_row"] == row_key]
    if rows.empty:
        return None
    val = str(rows.iloc[0][date_col]).strip()
    try:
        return date.fromisoformat(val)
    except ValueError:
        return None
