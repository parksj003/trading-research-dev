"""Calendar-accurate audit of actual preserved OHLC and close-only sessions.

Never infer/fill a price on an absent exchange session. No network calls.
"""
from __future__ import annotations

from .manual_eod_sessions import calendar

MAX_GAP_EXAMPLES=12


def session_coverage(market, dates, *, expected_session=None):
    """Count absent exchange sessions strictly between observed first/last.

    Trailing expected dates are separate: don't call a stale candle a valid
    current session. Trading-day calendars include weekends and local holidays.
    """
    if not dates:
        return None
    if dates!=sorted(set(dates)):
        raise ValueError('COVERAGE_DUPLICATE_OR_UNSORTED_DATES')
    sessions=calendar(market).sessions_in_range(dates[0],dates[-1])
    expected={x.date().isoformat() for x in sessions}
    observed=set(dates)
    if not observed.issubset(expected):
        raise ValueError('COVERAGE_BAR_OUTSIDE_MARKET_CALENDAR')
    gaps=sorted(expected-observed)
    trailing=[]
    if expected_session is not None and expected_session>dates[-1]:
        tail=calendar(market).sessions_in_range(dates[-1],expected_session)
        trailing=[x.date().isoformat() for x in tail if x.date().isoformat()>dates[-1]]
    return dict(
        first_session=dates[0],last_session=dates[-1],
        expected_sessions_between_first_last=len(expected),
        observed_sessions=len(dates),
        missing_between_first_last=len(gaps),
        recent_missing_examples=gaps[-MAX_GAP_EXAMPLES:],
        trailing_missing_sessions=len(trailing),
        trailing_missing_examples=trailing[-MAX_GAP_EXAMPLES:],
        calendar_audit='exchange-calendars:no-fill',
    )

