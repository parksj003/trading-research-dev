from copy import deepcopy
import numpy as np
import pandas as pd
import pytest
from watchlist import trend_watchlist as e


def bars(end):
    c=np.linspace(5000,10000,300)
    return pd.DataFrame({'Open':c,'High':c+100,'Low':c-100,'Close':c,'Volume':1e8,'Turnover':1e11,'_session_confirmed':True},index=pd.bdate_range(end=end,periods=300))


@pytest.mark.parametrize('fresh,expected_valid',[(40,True),(32,True),(29,False),(0,False)])
@pytest.mark.parametrize('lag',['2026-11-03','2026-10-29'])
def test_date_alignment_keeps_full_denominator(fresh,expected_valid,lag):
    rows=[dict(as_of='2026-11-04' if i<fresh else lag,above_50dma=True,above_200dma=True) for i in range(40)]
    r=e.compute_universe_breadth(rows,40,'2026-11-04')
    assert r['breadth_total']==40 and r['breadth_valid_count']==fresh
    assert r['breadth_coverage_pct']==fresh/40*100
    assert r['breadth_valid'] is expected_valid
    assert r['pct_above_50dma']==100 if expected_valid else pd.isna(r['pct_above_50dma'])


@pytest.mark.parametrize('track',['KOSPI','KOSDAQ','NASDAQ100','SP500'])
@pytest.mark.parametrize('stock_end',['2026-10-29','2026-11-04'])
def test_full_tracks_stale_forty_cannot_justify_attack(monkeypatch,tmp_path,track,stock_end):
    cfg=deepcopy(e.DEFAULT_CONFIG);cfg['runtime']['request_pause_seconds']=0
    kr=track in ('KOSPI','KOSDAQ')
    u=pd.DataFrame([dict(ticker=f'{i:06d}' if kr else f'T{i}',name=f'Stock{i}',sub_market=track,market_cap=2e12,sector='S',macro_theme='T',in_nasdaq100=track=='NASDAQ100',in_sp500=track=='SP500') for i in range(40)])
    hist=bars(stock_end);benchmark=bars('2026-11-04')
    if kr:
        monkeypatch.setattr(e,'get_kr_universe',lambda *a:u)
        monkeypatch.setattr(e,'bulk_sync_kr_recent_ohlcv',lambda *a:{})
        monkeypatch.setattr(e,'prefetch_histories',lambda tickers,*a,**kw:{t:hist for t in tickers})
        monkeypatch.setattr(e,'fetch_kr_history',lambda *a,**kw:benchmark if kw.get('is_index') else hist)
        monkeypatch.setattr(e,'KR_HISTORY_CACHE_DIR',tmp_path)
        result=e.run_kr_tracks(cfg,{}, {})
    else:
        monkeypatch.setattr(e,'get_us_universe',lambda *a:u)
        monkeypatch.setattr(e,'get_us_market_caps',lambda *a,**kw:({t:2e12 for t in u.ticker},{},{},{}))
        monkeypatch.setattr(e,'fetch_us_histories',lambda *a,**kw:{**{t:hist for t in u.ticker},'SPY':benchmark,'QQQ':benchmark})
        monkeypatch.setattr(e,'fetch_us_earnings_dates',lambda *a:{})
        result=e.run_us_tracks(cfg,{}, {})
    r=result[3][track]
    fresh=stock_end=='2026-11-04'
    assert r['breadth_valid'] is fresh
    assert r['breadth_total']==40
    assert r['breadth_valid_count']==(40 if fresh else 0)
    assert r['regime']==('ATTACK' if fresh else 'CAUTION')
    sizes=[dict(raw_stop_dist_pct=-.05)]
    e.sync_track_sizing_with_final_regime(sizes,r,cfg)
    assert sizes[0]['trade_risk_pct']==pytest.approx(1. if fresh else .75,abs=.01)


def test_holiday_weekend_and_joint_delay_are_explicit_historical_snapshots():
    # Equal exchange-session dates remain aligned across weekends/holidays.
    for day in ('2026-10-30','2026-11-25'):
        rows=[dict(as_of=day,above_50dma=True,above_200dma=True) for _ in range(40)]
        assert e.compute_universe_breadth(rows,40,day)['breadth_valid']
        assert not e.compute_universe_breadth(rows,40,'2026-11-04')['breadth_valid']
    assert not e.compute_universe_breadth(rows,40,'')['breadth_valid']

