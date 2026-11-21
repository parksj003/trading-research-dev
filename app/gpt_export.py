"""Create an in-memory ChatGPT attachment from allowlisted market analysis only."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd


TRACK_MARKETS = {"KOSPI": "KR", "KOSDAQ": "KR", "NASDAQ100": "US", "SP500": "US"}
METRIC_DEFINITION_FIELDS = ("official_performance", "legacy_validation", "breadth_nasdaq100", "breadth_sp500",
                            "breadth_filters", "near_52w", "rs_4w", "leading", "actionable")
PRICE_OBSERVATION_FIELDS = ("benchmark_as_of", "clock_reference_date", "session_unconfirmed",
                            "candidate_rows", "lagging_candidate_rows", "lagging_actionable_rows")
REGIME_FIELDS = (
    "regime", "benchmark", "as_of", "signal_light", "distribution_days_25d",
    "below_sma200_now", "benchmark_close", "benchmark_sma20", "benchmark_sma50",
    "benchmark_sma200", "breadth_total", "breadth_valid_count", "breadth_coverage_pct",
    "breadth_valid", "breadth_status", "pct_above_50dma", "pct_above_200dma",
    "nh_nl_label", "tier_a_total",
)
STOCK_FIELDS = (
    "market", "sub_market", "ticker", "name", "as_of", "currency", "rank",
    "overall_rank", "macro_theme", "sector", "tier", "setup_stage", "status_tag",
    "reason", "selected", "is_actionable", "in_buy_zone", "is_breakout",
    "is_true_vcp", "strict_minervini", "is_leading_sector", "rs_blue_dot",
    "rs_line_new_high", "down_resilient", "rs_score", "rs_change_4w", "topdown_score",
    "sector_power", "last_close", "pivot_price", "base_pivot_price", "high_20d", "base_len_days",
    "dist_to_pivot_pct", "stop_price", "stop_dist_pct", "stop_warning",
    "is_wide_stop", "wide_stop_actionable", "volume_ratio", "vol_ratio_50d",
    "vol_dry_up_ratio", "range_10d_pct", "down_day_excess_pct", "distance_from_high_pct",
    "dist_sma20_pct", "dist_sma50_pct", "sma20", "sma50", "sma150", "sma200",
    "sma200_rising", "trend_template", "dist_days_25d", "exit_signal",
    "earnings_tag", "earnings_days", "peg", "eps_growth_pct", "eps_status",
    "is_stale_price", "stale_bars", "short_history",
    "position_size_pct", "trade_risk_pct", "portfolio_alloc_pct", "portfolio_risk_contrib_pct",
    "portfolio_alloc_note", "raw_stop_dist_pct", "atr14_pct",
)
SECTOR_FIELDS = (
    "market", "level", "macro_rank", "sub_rank", "macro_theme", "sector",
    "univ_total", "passed_cnt", "pass_rate_pct", "tier_a_cnt", "pct_above_50dma",
    "median_rs", "rs_delta_4w", "actionable_cnt", "new_cnt", "macro_power",
    "sector_power", "rotation_phase", "top_sub_sector", "top_leader",
    "leader_ticker", "leader_name", "leader_rs", "leader_setup", "leader_blue_dot",
)
HEALTH_FIELDS = (
    "kr_universe", "kr_processed", "kr_failed", "kr_unclassified", "kr_stale_tickers",
    "us_universe", "us_processed", "us_failed", "us_unclassified", "us_stale_tickers",
    "stale_cache", "as_of",
    "kr_close_check_date", "kr_price_status", "kr_price_delayed_tickers", "kr_price_unavailable_tickers",
    "us_cap_requested", "us_cap_fallback", "us_cap_missing", "us_fundamentals_missing",
)

from gpt_prompts import ANALYSIS_PROMPT, MODE_DETAILS, build_prompt



@dataclass(frozen=True)
class AnalysisExport:
    text: str
    filename: str
    counts: dict[str, int]
    warnings: tuple[str, ...]


RESEARCH_GROUP_FIELDS = (
    "window", "dimension", "horizon", "market", "track", "setup_stage", "sector", "entry_regime", "signal_name", "signal_value",
    "signals", "completed", "pending_or_excluded", "unique_tickers", "unique_entry_dates",
    "publication_checked_n", "excess_n", "win_rate_pct", "mean_return_pct", "median_return_pct",
    "mean_excess_pp", "excess_win_rate_pct", "evidence", "stop_r_n", "mean_stop_r",
    "stop_return_n", "mean_stop_return_pct", "stop_touch_n", "stop_touch_pct", "mae_n",
    "mean_mae_pct", "mfe_n", "mean_mfe_pct",
    "config_hash", "engine_commit", "known_config_n", "config_versions", "wins", "losses", "mean_win_pct",
    "mean_loss_pct", "payoff_ratio", "rs_score_n", "mean_rs_score", "rs_change_n", "mean_rs_change_4w",
    "sector_power_n", "mean_sector_power",
    "paired_hold_n", "mean_paired_hold_return_pct", "mean_stop_minus_hold_pp",
)
RESEARCH_QUALITY_FIELDS = (
    "outcome_rows", "duplicate_outcomes", "unmatched_signals", "entry_before_signal", "timing_unknown",
    "publication_checked", "generation_checked_publication_unknown", "waiting_entry", "invalid_identity",
    "legacy_calculation_rows", "open_unverified_rows", "stop_basis_unverified_rows", "stop_basis_changed_rows", "entry_eligible_rows",
)


def parse_research_report(raw: bytes | str, manifest: dict, markets: tuple[str, ...]) -> dict:
    """Optional supplemental contract: exact source version plus an explicit field allowlist."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if len(raw) > 4_000_000:
        raise ValueError("research report too large")
    data = json.loads(raw.split("````json\n", 1)[1].rsplit("\n````", 1)[0])
    source = data.get("source", {})
    if data.get("schema") != "watchlist-research-1.1" or not manifest.get("run_id") or source.get("run_id") != manifest["run_id"]:
        raise ValueError("research run_id mismatch")
    for name in ("summary.json", "watchlist.csv", "candidates.csv", "sectors.csv"):
        expected = manifest.get("files", {}).get(name, {}).get("sha256")
        if not expected or source.get("market_file_sha256", {}).get(name) != expected:
            raise ValueError("research source hash mismatch")
    validation = data.get("validation", {})
    status = validation.get("status")
    if status not in ("available", "history_unavailable", "history_update_incomplete"):
        raise ValueError("invalid research status")
    if source.get("history_update_ok") is not True:
        status = "history_update_incomplete"
    groups = [_project(row, RESEARCH_GROUP_FIELDS) for row in validation.get("groups", [])
              if isinstance(row, dict) and row.get("market") in markets and row.get("horizon") in (5, 10, 20)] if status == "available" else []
    changes = data.get("market_changes", {})
    if (changes.get("baseline") or {}).get("run_id") == manifest["run_id"]:
        changes = {"baseline": changes["baseline"], "regimes": [], "sectors": []}
    sector_fields = tuple(["market", "level", "macro_theme", "sector", "status"] + [prefix+key
        for key in ("sector_power", "median_rs", "passed_cnt", "univ_total") for prefix in ("previous_", "current_")]
        + [key+"_change" for key in ("sector_power", "median_rs", "passed_cnt", "univ_total")])
    return {"status": status, "source": _project(source, ("run_id", "d_str", "generated_at", "config_hash", "history_update_ok")),
        "validation": {"policy": _project(validation.get("policy", {}),
            ("min_completed_sample", "optimization_applied", "unit", "returns", "maturity", "timing", "primary_calculation", "regime", "limitations", "stop", "round_trip_cost_pct", "configuration", "empty_groups", "authority")),
            "quality_all_markets": _project(validation.get("quality", {}), RESEARCH_QUALITY_FIELDS), "groups": groups},
        "evaluation_coverage": _project(data.get("evaluation_coverage", {}), ("entry", "warnings", "reentry", "rs_comparison", "long_horizons")),
        "market_changes": {"baseline": _project(changes.get("baseline") or {}, ("run_id", "generated_at", "d_str")),
            "sector_comparison_count_all_markets": _scalar(changes.get("sector_comparison_count")),
            "unchanged_sector_count_all_markets": _scalar(changes.get("unchanged_sector_count")),
            "regimes": [_project(row, ("track", "market", "previous_regime", "current_regime", "previous_as_of", "current_as_of", "previous_breadth_50", "current_breadth_50"))
                for row in changes.get("regimes", []) if isinstance(row, dict) and row.get("market") in markets],
            "sectors": [_project(row, sector_fields) for row in changes.get("sectors", [])
                if isinstance(row, dict) and row.get("market") in markets]}}


def _scalar(value: Any) -> Any:
    """Reject nested/unexpected values instead of serializing arbitrary source data."""
    if not pd.api.types.is_scalar(value):
        return None
    if hasattr(value, "item"):
        value = value.item()
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value.strip()[:600] or None
    return None


def _project(row: dict, fields: tuple[str, ...]) -> dict:
    return {key: _scalar(row[key]) for key in fields if key in row}


def _market_rows(frame: pd.DataFrame, markets: tuple[str, ...]) -> pd.DataFrame:
    if frame.empty or "market" not in frame:
        return pd.DataFrame()
    return frame.loc[frame["market"].isin(markets)].copy().reset_index(drop=True)


def _records(frame: pd.DataFrame, fields: tuple[str, ...]) -> list[dict]:
    # Select before converting to records: private/unknown columns never enter the payload.
    cols = [key for key in fields if key in frame]
    return [_project(row, fields) for row in frame[cols].to_dict("records")] if cols else []


def build_analysis_export(
    manifest: dict, summary: dict, watchlist: pd.DataFrame,
    candidates: pd.DataFrame, sectors: pd.DataFrame, *,
    market: str = "ALL", candidate_limit: int = 20, research_raw: bytes | str | None = None,
    mode: str = "comprehensive", stock_key: tuple[str, str, str] | None = None,
    snapshot_verified: bool | None = None, change_context: dict | None = None,
) -> AnalysisExport:
    prompt = build_prompt(mode)
    if any(len(frame) > 50_000 for frame in (watchlist, candidates, sectors)):
        raise ValueError("분석 입력이 최대 50,000행을 초과했습니다.")
    if market not in ("ALL", "KR", "US"):
        raise ValueError("market must be ALL, KR or US")
    if isinstance(candidate_limit, bool) or candidate_limit not in (20, 40, 80):
        raise ValueError("candidate_limit must be 20, 40 or 80 per market")
    markets = ("KR", "US") if market == "ALL" else (market,)
    tracks = [key for key, value in TRACK_MARKETS.items() if value in markets]
    selected = _market_rows(watchlist, markets)
    all_candidates = _market_rows(candidates, markets)
    all_sectors = _market_rows(sectors, markets)

    # Preserve each market's engine rank; no reranking or recalculation of signals.
    sort_fields = [key for key in ("overall_rank", "rank") if key in all_candidates]
    if sort_fields:
        sort_values = all_candidates[sort_fields].apply(pd.to_numeric, errors="coerce")
        order = sort_values.sort_values(sort_fields, kind="stable", na_position="last").index
        all_candidates = all_candidates.loc[order]
    limited = pd.concat([
        all_candidates.loc[all_candidates["market"] == key].head(candidate_limit)
        for key in markets
    ], ignore_index=True) if not all_candidates.empty else all_candidates

    deep_dive = None
    if mode == "stock_deep_dive":
        deep_dive = select_deep_dive(watchlist, candidates, all_sectors, markets, stock_key)

    # The selected CSV omits freshness flags: reuse exact matching candidate records.
    identity = ("market", "sub_market", "ticker")
    if all(key in candidates for key in identity) and all(key in selected for key in identity):
        quality = candidates.drop_duplicates(list(identity), keep="first").set_index(list(identity))
        for key in ("is_stale_price", "stale_bars", "trade_risk_pct", "raw_stop_dist_pct", "atr14_pct", "earnings_days", "stop_warning"):
            if key in selected:
                continue
            if key in quality:
                selected[key] = [quality[key].get(tuple(row), None)
                                 for row in selected[list(identity)].itertuples(index=False, name=None)]

    warnings = []
    if snapshot_verified is False:
        raise ValueError("게시본 파일 무결성을 확인하지 못해 내보내기를 중단합니다.")
    research = {"status": "unavailable"}
    if research_raw is not None:
        try:
            research = parse_research_report(research_raw, manifest, markets)
            if research["status"] != "available":
                warnings.append("실전 검증 이력이 불완전하여 성과 통계 결론을 보류합니다.")
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            warnings.append("실전 검증 자료가 현재 게시본과 일치하지 않거나 형식이 잘못되어 제외했습니다.")
    for key in ("run_id", "generated_at", "d_str"):
        if not manifest.get(key):
            warnings.append(f"manifest의 {key} 값이 없습니다.")
    as_of = manifest.get("as_of")
    as_of = as_of if isinstance(as_of, dict) else {}
    for track in tracks:
        if not as_of.get(track):
            warnings.append(f"{track} 시세 기준일이 없습니다.")
    for name, frame in (("게시 적격 종목", selected), ("후보 종목", all_candidates), ("섹터", all_sectors)):
        if frame.empty:
            warnings.append(f"{name} 데이터가 비어 있습니다. 미선정·누락 여부는 원본 게시 상태를 확인하세요.")

    def summary_block(key: str) -> dict:
        value = summary.get(key)
        return value if isinstance(value, dict) else {}

    health = {}
    for key in markets:
        raw = summary_block("health").get(key.lower())
        if not isinstance(raw, dict):
            warnings.append(f"{key} 수집 상태가 없습니다.")
            continue
        health[key] = _project(raw, HEALTH_FIELDS)
        if raw.get("kr_price_status") == "delayed_or_holiday":
            warnings.append(f"KR {raw.get('kr_close_check_date', '')} 마감 일봉 일부 미확보. 기존 자료 유지(휴장/공급 지연 확인 필요).")
        for field, label in (("us_cap_fallback", "US 시가총액·재무정보 이전 정상 캐시 사용"),
                             ("us_cap_missing", "US 유효 시총 없음(분석 대상 제외)")):
            count = pd.to_numeric(raw.get(field), errors="coerce")
            if pd.notna(count) and count > 0:
                warnings.append(f"{label}: {int(count)}종목. 캐시 폴백은 최신 재무정보가 아닙니다.")
        if str(raw.get("stale_cache", "")).lower() in ("true", "1"):
            warnings.append(f"{key} 종목 목록은 오래된 캐시를 사용했습니다.")
        for field, label in ((f"{key.lower()}_failed", "수집 실패"),
                             (f"{key.lower()}_stale_tickers", "시세 지연")):
            value = pd.to_numeric(raw.get(field), errors="coerce")
            if pd.notna(value) and value > 0:
                warnings.append(f"{key} {label}: {int(value)}종목. 전체 수집 대상 기준입니다.")
    raw_observation = summary_block("price_observation")
    raw_observed_tracks = raw_observation.get("tracks") or {}
    raw_observed_tracks = raw_observed_tracks if isinstance(raw_observed_tracks, dict) else {}
    observed_tracks = {key: _project(item, PRICE_OBSERVATION_FIELDS) for key in tracks
                       if isinstance((item := raw_observed_tracks.get(key)), dict)}
    for track, item in observed_tracks.items():
        if item.get("session_unconfirmed") or item.get("lagging_candidate_rows"):
            warnings.append(f"{track} 가격 확인 필요: 지수 {item.get('benchmark_as_of')} / 확인 대상 평일 {item.get('clock_reference_date')} · "
                            f"뒤처진 후보 {item.get('lagging_candidate_rows', 0)}개. 기술적 적격은 최신성 보증이 아니며 휴장/공급 지연은 구분 불가합니다.")
    counts = {"watchlist": len(selected), "candidates_available": len(all_candidates),
              "candidates_included": len(limited), "sectors": len(all_sectors)}
    payload = {
        "export_schema": "market-analysis-1.1",
        "exported_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": _project(manifest, ("schema_version", "run_id", "d_str", "generated_at")),
        "as_of_by_track": {key: _scalar(as_of.get(key)) for key in tracks},
        "metric_definitions": _project(summary_block("metric_definitions"), METRIC_DEFINITION_FIELDS),
        "price_observation": {"tracks": observed_tracks,
                              **_project(raw_observation, ("policy", "scope"))},
        "scope": {"markets": list(markets), "candidate_limit_per_market": candidate_limit,
                  "counts": counts, "candidates_truncated": len(limited) < len(all_candidates),
                  "all_sectors_included": True, "all_published_watchlist_included": True,
                  "watchlist_definition": "watchlist.csv는 적격 action_df입니다. 후보의 selected 전체와 다릅니다.",
                  "ranking": "엔진 overall_rank/rank 오름차순, 없으면 게시 순서",
                  "not_included": ["개인 계좌·보유 비중·거래 원장", "시계열 차트", "섹터 이동 경로 이력", "최신 뉴스"]},
        "warnings": warnings,
        "units": {
            "fraction_0_05_means_5_percent": ["dist_to_pivot_pct", "stop_dist_pct", "range_10d_pct",
                                             "distance_from_high_pct", "dist_sma20_pct", "dist_sma50_pct"],
            "already_percent": ["down_day_excess_pct", "eps_growth_pct", "pct_above_50dma",
                                "pct_above_200dma", "breadth_coverage_pct", "pass_rate_pct"],
            "engine_limits_already_percent": ["exposure_cap", "risk_cap", "alloc", "risk"],
            "rs_point_difference": ["rs_change_4w", "rs_delta_4w"],
            "price_currency": {"KR": "KRW", "US": "USD"},
            "missing": "null 또는 항목 없음 = 미확인",
        },
        "regimes": {key: _project(value, REGIME_FIELDS) for key in tracks
                    if isinstance((value := summary_block("regimes").get(key)), dict)},
        "engine_counts_all_markets": _project(summary_block("counts"),
            ("selected", "actionable", "passed", "leading_actionable", "dropped")),
        "engine_model_limits_not_actual_holdings": {
            key: _project(value, ("exposure_cap", "risk_cap", "alloc", "risk")) for key in markets
            if isinstance((value := summary_block("market_limits").get(key)), dict)},
        "health": health,
        "sectors": _records(all_sectors, SECTOR_FIELDS),
        "actionable_watchlist": _records(selected, STOCK_FIELDS),
        "candidate_sample": _records(limited, STOCK_FIELDS),
        "research": research,
    }
    if change_context is not None:
        from actionable_changes import export_changes
        payload["actionable_changes"] = export_changes(change_context, manifest, markets)
    payload["export_features_version"] = "analysis-modes-1.0"
    payload["analysis_mode"] = mode
    payload["scope"]["analysis_mode"] = mode
    payload["freshness"] = export_freshness(manifest, snapshot_verified)
    payload["scope"]["deep_dive_outside_candidate_limit"] = deep_dive is not None and not any(
        all(row.get(k) == deep_dive["stock"].get(k) for k in ("market", "sub_market", "ticker"))
        for row in payload["candidate_sample"])
    if deep_dive is not None:
        payload["deep_dive"] = deep_dive
    if mode == "weekly":
        payload["weekly_context"] = weekly_context(research)
        warnings.append("주간은 검증된 연구 집계에 한정된 부분 분석입니다. 전체 주간 변화·누적 수익은 제공하지 않습니다.")
    if mode in ("morning", "evening"):
        priority_market = "US" if mode == "morning" else "KR"
        if priority_market not in markets:
            warnings.append(f"{MODE_DETAILS[mode][0]} 우선 시장 {priority_market} 자료가 선택 범위에 없어 부분 분석입니다.")
        warnings.append("분석 모드는 해당 시간의 운영 갱신을 증명하지 않습니다. 확정 거래일·실행 상태는 별도 확인이 필요합니다.")
    payload["units"]["fraction_0_05_means_5_percent"] += ["raw_stop_dist_pct", "atr14_pct"]
    payload["units"]["already_percent"] += ["position_size_pct", "trade_risk_pct", "portfolio_alloc_pct", "portfolio_risk_contrib_pct"]
    metadata = f"분석 모드: {MODE_DETAILS[mode][0]} · 시장: {market} · run_id: {run_id_safe(manifest)}\n\n"
    text = ("# 추세추종 시장 분석 요청\n\n" + metadata + prompt + "\n\n## 분석 자료\n\n````json\n"
            + json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n````\n")
    run_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(manifest.get("run_id") or "unknown"))[:80]
    if len(text.encode("utf-8")) > 8_000_000:
        raise ValueError("분석 파일이 8MB 제한을 초과했습니다.")
    filename = f"watchlist_gpt_{market}_{run_id}.md" if mode == "comprehensive" else f"watchlist_gpt_{mode}_{market}_{run_id}.md"
    if mode == "stock_deep_dive":
        filename = f"watchlist_gpt_{mode}_{run_id_safe({'run_id': '_'.join(stock_key)})}_{run_id}.md"
    return AnalysisExport(text, filename, counts, tuple(warnings))


def run_id_safe(manifest: dict) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(manifest.get("run_id") or "unknown"))[:80]


def export_freshness(manifest: dict, verified: bool | None) -> dict:
    return {
        "snapshot_integrity": "verified" if verified is True else "caller_supplied_unverified",
        "exchange_session_verification": "unavailable",
        "scheduled_workflow_status": "unverified",
        "policy": "다운로드 순간 확보한 게시본입니다. 모드·생성일·요일만으로 예약 갱신 성공이나 확정 거래일을 보증하지 않습니다. 기준일이 오래됐거나 확인 불가이면 참고 분석에 한정합니다.",
    }


def stock_options(watchlist: pd.DataFrame, candidates: pd.DataFrame, markets: tuple[str, ...]) -> list[tuple[tuple[str, str, str], str]]:
    """Only identities loaded from the public snapshot; never query arbitrary symbols."""
    frames = [_market_rows(frame, markets) for frame in (candidates, watchlist)]
    rows = pd.concat(frames, ignore_index=True)
    keys = ("market", "sub_market", "ticker")
    if rows.empty or not all(key in rows for key in keys):
        return []
    out = []
    for row in rows.drop_duplicates(list(keys)).to_dict("records"):
        key = tuple(str(row[k]) for k in keys)
        if TRACK_MARKETS.get(key[1]) != key[0] or not key[2] or key[2] in ("nan", "None"):
            continue
        out.append((key, f"{key[0]} · {key[1]} · {row.get('name', '')} ({key[2]})"))
    return out


def select_deep_dive(watchlist, candidates, sectors, markets, key) -> dict:
    if not isinstance(key, tuple) or len(key) != 3 or key not in dict(stock_options(watchlist, candidates, markets)):
        raise ValueError("선택 종목이 현재 게시본·시장·트랙에 없습니다.")
    identity = ("market", "sub_market", "ticker")
    combined = {}
    for frame in (candidates, watchlist):
        if not all(k in frame for k in identity):
            continue
        matches = frame.loc[frame[list(identity)].astype(str).eq(pd.Series(dict(zip(identity, key)))).all(axis=1)]
        if len(matches) > 1:
            raise ValueError("선택 종목 식별자가 중복되어 심층 분석을 만들 수 없습니다.")
        if len(matches):
            # Both are from the same snapshot. Watchlist's constrained allocation wins.
            combined.update(_project(matches.iloc[0].to_dict(), STOCK_FIELDS))
    related = sectors if all(combined.get(k) for k in ("market", "sector")) else sectors.iloc[:0]
    for column in ("market", "macro_theme", "sector"):
        if column in related and column in combined:
            related = related.loc[related[column].eq(combined[column])]
    return {"stock": combined, "sectors": _records(related, SECTOR_FIELDS),
            "source": "same-snapshot candidates.csv plus matching watchlist.csv",
            "ohlc_included": False}


def weekly_context(research: dict) -> dict:
    changes = research.get("market_changes", {})
    baseline = changes.get("baseline", {})
    current = research.get("source", {}).get("run_id")
    return {"status": "partial" if research.get("status") == "available" else "unavailable",
            "performance_source": "verified analysis/latest.md only",
            "verified_history_csv_included": False,
            "history_csv_reason": "history CSV는 manifest 해시 대상이 아니므로 동일 게시본·이력 무결성을 검증할 수 없어 추가 집계하지 않습니다.",
            "previous_snapshot_comparison_available": bool(baseline.get("run_id") and baseline.get("run_id") != current),
            "full_week_changes_available": False,
            "policy": "all/recent30 성과를 주간 수익으로 바꾸지 않습니다. 직전 게시본 비교와 한 주 변화를 구분합니다. 주간 자산곡선·누적 수익은 미지원입니다."}


def snapshot_export_errors(raw_files: dict[str, bytes | None], manifest: dict) -> list[str]:
    """Attest the four market inputs before parsing; absent hashes fail closed."""
    import hashlib
    errors = []
    for name in ("summary.json", "watchlist.csv", "candidates.csv", "sectors.csv"):
        raw = raw_files.get("latest/" + name)
        expected = (manifest.get("files") or {}).get(name, {}).get("sha256")
        if raw is None or len(raw) > 8_000_000 or not expected or hashlib.sha256(raw).hexdigest() != expected:
            errors.append(name)
    return errors

