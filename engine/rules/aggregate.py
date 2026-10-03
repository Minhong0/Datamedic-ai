"""R5~R7 집계 규칙 — RuleSpec 검증, 사업 기준표 로더, 3종 알고리즘.

R1~R4 정규화가 끝난 Dataset에 실행한다.
금액은 정수, 날짜는 ISO(YYYY-MM-DD), 사업자번호는 숫자 10자리 전제.
"""
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

# 표준 컬럼명 집합 — group_by 검증에 사용
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


# ── 검증 ──────────────────────────────────────────────────────────────────────

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
            errors.append("한도 금액을 숫자로 알려주세요 (예: 6천만원)")   # 비면 run_sum_limit이 조용히 0건을 낸다
        else:
            try:
                v = int(p["max_amount"])
                if v < 0:
                    errors.append("max_amount는 0 이상이어야 합니다")
            except (ValueError, TypeError):
                errors.append("max_amount는 정수여야 합니다")
        scope = p.get("scope", "per_business")
        if scope not in ("per_business", "total"):
            errors.append(f"scope '{scope}'은 'per_business' 또는 'total'이어야 합니다")

    return errors


# ── 기준표 로더 ───────────────────────────────────────────────────────────────

def load_business_master(path: Path | str) -> list[RuleSpec]:
    """사업 기준표 CSV를 RuleSpec 목록으로 변환.

    각 행에서 R5(MAX_COUNT) 2종 + R7(SUM_LIMIT) 1종을 생성한다.
    """
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

            # R5-A: 같은 사업 연간 허용 횟수 (사업명+사업자번호, 행 기준)
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
                    tier="approval",
                    source="business_master",
                ))

            # R5-B: 시군 내 중복 불가 — 해당 사업을 같은 기관+기업이 연 1회 초과 수혜
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
                    tier="approval",
                    source="business_master",
                ))

            # R7: 기업당 한도 초과
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


# ── 공통 헬퍼 ─────────────────────────────────────────────────────────────────

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
    """집계 묶음 키용 사업자번호 — 하이픈 유무가 달라도 같은 기업으로 묶는다 (읽기 전용 복사본에서만 적용).

    숫자 10자리면 'XXX-XX-XXXXX'로 맞추고, 그 외(자릿수 오류·빈 값)는 그대로 둔다.
    """
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}" if len(digits) == 10 else value


def _add_year_col(df: pd.DataFrame, date_col: str = "지원일자") -> pd.DataFrame:
    """지원일자에서 연도 컬럼(_year)을 추가한 복사본을 반환.

    '2026-04-05'뿐 아니라 정규화 전 표기('4/5/2026', '20260405')에서도 4자리 연도를 읽는다 — 앞 4글자를 그대로 쓰면
    정규화되지 않은 날짜가 다른 연도 묶음으로 갈려 같은 기업의 횟수·합계 초과가 조용히 누락된다.
    """
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
    """집계용 금액 해석 (읽기 전용 — 셀은 바꾸지 않는다).

    '7402만원', '165,020,000원'처럼 정규화 전 표기도 R2 normalize_amount로 읽는다.
    해석할 수 없으면 None.
    """
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


# ── R5: MAX_COUNT ─────────────────────────────────────────────────────────────

def run_max_count(ds: Dataset, spec: RuleSpec, ctx: RuleContext) -> list[GroupIssue]:
    """R5: 허용 횟수 초과 수혜 탐지."""
    if spec.template != "MAX_COUNT":
        return []

    df = _build_merged_df(ds)
    if df.empty:
        return []

    # 사업 필터
    if spec.filter_business and "사업명" in df.columns:
        df = df[df["사업명"] == spec.filter_business].copy()
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


# ── R6: NEAR_DUPLICATE ────────────────────────────────────────────────────────

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
        df = df[df["사업명"] == spec.filter_business].copy()
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

        # 날짜를 datetime으로 파싱 (오류 무시)
        dates: list[pd.Timestamp | None] = []
        if has_date:
            for _, r in rows_list:
                try:
                    dates.append(pd.to_datetime(str(r["지원일자"]), errors="coerce"))
                except Exception:
                    dates.append(None)
        else:
            dates = [None] * n

        # 금액을 float으로 파싱
        amounts: list[float | None] = []
        if has_amount:
            for _, r in rows_list:
                amounts.append(_parse_amount(r["지원금액"]))
        else:
            amounts = [None] * n

        # 조건을 만족하는 쌍 탐색
        edges: list[tuple[int, int]] = []
        for i, j in combinations(range(n), 2):
            # 날짜 차이 검사
            if has_date and dates[i] is not None and dates[j] is not None:
                if abs((dates[i] - dates[j]).days) > window_days:
                    continue

            # 금액 차이 검사
            if has_amount and amounts[i] is not None and amounts[j] is not None:
                ai, aj = amounts[i], amounts[j]
                if ai == 0 and aj == 0:
                    pass  # 둘 다 0이면 조건 충족
                elif ai == 0 or aj == 0:
                    if tolerance_pct == 0:
                        pass  # 완전 일치 모드: 0 vs 0만 허용, 0 vs 비0은 차이 100%
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

            # 세부 분류
            files = {str(r["_file"]) for _, r in comp_df.iterrows() if "_file" in r.index}
            std_cols = [c for c in ["기관명", "사업명", "수혜기업명", "사업자번호", "지원일자", "지원금액"]
                        if c in comp_df.columns]
            if len(files) > 1:
                sub_msg = "여러 시군에서 같은 건 보고 의심"
            elif std_cols and comp_df[std_cols].duplicated(keep=False).all():
                sub_msg = "동일 행 중복 입력 의심"
            else:
                sub_msg = "이중 지급 의심"

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


# ── R7: SUM_LIMIT ─────────────────────────────────────────────────────────────

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
        df = df[df["사업명"] == spec.filter_business].copy()
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
    df["_amount_num"] = df[sum_col].map(_parse_amount)   # 정규화 전 표기('7402만원' 등)도 합산한다
    unreadable = df[df["_amount_num"].isna() & df[sum_col].astype(str).str.strip().ne("")]
    if not unreadable.empty:
        logger.warning("한도 검사에서 금액을 해석하지 못해 제외한 행: %s", sorted(unreadable["_row"].tolist()))

    if scope == "per_business":
        # group_by에 사업명이 없으면 추가 (사업별 합계)
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


# ── 진단 제외 행 집계 ──────────────────────────────────────────────────────────

def count_excluded_rows(ds: Dataset, sheet: str = "실적") -> int:
    """R1 빈 값 행 + R4 형식 오류 행처럼 집계에서 제외된 행 수를 반환한다."""
    df = _build_merged_df(ds, sheet)
    if df.empty:
        return 0
    # 사업자번호가 10자리 숫자가 아닌 행은 집계 제외 (R4 기준)
    if "사업자번호" not in df.columns:
        return 0
    invalid = df["사업자번호"].astype(str).str.replace(r"[^0-9]", "", regex=True).str.len() != 10
    return int(invalid.sum())
