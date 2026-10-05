"""R5~R7 집계 규칙 — RuleSpec 검증, 사업 기준표 로더, 3종 알고리즘."""
from __future__ import annotations

import csv
import logging
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import pandas as pd

from engine.models import CellRef, Dataset, GroupIssue, Period, RuleSpec, Template, Tier
from engine.rules.amount import normalize_amount
from engine.rules.base import RuleContext

logger = logging.getLogger(__name__)

STANDARD_COLUMNS: frozenset[str] = frozenset({
    "기관명", "사업코드", "사업명", "수혜기업명",
    "사업자번호", "지원일자", "지원금액",
})

_TEMPLATE_DEFAULT_TIER: dict[str, Tier] = {
    "MAX_COUNT": "approval",
    "NEAR_DUPLICATE": "approval",
    "SUM_LIMIT": "request",
}

_SPEC_COUNTER: list[int] = [0]


def _next_spec_id() -> str:
    _SPEC_COUNTER[0] += 1
    return f"RS-{_SPEC_COUNTER[0]:04d}"


def validate_spec(spec: RuleSpec) -> list[str]:
    """RuleSpec 검증. 실패 항목 목록 반환 (빈 리스트면 유효)."""
    errors: list[str] = []

    for col in spec.group_by:
        if col not in STANDARD_COLUMNS:
            errors.append(f"group_by '{col}'은 표준 컬럼이 아닙니다")

    p = spec.params

    if spec.template == "MAX_COUNT":
        if "max" in p:
            try:
                if int(p["max"]) < 1:
                    errors.append("max는 1 이상이어야 합니다")
            except (ValueError, TypeError):
                errors.append("max는 정수여야 합니다")
        cd = p.get("count_distinct", "사업명")
        if cd not in ("사업명", "행"):
            errors.append(f"count_distinct '{cd}'은 '사업명' 또는 '행'이어야 합니다")

    elif spec.template == "NEAR_DUPLICATE":
        if "amount_tolerance_pct" in p:
            try:
                v = float(p["amount_tolerance_pct"])
                if not 0 <= v <= 20:
                    errors.append("amount_tolerance_pct는 0~20 범위여야 합니다")
            except (ValueError, TypeError):
                errors.append("amount_tolerance_pct는 숫자여야 합니다")
        if "date_window_days" in p:
            try:
                v = int(p["date_window_days"])
                if not 0 <= v <= 365:
                    errors.append("date_window_days는 0~365 범위여야 합니다")
            except (ValueError, TypeError):
                errors.append("date_window_days는 정수여야 합니다")

    elif spec.template == "SUM_LIMIT":
        if p.get("max_amount") is None:
            errors.append("한도 금액을 숫자로 알려주세요 (예: 6천만원)")
        else:
            try:
                v = int(p["max_amount"])
                if v < 0:
                    errors.append("max_amount는 0 이상이어야 합니다")
            except (ValueError, TypeError):
                errors.append("max_amount는 정수여야 합니다")
        scope = p.get("scope", "per_business")
        if scope not in ("per_business", "total", "per_payment"):
            errors.append(f"scope '{scope}'은 'per_business', 'total', 'per_payment' 중 하나여야 합니다")

    return errors


def load_business_master(path: Path | str) -> list[RuleSpec]:
    """사업 기준표 CSV를 RuleSpec 목록으로 변환."""
    path = Path(path)
    if not path.exists():
        logger.warning("사업 기준표 없음: %s", path)
        return []

    specs: list[RuleSpec] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, 1):
            biz_name = row.get("사업명", "").strip()
            if not biz_name:
                continue

            annual = row.get("연간허용횟수", "").strip()
            if annual and annual.isdigit() and int(annual) >= 1:
                specs.append(RuleSpec(
                    spec_id=f"RS-{i:04d}A",
                    template="MAX_COUNT",
                    description=f"[{biz_name}] 연간 {annual}회 초과 수혜",
                    group_by=["사업명", "사업자번호"],
                    params={"count_distinct": "행", "max": annual},
                    period="year",
                    filter_business=biz_name,
                    tier="request",
                    source="business_master",
                ))

            no_dup = row.get("시군내중복허용", "").strip().upper() == "N"
            if no_dup:
                specs.append(RuleSpec(
                    spec_id=f"RS-{i:04d}B",
                    template="MAX_COUNT",
                    description=f"[{biz_name}] 시군 내 연 1회 초과 수혜",
                    group_by=["기관명", "사업자번호"],
                    params={"count_distinct": "행", "max": "1"},
                    period="year",
                    filter_business=biz_name,
                    tier="request",
                    source="business_master",
                ))

            limit = row.get("기업당한도", "").strip()
            if limit and limit.isdigit():
                specs.append(RuleSpec(
                    spec_id=f"RS-{i:04d}C",
                    template="SUM_LIMIT",
                    description=f"[{biz_name}] 기업당 한도 {int(limit):,}원 초과",
                    group_by=["사업자번호"],
                    params={"sum_column": "지원금액", "max_amount": limit, "scope": "per_business"},
                    period="year",
                    filter_business=biz_name,
                    tier="request",
                    source="business_master",
                ))

    logger.info("사업 기준표 RuleSpec %d건 생성", len(specs))
    return specs


def _build_merged_df(ds: Dataset, sheet: str = "실적") -> pd.DataFrame:
    """Dataset의 지정 시트 전체를 파일명(_file 컬럼 추가) 포함 하나의 DF로 합친다."""
    frames: list[pd.DataFrame] = []
    for (file, s), df in ds.tables.items():
        if s == sheet:
            tmp = df.copy()
            tmp["_file"] = file
            frames.append(tmp)
    if not frames:
        return pd.DataFrame()
    merged = pd.concat(frames, ignore_index=True)
    if "사업자번호" in merged.columns:
        merged["사업자번호"] = merged["사업자번호"].map(_group_bizno)
    return merged


def _group_bizno(value: object) -> object:
    """집계 묶음 키용 사업자번호 — 하이픈 유무가 달라도 같은 기업으로 묶는다 (읽기 전용 복사본에서만 적용)."""
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if len(digits) == 10 else value


def _filter_by_business(df: pd.DataFrame, filter_business: str | list[str] | None) -> pd.DataFrame:
    """사업명으로 한정한다 — 하나(문자열)이거나 여러 개(목록). 없으면 그대로."""
    if not filter_business or "사업명" not in df.columns:
        return df
    names = [filter_business] if isinstance(filter_business, str) else list(filter_business)
    return df[df["사업명"].isin(names)].copy()


def _add_year_col(df: pd.DataFrame, date_col: str = "지원일자") -> pd.DataFrame:
    """지원일자에서 연도 컬럼(_year)을 추가한 복사본을 반환."""
    df = df.copy()
    if date_col in df.columns:
        df["_year"] = df[date_col].astype(str).str.extract(r"(?<!\d)(\d{4})(?:\d{4})?(?!\d)")[0]
    else:
        df["_year"] = pd.NA
    return df


def _effective_group(spec: RuleSpec, df: pd.DataFrame) -> list[str]:
    """period=year이면 _year를 grouping key에 추가한 유효 컬럼 목록 반환."""
    cols = [c for c in spec.group_by if c in df.columns]
    if spec.period == "year" and "_year" in df.columns:
        cols = cols + ["_year"]
    return cols


def _parse_amount(v: object) -> float | None:
    """집계용 금액 해석 (읽기 전용 — 셀은 바꾸지 않는다)."""
    text = str(v).strip()
    if not text or text.lower() in ("nan", "none"):
        return None
    res = normalize_amount(text)
    return float(res.value) if res.value is not None else None


def _group_key_dict(cols: list[str], vals: tuple) -> dict[str, str]:
    """그룹 키 dict 생성 — 내부 컬럼(_year 등)은 제외."""
    return {
        col: str(val)
        for col, val in zip(cols, vals)
        if not col.startswith("_")
    }


def _to_cell_refs(group_df: pd.DataFrame, sheet: str, col: str) -> list[CellRef]:
    refs = []
    for _, row in group_df.iterrows():
        if "_row" in row.index and "_file" in row.index:
            refs.append(CellRef(
                file=str(row["_file"]),
                sheet=sheet,
                row=int(row["_row"]),
                column=col,
            ))
    return refs


def _to_tuple(v) -> tuple:
    return v if isinstance(v, tuple) else (v,)


def run_max_count(ds: Dataset, spec: RuleSpec, ctx: RuleContext) -> list[GroupIssue]:
    """R5: 허용 횟수 초과 수혜 탐지."""
    if spec.template != "MAX_COUNT":
        return []

    df = _build_merged_df(ds)
    if df.empty:
        return []

    if spec.filter_business and "사업명" in df.columns:
        df = _filter_by_business(df, spec.filter_business)
        if df.empty:
            return []

    df = _add_year_col(df)
    gcols = _effective_group(spec, df)
    if not gcols:
        return []

    p = spec.params
    max_count = int(p.get("max", 1))
    count_distinct = p.get("count_distinct", "사업명")

    issues: list[GroupIssue] = []

    for group_vals, gdf in df.groupby(gcols, dropna=False):
        group_vals = _to_tuple(group_vals)

        if count_distinct == "행":
            count = len(gdf)
        elif count_distinct in gdf.columns:
            count = gdf[count_distinct].nunique()
        else:
            continue

        if count <= max_count:
            continue

        unit = "사업" if count_distinct == "사업명" else "건"
        metric = f"{unit} {count}건 (허용 {max_count}건)"
        group_key = _group_key_dict(gcols, group_vals)
        rows = _to_cell_refs(gdf, "실적", "사업자번호")

        issues.append(GroupIssue(
            issue_id=ctx.next_issue_id(),
            ref=None,
            rule_id="MAX_COUNT",
            kind="rule",
            value=None,
            message=f"허용 횟수({max_count}건) 초과 수혜 의심 — {metric}. 사업별 중복 제한 규정 확인 필요",
            tier=spec.tier,
            group_key=group_key,
            rows=rows,
            metric=metric,
            spec_id=spec.spec_id,
        ))

    return issues


def _connected_components(n: int, edges: list[tuple[int, int]]) -> list[list[int]]:
    """연결된 컴포넌트 탐색 (크기 ≥ 2만 반환)."""
    adj: dict[int, list[int]] = defaultdict(list)
    for u, v in edges:
        adj[u].append(v)
        adj[v].append(u)

    visited: set[int] = set()
    components: list[list[int]] = []
    for start in range(n):
        if start in visited:
            continue
        component: list[int] = []
        queue = [start]
        while queue:
            node = queue.pop()
            if node in visited:
                continue
            visited.add(node)
            component.append(node)
            queue.extend(adj[node])
        if len(component) >= 2:
            components.append(component)
    return components


def run_near_duplicate(ds: Dataset, spec: RuleSpec, ctx: RuleContext) -> list[GroupIssue]:
    """R6: 이중 지급 의심 탐지."""
    if spec.template != "NEAR_DUPLICATE":
        return []

    df = _build_merged_df(ds)
    if df.empty:
        return []

    if spec.filter_business and "사업명" in df.columns:
        df = _filter_by_business(df, spec.filter_business)
        if df.empty:
            return []

    group_by = [c for c in (spec.group_by or ["사업자번호", "사업명"]) if c in df.columns]
    if not group_by:
        return []

    p = spec.params
    tolerance_pct = float(p.get("amount_tolerance_pct", 0))
    window_days = int(p.get("date_window_days", 30))

    has_date = "지원일자" in df.columns
    has_amount = "지원금액" in df.columns

    issues: list[GroupIssue] = []

    for group_vals, gdf in df.groupby(group_by, dropna=False):
        if len(gdf) < 2:
            continue

        rows_list = list(gdf.iterrows())
        n = len(rows_list)

        dates: list[pd.Timestamp | None] = []
        if has_date:
            for _, r in rows_list:
                try:
                    dates.append(pd.to_datetime(str(r["지원일자"]), errors="coerce"))
                except Exception:
                    dates.append(None)
        else:
            dates = [None] * n

        amounts: list[float | None] = []
        if has_amount:
            for _, r in rows_list:
                amounts.append(_parse_amount(r["지원금액"]))
        else:
            amounts = [None] * n

        edges: list[tuple[int, int]] = []
        for i, j in combinations(range(n), 2):
            if has_date and dates[i] is not None and dates[j] is not None:
                if abs((dates[i] - dates[j]).days) > window_days:
                    continue

            if has_amount and amounts[i] is not None and amounts[j] is not None:
                ai, aj = amounts[i], amounts[j]
                if ai == 0 and aj == 0:
                    pass
                elif ai == 0 or aj == 0:
                    if tolerance_pct == 0:
                        pass
                        if ai != aj:
                            continue
                    else:
                        continue
                else:
                    diff_pct = abs(ai - aj) / max(abs(ai), abs(aj)) * 100
                    if diff_pct > tolerance_pct:
                        continue

            edges.append((i, j))

        if not edges:
            continue

        components = _connected_components(n, edges)
        for component in components:
            comp_df = gdf.iloc[sorted(component)]
            group_vals_t = _to_tuple(group_vals)
            group_key = _group_key_dict(group_by, group_vals_t)
            rows = _to_cell_refs(comp_df, "실적", "사업자번호")

            files = {str(r["_file"]) for _, r in comp_df.iterrows() if "_file" in r.index}
            std_cols = [c for c in ["기관명", "사업명", "수혜기업명", "사업자번호", "지원일자", "지원금액"]
                        if c in comp_df.columns]
            if len(files) > 1:
                sub_msg = "여러 시군에서 같은 건 보고 의심"
            elif std_cols and comp_df[std_cols].duplicated(keep=False).all():
                sub_msg = "동일 행 중복 입력 의심 — 모든 칸이 같은 행입니다. 중복 입력인지 이중 지급인지 기관 확인이 필요합니다"
            else:
                sub_msg = "이중 지급 의심 — 금액이 같고 날짜가 가까운 건입니다. 정당한 2회 지급인지 기관 확인이 필요합니다"

            metric = f"{len(component)}건 중복 의심"
            issues.append(GroupIssue(
                issue_id=ctx.next_issue_id(),
                ref=None,
                rule_id="NEAR_DUPLICATE",
                kind="rule",
                value=None,
                message=sub_msg,
                tier=spec.tier,
                group_key=group_key,
                rows=rows,
                metric=metric,
                spec_id=spec.spec_id,
            ))

    return issues


def run_sum_limit(ds: Dataset, spec: RuleSpec, ctx: RuleContext) -> list[GroupIssue]:
    """R7: 지원금 합계 한도 초과 탐지."""
    if spec.template != "SUM_LIMIT":
        return []

    df = _build_merged_df(ds)
    p = spec.params
    sum_col = p.get("sum_column", "지원금액")

    if df.empty or sum_col not in df.columns:
        return []

    if spec.filter_business and "사업명" in df.columns:
        df = _filter_by_business(df, spec.filter_business)
        if df.empty:
            return []

    max_amount = p.get("max_amount")
    if max_amount is None:
        return []
    try:
        max_amount_int = int(max_amount)
    except (ValueError, TypeError):
        logger.warning("max_amount 파싱 실패: %s", max_amount)
        return []

    scope = p.get("scope", "per_business")
    df = _add_year_col(df)
    df = df.copy()
    df["_amount_num"] = df[sum_col].map(_parse_amount)
    unreadable = df[df["_amount_num"].isna() & df[sum_col].astype(str).str.strip().ne("")]
    if not unreadable.empty:
        logger.warning("한도 검사에서 금액을 해석하지 못해 제외한 행: %s", sorted(unreadable["_row"].tolist()))

    if scope == "per_payment":
        return _per_payment_issues(df, sum_col, max_amount_int, spec, ctx)

    if scope == "per_business":
        gcols = list(spec.group_by)
        if "사업명" not in gcols and "사업명" in df.columns:
            gcols.append("사업명")
    else:
        gcols = list(spec.group_by)

    if spec.period == "year" and "_year" in df.columns:
        gcols = gcols + ["_year"]

    gcols = [c for c in gcols if c in df.columns]
    if not gcols:
        return []

    issues: list[GroupIssue] = []

    for group_vals, gdf in df.groupby(gcols, dropna=False):
        total = gdf["_amount_num"].sum()
        if pd.isna(total) or total <= max_amount_int:
            continue

        group_vals_t = _to_tuple(group_vals)
        group_key = _group_key_dict(gcols, group_vals_t)
        rows = _to_cell_refs(gdf, "실적", sum_col)

        excess = int(total) - max_amount_int
        metric = f"합계 {int(total):,}원 (한도 {max_amount_int:,}원, 초과 {excess:,}원)"
        scope_label = "" if scope == "per_business" else " (합산)"
        issues.append(GroupIssue(
            issue_id=ctx.next_issue_id(),
            ref=None,
            rule_id="SUM_LIMIT",
            kind="rule",
            value=None,
            message=f"지원금 합계 한도 초과{scope_label} — {metric}",
            tier=spec.tier,
            group_key=group_key,
            rows=rows,
            metric=metric,
            spec_id=spec.spec_id,
        ))

    return issues


def _per_payment_issues(df: pd.DataFrame, sum_col: str, max_amount: int, spec: RuleSpec,
                        ctx: RuleContext) -> list[GroupIssue]:
    """건별 한도 — 지원금 한 건(한 행)이 한도를 넘는 경우. 합계가 아니라 행마다 본다."""
    issues: list[GroupIssue] = []
    for idx, row in df.iterrows():
        amount = row["_amount_num"]
        if pd.isna(amount) or amount <= max_amount:
            continue
        key_cols = [c for c in ("사업자번호", "사업명", "지원일자") if c in df.columns]
        group_key = {c: str(row[c]) for c in key_cols}
        excess = int(amount) - max_amount
        metric = f"1건 {int(amount):,}원 (한도 {max_amount:,}원, 초과 {excess:,}원)"
        issues.append(GroupIssue(
            issue_id=ctx.next_issue_id(), ref=None, rule_id="SUM_LIMIT", kind="rule", value=None,
            message=f"지원금 건별 한도 초과 — {metric}", tier=spec.tier, group_key=group_key,
            rows=_to_cell_refs(df.loc[[idx]], "실적", sum_col), metric=metric, spec_id=spec.spec_id,
        ))
    return issues


def count_excluded_rows(ds: Dataset, sheet: str = "실적") -> int:
    """R1 빈 값 행 + R4 형식 오류 행처럼 집계에서 제외된 행 수를 반환한다."""
    df = _build_merged_df(ds, sheet)
    if df.empty:
        return 0
    if "사업자번호" not in df.columns:
        return 0
    invalid = df["사업자번호"].astype(str).str.replace(r"[^0-9]", "", regex=True).str.len() != 10
    return int(invalid.sum())
