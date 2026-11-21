"""Non-invasive, bounded source-date diagnostics for European EOD candles.

This module never modifies, fills, or relaxes publication validation. It
explains the provider's raw timestamps and how the existing final-session
policy treats them. Only safe market metadata/dates/rules are emitted; OHLCV
values, URLs, cookies and credentials are never logged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from zoneinfo import ZoneInfo

from .manual_eod_sessions import confirmed, latest_session

EUROPE_MARKETS=frozenset({'DE','CH','ES'})


def _bar_reason(quote, index, market, local_day, utc_time, cutoff):
    if utc_time>cutoff:
        return 'SOURCE_TIMESTAMP_AFTER_CUTOFF'
    try:
        final=confirmed(market,local_day,cutoff)
    except (ValueError,KeyError,TypeError):
        return 'NOT_REVIEWED_EXCHANGE_SESSION'
    if not final:
        return 'SESSION_CLOSE_BUFFER_NOT_ELAPSED'
    try:
        values=[quote[key][index] for key in ('open','high','low','close')]
    except (KeyError,IndexError,TypeError):
        return 'OHLC_ARRAY_UNAVAILABLE'
    if any(v is None for v in values):
        return 'OHLC_VALUE_MISSING'
    if not all(type(v) in (int,float) and isfinite(v) and v>0 for v in values):
        return 'OHLC_VALUE_INVALID'
    o,h,l,c=values
    if h<max(o,c):return 'HIGH_BELOW_OPEN_OR_CLOSE'
    if l>min(o,c):return 'LOW_ABOVE_OPEN_OR_CLOSE'
    if h<l:return 'HIGH_BELOW_LOW'
    return 'ELIGIBLE_FINAL_DAILY_BAR'


def europe_source_day_evidence(result, spec, cutoff):
    """Return last three source UTC timestamps and policy verdict, never prices.

    Intended to distinguish a provider missing the expected bar from a bar
    excluded by calendar/timezone validation. Network or exchange/calendar
    failures cannot be caused by collecting this additional evidence.
    """
    if spec['market'] not in EUROPE_MARKETS:
        return None
    try:
        market=spec['market']
        utc_cutoff=cutoff.astimezone(timezone.utc)
        expected=latest_session(market,cutoff)
        source_meta=result['meta']
        stamps=result['timestamp']
        quote=result['indicators']['quote'][0]
        if not isinstance(stamps,list):
            return dict(status='SOURCE_TIMESTAMP_ARRAY_UNAVAILABLE',expected_session=expected)
        indexed=sorted(enumerate(stamps),key=lambda v:v[1])
        observed=[]
        for index,stamp in indexed:
            raw_utc=datetime.fromtimestamp(stamp,timezone.utc)
            local=raw_utc.astimezone(ZoneInfo(spec['exchange_timezone']))
            observed.append((index,raw_utc,local.date().isoformat()))
        last=[]
        for index,raw_utc,local_day in observed[-3:]:
            verdict=_bar_reason(quote,index,market,local_day,raw_utc,utc_cutoff)
            # Presence and numeric validity are distinct: a close-only series
            # is possible when O/H/L are missing but the actual close exists.
            # Never include the price values themselves in diagnostics.
            presence={}
            for field in ('open','high','low','close'):
                values=quote.get(field)
                val=(values[index] if isinstance(values,list)
                     and index<len(values) else None)
                presence[field+'_present']=val is not None
                presence[field+'_valid']=(type(val) in (int,float)
                    and isfinite(val) and val>0)
            last.append(dict(utc_timestamp=raw_utc.isoformat(),
                local_session=local_day,policy_verdict=verdict,
                field_availability=presence))
        expected_indices=[i for i,_,local_day in observed if local_day==expected]
        expected_reasons=[
            _bar_reason(quote,i,market,local_day,stamp,utc_cutoff)
            for i,stamp,local_day in observed if local_day==expected]
        if any(v=='ELIGIBLE_FINAL_DAILY_BAR' for v in expected_reasons):
            finding='EXPECTED_BAR_ELIGIBLE'
        elif expected_reasons:
            finding='EXPECTED_BAR_PRESENT_BUT_EXCLUDED'
        elif observed and observed[-1][2]<expected:
            finding='PROVIDER_SOURCE_BEHIND_EXPECTED_SESSION'
        else:
            finding='EXPECTED_SESSION_NOT_PRESENT'
        return dict(
            status='RECORDED',
            expected_session=expected,
            source_rows=len(stamps),
            expected_session_source_count=len(expected_indices),
            finding=finding,
            candidate=source_meta.get('symbol'),
            provider_exchange=source_meta.get('exchangeName'),
            provider_timezone=source_meta.get('exchangeTimezoneName'),
            policy_timezone=spec['exchange_timezone'],
            cutoff_utc=utc_cutoff.isoformat(),
            recent_source_days=last,
        )
    except (KeyError,ValueError,TypeError,OverflowError,IndexError,OSError):
        # Additional diagnostics must never affect strict price publication.
        return dict(status='SOURCE_EVIDENCE_UNAVAILABLE')

