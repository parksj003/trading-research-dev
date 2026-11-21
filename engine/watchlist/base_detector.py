"""Causal, cache-only Base Count observations (Phase 2, not trading signals).

No engine imports, provider calls, environment reads or automatic file writes.
All pattern thresholds are REQUIRED configuration: there are no market defaults.
Full and incremental replay share the same chronological transition function.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import copy
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import fmean
import tempfile

import exchange_calendars as xc
import pandas as pd

if __package__:
    from .session_calendar import get_calendar, calendar_policy
else:
    from session_calendar import get_calendar, calendar_policy

ALGORITHM_VERSION = "base-observation-0.1"
CHECKPOINT_SCHEMA = "base-checkpoint-1"
CALENDAR_POLICY = calendar_policy("KR")  # US keeps its previous policy via calendar_policy(market).
STATES = frozenset(("UNKNOWN", "NO_BASE", "BASE_FORMING", "BASE_CONFIRMED",
                    "BREAKOUT_PENDING", "BREAKOUT_CONFIRMED", "BASE_FAILED", "TREND_RESET"))
CSAT = frozenset(("2021-11-18", "2022-11-17", "2023-11-16", "2024-11-14", "2025-11-13", "2026-11-19"))
REQUIRED = ("Open", "High", "Low", "Close", "Volume", "_raw_open_valid", "_raw_ohlc_valid", "_session_confirmed")


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _truth(value):
    return str(value).lower() in ("true", "1", "1.0")


class StateConflict(ValueError):
    """State lineage, checksum or compare-and-swap validation failed."""


@dataclass(frozen=True)
class DetectorConfig:
    min_length: int
    max_length: int
    min_depth: float
    max_depth: float
    stability_window: int
    max_stability_range: float
    pullback_atr: float
    separation_atr: float
    pivot_buffer_atr: float
    failure_buffer_atr: float
    breakout_volume_ratio: float
    hold_sessions: int
    trend_sessions: int
    reset_sessions: int

    def __post_init__(self):
        for key in ("min_length", "max_length", "stability_window", "hold_sessions", "trend_sessions", "reset_sessions"):
            v = getattr(self, key)
            if type(v) is not int or v < 1:
                raise ValueError(f"{key}: positive integer required")
        for f in fields(self):
            v = getattr(self, f.name)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                raise ValueError(f"{f.name}: finite numeric value required")
        if not 0 < self.min_depth < self.max_depth < 1:
            raise ValueError("depth fractions must satisfy 0 < min < max < 1")
        if not self.stability_window <= self.min_length <= self.max_length:
            raise ValueError("stability_window <= min_length <= max_length required")
        for key in ("max_stability_range", "pullback_atr", "separation_atr", "breakout_volume_ratio"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.pivot_buffer_atr < 0 or self.failure_buffer_atr < 0:
            raise ValueError("buffers must be nonnegative")

    @property
    def signature(self):
        return _digest(asdict(self))


@dataclass(frozen=True)
class Snapshot:
    market: str
    ticker: str
    as_of: str | None
    status: str
    quality: str
    reason: str
    completed_base_count: int | None
    current_base_ordinal: int | None
    cycle_id: str | None
    base_id: str | None
    base_on_base: bool
    base_start_date: str | None
    base_first_observed_date: str | None
    base_confirmed_date: str | None
    base_breakout_date: str | None
    base_breakout_confirmed_date: str | None
    base_end_date: str | None
    base_length_days: int | None
    base_depth_pct: float | None
    base_structural_pivot: float | None
    geometry_revision: int | None
    algorithm_version: str
    config_hash: str
    price_basis: str
    data_revision: str
    recorded_at: str
    mode: str

    def logical(self):
        return {k: v for k, v in asdict(self).items() if k not in ("recorded_at", "mode")}


@dataclass(frozen=True)
class Event:
    event_id: str
    date: str
    kind: str
    cycle_id: str | None
    base_id: str | None
    attempt_id: str | None
    ordinal: int | None
    completed_base_count: int | None
    reason: str
    pivot: float | None
    recorded_at: str
    mode: str

    def logical(self):
        return {k: v for k, v in asdict(self).items() if k not in ("recorded_at", "mode")}


@dataclass(frozen=True)
class Batch:
    observations: tuple[Snapshot, ...]
    events: tuple[Event, ...]
    latest: Snapshot | None
    issue: str | None = None


class BaseDetector:
    """One instrument and one explicit price/configuration lineage.

    update expects the available input window INCLUDING the last processed bar.
    Removed old rows are allowed; changed old rows require a new data_revision
    and an explicit rebuild. Missing bars are never interpolated or skipped.
    ``REPLAY`` is retrospective; it never claims historical publication evidence.
    ``LIVE`` marks only the final bar of a supplied warm-up window as live.
    """

    def __init__(self, config: DetectorConfig, *, market: str, ticker: str,
                 price_basis: str, data_revision: str):
        if market not in ("KR", "US"):
            raise ValueError("market must be KR or US")
        if not isinstance(config, DetectorConfig):
            raise TypeError("explicit DetectorConfig required")
        if not all(isinstance(x, str) and x.strip() for x in (ticker, price_basis, data_revision)):
            raise ValueError("instrument, price_basis and data_revision required")
        self.config = config
        self.market = market
        self.ticker = ticker.strip().zfill(6) if market == "KR" else ticker.strip().upper()
        self.price_basis = price_basis
        self.data_revision = data_revision
        self.calendar = get_calendar(market)
        self._context = dict(algorithm=ALGORITHM_VERSION, config=config.signature, market=market,
                             ticker=self.ticker, price_basis=price_basis, data_revision=data_revision,
                             calendar_library=xc.__version__, calendar_policy=calendar_policy(market))
        self._state = dict(last=None, recent=[], seen={}, phase="NO_BASE", cycle=None,
                           anchored=False, completed=0, trend_streak=0, down_streak=0,
                           reset_date=None, awaiting_recovery=False, run_peak=None,
                           run_peak_date=None, run_atr=None, floor=None, base=None,
                           last_completed=None, parent=None, event_ids=[], latest=None)

    def _event(self, events, kind, date, reason, recorded_at, mode):
        s = self._state; b = s["base"] or {}
        attempt = f"{b['id']}:{b['attempt']}" if b.get("attempt") else None
        key = _digest([self._context, s["cycle"], b.get("id"), date, kind, attempt])
        if key in s["event_ids"]:
            return
        s["event_ids"].append(key)
        events.append(Event(key, date, kind, s["cycle"], b.get("id"), attempt,
                            b.get("ordinal"), s["completed"] if s["anchored"] else None,
                            reason, b.get("pivot"), recorded_at, mode))

    def _snapshot(self, date, reason, recorded_at, mode, *, invalid=False):
        s = self._state; b = s["base"] or {}
        quality = ("UNKNOWN" if invalid else "HISTORY_INSUFFICIENT" if not s["anchored"]
                   else "ESTIMATED" if b and not b["confirmed_date"] else "CONFIRMED")
        depth = (1 - b["low"] / b["high"]) * 100 if b else None
        return Snapshot(self.market, self.ticker, date, "UNKNOWN" if invalid else s["phase"],
            quality, reason, None if invalid or not s["anchored"] else s["completed"],
            None if invalid else b.get("ordinal"), s["cycle"], b.get("id"),
            bool(b.get("base_on_base")), b.get("start"), b.get("first_observed"),
            b.get("confirmed_date"), b.get("breakout_date"), b.get("completed_date"),
            b.get("end_date"), b.get("length"), depth, b.get("pivot"), b.get("geometry_revision"),
            ALGORITHM_VERSION, self.config.signature, self.price_basis, self.data_revision,
            recorded_at, mode)

    def _new_cycle(self, bar, anchored, events, recorded_at, mode):
        s = self._state; date = bar["date"]
        s.update(cycle=_digest([self._context, s["reset_date"], date]), anchored=anchored,
                 completed=0, base=None, parent=None, last_completed=None, phase="NO_BASE",
                 awaiting_recovery=False, floor=bar["Low"], run_peak=bar["High"],
                 run_peak_date=date, run_atr=self._metrics()["atr"])
        self._event(events, "CYCLE_START", date, "OBSERVED_RECOVERY" if anchored else "LEFT_CENSORED",
                    recorded_at, mode)

    def _metrics(self):
        r = self._state["recent"]; closes = [x["Close"] for x in r]
        atr = fmean(x["tr"] for x in r[-14:]) if len(r) >= 14 else None
        good = down = False
        if len(r) >= 220:
            a50, a150, a200 = (fmean(closes[-n:]) for n in (50, 150, 200))
            previous = fmean(closes[-220:-20])
            good = closes[-1] > a50 > a150 > a200 and a200 >= previous
            down = closes[-1] < a200 and a200 < previous
        volume = fmean(x["Volume"] for x in r[-51:-1]) if len(r) >= 51 else None
        return dict(atr=atr, good=good, down=down, volume=volume)

    def _candidate(self, bar, parent, events, recorded_at, mode):
        s = self._state; date = bar["date"]
        start = s["run_peak_date"]
        length = len(self.calendar.sessions_in_range(start, date))
        s["parent"] = copy.deepcopy(parent)
        s["base"] = dict(id=_digest([self._context, s["cycle"], date, parent["id"] if parent else None]),
            start=start, first_observed=date, high=s["run_peak"], low=bar["Low"],
            length=length, anchor_atr=s["run_atr"], pivot=None,
            ordinal=s["completed"] + 1 if s["anchored"] else None, confirmed_date=None,
            breakout_date=None, completed_date=None, end_date=None, attempt=0, hold=0,
            completed=False, failure=None, base_on_base=False, geometry_revision=0)
        s["phase"] = "BASE_FORMING"
        self._event(events, "FORMING", date, "PULLBACK_RECOGNIZED", recorded_at, mode)

    def _fail(self, reason, date, events, recorded_at, mode):
        s = self._state; b = s["base"]
        s["phase"] = "BASE_FAILED"; b["failure"] = reason; b["end_date"] = date
        self._event(events, "FAILED", date, reason, recorded_at, mode)

    def _advance(self, bar, events, recorded_at, mode):
        s = self._state; c = self.config; date = bar["date"]
        previous_close = s["recent"][-1]["Close"] if s["recent"] else bar["Close"]
        bar = dict(bar, tr=max(bar["High"]-bar["Low"], abs(bar["High"]-previous_close), abs(bar["Low"]-previous_close)))
        s["recent"].append(bar)
        s["recent"] = s["recent"][-max(220, c.stability_window):]
        m = self._metrics(); atr = m["atr"]
        s["trend_streak"] = s["trend_streak"] + 1 if m["good"] else 0
        s["down_streak"] = s["down_streak"] + 1 if m["down"] else 0
        if s["cycle"] and not s["awaiting_recovery"] and atr is not None and s["floor"] is not None:
            if s["down_streak"] >= c.reset_sessions and bar["Close"] < s["floor"]-c.failure_buffer_atr*atr:
                s.update(phase="TREND_RESET", reset_date=date, awaiting_recovery=True)
                if s["base"]: s["base"]["end_date"] = date
                self._event(events, "RESET", date, "PERSISTENT_DOWNTREND_AND_STRUCTURE_BREAK", recorded_at, mode)
                return "CYCLE_CLOSED"
        if s["awaiting_recovery"]:
            if s["trend_streak"] >= c.trend_sessions:
                self._new_cycle(bar, True, events, recorded_at, mode)
                return "NEW_ANCHORED_CYCLE"
            return "AWAITING_RECOVERY"
        if s["cycle"] is None:
            if s["trend_streak"] >= c.trend_sessions:
                self._new_cycle(bar, False, events, recorded_at, mode)
                return "CYCLE_START_UNKNOWN"
            return "TREND_WARMUP"
        if bar["High"] > s["run_peak"]:
            s.update(run_peak=bar["High"], run_peak_date=date, run_atr=atr)
        b = s["base"]; phase = s["phase"]
        if phase == "NO_BASE":
            if atr and s["run_atr"] and s["run_peak"]-bar["Close"] >= c.pullback_atr*s["run_atr"]:
                parent = s["last_completed"]
                if not parent or self._independent(bar["Low"], parent):
                    self._candidate(bar, parent, events, recorded_at, mode)
            return "PRICE_OBSERVATION"
        if b is None:
            raise StateConflict("active phase without base")
        buffer = c.failure_buffer_atr*b["anchor_atr"]
        age = len(self.calendar.sessions_in_range(b["start"], date))
        if phase in ("BASE_FORMING", "BASE_CONFIRMED", "BREAKOUT_PENDING"):
            b["length"] = age
        if phase != "BASE_FAILED" and bar["Close"] < b["low"]-buffer:
            # Forming lows are allowed to develop until max_depth is exceeded.
            if phase != "BASE_FORMING":
                self._fail("STRUCTURAL_INVALIDATION", date, events, recorded_at, mode)
                return "STRUCTURE_FAILED"
        if phase == "BASE_FORMING":
            b["low"] = min(b["low"], bar["Low"]); b["length"] = age
            if s["parent"] and not self._independent(b["low"], s["parent"]):
                # A provisional new range merged back into its parent. Preserve
                # the parent's identity and completed flag, never count it twice.
                old = copy.deepcopy(s["parent"]); old.update(base_on_base=True, failure=None,
                    end_date=None, geometry_revision=old["geometry_revision"]+1)
                s["base"] = old; s["parent"] = None
                s["phase"] = "BREAKOUT_CONFIRMED"
                self._event(events, "MERGED", date, "BASE_ON_BASE", recorded_at, mode)
                if bar["Close"] < old["pivot"]-c.failure_buffer_atr*old["anchor_atr"]:
                    self._fail("BREAKOUT_RETURN", date, events, recorded_at, mode)
                return "BASE_ON_BASE"
            depth = 1-b["low"]/b["high"]
            if depth > c.max_depth or age > c.max_length:
                self._fail("MAX_DEPTH" if depth > c.max_depth else "EXPIRED", date, events, recorded_at, mode)
                return "STRUCTURE_FAILED"
            if bar["Close"] > b["high"]:
                # An immature rally is not a retrospectively confirmed base.
                self._fail("IMMATURE_RALLY", date, events, recorded_at, mode)
                return "NO_BACKDATED_BREAKOUT"
            recent = s["recent"][-c.stability_window:]
            stable = max(x["High"] for x in recent)/min(x["Low"] for x in recent)-1 <= c.max_stability_range
            if age >= c.min_length and depth >= c.min_depth and stable:
                b["pivot"] = b["high"]
                if b["confirmed_date"] is None: b["confirmed_date"] = date
                s["phase"] = "BASE_CONFIRMED"; s["floor"] = b["low"]
                self._event(events, "READY", date, "STRUCTURE_CONFIRMED", recorded_at, mode)
            return "FORMING"
        if phase == "BASE_CONFIRMED":
            if age > c.max_length:
                self._fail("EXPIRED", date, events, recorded_at, mode)
            elif (previous_close <= b["pivot"]+c.pivot_buffer_atr*b["anchor_atr"] < bar["Close"]
                  and m["volume"] and bar["Volume"] >= m["volume"]*c.breakout_volume_ratio):
                b.update(attempt=b["attempt"]+1, hold=0, breakout_date=date, failure=None, end_date=None)
                s["phase"] = "BREAKOUT_PENDING"
                self._event(events, "BREAKOUT", date, "CLOSE_CROSS_AND_VOLUME", recorded_at, mode)
            return "READY"
        if phase == "BREAKOUT_PENDING":
            if bar["Close"] < b["pivot"]-buffer:
                self._fail("BREAKOUT_RETURN", date, events, recorded_at, mode)
            else:
                b["hold"] += 1
                if b["hold"] >= c.hold_sessions:
                    s["phase"] = "BREAKOUT_CONFIRMED"
                    if not b["completed"]:
                        s["completed"] += 1; b.update(completed=True, completed_date=date, end_date=date)
                        self._event(events, "COMPLETE", date, "FOLLOW_THROUGH_OBSERVED", recorded_at, mode)
                    else:
                        self._event(events, "RECONFIRMED", date, "SAME_BASE_RETEST", recorded_at, mode)
                    s["last_completed"] = copy.deepcopy(b)
            return "FOLLOW_THROUGH"
        if phase == "BREAKOUT_CONFIRMED":
            if s["run_atr"] and s["run_peak"]-bar["Close"] >= c.pullback_atr*s["run_atr"]:
                if self._independent(bar["Low"], b):
                    self._candidate(bar, b, events, recorded_at, mode)
                else:
                    if not b["base_on_base"]:
                        b.update(base_on_base=True, geometry_revision=b["geometry_revision"]+1)
                        self._event(events, "EXTENDED", date, "NO_STRUCTURAL_SEPARATION", recorded_at, mode)
                    if bar["Close"] < b["pivot"]-buffer:
                        self._fail("BREAKOUT_RETURN", date, events, recorded_at, mode)
            return "COMPLETED_BASE_OBSERVATION"
        if phase == "BASE_FAILED":
            if b["failure"] == "BREAKOUT_RETURN" and b["low"]-buffer <= bar["Close"] <= b["pivot"]+buffer:
                b.update(end_date=None, geometry_revision=b["geometry_revision"]+1)
                s["phase"] = "BASE_CONFIRMED"
                self._event(events, "RETEST", date, "ORIGINAL_RANGE_INTACT", recorded_at, mode)
            elif m["good"] and bar["Close"] > b["high"]+buffer:
                s.update(base=None, parent=None, phase="NO_BASE", run_peak=bar["High"], run_peak_date=date, run_atr=atr)
            return "FAILED_BASE_OBSERVATION"
        raise StateConflict("unexpected state transition")

    def _independent(self, low, parent):
        return (low > parent["pivot"]+self.config.pivot_buffer_atr*parent["anchor_atr"]
                and self._state["run_peak"]-parent["pivot"] >= self.config.separation_atr*parent["anchor_atr"])

    def update(self, prices: pd.DataFrame, *, observed_at, mode: str) -> Batch:
        if mode not in ("LIVE", "REPLAY"):
            raise ValueError("explicit LIVE or REPLAY mode required")
        stamp = pd.Timestamp(observed_at)
        if pd.isna(stamp) or stamp.tzinfo is None:
            raise ValueError("timezone-aware observed_at required")
        recorded = stamp.isoformat(); observations = []; events = []
        def invalid(reason, date=None):
            snap = self._snapshot(date, reason, recorded, mode, invalid=True)
            return Batch(tuple(observations), tuple(events), snap, reason)
        if not isinstance(prices, pd.DataFrame) or not set(REQUIRED).issubset(prices):
            return invalid("REQUIRED_PRICE_PROVENANCE_MISSING")
        if prices.empty:
            return invalid("PRICE_WINDOW_EMPTY")
        if not isinstance(prices.index, pd.DatetimeIndex) or prices.index.tz is not None or prices.index.hasnans:
            return invalid("CANONICAL_SESSION_INDEX_REQUIRED")
        if not prices.index.equals(prices.index.normalize()) or not prices.index.is_monotonic_increasing:
            return invalid("ORDERED_MIDNIGHT_SESSIONS_REQUIRED")
        last = self._state["last"]
        if last and pd.Timestamp(last) not in prices.index:
            return invalid("CHECKPOINT_OVERLAP_MISSING", str(prices.index[-1].date()))
        duplicates = set(prices.index[prices.index.duplicated(keep=False)])
        for date, series in prices.iterrows():
            name = date.strftime("%Y-%m-%d")
            if date in duplicates:
                return invalid("DUPLICATE_SESSION", name)
            if date < self.calendar.first_session or date > self.calendar.last_session:
                return invalid("CALENDAR_COVERAGE_UNKNOWN", name)
            if not self.calendar.is_session(date):
                return invalid("NON_EXCHANGE_SESSION", name)
            if self.market == "KR" and date.year > 2026:
                return invalid("UNREVIEWED_KR_SPECIAL_SESSION_YEAR", name)
            ready = self.calendar.session_close(date)+pd.Timedelta(minutes=5)
            if self.market == "KR" and name in CSAT:
                ready = pd.Timestamp(name, tz="Asia/Seoul")+pd.Timedelta(hours=16, minutes=35)
            if stamp < ready:
                return invalid("SESSION_NOT_YET_FINAL", name)
            try:
                values = {k: float(series[k]) for k in ("Open", "High", "Low", "Close", "Volume")}
                if (not all(math.isfinite(v) and v > 0 for v in values.values())
                    or values["High"] < max(values["Open"], values["Close"], values["Low"])
                    or values["Low"] > min(values["Open"], values["Close"], values["High"])):
                    return invalid("INVALID_OR_HALTED_OHLCV", name)
                if not all(_truth(series[k]) for k in REQUIRED[5:]) or _truth(series.get("IsHalt", False)):
                    return invalid("UNVERIFIED_OR_HALTED_BAR", name)
                split = float(series.get("Stock Splits", 0.))
                if not math.isfinite(split) or split != 0:
                    return invalid("SPLIT_REQUIRES_NEW_PRICE_LINEAGE", name)
            except (ValueError, TypeError, OverflowError):
                return invalid("INVALID_OHLCV_VALUE", name)
            row = dict(values, date=name)
            fingerprint = _digest(row)
            if last and name <= last:
                if self._state["seen"].get(name) != fingerprint:
                    return invalid("HISTORY_REVISION_REQUIRES_REBUILD", name)
                continue
            cursor = self._state["last"]
            if cursor and self.calendar.next_session(pd.Timestamp(cursor)) != date:
                return invalid("MISSING_EXCHANGE_SESSION", name)
            effective_mode = "REPLAY" if mode == "LIVE" and date != prices.index[-1] else mode
            reason = self._advance(row, events, recorded, effective_mode)
            self._state["last"] = name; self._state["seen"][name] = fingerprint
            snapshot = self._snapshot(name, reason, recorded, effective_mode)
            self._state["latest"] = asdict(snapshot)
            observations.append(snapshot)
        latest = Snapshot(**self._state["latest"]) if self._state["latest"] else None
        return Batch(tuple(observations), tuple(events), latest)

    def checkpoint(self):
        """Return a detached, checksummed JSON envelope; never a pickle."""
        payload = dict(schema=CHECKPOINT_SCHEMA, context=self._context, config=asdict(self.config), state=self._state)
        return json.loads(_json(dict(payload=payload, sha256=_digest(payload))))

    @classmethod
    def restore(cls, envelope, *, config, market, ticker, price_basis, data_revision):
        detector = cls(config, market=market, ticker=ticker, price_basis=price_basis, data_revision=data_revision)
        try:
            payload = envelope["payload"]
            if envelope["sha256"] != _digest(payload) or payload["schema"] != CHECKPOINT_SCHEMA:
                raise StateConflict("checkpoint checksum/schema mismatch")
            if payload["context"] != detector._context or payload["config"] != asdict(config):
                raise StateConflict("checkpoint lineage/configuration mismatch")
            state = payload["state"]
            if set(state) != set(detector._state) or state["phase"] not in STATES-{"UNKNOWN"}:
                raise StateConflict("checkpoint state shape mismatch")
            if type(state["completed"]) is not int or state["completed"] < 0:
                raise StateConflict("invalid completed count")
            if any(type(state[k]) is not bool for k in ("anchored", "awaiting_recovery")):
                raise StateConflict("invalid cycle flags")
            if any(type(state[k]) is not int or state[k] < 0 for k in ("trend_streak", "down_streak")):
                raise StateConflict("invalid trend persistence")
            if len(set(state["event_ids"])) != len(state["event_ids"]):
                raise StateConflict("duplicate event identities")
            if state["last"]:
                if not state["recent"] or state["recent"][-1]["date"] != state["last"] or state["last"] not in state["seen"]:
                    raise StateConflict("checkpoint cursor/history mismatch")
                snap = Snapshot(**state["latest"])
                if snap.as_of != state["last"] or snap.status != state["phase"]:
                    raise StateConflict("checkpoint snapshot mismatch")
                expected = detector._state
                detector._state = state
                computed = detector._snapshot(state["last"], snap.reason, snap.recorded_at, snap.mode)
                detector._state = expected
                if computed != snap:
                    raise StateConflict("checkpoint snapshot/count/geometry mismatch")
                dates = [row["date"] for row in state["recent"]]
                if dates != sorted(set(dates)) or len(dates) > max(220, config.stability_window):
                    raise StateConflict("checkpoint recent history ordering mismatch")
                for row in state["recent"]:
                    values = {k: row[k] for k in ("date", "Open", "High", "Low", "Close", "Volume")}
                    if state["seen"].get(row["date"]) != _digest(values):
                        raise StateConflict("checkpoint recent history fingerprint mismatch")
                    if not math.isfinite(row["tr"]) or row["tr"] < 0:
                        raise StateConflict("invalid true range")
            elif state != detector._state:
                raise StateConflict("nonempty state without cursor")
            if state["phase"].startswith(("BASE_", "BREAKOUT_")) and state["base"] is None:
                raise StateConflict("checkpoint base missing")
            b = state["base"]
            if b:
                if type(b["completed"]) is not bool or not b["low"] <= b["high"] or b["anchor_atr"] <= 0:
                    raise StateConflict("invalid base geometry/completion")
                ordinal = state["completed"] if b["completed"] else state["completed"] + 1
                if b["ordinal"] != (ordinal if state["anchored"] else None):
                    raise StateConflict("base ordinal/count mismatch")
                if state["phase"] == "BREAKOUT_CONFIRMED" and not b["completed"]:
                    raise StateConflict("uncompleted confirmed breakout")
                if state["phase"] in ("BASE_CONFIRMED", "BREAKOUT_PENDING", "BREAKOUT_CONFIRMED"):
                    if b["pivot"] is None or b["confirmed_date"] is None:
                        raise StateConflict("confirmed structure missing pivot/date")
            detector._state = copy.deepcopy(state)
            return detector
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, StateConflict): raise
            raise StateConflict("invalid checkpoint") from exc

    def save(self, path, *, expected_digest=None):
        """Explicit local checkpoint write with lock, CAS and atomic replacement.

        Existing files require their exact previous digest; no silent overwrite.
        A stale lock is never automatically stolen. No operational path is chosen.
        """
        path = Path(path); lock = path.with_name(path.name+".lock"); temporary = None
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise StateConflict("checkpoint writer locked") from exc
        try:
            os.close(fd)
            if path.exists():
                try:
                    old = json.loads(path.read_text())
                except (ValueError, TypeError) as exc:
                    raise StateConflict("existing checkpoint is unreadable") from exc
                if not isinstance(old, dict) or expected_digest is None or old.get("sha256") != expected_digest:
                    raise StateConflict("checkpoint compare-and-swap failed")
                self.restore(old, config=self.config, market=self.market, ticker=self.ticker,
                             price_basis=self.price_basis, data_revision=self.data_revision)
            elif expected_digest is not None:
                raise StateConflict("expected checkpoint is missing")
            envelope = self.checkpoint()
            fd, temporary = tempfile.mkstemp(prefix=path.name+".", suffix=".tmp", dir=path.parent)
            with os.fdopen(fd, "w") as stream:
                stream.write(_json(envelope)); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return envelope["sha256"]
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)
            lock.unlink()

    @classmethod
    def load(cls, path, **context):
        path = Path(path)
        if path.stat().st_size > 64_000_000:
            raise StateConflict("checkpoint size limit")
        try:
            return cls.restore(json.loads(path.read_text()), **context)
        except (ValueError, TypeError) as exc:
            raise StateConflict("checkpoint could not be loaded") from exc

