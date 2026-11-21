"""Read-only inspection of the exact trusted engine cache restored by the workflow.

Never infer freshness from mtime, an index membership, or a restored file's name.
Pickles are executable: callers must supply only the trusted main-run cache.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import isfinite
from pathlib import Path
import re

import pandas as pd

from .manual_eod_contract import MAX_BARS, aware
from .manual_eod_sessions import CSAT, confirmed, latest_session

BASES = {'KR': 'pykrx:date:adjusted=True', 'US': 'yfinance:auto_adjust=True'}
IDENTITY_POLICY = 'yahoo-chart-meta-v1'
IDENTITY_TTL = timedelta(days=7)
# Do not chart a partial adjusted-price run without a 200-session moving average.
MIN_VERIFIED_BASIS_BARS = 200


def core_identity(spec):
    code = spec['symbol'].split(':')[1]
    if spec['market'] == 'KR' and re.fullmatch(r'\d{6}', code):
        return 'KR', code
    if spec['market'] == 'US' and re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,30}', code):
        return 'US', code.replace('.', '-')
    return None


def valid_identity(spec, evidence, now):
    """Bound cached metadata to an exact listing/type/currency and a short TTL."""
    if not isinstance(evidence, dict) or evidence.get('evidence') != IDENTITY_POLICY:
        return None
    try:
        stamp = aware(evidence['verified_at'])
        if not stamp <= now <= stamp + IDENTITY_TTL:
            return None
        if (evidence.get('provider_symbol') not in spec['candidates']
                or evidence.get('provider_exchange') not in spec['provider_exchanges']
                or evidence.get('currency') != spec['currency']
                or evidence.get('timezone') not in spec['provider_timezones']
                or evidence.get('instrument_type') != ('ETF' if spec['asset_type'] == 'etf' else 'EQUITY')):
            return None
        return dict(evidence)
    except (ValueError, KeyError, TypeError):
        return None


def attest_identity(spec, evidence, now):
    result = dict(evidence, evidence=IDENTITY_POLICY, verified_at=now.isoformat())
    if valid_identity(spec, result, now) is None:
        raise ValueError('unverified provider identity')
    return result


@dataclass
class Inspection:
    reason: str
    classification: str
    checks: dict = field(default_factory=dict)
    rows: list | None = None
    basis: str | None = None
    identity: dict | None = None
    last_session: str | None = None
    excluded_unfinished: int = 0
    excluded_unverified: int = 0
    excluded_basis: int = 0
    basis_evidence: dict = field(default_factory=dict)

    def diagnostic(self):
        return dict(reason=self.reason, classification=self.classification, checks=self.checks,
                    last_session=self.last_session, excluded_unfinished=self.excluded_unfinished,
                    excluded_unverified=self.excluded_unverified, excluded_basis=self.excluded_basis,
                    basis_evidence=self.basis_evidence)


def inspect_core_cache(data_dir, spec, cutoff, *, evidence=None, now=None):
    checks = {k: None for k in ('exists', 'fresh', 'raw_ohlcv', 'sessions', 'basis', 'identity')}
    out = Inspection('CACHE_NOT_INSPECTED', 'not_inspected', checks)
    key = core_identity(spec)
    if key is None:
        out.reason, out.classification = 'CACHE_NOT_SUPPORTED', 'other'
        return out
    if data_dir is None:
        return out
    market, ticker = key
    out.basis = BASES[market]
    path = Path(data_dir) / ('kr_history_cache' if market == 'KR' else 'us_history_cache') / (ticker + '.pkl')
    checks['exists'] = path.is_file()
    if not checks['exists']:
        out.reason, out.classification = 'CACHE_MISSING', 'no_cache'
        return out
    try:
        target = latest_session(market, cutoff)
        frame = pd.read_pickle(path)
        required = {'Open', 'High', 'Low', 'Close', '_raw_ohlc_valid', '_session_confirmed', '_session_finality_version', '_base_price_basis'}
        if not isinstance(frame, pd.DataFrame) or not required.issubset(frame.columns):
            out.reason, out.classification = 'CACHE_REQUIRED_FIELDS_MISSING', 'other'
            return out
        dates = pd.DatetimeIndex(frame.index)
        if dates.hasnans:
            raise ValueError('invalid dates')
        if dates.tz is not None:
            dates = dates.tz_convert(spec['exchange_timezone']).tz_localize(None)
        frame = frame.copy()
        frame.index = dates.normalize()
        if frame.index.has_duplicates:
            raise ValueError('duplicate sessions')
        out.excluded_unfinished = int((frame.index > pd.Timestamp(target)).sum())
        frame = frame.loc[frame.index <= pd.Timestamp(target)].sort_index().tail(MAX_BARS)
        if frame.empty:
            out.reason, out.classification = 'CACHE_NO_FINAL_BARS', 'stale'
            checks['fresh'] = False
            return out
        yes = lambda col: frame[col].astype(str).str.lower().isin(('true', '1', '1.0'))
        versions = pd.to_numeric(frame.get('_session_finality_version', pd.Series(0, index=frame.index)), errors='coerce')
        acquired = yes('_session_confirmed') & versions.ge(1)
        if market == 'KR':
            acquired &= ~frame.index.isin(pd.to_datetime(CSAT)) | versions.ge(2).to_numpy()
        raw_valid = yes('_raw_ohlc_valid')
        eligible = acquired & raw_valid
        out.excluded_unverified = int((~eligible).sum())
        target_rejected = pd.Timestamp(target) in frame.index and not bool(eligible.loc[pd.Timestamp(target)])
        if not eligible.any():
            checks.update(fresh=False, sessions=bool(acquired.all()), raw_ohlcv=bool(raw_valid.all()))
            out.reason, out.classification = 'CACHE_PRICE_OR_SESSION_INVALID', 'other'
            return out
        # A historical suspension/unknown acquisition is a gap, never a filled
        # candle. Preserve the original per-row guard without rejecting unrelated
        # verified history. A rejected latest bar cannot make this cache current.
        frame = frame.loc[eligible]
        out.last_session = str(frame.index[-1].date())
        checks['fresh'] = out.last_session == target
        # Legacy engine histories may have mixed per-row provenance. Never
        # relabel old prices. A sufficiently long *contiguous trailing* run
        # carrying the exact adjusted basis may be displayed on its own.
        tags = frame['_base_price_basis']
        basis_ok = tags.eq(out.basis).fillna(False)
        missing_tag = tags.isna() | tags.astype(str).str.strip().eq('')
        # Missing tags are NOT evidence that the prices have the adjusted
        # basis. Report exact certified dates so source lineage can be reviewed
        # independently before any recovery. This is read-only diagnostic data:
        # no NaN filling, price patching, or weakened basis acceptance.
        different_tag=~basis_ok & ~missing_tag
        out.basis_evidence = dict(
            inspected=int(len(frame)),
            expected=int(basis_ok.sum()),
            missing=int(missing_tag.sum()),
            different=int(different_tag.sum()),
            missing_dates=[d.date().isoformat() for d in frame.index[missing_tag]][-16:],
            different_dates=[d.date().isoformat() for d in frame.index[different_tag]][-16:],
            last_session_basis_verified=bool(basis_ok.iloc[-1]),
        )
        if not bool(basis_ok.all()):
            suffix_size = 0
            for verified in reversed(basis_ok.tolist()):
                if not verified:
                    break
                suffix_size += 1
            if suffix_size >= MIN_VERIFIED_BASIS_BARS:
                out.excluded_basis = int(len(frame) - suffix_size)
                frame = frame.tail(suffix_size)
                basis_ok = basis_ok.tail(suffix_size)
        checks['basis'] = bool(basis_ok.all())
        checks['sessions'] = True
        checks['sessions'] &= all(confirmed(market, str(d.date()), cutoff) for d in frame.index)
        checks['raw_ohlcv'] = True
        records = []
        for stamp, record in frame.iterrows():
            values = [float(record[k]) for k in ('Open', 'High', 'Low', 'Close')]
            o, h, l, c = values
            valid = all(isfinite(v) and v > 0 for v in values) and h >= max(o, c) and l <= min(o, c) and h >= l
            v = record.get('Volume')
            v = float(v) if v is not None and pd.notna(v) else None
            checks['raw_ohlcv'] &= valid and (v is None or isfinite(v) and v >= 0)
            records.append(dict(d=str(stamp.date()), o=o, h=h, l=l, c=c, v=v))
        # The engine applies this basis tag only to its pykrx stock-by-date
        # endpoint (not indexes/bulk quotes). ETF identity needs explicit metadata.
        if market == 'KR' and spec['asset_type'] == 'stock' and checks['basis']:
            out.identity = dict(provider_symbol=ticker, provider_exchange='KRX', currency='KRW',
                                instrument_type='EQUITY', timezone='Asia/Seoul',
                                evidence='core_pykrx_stock_by_date_and_basis')
        else:
            out.identity = valid_identity(spec, evidence, now or cutoff)
        checks['identity'] = out.identity is not None
        if not checks['raw_ohlcv'] or not checks['sessions'] or target_rejected:
            out.reason, out.classification = 'CACHE_PRICE_OR_SESSION_INVALID', 'other'
        elif not checks['basis']:
            out.reason, out.classification = 'CACHE_ADJUSTMENT_MISMATCH', 'basis_mismatch'
        else:
            out.rows = records
            if not checks['fresh']:
                out.reason, out.classification = 'CACHE_BEHIND_FINAL_SESSION', 'stale'
            elif not checks['identity']:
                out.reason, out.classification = 'CACHE_IDENTITY_UNVERIFIED', 'identity_pending'
            else:
                out.reason, out.classification = 'CACHE_REUSABLE', 'reusable'
        return out
    except Exception:
        # No cache writes, provider calls, or raw exception/secret text on failure.
        out.rows = None
        out.reason, out.classification = 'CACHE_UNREADABLE_OR_INVALID', 'other'
        return out

