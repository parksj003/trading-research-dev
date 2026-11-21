"""Reviewed cash-market sessions; never mutate exchange-calendars' shared cache.

Only official KRX closures listed below override the vendor calendar. This does
not certify an entire year, infer missing dates, or extend special-hours review.
"""
from numbers import Integral

import exchange_calendars as xc
import pandas as pd

LEGACY_POLICY = 'reviewed-finality-through-2026-v1'
KR_POLICY = 'reviewed-finality-through-2026-kr-closures-v2'
KR_CLOSED_DATES = ('2026-06-03', '2026-07-17')
KR_CLOSURE_SOURCES = (
    'https://kind.krx.co.kr/external/2026/05/20/000117/20260520000009/99340.htm',
    'https://kind.krx.co.kr/external/2026/05/20/000121/20260520000184/71985.htm',
)


def is_kr(market):
    return market.upper() in ('KR', 'KOSPI', 'KOSDAQ')


def calendar_policy(market):
    return KR_POLICY if is_kr(market) else LEGACY_POLICY


class ReviewedKRCalendar:
    """Small explicit session API used by acquisition, tracking and research."""

    def __init__(self, base):
        self._base = base
        self.sessions = base.sessions[~base.sessions.isin(pd.to_datetime(KR_CLOSED_DATES))]
        self.schedule = base.schedule.loc[self.sessions].copy()
        self.first_session = self.sessions[0]
        self.last_session = self.sessions[-1]

    def is_session(self, date):
        # Delegate input/coverage validation; a false vendor holiday is still false.
        return self._base.is_session(date) and pd.Timestamp(date) not in pd.to_datetime(KR_CLOSED_DATES)

    def sessions_in_range(self, start, end):
        sessions = self._base.sessions_in_range(start, end)
        return sessions[~sessions.isin(pd.to_datetime(KR_CLOSED_DATES))]

    def session_offset(self, date, count):
        if not isinstance(count, Integral):
            raise TypeError('session offset must be an integer')
        if not self.is_session(date):
            raise ValueError('not a reviewed exchange session')
        position = self.sessions.get_loc(pd.Timestamp(date)) + count
        if position < 0 or position >= len(self.sessions):
            raise ValueError('reviewed calendar coverage exceeded')
        return self.sessions[position]

    def next_session(self, date):
        return self.session_offset(date, 1)

    def previous_session(self, date):
        return self.session_offset(date, -1)

    def date_to_session(self, date, direction='none'):
        session = self._base.date_to_session(date, direction=direction)
        if session in self.sessions:
            return session
        if direction == 'previous':
            return self.sessions[self.sessions.searchsorted(session) - 1]
        if direction == 'next':
            position = self.sessions.searchsorted(session)
            if position < len(self.sessions):
                return self.sessions[position]
        raise ValueError('not a reviewed exchange session')

    def session_close(self, date):
        if not self.is_session(date):
            raise ValueError('not a reviewed exchange session')
        return self._base.session_close(date)

    def session_open(self, date):
        if not self.is_session(date):
            raise ValueError('not a reviewed exchange session')
        return self._base.session_open(date)


def get_calendar(market):
    base = xc.get_calendar('XKRX' if is_kr(market) else 'XNYS')
    return ReviewedKRCalendar(base) if is_kr(market) else base

