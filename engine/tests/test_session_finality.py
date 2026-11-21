"""Acquisition evidence, not elapsed wall time, determines finality."""
from datetime import datetime
import pandas as pd
import pytest
from watchlist import trend_watchlist as e


def clock(monkeypatch, value):
    now=datetime.fromisoformat(value)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr(e,'datetime',Clock)


@pytest.mark.parametrize('market,day,acquired,later',[
 ('KR','2026-11-03','2026-11-03T14:00:00+09:00','2026-11-04T07:00:00+09:00'),
 ('US','2026-11-03','2026-11-03T14:00:00-05:00','2026-11-04T08:00:00-05:00'),
 ('US','2026-10-30','2026-10-30T14:00:00-04:00','2026-11-02T08:00:00-05:00'),
 ('US','2026-11-25','2026-11-25T14:00:00-05:00','2026-11-27T08:00:00-05:00'),
])
def test_partial_never_promoted(monkeypatch,tmp_path,market,day,acquired,later):
    clock(monkeypatch,acquired)
    raw=pd.DataFrame({'Close':[100.]}, index=pd.to_datetime([day]))
    d=e.tag_completed_sessions(raw,market);p=tmp_path/'cache.pkl';d.to_pickle(p)
    assert e._completed_cached_bars(p,d,market).empty
    clock(monkeypatch,later)
    # Restoring/touching a cache cannot promote its original rows.
    d.to_pickle(p)
    assert e._completed_cached_bars(p,pd.read_pickle(p),market).empty


@pytest.mark.parametrize('market,day,at,confirmed',[
 ('US','2026-11-27','2026-11-27T13:04:00-05:00',False),
 ('US','2026-11-27','2026-11-27T13:05:00-05:00',True),
 ('US','2026-11-26','2026-11-27T14:00:00-05:00',False),
 ('US','2026-07-07','2026-07-07T16:05:00-04:00',True),
 ('KR','2026-11-03','2026-11-04T07:00:00+09:00',True),
 ('KR','2026-11-03','2026-11-03T15:34:00+09:00',False),
])
def test_exchange_close_holiday_dst(monkeypatch,market,day,at,confirmed):
    clock(monkeypatch,at)
    d=e.tag_completed_sessions(pd.DataFrame({'Close':[100.]},index=pd.to_datetime([day])),market)
    assert bool(d['_session_confirmed'].iloc[0]) is confirmed


def test_unknown_legacy_preserved_on_disk(tmp_path):
    p=tmp_path/'old.pkl';d=pd.DataFrame({'Close':[100.]},index=pd.to_datetime(['2026-10-01']));d.to_pickle(p)
    before=p.read_bytes()
    assert e.kr_completed_cached_bars(p,d).empty
    assert e.us_completed_cached_bars(p,d).empty
    assert p.read_bytes()==before


def test_confirmed_rows_survive_mixed_cache_and_merge(tmp_path):
    p=tmp_path/'cache.pkl';d=pd.DataFrame({'Close':[100.,101.], '_session_confirmed':[True,False]},index=pd.to_datetime(['2026-11-02','2026-11-03']))
    d.to_pickle(p)
    assert e.us_completed_cached_bars(p,d).Close.tolist()==[100.]


@pytest.mark.parametrize('day',['2021-11-18','2022-11-17','2023-11-16','2024-11-14','2025-11-13','2026-11-19'])
@pytest.mark.parametrize('at,expected',[('16:00',False),('16:34',False),('16:35',True)])
def test_csat_close_consistent_across_tag_trim_and_refresh(monkeypatch,day,at,expected):
    clock(monkeypatch,f'{day}T{at}:00+09:00')
    raw=pd.DataFrame({'Close':[100.]}, index=pd.to_datetime([day]))
    tagged=e.tag_completed_sessions(raw,'KR')
    assert bool(tagged['_session_confirmed'].iloc[0]) is expected
    assert (len(e.trim_incomplete_bar(tagged,'KR'))==1) is expected
    assert (e.kr_close_check_date() is not None) is expected


def test_csat_old_attestation_rejected_but_normal_history_preserved(tmp_path):
    p=tmp_path/'prices.pkl'
    d=pd.DataFrame({'Close':[100.,101.,102.], '_session_confirmed':True},index=pd.to_datetime(['2024-11-13','2024-11-14','2024-11-15']))
    d.to_pickle(p);before=p.read_bytes()
    assert e.kr_completed_cached_bars(p,d).Close.tolist()==[100.,102.]
    assert p.read_bytes()==before


@pytest.mark.parametrize('at,expected',[('13:04',False),('13:05',True),('13:10',True)])
def test_us_early_close_survives_full_filter_chain(monkeypatch,tmp_path,at,expected):
    clock(monkeypatch,f'2026-11-27T{at}:00-05:00')
    raw=pd.DataFrame({'Close':[100.]},index=pd.to_datetime(['2026-11-27']))
    tagged=e.tag_completed_sessions(raw,'US');p=tmp_path/'SPY.pkl';tagged.to_pickle(p)
    result=e.trim_incomplete_bar(e.us_completed_cached_bars(p,pd.read_pickle(p)),'US')
    assert (len(result)==1) is expected
    assert (e.us_close_check_date() is not None) is expected


def test_unknown_future_kr_special_coverage_fails_closed(monkeypatch):
    clock(monkeypatch,'2027-03-04T19:00:00+09:00')
    raw=pd.DataFrame({'Close':[100.]},index=pd.to_datetime(['2027-03-04']))
    assert not e.tag_completed_sessions(raw,'KR')['_session_confirmed'].iloc[0]
    assert e.kr_close_check_date() is None


@pytest.mark.parametrize('provider_fails',[False,True])
def test_old_csat_attestation_repairs_incrementally_and_preserves_on_failure(monkeypatch,tmp_path,provider_fails):
    import sys
    from types import SimpleNamespace
    from copy import deepcopy
    import exchange_calendars as xc
    clock(monkeypatch,'2026-11-04T19:00:00+09:00')
    monkeypatch.setattr(e,'KR_HISTORY_CACHE_DIR',tmp_path)
    ix=xc.get_calendar('XKRX').sessions_in_range('2023-01-03','2026-11-04')
    d=pd.DataFrame({'Open':100.,'High':101.,'Low':99.,'Close':100.,'Volume':1000.,'_session_confirmed':True,'_session_finality_version':2},index=ix)
    d.loc['2024-11-14','_session_finality_version']=1
    d.loc['2024-11-14','Close']=88.
    p=tmp_path/'005930.pkl';d.to_pickle(p);before=p.read_bytes();calls=[]
    def fetch(start,end,ticker):
        calls.append((start,end))
        if provider_fails:return pd.DataFrame()
        raw=d.loc[start:end].drop(columns=['_session_confirmed','_session_finality_version']).copy()
        raw.loc[:,'Close']=100.
        return raw.rename(columns={'Open':'시가','High':'고가','Low':'저가','Close':'종가','Volume':'거래량'})
    monkeypatch.setitem(sys.modules,'pykrx',SimpleNamespace(stock=SimpleNamespace(get_market_ohlcv_by_date=fetch)))
    cfg=deepcopy(e.DEFAULT_CONFIG)
    result=e.fetch_kr_history('005930',2,cfg=cfg)
    assert calls==[('20241114','20261104')]
    if provider_fails:
        assert pd.Timestamp('2024-11-14') not in result.index
        assert p.read_bytes()==before
    else:
        assert result.loc['2024-11-14','Close']==100.
        # Fresh KR attestations carry v3; existing valid CSAT v2 remains accepted.
        assert pd.read_pickle(p).loc['2024-11-14','_session_finality_version']==3
        e.fetch_kr_history('005930',2,cfg=cfg)
        assert len(calls)==1

