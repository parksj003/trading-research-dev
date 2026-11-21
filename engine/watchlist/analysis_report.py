"""Publish market-change evidence and horizon-safe signal statistics, without AI calls."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd


SCHEMA = "watchlist-research-1.1"
MARKET_FILES = ("summary.json", "watchlist.csv", "candidates.csv", "sectors.csv")
HISTORY_FILES = ("signals.csv", "signal_outcomes.csv", "regime.csv", "sector_power.csv")
KEY = ["first_seen", "market", "ticker"]
OUTCOME_FIELDS = KEY + ["as_of", "track", "sub_market", "sector", "setup_stage", "entry_date",
    "entry_price", "stop_price", "bars", "ret_5d", "ret_10d", "ret_20d", "ex_5d", "ex_10d",
    "ex_20d", "mfe_10d_pct", "mae_10d_pct", "mfe_pct", "mae_pct", "stop_hit_10d",
    "stop_aware_ret_10d", "stop_aware_r_10d", "calculation_version", "entry_price_source", "stop_price_basis",
    "evaluated_as_of", "eval_date_5d", "eval_date_10d", "eval_date_20d", "path_ohlc_complete_10d",
    "calendar_complete_5d", "calendar_complete_10d", "calendar_complete_20d", "entry_session_verified"]
SIGNAL_FLAGS = ("is_breakout", "is_true_vcp", "rs_blue_dot", "rs_line_new_high",
                "down_resilient", "is_wide_stop", "wide_stop_actionable", "is_leading_sector")
SIGNAL_FIELDS = KEY + ["regime", "generated_at", "published_at", "workflow_run_id", "rs_score",
    "rs_change_4w", "sector_power", "config_hash", "engine_version", "engine_commit"] + list(SIGNAL_FLAGS)
CALCULATION_VERSION = "fixed_horizon_dates_v2"
SECTOR_KEY = ["market", "level", "macro_theme", "sector"]

def metric_definitions(cfg):
    """Display metadata only: never changes the engine's selection or risk rules."""
    if __package__:
        from .session_calendar import calendar_policy
    else:
        from session_calendar import calendar_policy
    ex = cfg.get("market", {}).get("exclude_nasdaq_from_sp500", True)
    return {
        "official_performance": "공식 모의 성과는 published/analysis/latest.md의 게시·시가·거래일 검증을 통과한 표본만 사용합니다.",
        "session_calendar": f"KR={calendar_policy('KR')}; US={calendar_policy('US')}. 새 결과의 calendar_policy는 세션 정책이며 수익 공식 버전과 별개입니다. 구형 성과는 소급 재계산하지 않습니다.",
        "legacy_validation": "validation.csv는 과거 후보의 참고용 포워드 집계입니다. 게시 시각·원본 시가·거래일 정렬을 보증하지 않으므로 전략 우열 판단에 사용하지 않습니다.",
        "breadth_nasdaq100": "NASDAQ100 전략용 Breadth 모집단(최종 RS·후보 모집단과 다름)",
        "breadth_sp500": "S&P500 ex-NDX 전략용 Breadth 모집단" if ex else "S&P500 전략용 Breadth 모집단(NDX 포함 설정)",
        "breadth_filters": "시총 하한은 US 두 트랙 중 낮은 기준을 사용합니다. 유효 시세·최소 주가·최소 200봉 조건을 확인하며, 후보 선정용 거래대금 및 NDX 별도 시총 하한은 Breadth 수집 이후 적용합니다. 지수와 동일한 기준일의 종목만 유효 표본에 포함하며 지연 종목도 커버리지 분모에는 남습니다. 동일 기준일의 과거 게시본은 현재 시세를 보증하지 않습니다. 전체 지수 구성종목 Breadth가 아닙니다.",
        "near_52w": "최근 최대 252거래일 종가 고점·저점의 3% 이내 종목 수입니다. 짧은 이력은 가용 기간 기준이며 실제 신고가·신저가 돌파 건수가 아닙니다.",
        "rs_4w": "현재 계산 모집단에서 현재와 20거래일 전 원시 RS를 각각 백분위화한 차이입니다. 섹터는 종목별 차이의 중앙값입니다. 과거 저장 당시 RS와의 차이가 아닙니다.",
        "leading": "로테이션 Leading은 RS 강세 분류이며 Breadth가 낮아도 유지될 수 있습니다. Leading ↑와 👑주도 표시에는 별도 조건이 있습니다. 매수 적격 여부와 별개이며 표본 수·50일선 상회율·Actionable 수를 함께 확인하세요.",
        "actionable": "is_actionable은 기존 기술적 적격입니다. 최신 시세 확보를 보증하지 않으며, 종목·벤치마크 기준일과 가격 관측 상태를 별도로 확인해야 합니다.",
    }

def price_observation(regimes, candidates, generated_at):
    """Clock/date diagnostics, not a holiday calendar or an eligibility gate."""
    now = _aware_time(generated_at)
    tracks = {}
    for key, regime in regimes.items():
        market = "KR" if key in ("KOSPI", "KOSDAQ") else "US"
        zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
        close = time(15,35) if market == "KR" else time(16,5)
        reference = None
        if now is not None:
            local = now.tz_convert(zone)
            reference = local.date()
            if local.time() < close: reference -= timedelta(days=1)
            while reference.weekday() >= 5: reference -= timedelta(days=1)
        actual = _date(regime.get("as_of"))
        subset = candidates.loc[candidates.sub_market.eq(key)] if "sub_market" in candidates else candidates.iloc[:0]
        lag = number(subset,"stale_bars").gt(0)
        actionable = subset.get("is_actionable",pd.Series(index=subset.index,dtype=str)).astype(str).str.lower().isin(("true","1","1.0"))
        tracks[key] = {"benchmark_as_of": str(actual) if actual is not None else None,
            "clock_reference_date": str(reference) if reference else None,
            "session_unconfirmed": actual is None or reference is None or actual != reference,
            "candidate_rows": len(subset), "lagging_candidate_rows": int(lag.sum()),
            "lagging_actionable_rows": int((lag & actionable).sum())}
    return {"scope": "published candidate rows; weekday close clock does not prove an exchange session",
        "policy": "기술적 적격과 최신성은 별개입니다. 확인 대상 평일 일봉 미확보는 휴장/공급 지연을 구분할 수 없으며 주문 신호를 변경하지 않습니다.",
        "tracks": tracks}


def scalar(value):
    if not pd.api.types.is_scalar(value) or value is None or value is pd.NA or value is pd.NaT:
        return None
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    return value.strip()[:600] or None if isinstance(value, str) else None


def project(row, fields):
    return {key: scalar(row.get(key)) for key in fields}


def number(frame, key):
    value = frame[key] if key in frame else pd.Series(index=frame.index, dtype=float)
    return pd.to_numeric(value, errors="coerce").replace([float("inf"), -float("inf")], float("nan"))


def tag_raw_price_quality(frame):
    """Preserve provenance BEFORE existing normalization fills OHLC; never change price values."""
    if frame is None or frame.empty:
        return pd.DataFrame() if frame is None else frame.copy()
    out = frame.copy()
    values = {key: number(out, key) for key in ("Open","High","Low","Close")}
    out["_raw_open_valid"] = values["Open"].notna() & values["Open"].gt(0)
    path = pd.Series(True,index=out.index)
    for value in values.values():path &= value.notna() & value.gt(0)
    path &= values["High"] >= pd.concat([values["Open"],values["Close"]],axis=1).max(axis=1)
    path &= values["Low"] <= pd.concat([values["Open"],values["Close"]],axis=1).min(axis=1)
    out["_raw_ohlc_valid"] = path
    return out


def _read_csv(path):
    return pd.read_csv(path, dtype={"ticker": str, "workflow_run_id": str}) if path.exists() else pd.DataFrame()


def _aware_time(value):
    try:
        ts = pd.Timestamp(value)
        return ts.tz_convert("UTC") if pd.notna(ts) and ts.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def _workflow_fact(raw, run_id):
    """Use the same conservative successful-Workflow bound already used by Collector."""
    if not isinstance(raw, dict): return None
    start, finish = _aware_time(raw.get("created_at")), _aware_time(raw.get("updated_at"))
    if (str(raw.get("id")) != run_id or raw.get("status") != "completed" or raw.get("conclusion") != "success"
            or str(raw.get("path", "")).split("@")[0] != ".github/workflows/watchlist.yml"
            or raw.get("head_branch") != "main" or start is None or finish is None or start > finish):
        return None
    return {"id": run_id, "status": "completed", "conclusion": "success", "head_branch": "main",
            "path": ".github/workflows/watchlist.yml", "created_at": start.isoformat(),
            "updated_at": finish.isoformat(), "run_attempt": scalar(raw.get("run_attempt"))}


def resolve_publications(signals, cached=None, lookup=None, max_calls=4):
    """No broker/price/API-model calls. Reuse a bounded cache inside the same research MD."""
    s = signals.copy()
    facts = {}
    if "workflow_run_id" not in s or "generated_at" not in s:
        return s, facts
    ids = {str(value).strip() for value in s.workflow_run_id.dropna() if str(value).strip().isdigit()}
    needed = {str(row.get("workflow_run_id", "")).strip() for row in s.to_dict("records")
              if _aware_time(row.get("published_at")) is None and _aware_time(row.get("generated_at")) is not None}
    for key, raw in (cached if isinstance(cached, dict) else {}).items():
        if key not in ids: continue
        items = raw if isinstance(raw, list) else [raw]
        verified = [fact for item in items if (fact := _workflow_fact(item, key))]
        if verified: facts[key] = verified
    def covering(run_id, generated):
        return [fact for fact in facts.get(run_id, []) if generated is not None and
                _aware_time(fact["created_at"]) <= generated <= _aware_time(fact["updated_at"])]
    missing = {str(row.get("workflow_run_id", "")).strip() for row in s.to_dict("records")
               if str(row.get("workflow_run_id", "")).strip() in needed and
               not covering(str(row.get("workflow_run_id", "")).strip(), _aware_time(row.get("generated_at")))}
    if lookup is None:
        repository, token = os.getenv("GITHUB_REPOSITORY", ""), os.getenv("GITHUB_TOKEN", "")
        if os.getenv("GITHUB_ACTIONS") == "true" and os.getenv("WATCHLIST_VERIFY_PUBLICATIONS") == "true" and token and re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            import requests
            def lookup(run_id):
                response = requests.get(f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=(3, 5))
                response.raise_for_status()
                return response.json()
    calls = 0
    for run_id in sorted(ids & missing, key=int, reverse=True):
        if lookup is None or calls >= max_calls: break
        calls += 1
        try:
            fact = _workflow_fact(lookup(run_id), run_id)
            if fact and fact not in facts.get(run_id, []): facts.setdefault(run_id, []).append(fact)
        except Exception:
            pass  # Access failures leave publication unknown; never invent a timestamp.
    if "published_at" not in s: s["published_at"] = None
    s["published_at"] = s["published_at"].astype(object)
    for index, row in s.iterrows():
        if _aware_time(row.get("published_at")) is not None: continue
        generated = _aware_time(row.get("generated_at"))
        bounds = covering(str(row.get("workflow_run_id", "")).strip(), generated)
        if bounds:
            s.at[index, "published_at"] = min(fact["updated_at"] for fact in bounds)
    # One positive fact per referenced source run, reused on later executions.
    return s, {key: facts[key] for key in sorted(facts, key=int, reverse=True)}


def read_previous_market(base: Path):
    """Only public market artifacts; never inspect other repository directories."""
    latest = base / "latest"
    if not (latest / "manifest.json").exists():
        return None
    manifest = json.loads((latest / "manifest.json").read_text())
    for name in ("summary.json", "sectors.csv"):
        expected = manifest.get("files", {}).get(name, {}).get("sha256")
        if not expected or hashlib.sha256((latest / name).read_bytes()).hexdigest() != expected:
            return None
    return {"source": project(manifest, ("run_id", "generated_at", "d_str")),
            "summary": json.loads((latest / "summary.json").read_text()),
            "sectors": _read_csv(latest / "sectors.csv")}


def market_changes(manifest, summary, sectors, previous):
    result = {"baseline": previous["source"] if previous else None, "regimes": [], "sectors": [],
              "sector_comparison_count": 0, "unchanged_sector_count": 0}
    if not previous:
        return result
    old_regimes = previous["summary"].get("regimes", {})
    for track, current in summary.get("regimes", {}).items():
        if track not in ("KOSPI", "KOSDAQ", "NASDAQ100", "SP500"):
            continue
        old = old_regimes.get(track, {})
        result["regimes"].append({"track": track, "market": "KR" if track in ("KOSPI", "KOSDAQ") else "US",
            "previous_regime": scalar(old.get("regime")), "current_regime": scalar(current.get("regime")),
            "previous_as_of": scalar(old.get("as_of")), "current_as_of": scalar(current.get("as_of")),
            "previous_breadth_50": scalar(old.get("pct_above_50dma")),
            "current_breadth_50": scalar(current.get("pct_above_50dma"))})
    before = previous["sectors"]
    fields = ["sector_power", "median_rs", "passed_cnt", "univ_total"]
    if not all(key in before and key in sectors for key in SECTOR_KEY):
        return result
    def index(frame):
        d = frame.copy()
        d[SECTOR_KEY] = d[SECTOR_KEY].fillna("")
        if "macro_power" in d:
            d["sector_power"] = number(d, "sector_power").fillna(number(d, "macro_power"))
        return {tuple(row[key] for key in SECTOR_KEY): row for row in d.to_dict("records")}
    old, new = index(before), index(sectors)
    for key in sorted(set(old) | set(new)):
        row = dict(zip(SECTOR_KEY, key))
        row["status"] = "continued" if key in old and key in new else "added" if key in new else "removed"
        for field in fields:
            a, b = scalar(old.get(key, {}).get(field)), scalar(new.get(key, {}).get(field))
            row["previous_" + field], row["current_" + field] = a, b
            row[field + "_change"] = b - a if isinstance(a, (int, float)) and isinstance(b, (int, float)) else None
        result["sector_comparison_count"] += 1
        if row["status"]=="continued" and all(row["previous_"+field] is not None and
                row["previous_"+field]==row["current_"+field] for field in fields):
            result["unchanged_sector_count"] += 1
            continue
        result["sectors"].append(row)
    return result


def _date(value):
    try:
        parsed = pd.Timestamp(value) if pd.notna(value) else pd.NaT
        return parsed.date() if pd.notna(parsed) else None
    except (ValueError, TypeError):
        return None


def _availability(row):
    """Generation is NOT publication evidence. Check both separately without inventing times."""
    entry = _date(row.get("entry_date"))
    seen = _date(row.get("first_seen"))
    if entry is None:
        return "waiting_entry", False
    market = row.get("market")
    if market not in ("KR", "US") or seen is None:
        return "timing_unknown", False
    zone = ZoneInfo("Asia/Seoul" if market == "KR" else "America/New_York")
    opened = pd.Timestamp.combine(entry, time(9) if market == "KR" else time(9, 30)).tz_localize(zone)
    generated, published = None, None
    for key in ("generated_at", "published_at"):
        try:
            ts = pd.Timestamp(row.get(key))
            if pd.notna(ts) and ts.tzinfo is not None:
                if key == "generated_at": generated = ts
                else: published = ts
        except (ValueError, TypeError):
            pass
    if any(opened < ts for ts in (generated, published) if ts is not None):
        return "entry_before_signal", False
    if published is not None and generated is not None and published < generated:
        return "timing_unknown", False
    if generated is None and published is None and entry < seen:
        return "entry_before_signal", False
    if published is not None:
        return "publication_checked", True
    return "generation_checked_publication_unknown" if generated is not None else "timing_unknown", False


def _groups(subset, definitions):
    for dimension, keys in definitions.items():
        for _, group in subset.groupby(keys, dropna=False, sort=True):
            yield dimension, keys, group
    for flag in SIGNAL_FLAGS:
        if flag not in subset:
            continue
        raw = subset[flag].astype(str).str.strip().str.lower()
        valid = raw.isin(("true", "false", "1", "0", "1.0", "0.0"))
        known = subset.loc[valid].copy()
        known["signal_name"] = flag
        known["signal_value"] = raw.loc[valid].isin(("true", "1", "1.0"))
        keys = ["market", "signal_name", "signal_value"]
        for _, group in known.groupby(keys, sort=True):
            yield "signal", keys, group


def validation_statistics(signals, outcomes, manifest):
    """Aggregate existing simulations; never regenerate prices, trades, or entry signals."""
    quality = {"outcome_rows": len(outcomes), "duplicate_outcomes": 0, "unmatched_signals": 0,
               "entry_before_signal": 0, "timing_unknown": 0, "publication_checked": 0,
               "generation_checked_publication_unknown": 0, "waiting_entry": 0, "invalid_identity": 0,
               "legacy_calculation_rows": 0, "open_unverified_rows": 0, "stop_basis_unverified_rows": 0,
               "stop_basis_changed_rows": 0, "entry_eligible_rows": 0}
    policy = {
        "min_completed_sample": 20, "optimization_applied": False,
        "unit": "returns/MFE/MAE = percent, excess = percentage points, R = risk multiple",
        "returns": "ret_Nd is fixed-horizon hold after modeled costs; stop-aware return is separate; win = ret_Nd > 0, never actual account performance",
        "maturity": "finite ret_Nd AND bars >= N AND complete matching benchmark session dates; early exits cannot mature a 10-day cohort",
        "timing": "primary counts require publication evidence before entry open; generation alone is never sufficient",
        "primary_calculation": CALCULATION_VERSION,
        "authority": "official simulated performance; legacy validation.csv is reference only",
        "regime": "use the original signals.csv regime only; missing stays UNKNOWN, never substitute today's regime",
        "limitations": "overlapping signals, shared tickers, selection/survivorship bias and changing universe; no causal or optimal-parameter claims",
        "stop": "10-day stop-aware R only; never substitute unstopped R or 20-day MAE; missing stop is unknown",
        "empty_groups": "non-market/track groups with zero mature returns are omitted; absence is not a zero win rate",
    }
    if not set(KEY).issubset(outcomes) or not set(KEY).issubset(signals):
        return {"status": "history_unavailable", "policy": policy, "quality": quality, "groups": []}
    d = outcomes[[key for key in OUTCOME_FIELDS if key in outcomes]].copy()
    s = signals[[key for key in SIGNAL_FIELDS if key in signals]].copy()
    for frame in (d, s):
        for key in KEY:
            frame[key] = frame[key].fillna("").astype(str).str.strip()
        frame["ticker"] = [ticker.zfill(6) if market == "KR" and ticker else ticker.upper()
                           for ticker, market in zip(frame.ticker, frame.market)]
    valid = d.market.isin(("KR", "US")) & d.ticker.ne("") & d.first_seen.map(_date).notna()
    quality["invalid_identity"] = int((~valid).sum())
    d = d.loc[valid]
    dup = d.duplicated(KEY, keep=False)
    quality["duplicate_outcomes"] = int(dup.sum())
    d = d.loc[~dup]  # Ambiguous repeated events cannot inflate the sample.
    s = s.loc[~s.duplicated(KEY, keep=False)]
    d = d.merge(s, on=KEY, how="left", validate="one_to_one", indicator=True)
    quality["unmatched_signals"] = int(d["_merge"].ne("both").sum())
    d["entry_regime"] = d.get("regime", pd.Series(index=d.index, dtype=str)).fillna("UNKNOWN")
    d["entry_regime"] = d["entry_regime"].where(d.entry_regime.isin(("ATTACK", "CAUTION", "RISK_OFF")), "UNKNOWN")
    if "track" not in d: d["track"] = d.get("sub_market", "UNKNOWN")
    for key in ("track", "sector", "setup_stage", "config_hash", "engine_commit"):
        if key not in d: d[key] = "UNKNOWN"
        d[key] = d[key].fillna("UNKNOWN").replace("", "UNKNOWN")
    timing = [_availability(row) for row in d.to_dict("records")]
    d["timing"] = [pair[0] for pair in timing]
    d["publication_verified"] = [pair[1] for pair in timing]
    for key in ("entry_before_signal", "timing_unknown", "publication_checked", "generation_checked_publication_unknown", "waiting_entry"):
        quality[key] = int(d.timing.eq(key).sum())
    calculation = d.get("calculation_version", pd.Series(index=d.index, dtype=str)).fillna("")
    open_basis = d.get("entry_price_source", pd.Series(index=d.index, dtype=str)).fillna("")
    stop_basis = d.get("stop_price_basis", pd.Series(index=d.index, dtype=str)).fillna("")
    quality["legacy_calculation_rows"] = int(calculation.ne(CALCULATION_VERSION).sum())
    quality["open_unverified_rows"] = int(open_basis.ne("open").sum())
    quality["stop_basis_unverified_rows"] = int((~stop_basis.isin(("matched", "changed"))).sum())
    quality["stop_basis_changed_rows"] = int(stop_basis.eq("changed").sum())
    d["eligible"] = d.publication_verified & d["_merge"].eq("both") & calculation.eq(CALCULATION_VERSION) & open_basis.eq("open")
    d["eligible"] &= d.get("entry_session_verified",pd.Series(index=d.index,dtype=str)).astype(str).str.lower().isin(("true","1","1.0"))
    d["eligible"] &= number(d, "entry_price") > 0
    # Exclude future-dated entries, even if a malformed row already contains returns.
    cutoffs = manifest.get("as_of", {})
    cutoffs = cutoffs if isinstance(cutoffs, dict) else {}
    d["eligible"] &= pd.Series([(_date(row.get("entry_date")) is not None and
        _date(cutoffs.get(row.get("track"))) is not None and
        _date(row.get("entry_date")) <= _date(cutoffs.get(row.get("track"))))
        for row in d.to_dict("records")], index=d.index, dtype=bool)
    quality["entry_eligible_rows"] = int(d.eligible.sum())
    groups = []
    definitions = {"market": ["market"], "track": ["market", "track"],
                   "setup": ["market", "setup_stage"], "regime": ["market", "entry_regime"],
                   "setup_regime": ["market", "setup_stage", "entry_regime"], "sector": ["market", "sector"],
                   "configuration": ["market", "config_hash"], "engine_revision": ["market", "engine_commit"]}
    anchor = _date(manifest.get("d_str"))
    for window in ("all", "recent30"):
        subset = d
        if window == "recent30":
            if anchor is None: continue
            subset = d.loc[[date is not None and anchor-timedelta(days=30) <= date <= anchor
                            for date in d.first_seen.map(_date)]]
        for dimension, keys, g in _groups(subset, definitions):
            for horizon in (5, 10, 20):
                ret = number(g, f"ret_{horizon}d")
                calendar = g.get(f"calendar_complete_{horizon}d", pd.Series(index=g.index,dtype=str)).astype(str).str.lower().isin(("true","1","1.0"))
                complete = g.eligible & (number(g, "bars") >= horizon) & ret.notna() & calendar
                evaluated = g.loc[complete]
                r = ret.loc[complete]
                if not len(r) and dimension not in ("market", "track"):
                    continue
                ex = number(evaluated, f"ex_{horizon}d").dropna()
                row = {"window": window, "dimension": dimension, "horizon": horizon,
                       **{key: scalar(g.iloc[0][key]) for key in keys},
                       "signals": len(g), "completed": len(r), "pending_or_excluded": len(g)-len(r),
                       "unique_tickers": int(evaluated.ticker.nunique()),
                       "unique_entry_dates": int(evaluated.get("entry_date", pd.Series(dtype=str)).nunique()),
                       "publication_checked_n": int(evaluated.publication_verified.sum()),
                       "excess_n": len(ex), "win_rate_pct": scalar((r > 0).mean()*100),
                       "mean_return_pct": scalar(r.mean()), "median_return_pct": scalar(r.median()),
                       "mean_excess_pp": scalar(ex.mean()), "excess_win_rate_pct": scalar((ex > 0).mean()*100),
                       "known_config_n": int(evaluated.config_hash.ne("UNKNOWN").sum()),
                       "config_versions": int(evaluated.loc[evaluated.config_hash.ne("UNKNOWN"), "config_hash"].nunique()),
                       "evidence": "insufficient_sample" if len(r)<20 else "simulation_only"}
                wins, losses = r.loc[r>0], r.loc[r<0]
                row.update(wins=len(wins), losses=len(losses), mean_win_pct=scalar(wins.mean()),
                           mean_loss_pct=scalar(losses.mean()), payoff_ratio=scalar(wins.mean()/abs(losses.mean())) if len(losses) else None)
                for field, prefix in (("rs_score", "rs_score"), ("rs_change_4w", "rs_change"), ("sector_power", "sector_power")):
                    values = number(evaluated, field).dropna()
                    row[prefix+"_n"], row["mean_"+field] = len(values), scalar(values.mean())
                if horizon == 10:
                    valid_stop = (number(evaluated, "stop_price") > 0) & (number(evaluated, "entry_price") > number(evaluated, "stop_price"))
                    valid_path = evaluated.get("path_ohlc_complete_10d", pd.Series(index=evaluated.index, dtype=str)).astype(str).str.lower().isin(("true","1","1.0"))
                    valid_stop &= evaluated.get("stop_price_basis", pd.Series(index=evaluated.index, dtype=str)).eq("matched") & valid_path
                    stop_rows = evaluated.loc[valid_stop]
                    stop_r = number(stop_rows, "stop_aware_r_10d").dropna()
                    stop_ret = number(stop_rows, "stop_aware_ret_10d").dropna()
                    paired_hold = number(stop_rows.loc[stop_ret.index], "ret_10d")
                    stop_raw = stop_rows.get("stop_hit_10d", pd.Series(dtype=str)).astype(str).str.lower().str.strip()
                    stop_raw = stop_raw.loc[stop_raw.isin(("true", "false", "1", "0", "1.0", "0.0"))]
                    mae, mfe = number(evaluated.loc[valid_path], "mae_10d_pct").dropna(), number(evaluated.loc[valid_path], "mfe_10d_pct").dropna()
                    row.update(stop_r_n=len(stop_r), mean_stop_r=scalar(stop_r.mean()),
                        stop_return_n=len(stop_ret), mean_stop_return_pct=scalar(stop_ret.mean()),
                            paired_hold_n=len(paired_hold), mean_paired_hold_return_pct=scalar(paired_hold.mean()),
                            mean_stop_minus_hold_pp=scalar((stop_ret-paired_hold).mean()),
                        stop_touch_n=len(stop_raw), stop_touch_pct=scalar(stop_raw.isin(("true","1","1.0")).mean()*100),
                        mae_n=len(mae), mean_mae_pct=scalar(mae.mean()), mfe_n=len(mfe), mean_mfe_pct=scalar(mfe.mean()))
                groups.append(row)
    return {"status": "available", "policy": policy, "quality": quality, "groups": groups}


def publish_analysis_report(base: Path, previous=None, *, history_update_ok=True, round_trip_cost_pct=None, workflow_lookup=None):
    """One supplemental atomic artifact; existing latest manifest/CSV contract is untouched."""
    latest = base / "latest"
    manifest = json.loads((latest / "manifest.json").read_text())
    file_hashes = {}
    for name in MARKET_FILES:
        actual = hashlib.sha256((latest / name).read_bytes()).hexdigest()
        if actual != manifest.get("files", {}).get(name, {}).get("sha256"):
            raise ValueError("market artifact hash mismatch")
        file_hashes[name] = actual
    summary = json.loads((latest / "summary.json").read_text())
    history, sources = {}, {}
    for name in HISTORY_FILES:
        path = base / "history" / name
        history[name] = _read_csv(path)
        sources[name] = {"rows": len(history[name]), "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None}
    cached = {}
    report_path = base / "analysis" / "latest.md"
    if report_path.exists():
        try:
            old = json.loads(report_path.read_text().split("````json\n",1)[1].rsplit("\n````",1)[0])
            cached = old.get("publication_evidence", {}) if isinstance(old, dict) else {}
        except (ValueError, IndexError, TypeError):
            pass
    resolved, publication_evidence = resolve_publications(history["signals.csv"], cached, workflow_lookup,
                                                         max_calls=4 if history_update_ok else 0)
    validation = validation_statistics(resolved, history["signal_outcomes.csv"], manifest)
    validation["policy"]["round_trip_cost_pct"] = scalar(round_trip_cost_pct)
    validation["policy"]["configuration"] = "current config only; per-event past configuration changes are not fully recorded"
    if not history_update_ok:
        validation["status"] = "history_update_incomplete"
        validation["groups"] = []
    payload = {"schema": SCHEMA,
        "source": {**project(manifest, ("run_id", "d_str", "generated_at", "config_hash")), "market_file_sha256": file_hashes,
                   "history_update_ok": bool(history_update_ok), "history_sources": sources},
        "validation": validation,
        "publication_evidence": publication_evidence,
        "evaluation_coverage": {
            "entry": "publication-checked fixed-horizon simulation; not a pivot-order fill or actual trade",
            "warnings": "unavailable: no complete timestamped warning events and negative controls",
            "reentry": "unavailable: no linked simulated exit/new-setup cycles; private portfolio cycles are excluded",
            "rs_comparison": "signal-time context only; legacy RS-change and strategy versions are incomplete; no causal ranking",
            "long_horizons": "40/60 sessions deferred; existing 5/10/20 unchanged"},
        "market_changes": market_changes(manifest, summary, _read_csv(latest / "sectors.csv"), previous)}
    text = ("# Watchlist 시장 변화·실전 검증 자료\n\n"
        "이 자료는 Python이 기존 신호 시뮬레이션을 집계한 결과이며 GPT 분석 결과나 실제 계좌 성과가 아닙니다.\n"
        "먼저 latest/manifest.json의 run_id와 source.run_id를 대조하세요. 불일치하면 이 자료로 분석하지 마세요.\n"
        "완료 표본 20개 미만은 판단 보류이며, 생성 시각은 GitHub 게시 시각을 입증하지 않습니다.\n"
        "시장 데이터는 latest/summary.json, watchlist.csv, candidates.csv, sectors.csv를 함께 읽으세요.\n\n"
        "````json\n" + json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n````\n")
    dest = base / "analysis" / "latest.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_suffix(".tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
    return dest

