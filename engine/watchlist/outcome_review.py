"""Review fixed outcomes from pinned private publications and trusted cache copies."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import io
import json
import tempfile
from pathlib import Path

import pandas as pd

from . import analysis_report, trend_watchlist as engine

KEY = analysis_report.KEY
FACTS = ("status", "bars", "entry_date", "entry_price", "stop_price", "stop_price_basis",
         "entry_session_verified", "stop_exit_date", "stop_exit_reason", "stop_aware_ret_10d",
         "stop_aware_r_10d", "path_ohlc_complete_10d", "ret_5d", "ret_10d", "ret_20d",
         "ex_5d", "ex_10d", "ex_20d", "eval_date_5d", "eval_date_10d", "eval_date_20d",
         "calendar_complete_5d", "calendar_complete_10d", "calendar_complete_20d",
         "calculation_version", "path_policy", "price_prefix_sha256", "review_reason", "review_required", "signal_run_id", "signal_config_hash",
         "round_trip_cost_pct", "evaluated_as_of", "mfe_10d_pct", "mae_10d_pct", "stop_hit_10d")


def snapshot(base):
    raw_manifest = (base / "latest/manifest.json").read_bytes()
    manifest = json.loads(raw_manifest)
    report = json.loads((base / "analysis/latest.md").read_text().split('````json\n', 1)[1].rsplit('\n````', 1)[0])
    source = report["source"]
    if source.get("history_update_ok") is not True or any(
        not manifest.get(k) or source.get(k) != manifest[k] for k in ("run_id", "config_hash", "generated_at")
    ):
        raise ValueError("analysis and core publication differ")
    frames = []
    for name in ("signals.csv", "signal_outcomes.csv"):
        raw = (base / "history" / name).read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["history_sources"][name]["sha256"]:
            raise ValueError(f"history hash mismatch: {name}")
        frame = pd.read_csv(io.BytesIO(raw), dtype=str)
        if frame[KEY].isna().any().any() or frame.duplicated(KEY).any():
            raise ValueError(f"ambiguous identity: {name}")
        frame["market"] = frame.market.str.upper()
        frame["ticker"] = frame.ticker.str.upper().where(frame.market != "KR", frame.ticker.str.zfill(6))
        if frame.duplicated(KEY).any():
            raise ValueError(f"ambiguous normalized identity: {name}")
        frames.append(frame)
    return manifest, report, *frames, hashlib.sha256(raw_manifest).hexdigest()


def review(published, data, start, previous=None):
    manifest, report, signals, outcomes, manifest_hash = snapshot(published)
    cutoff = pd.Timestamp(manifest["generated_at"])
    prior = {}
    prior_hash = None
    if previous:
        older, _, _, old_rows, prior_hash = snapshot(previous)
        if pd.Timestamp(older["generated_at"]) >= cutoff:
            raise ValueError("previous publication must precede current")
        prior = old_rows.set_index(KEY).to_dict("index")
    current = outcomes.set_index(KEY).to_dict("index")
    cost = float(report["validation"]["policy"]["round_trip_cost_pct"])
    if not 0 <= cost < 100:
        raise ValueError("invalid published simulation cost")
    cfg = deepcopy(engine.load_config())
    cfg["screen"]["round_trip_cost_pct"] = cost / 100
    # Reuse only recorded publication evidence. Review has no network lookup.
    signals, _ = analysis_report.resolve_publications(signals, report.get("publication_evidence"), max_calls=0)
    calendars = {m: engine.get_calendar(m) for m in ("KR", "US")}
    selected = []
    end_by_track = manifest["as_of"]
    for row in signals.to_dict("records"):
        end = end_by_track.get(row["sub_market"])
        if not end:
            continue
        cal = calendars[row["market"]]
        date = pd.Timestamp(row.get("as_of") if pd.notna(row.get("as_of")) else row["first_seen"])
        if date <= pd.Timestamp(end) and cal.is_session(date) and cal.session_offset(date, 20) >= start:
            selected.append(row)
    dirs = {m: data / (m.lower() + "_history_cache") for m in calendars}
    proposals = {}
    if selected:
        # Deliberately omit old outcomes so sealed rows can also be compared.
        # Only this temporary directory is writable by the evaluator.
        with tempfile.TemporaryDirectory() as scratch:
            base = Path(scratch)
            (base / "history").mkdir()
            pd.DataFrame(selected).to_csv(base / "history/signals.csv", index=False)
            engine.update_signal_outcomes(base, cfg, price_dirs=dirs, cutoffs=manifest["as_of"], evaluated_at=cutoff)
            proposals = pd.read_csv(base / "history/signal_outcomes.csv", dtype=str).set_index(KEY).to_dict("index")
    records = []
    price_hashes = {}
    for row in selected:
        market, symbol = row["market"], row["ticker"]
        path = dirs[market] / ((symbol.zfill(6) if market == "KR" else symbol.upper()) + ".pkl")
        name = f"{market}/{path.name}"
        if name not in price_hashes:
            price_hashes[name] = engine._sha256_file(path) if path.is_file() else None
    for track, path in engine.benchmark_cache_files(cfg, dirs).items():
        price_hashes[f"{track}/{path.name}"] = engine._sha256_file(path) if path.is_file() else None
    def facts(row):
        return {k: row[k] for k in FACTS if pd.notna(row.get(k))}

    for signal in selected:
        key = tuple(signal[k] for k in KEY)
        old, new = current.get(key, {}), proposals.get(key, {})
        before = prior.get(key, {})
        published_changes = engine.fixed_conflicts(before, old)
        conflicts = engine.fixed_conflicts(old, new)
        cal = calendars[signal["market"]]
        asof = pd.Timestamp(signal.get("as_of") if pd.notna(signal.get("as_of")) else signal["first_seen"])
        entry = cal.next_session(asof)
        due = cal.sessions_in_range(asof, end_by_track[signal["sub_market"]])[1:21]
        observed = int(float(new.get("bars", 0)))
        unavailable = [k for k in conflicts if pd.isna(new.get(k))]
        conflicts = [k for k in conflicts if k not in unavailable]
        records.append({
            **dict(zip(KEY, key)), "as_of": str(asof.date()), "track": signal["sub_market"],
            "expected_entry": str(entry.date()), "sessions_due": len(due),
            "first_unresolved_session": str(due[observed].date()) if observed < len(due) else None,
            "source_signal_run_id": signal["run_id"] if pd.notna(signal.get("run_id")) else None,
            "published_at": signal["published_at"] if pd.notna(signal.get("published_at")) else None,
            "expected_entry_timing": analysis_report._availability({**signal, "entry_date": entry})[0],
            "previously_published_changes": {
                k: [before[k], old[k] if pd.notna(old.get(k)) else None] for k in published_changes
            },
            "stored": facts(old), "cache_proposal": facts(new),
            "unverifiable_fields": unavailable, "conflicting_fields": conflicts,
        })
    return {
        "source_run_id": manifest["run_id"], "manifest_sha256": manifest_hash,
        "previous_manifest_sha256": prior_hash, "history_sources": report["source"]["history_sources"],
        "market_cutoffs": manifest["as_of"], "price_files_sha256": price_hashes,
        "evaluated_at": cutoff.isoformat(), "from_session": str(start.date()), "round_trip_cost_pct": cost,
        "calculation_version": engine.CALCULATION_VERSION, "path_policy": engine.FIXED_PATH_POLICY,
        "records": records,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--published", required=True, type=Path)
    parser.add_argument("--previous", type=Path, help="last good pinned published directory")
    parser.add_argument("--data", required=True, type=Path, help="trusted core cache directory")
    parser.add_argument("--from-session", required=True, type=pd.Timestamp)
    args = parser.parse_args()
    result = review(args.published, args.data, args.from_session, args.previous)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
