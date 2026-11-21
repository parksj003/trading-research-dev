"""No network: prove the 2026-11-18 EU daily-bar timezone explanation.

Do not change source prices or accept a stale/invalid bar merely because a
provider's endpoint returns HTTP 200. GM NASDAQ venue transfer is a catalog
change, not a relaxation of the independent listing identity contract.
"""
from datetime import datetime, timezone, timedelta

import pytest

from watchlist.manual_eod_provider import (
    YahooDaily, Unavailable, specification, result_of,
)
from watchlist.manual_eod_source_diagnostics import europe_source_day_evidence


CUTOFF=datetime(2026,11,19,3,15,tzinfo=timezone.utc)


def listing(symbol,unit):
    return dict(symbol=symbol,asset_type='stock',unit=unit)


def stamp(date, hour=1):
    return int(datetime.fromisoformat(f'{date}T{hour:02d}:00:00+01:00').timestamp())


def chart(spec, day_stamps, *, missing_last=False, bad_last=False):
    n=len(day_stamps)
    values=dict(open=[100.]*n,high=[104.]*n,low=[98.]*n,
        close=[102.]*n,volume=[500.]*n)
    if missing_last:values['close'][-1]=None
    if bad_last:values['high'][-1]=99.
    return dict(meta=dict(
        symbol=spec['candidates'][0],
        exchangeName=spec['provider_exchanges'][0],
        exchangeTimezoneName=spec['provider_timezones'][0],
        dataGranularity='1d',currency=spec['currency'],instrumentType='EQUITY'),
        timestamp=day_stamps,indicators=dict(quote=[values]))


@pytest.mark.parametrize('ticker,unit,market',[
    ('XETR:ADS','EUR','DE'),('SIX:ABBN','CHF','CH'),('BME:BBVA','EUR','ES')
])
def test_europe_source_session_is_nov_18_not_nov_17_after_utc_conversion(ticker,unit,market):
    spec=specification(listing(ticker,unit))
    day=chart(spec,[stamp('2026-11-16'),stamp('2026-11-17'),stamp('2026-11-18')])
    evidence=europe_source_day_evidence(day,spec,CUTOFF)
    assert evidence['status']=='RECORDED'
    assert evidence['expected_session']=='2026-11-18'
    assert evidence['provider_timezone']==spec['provider_timezones'][0]
    assert evidence['policy_timezone']==spec['exchange_timezone']
    assert evidence['finding']=='EXPECTED_BAR_ELIGIBLE'
    assert evidence['expected_session_source_count']==1
    last=evidence['recent_source_days'][-1]
    assert {k:last[k] for k in (
        'utc_timestamp','local_session','policy_verdict')}=={
        'utc_timestamp':'2026-11-18T00:00:00+00:00',
        'local_session':'2026-11-18',
        'policy_verdict':'ELIGIBLE_FINAL_DAILY_BAR'
    }
    assert all(last['field_availability'][field+'_present']
        and last['field_availability'][field+'_valid']
        for field in ('open','high','low','close'))


@pytest.mark.parametrize('ticker,unit',[
    ('XETR:ADS','EUR'),('SIX:ABBN','CHF'),('BME:BBVA','EUR')
])
def test_diagnostic_identifies_provider_missing_latest_date(ticker,unit):
    spec=specification(listing(ticker,unit))
    evidence=europe_source_day_evidence(
        chart(spec,[stamp('2026-11-16'),stamp('2026-11-17')]),spec,CUTOFF)
    assert evidence['expected_session']=='2026-11-18'
    assert evidence['source_rows']==2
    assert evidence['finding']=='PROVIDER_SOURCE_BEHIND_EXPECTED_SESSION'
    assert evidence['recent_source_days'][-1]['local_session']=='2026-11-17'


@pytest.mark.parametrize('missing,bad,expected_reason',[
    (True,False,'OHLC_VALUE_MISSING'),
    (False,True,'HIGH_BELOW_OPEN_OR_CLOSE'),
])
def test_diagnostic_identifies_why_present_nov18_bar_is_excluded(missing,bad,expected_reason):
    spec=specification(listing('XETR:ADS','EUR'))
    evidence=europe_source_day_evidence(
        chart(spec,[stamp('2026-11-17'),stamp('2026-11-18')],
              missing_last=missing,bad_last=bad),spec,CUTOFF)
    assert evidence['finding']=='EXPECTED_BAR_PRESENT_BUT_EXCLUDED'
    assert evidence['recent_source_days'][-1]['policy_verdict']==expected_reason
    # No request URLs, OHLCV numbers, tokens or credentials in the artifact.
    assert not any(field in evidence['recent_source_days'][-1] for field in
                   ('open','high','low','close','volume','price','url','token'))


def test_utc_midnight_is_local_next_calendar_day_and_not_mislabeled():
    spec=specification(listing('XETR:ADS','EUR'))
    # UTC 23:00 on Nov 17 is already Nov 18 midnight in Berlin (CET).
    d=chart(spec,[int(datetime(2026,11,17,23,tzinfo=timezone.utc).timestamp())])
    evidence=europe_source_day_evidence(d,spec,CUTOFF)
    assert evidence['finding']=='EXPECTED_BAR_ELIGIBLE'
    assert evidence['recent_source_days'][0]['local_session']=='2026-11-18'


def test_source_diagnostic_is_attached_to_actual_europe_http_200_without_another_request():
    spec=specification(listing('XETR:ADS','EUR'))
    raw=chart(spec,[stamp('2026-11-17'),stamp('2026-11-18')])
    class Response:
        status_code=200
        def json(self):return dict(chart=dict(error=None,result=[raw]))
    class Session:
        def __init__(self):self.calls=0
        def get(self,*args,**kwargs):
            self.calls+=1
            return Response()
    session=Session()
    provider=YahooDaily(session=session,pause=0)
    rows,identity,excluded=provider.fetch(spec,CUTOFF)
    assert session.calls==1
    assert rows[-1]['d']=='2026-11-18'
    assert identity['provider_exchange']=='GER'
    assert excluded==0
    log=provider.request_log[0]
    assert log['status']==200
    assert log['europe_source_days']['finding']=='EXPECTED_BAR_ELIGIBLE'
    assert log['europe_source_days']['recent_source_days'][-1]['local_session']=='2026-11-18'
    assert '100.0' not in str(log) and '102.0' not in str(log)


def test_gm_verified_listing_changed_from_nyse_to_nasdaq_nov17_2026():
    nyse=specification(listing('NYSE:GM','USD'))
    nasdaq=specification(listing('NASDAQ:GM','USD'))
    assert nyse['candidates']==nasdaq['candidates']==('GM',)
    assert nyse['provider_exchanges']==('NYQ',)
    assert 'NMS' in nasdaq['provider_exchanges']
    # Yahoo now reports NASDAQ; NYSE:GM correctly fails closed.
    d=chart(nasdaq,[int(datetime(2026,11,18,19,tzinfo=timezone.utc).timestamp())])
    with pytest.raises(Unavailable,match='PROVIDER_EXCHANGE_MISMATCH'):
        result_of(dict(chart=dict(error=None,result=[d])),'GM',nyse)
    _,identity=result_of(dict(chart=dict(error=None,result=[d])),'GM',nasdaq)
    assert identity['provider_exchange']=='NMS'

