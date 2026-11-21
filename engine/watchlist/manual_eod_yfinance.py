"""Bounded yfinance batch acquisition for the optional manual EOD publisher.

Use the SAME auto-adjusted OHLC basis as the tactical engine, while keeping
native listing/currency/asset-type checks and independently confirmed sessions.
The batch is opportunistic: failures are explicit, never a reason to infer a
ticker's identity or turn legacy core history into certified adjusted prices.
No core strategy cache writes and no account credentials.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
import math

import pandas as pd

from .manual_eod_contract import MAX_BARS
from .manual_eod_provider import Unavailable
from .manual_eod_sessions import confirmed

BASIS = 'yfinance:auto_adjust=True'
SOURCE = 'yfinance adjusted daily batch'
MAX_SYMBOLS = 16
BATCH_SIZE = 8


def _ticker_frame(raw, ticker, batch_size):
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        return None
    if isinstance(raw.columns, pd.MultiIndex):
        for level in range(raw.columns.nlevels):
            if ticker in raw.columns.get_level_values(level):
                frame = raw.xs(ticker, axis=1, level=level, drop_level=True).copy()
                if isinstance(frame.columns, pd.MultiIndex):
                    frame.columns = [
                        next((str(col) for col in label
                              if str(col) in ('Open','High','Low','Close','Volume')), '')
                        for label in frame.columns
                    ]
                return frame
        if batch_size > 1:
            return None
        return None
    return raw.copy() if batch_size == 1 else None


def _verified_identity(meta, spec):
    """Exact market-specific venue checks, not an inferred Yahoo ticker mapping."""
    fields = ('symbol','exchangeName','exchangeTimezoneName',
              'currency','instrumentType')
    if not isinstance(meta, dict) or any(not meta.get(field) for field in fields):
        # Absent yfinance history-metadata is not evidence that the listing
        # itself conflicts. The independent direct Yahoo chart meta may attest
        # it, without promoting partially specified yfinance information.
        raise Unavailable('YFINANCE_LISTING_METADATA_UNAVAILABLE')
    symbol = spec['candidates'][0]
    if meta.get('symbol') != symbol:
        raise Unavailable('YFINANCE_SYMBOL_IDENTITY_MISMATCH')
    required = (
        ('exchangeName', spec['provider_exchanges'], 'EXCHANGE'),
        ('exchangeTimezoneName', spec['provider_timezones'], 'TIMEZONE'),
        ('currency', (spec['currency'],), 'CURRENCY'),
        ('instrumentType', ('EQUITY' if spec['asset_type']=='stock' else 'ETF',), 'ASSET_TYPE'),
    )
    for key, allowed, label in required:
        if meta.get(key) not in allowed:
            raise Unavailable('YFINANCE_'+label+'_MISMATCH')
    return dict(provider_symbol=symbol, provider_exchange=meta['exchangeName'],
                currency=meta['currency'], instrument_type=meta['instrumentType'],
                timezone=meta['exchangeTimezoneName'])


def _final_bars(frame, spec, cutoff):
    if frame is None or frame.empty:
        raise Unavailable('YFINANCE_PRICE_MISSING')
    required = {'Open', 'High', 'Low', 'Close'}
    if not required.issubset(frame.columns):
        raise Unavailable('YFINANCE_OHLC_FIELDS_MISSING')
    frame = frame.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.hasnans or index.has_duplicates:
        raise Unavailable('YFINANCE_PRICE_DATE_INVALID')
    if index.tz is not None:
        index = index.tz_convert(spec['exchange_timezone']).tz_localize(None)
    index = index.normalize()
    if index.has_duplicates:
        raise Unavailable('YFINANCE_DUPLICATE_SESSIONS')
    frame.index = index
    frame = frame.sort_index()
    rows = []
    for stamp, observation in frame.iterrows():
        session = str(stamp.date())
        try:
            if not confirmed(spec['market'], session, cutoff):
                continue
        except ValueError as exc:
            # Do not relabel an unexpected price as a reviewed exchange session.
            raise Unavailable('YFINANCE_UNREVIEWED_TRADING_SESSION') from exc
        values = [observation[k] for k in ('Open','High','Low','Close')]
        if any(pd.isna(v) for v in values):
            continue  # Preserve a real gap, never invent OHLC.
        try:
            o,h,l,c = [float(v) for v in values]
        except (ValueError,TypeError) as exc:
            raise Unavailable('YFINANCE_INVALID_OHLC') from exc
        if not all(math.isfinite(v) and v > 0 for v in (o,h,l,c)):
            raise Unavailable('YFINANCE_INVALID_OHLC')
        if h < max(o,c) or l > min(o,c) or h < l:
            raise Unavailable('YFINANCE_INVALID_OHLC')
        volume = observation.get('Volume')
        if volume is not None and not pd.isna(volume):
            try: volume = float(volume)
            except (ValueError,TypeError) as exc:
                raise Unavailable('YFINANCE_INVALID_VOLUME') from exc
            if not math.isfinite(volume) or volume < 0:
                raise Unavailable('YFINANCE_INVALID_VOLUME')
        else:
            volume = None
        rows.append(dict(d=session,o=o,h=h,l=l,c=c,v=volume))
    if not rows:
        raise Unavailable('YFINANCE_NO_FINAL_DAILY_BARS')
    return rows[-MAX_BARS:]


class YFinanceBatch:
    """Bounded staged collection; `get()` never performs network requests.

    download() is batched by exchange market to keep each listing's local date
    convention; ticker metadata must attest the EXACT venue, unit and type.
    Each staged symbol is attempted only once per EOD execution.
    The number of internal Yahoo HTTP requests is opaque to yfinance; do not
    misrepresent yfinance symbol/batch counts as HTTP request counts.
    """
    def __init__(self, download=None, ticker_factory=None,
                 max_symbols=MAX_SYMBOLS, batch_size=BATCH_SIZE):
        if download is None or ticker_factory is None:
            import yfinance as yf
            download = download or yf.download
            ticker_factory = ticker_factory or yf.Ticker
        self.download, self.ticker_factory = download, ticker_factory
        self.max_symbols, self.batch_size = max_symbols, batch_size
        self.results = {}
        self.attempted = []
        self.batches = 0
        self.identity_checks = 0
        self.events = []

    def prepare(self, candidates, cutoff, *, previous=None):
        """Stage market-grouped full vs incremental batches, max max_symbols.

        Only a *previously verified* yfinance adjusted series for the exact
        same listing can use the 10-calendar-day overlapping increment.
        A detected adjusted-price seam mismatch triggers an independent full
        replacement on the next run, never silent historical splicing.
        """
        selected = list(candidates)[:self.max_symbols]
        self.attempted = [s['symbol'] for s, _ in selected]
        groups = defaultdict(list)
        self.events = []
        previous = previous or {}
        for spec, metadata_only in selected:
            old = previous.get(spec['symbol']) or {}
            same = all(old.get(k) == spec.get(k) for k in (
                'symbol','exchange','asset_type','market','currency','exchange_timezone'))
            incremental = bool(same and old.get('bars') and old.get('adjustment') == BASIS
                               and old.get('last_final_session')
                               and old.get('reason') != 'YFINANCE_ADJUSTED_SEAM_MISMATCH')
            self.results[spec['symbol']] = dict(rows=None, identity=None,
                reason='YFINANCE_BATCH_UNAVAILABLE', metadata_only=metadata_only,
                incremental=incremental)
            if not metadata_only:
                groups[(spec['market'],'incremental' if incremental else 'full')].append(spec)
        for (market,mode), specs in groups.items():
            for offset in range(0, len(specs), self.batch_size):
                batch = specs[offset:offset+self.batch_size]
                symbols = [spec['candidates'][0] for spec in batch]
                if mode=='incremental':
                    start=min((pd.Timestamp(previous[spec['symbol']]['last_final_session'])
                        -pd.Timedelta(days=10)).date().isoformat() for spec in batch)
                else:
                    start=(pd.Timestamp(cutoff.date())-pd.Timedelta(days=1060)).date().isoformat()
                end=(pd.Timestamp(cutoff.date())+pd.Timedelta(days=2)).date().isoformat()
                self.batches += 1
                try:
                    raw=self.download(tickers=symbols,start=start,end=end,
                        interval='1d',auto_adjust=True,group_by='ticker',
                        progress=False,threads=True,timeout=25)
                except Exception:
                    self.events.append(dict(market=market,mode=mode,kind='download',
                        count=len(batch),status='error'))
                    continue
                self.events.append(dict(market=market,mode=mode,kind='download',
                    count=len(batch),status='returned'))
                for spec in batch:
                    key=spec['symbol']
                    try:
                        frame=_ticker_frame(raw,spec['candidates'][0],len(batch))
                        self.results[key]['rows']=_final_bars(frame,spec,cutoff)
                        self.results[key]['reason']='YFINANCE_LISTING_METADATA_UNAVAILABLE'
                    except Unavailable as exc:
                        self.results[key]['reason']=str(exc)
                    except Exception:
                        self.results[key]['reason']='YFINANCE_PRICE_STRUCTURE_INVALID'
        for spec, _ in selected:
            key = spec['symbol']
            result = self.results[key]
            if not result['metadata_only'] and not result['rows']:
                continue
            self.identity_checks += 1
            try:
                meta = self.ticker_factory(spec['candidates'][0]).get_history_metadata()
                result['identity'] = _verified_identity(meta, spec)
                result['reason'] = ''
            except Unavailable as exc:
                result['reason'] = str(exc)
            except Exception:
                result['reason'] = 'YFINANCE_LISTING_METADATA_UNAVAILABLE'
        return self

    def get(self, spec):
        """A failed speculative stage must never suppress certified fallback."""
        result = self.results.get(spec['symbol'])
        if result is None or not result['identity']:
            return None
        if not result['metadata_only'] and not result['rows']:
            return None
        return result

    def accept_verified_listing(self, spec, evidence, source, now):
        """A caller may attach *already attested* listing evidence to prices.

        Only an existing verified manual EOD listing identity or the direct
        Yahoo chart metadata can establish a listing. No symbol inference.
        """
        from .manual_eod_cache import valid_identity
        if source not in ('previous_snapshot', 'direct_chart_metadata'):
            raise ValueError('UNVERIFIED_YFINANCE_IDENTITY_SOURCE')
        if valid_identity(spec, evidence, now) is None:
            raise ValueError('YFINANCE_IDENTITY_EVIDENCE_INVALID')
        result = self.results.get(spec['symbol'])
        if result is None or not result['rows']:
            raise ValueError('YFINANCE_NO_VALID_PRICES_TO_ATTEST')
        result['identity'] = evidence
        result['identity_source'] = source
        result['reason'] = ''
        return result

    def diagnostics(self):
        market_batches={}
        for event in self.events:
            key=event['market']
            summary=market_batches.setdefault(key,dict(
                batches=0,full=0,incremental=0,symbols=0,errors=0))
            summary['batches']+=1
            summary[event['mode']]+=1
            summary['symbols']+=event['count']
            summary['errors']+=event['status']=='error'
        return dict(symbols_staged=len(self.attempted), markets=sorted({
            e['market'] for e in self.events}), batches=self.batches,
            batch_mix=market_batches,
            direct_http_count_is_separate=True,
            metadata_checks=self.identity_checks,
            verified=sum(bool(v['identity']) and (v['metadata_only'] or bool(v['rows']))
                         for v in self.results.values()),
            identity_verified_by_direct=sum(v.get('identity_source')=='direct_chart_metadata'
                for v in self.results.values()),
            identity_verified_by_previous=sum(v.get('identity_source')=='previous_snapshot'
                for v in self.results.values()),
            failure_reasons={k:v['reason'] for k,v in self.results.items() if v['reason']})

