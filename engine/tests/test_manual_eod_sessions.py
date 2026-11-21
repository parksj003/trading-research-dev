from datetime import datetime,timezone
import pytest
from watchlist.manual_eod_sessions import latest_session,close_at,confirmed


def dt(text):return datetime.fromisoformat(text.replace('Z','+00:00'))


@pytest.mark.parametrize('market,instant,expected',[
 ('KR','2026-11-12T06:49:00Z','2026-11-11'),('KR','2026-11-12T06:50:00Z','2026-11-12'),
 ('US','2026-11-12T20:59:00Z','2026-11-11'),('US','2026-11-12T21:20:00Z','2026-11-12'),
 ('JP','2026-11-12T06:49:00Z','2026-11-11'),('JP','2026-11-12T06:50:00Z','2026-11-12'),
 ('CN','2026-11-12T07:19:00Z','2026-11-11'),('CN','2026-11-12T07:20:00Z','2026-11-12'),
 ('HK','2026-11-12T08:19:00Z','2026-11-11'),('HK','2026-11-12T08:20:00Z','2026-11-12'),
 ('US','2026-11-27T18:19:00Z','2026-11-25'),('US','2026-11-27T18:20:00Z','2026-11-27'),
 ('KR','2026-11-19T07:49:00Z','2026-11-18'),('KR','2026-11-19T07:50:00Z','2026-11-19'),
 ('KR','2026-07-17T12:00:00Z','2026-07-16'),
 ('CN','2026-10-01T12:00:00Z','2026-09-30'),('JP','2026-11-03T10:00:00Z','2026-11-02')])
def test_market_close_buffer_holidays_and_special_hours(market,instant,expected):
    assert latest_session(market,dt(instant))==expected


def test_us_dst_and_half_day_are_actual_calendar_closes():
    assert close_at('US','2026-03-06').hour==21
    assert close_at('US','2026-03-09').hour==20
    assert close_at('US','2026-11-27').hour==18
    assert close_at('HK','2026-12-24').hour==4


def test_calendar_unknown_does_not_infer_a_normal_weekday():
    with pytest.raises(ValueError):latest_session('KR',dt('2027-01-04T12:00:00Z'))
    with pytest.raises(ValueError):confirmed('KR','2026-06-03',dt('2026-06-03T20:00:00Z'))
    with pytest.raises(ValueError):latest_session('CN',dt('2027-01-04T12:00:00Z'))


def test_2026_sse_and_szse_announced_holidays_match_shared_calendar_exactly():
    # SSE 2025-12-22 calendar and SZSE notice 深证会〔2025〕481号.
    # Sources and the reviewed-year limit are recorded in MANUAL_EOD.md.
    import pandas as pd
    from watchlist.manual_eod_sessions import calendar
    spans=[('2026-01-01','2026-01-03'),('2026-02-15','2026-02-23'),('2026-04-04','2026-04-06'),
        ('2026-05-01','2026-05-05'),('2026-06-19','2026-06-21'),('2026-09-25','2026-09-27'),('2026-10-01','2026-10-07')]
    announced={d for start,end in spans for d in pd.bdate_range(start,end)}
    actual=set(pd.bdate_range('2026-01-01','2026-12-31')).difference(calendar('CN').sessions)
    assert actual==announced

