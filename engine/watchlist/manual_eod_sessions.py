"""Independent EOD session policy. No changes to the strategy's calendar calls."""
from __future__ import annotations
from functools import lru_cache
import exchange_calendars as xc
import pandas as pd
from .session_calendar import get_calendar as reviewed_calendar

POLICY='manual-eod-calendar-v1'
CSAT=('2021-11-18','2022-11-17','2023-11-16','2024-11-14','2025-11-13','2026-11-19')
MARKETS={'KR':('XKRX','Asia/Seoul'),'US':('XNYS','America/New_York'),
    'JP':('XTKS','Asia/Tokyo'),'CN':('XSHG','Asia/Shanghai'),'HK':('XHKG','Asia/Hong_Kong'),
    'DE':('XETR','Europe/Berlin'),'CH':('XSWX','Europe/Zurich'),'ES':('XMAD','Europe/Madrid')}


@lru_cache(maxsize=8)
def calendar(market):
    if market not in MARKETS:raise ValueError('unsupported market calendar')
    return reviewed_calendar('KR') if market=='KR' else xc.get_calendar(MARKETS[market][0])


def close_at(market,date):
    date=pd.Timestamp(date).normalize().tz_localize(None)
    if market=='KR' and date.year>2026:raise ValueError('KR special-hours review ends in 2026')
    cal=calendar(market)
    if not cal.is_session(date):raise ValueError('not an exchange session')
    if market=='KR' and str(date.date()) in CSAT:
        return pd.Timestamp(str(date.date())+' 16:30',tz='Asia/Seoul').tz_convert('UTC')
    return pd.Timestamp(cal.session_close(date)).tz_convert('UTC')


def latest_session(market,cutoff,buffer_minutes=20):
    cutoff=pd.Timestamp(cutoff)
    if cutoff.tzinfo is None:raise ValueError('aware cutoff required')
    cal=calendar(market);local=cutoff.tz_convert(MARKETS[market][1]).date();date=pd.Timestamp(local)
    if date>cal.last_session or date<cal.first_session or market=='KR' and date.year>2026:raise ValueError('calendar coverage unknown')
    session=cal.date_to_session(date,direction='previous')
    for _ in range(4):
        if close_at(market,session)+pd.Timedelta(minutes=buffer_minutes)<=cutoff:return str(session.date())
        session=cal.previous_session(session)
    raise ValueError('no confirmed session')


def confirmed(market,date,cutoff,buffer_minutes=20):
    return close_at(market,date)+pd.Timedelta(minutes=buffer_minutes)<=pd.Timestamp(cutoff)

