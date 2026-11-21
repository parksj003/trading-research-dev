"""Offline observations. Run from a repository root with its version on PYTHONPATH.

PYTHONPATH=. python research/compass/replay.py watchlist/published /path/to/quotes
quotes/{QQQ,SPY,^IXIC,^GSPC,^KS11,^KQ11}.csv: local session date followed by
Open,High,Low,Close,Volume. Supply adjusted OHLC and native volume for each series.
No provider calls, production writes, session certifications or ledger updates.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from watchlist import trend_watchlist as engine


def read_publications(root):
    for day in sorted((root / "daily").iterdir()):
        manifest = json.loads((day / "manifest.json").read_bytes())
        for name in ("summary.json", "candidates.csv", "watchlist.csv"):
            assert hashlib.sha256((day / name).read_bytes()).hexdigest() == manifest["files"][name]["sha256"]
        yield day, json.loads((day / "summary.json").read_bytes())


def allocations(day, summary, cfg):
    watch = pd.read_csv(day / "watchlist.csv", dtype={"ticker": str})
    candidates = pd.read_csv(day / "candidates.csv", dtype={"ticker": str})
    fields = ["market", "ticker", "raw_stop_dist_pct"]
    if "earnings_days" in candidates:
        fields.append("earnings_days")
    frame = watch.merge(candidates[fields],
                        on=["market", "ticker"], validate="one_to_one", how="left")
    assert frame.raw_stop_dist_pct.notna().all()
    result = {}
    for rescale in (False, True):
        if rescale:
            if "earnings_days" not in frame:
                result["with_sizing"] = None
                continue
            weights = []
            for row in frame.to_dict("records"):
                engine.sync_track_sizing_with_final_regime([row], summary["regimes"][row["sub_market"]], cfg)
                weight = row["position_size_pct"]
                if row["market"] == "US" and 4 <= row["earnings_days"] <= 7:
                    weight = float(np.round(weight * .5, 1))
                weights.append(weight)
            frame["position_size_pct"] = weights
        out, meta = engine.allocate_portfolio_risk_budget(frame, summary["regimes"], cfg)
        risk = out.portfolio_alloc_pct * (frame.raw_stop_dist_pct.abs() + cfg["screen"]["slippage_pct"]).clip(lower=.02)
        result["with_sizing" if rescale else "allocation_only"] = {
            "weight_pct": round(meta["total_alloc_pct"], 3), "risk_pct": round(risk.sum(), 8),
            "risk_cap_pct": meta["risk_cap_pct"],
            "changed_weights": int((out.portfolio_alloc_pct != watch.portfolio_alloc_pct).sum()),
            "positive": int(out.portfolio_alloc_pct.gt(0).sum()), "candidates": len(out),
        }
    return result


def quotes(path, published):
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    events = {}
    sensitivity = {"gain_1.7pct": set(), "no_prior_8pct_gate": set()}
    active = 0
    for i in range(39, len(frame)):
        window = frame.iloc[max(0, i - 59):i + 1]
        for policy, gain, decline in (("gain_1.7pct", .017, .08), ("no_prior_8pct_gate", .0125, 0.)):
            found, date = engine._detect_ftd(window, gain, decline)
            if found:
                sensitivity[policy].add(next(d for d in window.index if d.strftime("%m/%d") == date))
        detected, label = engine._detect_ftd(window)
        if detected:
            active += 1
            candidate = next(day for day in window.index if day.strftime("%m/%d") == label)
            events.setdefault(candidate, i)
    matured = [(day, i) for day, i in events.items() if i + 10 < len(frame)]
    track = {"QQQ": "NASDAQ100", "SPY": "SP500", "^KS11": "KOSPI", "^KQ11": "KOSDAQ"}.get(path.stem)
    compare = {}
    for _, summary in published:
        if track is None:
            break
        rec = summary["regimes"][track]
        date = rec["as_of"]
        if date in compare or pd.Timestamp(date) not in frame.index:
            continue
        # Only use Breadth actually published for this date. Yahoo data is a later vintage.
        b = {k: rec[k] for k in ("breadth_valid", "pct_above_50dma") if k in rec}
        r = engine.calc_market_regime(frame.loc[:date], breadth_info=b)
        compare[date] = [r["regime"], r["distribution_days_25d"]]
    return {
        "csv_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": len(frame), "dates": [str(frame.index[0].date()), str(frame.index[-1].date())],
        "invalid_volume": int((frame.Volume.isna() | frame.Volume.le(0)).sum()),
        "daily_volatility_pct": round(frame.Close.pct_change().std() * 100, 2),
        "active_observations": active,
        "alternative_event_counts": {k: len(v) for k, v in sensitivity.items()},
        "candidate_first_seen": {str(d.date()): str(frame.index[i].date()) for d, i in events.items()},
        "ten_session_outcomes": len(matured),
        "low_breaches": sum(bool(frame.Low.iloc[i+1:i+11].min() < frame.at[d, "Low"]*.99) for d, i in matured),
        "median_forward_10_pct": round(float(np.median([100*(frame.Close.iloc[i+10]/frame.Close.iloc[i]-1)
                                                        for _, i in matured])), 2) if matured else None,
        "regime_on_published_breadth": compare,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("published", type=Path)
    parser.add_argument("quotes", type=Path)
    args = parser.parse_args()
    pubs = list(read_publications(args.published))
    cfg = engine.load_config()
    result = {"budgets": {day.name: allocations(day, summary, cfg) for day, summary in pubs},
              "benchmarks": {path.stem: quotes(path, pubs) for path in sorted(args.quotes.glob("*.csv"))}}
    print(json.dumps(result, indent=2, ensure_ascii=False))

