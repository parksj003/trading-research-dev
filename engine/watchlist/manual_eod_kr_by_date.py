"""Bounded, independently verified KR stock-by-date EOD rescue.

Only for manual-interest-stock EOD; never modifies the core engine cache,
the official trading signals, or the immutable exit experiment calculation
code. A by-ticker bulk snapshot cannot certify adjusted-price provenance.
"""
from __future__ import annotations

from datetime import timedelta
from math import isfinite

import pandas as pd

from .manual_eod_cache import BASES
from .manual_eod_contract import MAX_BARS
from .manual_eod_provider import Unavailable
from .manual_eod_sessions import confirmed,latest_session

SOURCE='pykrx stock-by-date adjusted=True (independently verified)'
BASIS=BASES['KR']


class VerifiedKRByDate:
    """One bounded pykrx by-date request per listing, no Yahoo fallthrough tricks.

    max_attempts caps *symbol requests*. The pykrx library's internal HTTP
    attempts are opaque and are reported separately from the 64 direct Yahoo
    transport requests. A caller must not interpret this as an HTTP cap.
    """
    def __init__(self, fetcher=None, max_attempts=20):
        self.fetcher=fetcher
        self.max_attempts=max_attempts
        self.attempted=0
        self.succeeded=0
        self.failures={}
        self.symbols=[]

    def fetch(self,spec,cutoff):
        if (spec['market']!='KR' or spec['exchange']!='KRX'
                or spec['asset_type']!='stock'
                or not spec['symbol'].startswith('KRX:')
                or len(spec['symbol'].split(':')[-1])!=6
                or not spec['symbol'].split(':')[-1].isdigit()):
            raise Unavailable('KR_BY_DATE_LISTING_NOT_CERTIFIED')
        if self.attempted>=self.max_attempts:
            raise Unavailable('KR_BY_DATE_ATTEMPT_BUDGET_DEFERRED')
        code=spec['symbol'].split(':')[1]
        self.attempted+=1
        self.symbols.append(spec['symbol'])
        try:
            fetcher=self.fetcher
            if fetcher is None:
                from pykrx import stock
                fetcher=stock.get_market_ohlcv_by_date
            target=latest_session('KR',cutoff)
            start=(pd.Timestamp(target)-pd.Timedelta(days=805)).strftime('%Y%m%d')
            end=pd.Timestamp(target).strftime('%Y%m%d')
            # Unlike get_market_ohlcv_by_ticker, this explicitly certifies
            # the adjusted=True data source; never infer it from raw bulk.
            frame=fetcher(start,end,code,adjusted=True)
            if not isinstance(frame,pd.DataFrame) or frame.empty:
                raise Unavailable('KR_BY_DATE_SOURCE_EMPTY')
            required=('시가','고가','저가','종가')
            if not set(required).issubset(frame.columns):
                raise Unavailable('KR_BY_DATE_REQUIRED_COLUMNS_MISSING')
            dates=pd.DatetimeIndex(frame.index)
            if dates.hasnans or dates.has_duplicates:
                raise Unavailable('KR_BY_DATE_SESSION_IDENTITY_INVALID')
            if dates.tz is not None:
                dates=dates.tz_convert('Asia/Seoul').tz_localize(None)
            if any(date.time().isoformat()!='00:00:00' for date in dates):
                raise Unavailable('KR_BY_DATE_SESSION_TIME_UNVERIFIED')
            data=frame.copy()
            data.index=dates.normalize()
            if data.index.has_duplicates:
                raise Unavailable('KR_BY_DATE_DUPLICATE_LOCAL_SESSION')
            rows=[];excluded=0
            for date,record in data.sort_index().iterrows():
                session=date.date().isoformat()
                if session>target or not confirmed('KR',session,cutoff):
                    excluded+=1
                    continue
                raw=[record[key] for key in required]
                if any(pd.isna(x) for x in raw):
                    excluded+=1
                    continue
                try:
                    o,h,l,c=[float(x) for x in raw]
                except (ValueError,TypeError) as exc:
                    raise Unavailable('KR_BY_DATE_PRICE_INVALID') from exc
                # Zero-price traded suspensions are absent sessions. Never
                # manufacture bars or replace a nonzero OHLC contradiction.
                if o==h==l==c==0:
                    excluded+=1
                    continue
                if not all(isfinite(x) and x>0 for x in (o,h,l,c)):
                    raise Unavailable('KR_BY_DATE_PRICE_INVALID')
                if h<max(o,c) or l>min(o,c) or h<l:
                    raise Unavailable('KR_BY_DATE_OHLC_INTEGRITY_FAILED')
                raw_volume=record.get('거래량')
                volume=float(raw_volume) if raw_volume is not None and pd.notna(raw_volume) else None
                if volume is not None and (not isfinite(volume) or volume<0):
                    raise Unavailable('KR_BY_DATE_VOLUME_INVALID')
                rows.append(dict(d=session,o=o,h=h,l=l,c=c,v=volume))
            rows=rows[-MAX_BARS:]
            if len(rows)<2:
                raise Unavailable('KR_BY_DATE_NOT_ENOUGH_FINAL_BARS')
            # Only use a current close if the official session is present;
            # stale source rows remain stale, and cannot inherit old timestamps.
            identity=dict(provider_symbol=code,provider_exchange='KRX',
                currency='KRW',instrument_type='EQUITY',
                timezone='Asia/Seoul',
                evidence='pykrx:stock-by-date:adjusted=True')
            self.succeeded+=1
            return rows,identity,excluded
        except Unavailable as exc:
            self.failures[spec['symbol']]=str(exc)
            raise
        except Exception as exc:
            self.failures[spec['symbol']]='KR_BY_DATE_SOURCE_UNAVAILABLE'
            raise Unavailable('KR_BY_DATE_SOURCE_UNAVAILABLE') from exc

    def diagnostics(self):
        return dict(attempted=self.attempted,succeeded=self.succeeded,
            failed=len(self.failures),max_symbol_attempts=self.max_attempts,
            scope='KR stocks, per-symbol by-date adjusted=True',
            http_calls_observed=None,
            not_counted_in_yahoo_direct_budget=True,
            failed_reasons=self.failures)

