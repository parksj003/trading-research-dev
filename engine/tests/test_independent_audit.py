"""A-01..A-09 synthetic audit evidence; no real provider/broker/GitHub requests."""
from copy import deepcopy
from datetime import datetime, timezone
import os
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from watchlist import trend_watchlist as engine
from watchlist import analysis_report as research


def prices(dates, close=100.):
    d = pd.DataFrame(index=pd.to_datetime(dates))
    d['Open'] = close; d['High'] = close+2; d['Low'] = close-2; d['Close'] = close; d['Volume'] = 1000.
    d['_session_finality_version'] = 2
    d['_session_confirmed'] = True
    return research.tag_raw_price_quality(d)


def fixed_clock(monkeypatch, value='2026-11-04T21:00:00+09:00'):
    moment = datetime.fromisoformat(value)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)
    monkeypatch.setattr(engine, 'datetime', Clock)
    monkeypatch.setattr(engine.time,'time',lambda:moment.timestamp())
    original = pd.DataFrame.to_pickle
    def write(frame,path,*args,**kwargs):
        original(frame,path,*args,**kwargs)
        if isinstance(path,(str,os.PathLike)): os.utime(path,(moment.timestamp(),moment.timestamp()))
    monkeypatch.setattr(pd.DataFrame,'to_pickle',write)


def test_a01_legacy_and_primary_have_different_dates_and_publication_requirements(tmp_path, monkeypatch):
    fixed_clock(monkeypatch)
    cache = tmp_path/'cache'; cache.mkdir()
    history = tmp_path/'published/history'; history.mkdir(parents=True)
    monkeypatch.setattr(engine, 'ROOT', tmp_path)
    monkeypatch.setattr(engine, 'REPORT_DIR', tmp_path/'reports')
    monkeypatch.setattr(engine, 'US_HISTORY_CACHE_DIR', cache)
    dates = pd.bdate_range('2026-10-01', periods=15)
    benchmark = prices(dates); benchmark['Close'] += np.arange(len(dates))
    stock = prices(dates.delete(3))  # A stock session is absent, benchmark remains complete.
    stock.to_pickle(cache/'EX.pkl')
    monkeypatch.setattr(engine, '_make_bench_getter', lambda _: lambda *args: benchmark)
    rows = [dict(date=date, as_of=date, market='US', sub_market='SP500', ticker='EX',
                 setup_stage='VCP', selected=True) for date in ('2026-10-01','2026-10-02','2026-10-05')]
    pd.DataFrame(rows).to_csv(history/'candidates_daily.csv', index=False)
    signal = dict(first_seen='2026-10-01',as_of='2026-10-01',market='US',sub_market='SP500',ticker='EX',
                  setup_stage='VCP',stop_price=90.,signal_close=100.,
                  generated_at='2026-10-02T23:00:00+09:00',published_at='2026-10-02T23:05:00+09:00')
    pd.DataFrame([signal]).to_csv(history/'signals.csv',index=False)
    cfg=deepcopy(engine.DEFAULT_CONFIG)
    legacy = engine.compute_forward_validation('2026-11-04',cfg)
    assert legacy.iloc[-1]['sample_cnt'] == 1
    engine.update_signal_outcomes(tmp_path/'published',cfg)
    outcomes = pd.read_csv(history/'signal_outcomes.csv')
    assert outcomes.iloc[0]['calendar_complete_10d'] == False
    assert outcomes.iloc[0]['ex_10d'] != legacy.iloc[-1]['avg_ex_10d']
    result = research.validation_statistics(pd.DataFrame([signal]),outcomes,
                                           {'as_of':{'SP500':'2026-11-03'}})
    assert result['quality']['entry_before_signal'] == 1
    assert all(g['completed']==0 for g in result['groups'])


@pytest.mark.parametrize('previous_close',[99.,101.])
def test_a04_breakout_includes_first_cross_and_continuation_when_last_five_days_are_outside_pivot(previous_close):
    d=prices(pd.bdate_range('2025-01-02',periods=280),99.)
    d['High']=100.;d['Low']=98.
    d.iloc[-5:,d.columns.get_loc('Close')]=previous_close
    d.iloc[-5:,d.columns.get_loc('High')]=previous_close+1
    d.iloc[-1,d.columns.get_loc('Close')]=103.
    d.iloc[-1,d.columns.get_loc('High')]=104.
    d.iloc[-1,d.columns.get_loc('Volume')]=2000.
    feature=engine.compute_features(d,prices(d.index),deepcopy(engine.DEFAULT_CONFIG))
    assert feature['pivot_price']==100. and feature['is_breakout'] is True
    assert (previous_close > feature['pivot_price']) == (previous_close==101.)


def test_a05_one_session_or_jointly_old_prices_are_not_rejected_by_relative_stale_rule():
    cfg=deepcopy(engine.DEFAULT_CONFIG)
    stock=prices(pd.bdate_range('2025-01-02','2026-11-02'))
    bench=prices(pd.bdate_range('2025-01-02','2026-11-03'))
    f=engine.compute_features(stock,bench,cfg)
    assert f['stale_bars']==1 and f['is_stale_price'] is False
    joint=engine.compute_features(stock,stock,cfg)
    assert joint['stale_bars']==0 and joint['is_stale_price'] is False


def test_a05_us_morning_ttl_refreshes_last_evenings_previous_close(tmp_path,monkeypatch):
    fixed_clock(monkeypatch,'2026-11-05T07:00:00+09:00')
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',tmp_path)
    d=prices(pd.bdate_range('2023-01-02','2026-11-03'));path=tmp_path/'SPY.pkl';d.to_pickle(path)
    last_evening=datetime.fromisoformat('2026-11-04T19:05:00+09:00').timestamp()
    now=datetime.fromisoformat('2026-11-05T07:00:00+09:00').timestamp()
    os.utime(path,(last_evening,last_evening));monkeypatch.setattr(engine.time,'time',lambda:now)
    calls=[]
    def download(**kwargs):
        calls.append(kwargs)
        return prices(pd.bdate_range('2026-10-27','2026-11-04'))
    monkeypatch.setitem(sys.modules,'yfinance',SimpleNamespace(download=download))
    result=engine.fetch_us_histories(['SPY'],3,deepcopy(engine.DEFAULT_CONFIG))
    assert result['SPY'].index[-1] == pd.Timestamp('2026-11-04')
    assert len(calls)==1 and calls[0]['start']=='2026-10-27'
    engine.fetch_us_histories(['SPY'],3,deepcopy(engine.DEFAULT_CONFIG))
    assert len(calls)==1


def test_a07_leading_is_not_breadth_acceleration_or_trade_eligibility():
    assert 'Leading)' in engine._classify_rotation_phase(90.,1.,20.)
    assert 'Leading ↑' in engine._classify_rotation_phase(90.,5.,65.)
    assert 'Leading ↑' not in engine._classify_rotation_phase(90.,5.,20.)


def test_a08_rs_change_is_re_ranked_on_current_population_not_archived_rank(monkeypatch):
    monkeypatch.setattr(engine,'score_candidate',lambda *_:(True,0.,''))
    a=dict(ticker='A',raw_rs=.5,raw_rs_fast=.5,raw_rs_prev20=.1)
    b=dict(ticker='B',raw_rs=.2,raw_rs_fast=.2,raw_rs_prev20=.2)
    c=dict(ticker='C',raw_rs=.8,raw_rs_fast=.8,raw_rs_prev20=.05)
    cfg=deepcopy(engine.DEFAULT_CONFIG)
    old=engine.apply_percentile_rs_and_filter([a,b],cfg,'SP500')[0][0]
    new=engine.apply_percentile_rs_and_filter([a,b,c],cfg,'SP500')[0][0]
    assert old['rs_change_4w']==49. and new['rs_change_4w']==0.


def test_a09_calendar_day_cooldown_suppresses_reentry_and_repeated_runs_are_idempotent(tmp_path):
    action=pd.DataFrame([dict(market='US',sub_market='SP500',ticker='EX',last_close=100.,stop_price=90.)])
    assert engine.update_signal_ledger(tmp_path,action,'2026-10-20')==1
    assert engine.update_signal_ledger(tmp_path,action,'2026-10-20')==0
    assert engine.update_signal_ledger(tmp_path,action,'2026-11-01')==0
    assert engine.update_signal_ledger(tmp_path,action,'2026-11-03')==1
    assert len(pd.read_csv(tmp_path/'history/signals.csv'))==2


def test_a02_breadth_precedes_final_ndx_cap_and_liquidity_filters(monkeypatch):
    cfg=deepcopy(engine.DEFAULT_CONFIG)
    rows=[dict(ticker=t,in_nasdaq100=ndx,in_sp500=sp,name=t) for t,ndx,sp in
          [('BOTH',True,True),('NDX12B',True,False),('SP',False,True),('LOWPRICE',False,True),('SHORT',False,True)]]
    universe=pd.DataFrame(rows)
    caps={row['ticker']:12e9 if row['ticker']=='NDX12B' else 20e9 for row in rows}
    history={t:prices(pd.bdate_range('2025-01-01',periods=260),5. if t=='LOWPRICE' else 100.) for t in caps}
    history['SHORT']=history['SHORT'].tail(100)
    history.update(SPY=prices(history['SP'].index),QQQ=prices(history['SP'].index))
    monkeypatch.setattr(engine,'get_us_universe',lambda *_:universe)
    monkeypatch.setattr(engine,'get_us_market_caps',lambda *a,**k:(caps,{},{},{}))
    monkeypatch.setattr(engine,'fetch_us_histories',lambda *a,**k:history)
    _,_,_,regimes,_=engine.run_us_tracks(cfg,{},{})
    assert regimes['NASDAQ100']['breadth_total']==2 and regimes['NASDAQ100']['breadth_valid_count']==2
    assert regimes['SP500']['breadth_total']==3 and regimes['SP500']['breadth_valid_count']==1
    definitions=research.metric_definitions(cfg)
    assert 'ex-NDX' in definitions['breadth_sp500']
    assert '거래대금' in definitions['breadth_filters'] and '이후 적용' in definitions['breadth_filters']


@pytest.mark.parametrize('value,expected', [('2026-11-05T07:00:00+09:00','2026-11-04'),
                                         ('2026-07-07T07:00:00+09:00','2026-07-06')])
def test_a05_us_standard_and_daylight_time_session_dates(value,expected,monkeypatch):
    fixed_clock(monkeypatch,value)
    assert str(engine.us_close_check_date().date())==expected
    obs=research.price_observation({'SP500':{'as_of':expected}},pd.DataFrame(),value)
    assert obs['tracks']['SP500']['clock_reference_date']==expected
    assert obs['tracks']['SP500']['session_unconfirmed'] is False


@pytest.mark.parametrize('response',['empty','overlap'])
def test_a05_us_provider_delay_does_not_touch_good_cache_or_fetch_full_history(tmp_path,monkeypatch,response):
    fixed_clock(monkeypatch,'2026-11-05T07:00:00+09:00')
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',tmp_path)
    path=tmp_path/'SPY.pkl';prices(pd.bdate_range('2023-01-02','2026-11-03')).to_pickle(path)
    stamp=datetime.fromisoformat('2026-11-04T19:05:00+09:00').timestamp();os.utime(path,(stamp,stamp))
    before=path.read_bytes(),path.stat().st_mtime_ns;calls=[]
    def download(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame() if response=='empty' else prices(pd.bdate_range('2026-10-27','2026-11-03'))
    monkeypatch.setitem(sys.modules,'yfinance',SimpleNamespace(download=download))
    result=engine.fetch_us_histories(['SPY'],3,deepcopy(engine.DEFAULT_CONFIG))
    assert result['SPY'].index[-1]==pd.Timestamp('2026-11-03')
    assert len(calls)==1 and calls[0]['start']=='2026-10-27'
    assert before==(path.read_bytes(),path.stat().st_mtime_ns)
    obs=research.price_observation({'SP500':{'as_of':'2026-11-03'}},pd.DataFrame(),'2026-11-05T07:00:00+09:00')
    assert obs['tracks']['SP500']['session_unconfirmed'] is True


def test_a06_new_download_retains_invalid_raw_open_after_existing_normalization(tmp_path,monkeypatch):
    fixed_clock(monkeypatch)
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',tmp_path)
    raw=prices(pd.bdate_range('2026-10-01',periods=5)).drop(columns=['_raw_open_valid','_raw_ohlc_valid'])
    raw.iloc[1,raw.columns.get_loc('Open')]=0.
    monkeypatch.setitem(sys.modules,'yfinance',SimpleNamespace(download=lambda **_:raw))
    frame=engine.fetch_us_histories(['EX'],3,deepcopy(engine.DEFAULT_CONFIG))['EX']
    assert frame['Open'].iloc[1]>0  # Existing normalization remains unchanged.
    assert not bool(frame['_raw_open_valid'].iloc[1]) and not bool(frame['_raw_ohlc_valid'].iloc[1])
    assert frame['_raw_open_valid'].iloc[0]
    saved=pd.read_pickle(tmp_path/'EX.pkl')
    assert not bool(saved['_raw_open_valid'].iloc[1])


def test_a06_new_verified_cohort_matures_without_promoting_legacy_or_rewriting_signal_ledger(tmp_path,monkeypatch):
    cache=tmp_path/'prices';cache.mkdir();base=tmp_path/'published'
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',cache)
    cfg=deepcopy(engine.DEFAULT_CONFIG)
    signal=pd.DataFrame([dict(market='US',sub_market='SP500',ticker='EX',as_of='2026-10-01',
                             last_close=100.,stop_price=90.,setup_stage='VCP')])
    assert engine.update_signal_ledger(base,signal,'2026-10-02',
        generated_at='2026-10-02T06:00:00+09:00',published_at='2026-10-02T07:00:00+09:00')==1
    ledger=base/'history/signals.csv';before=ledger.read_bytes()
    days=pd.bdate_range('2026-10-01',periods=21)
    for bars in [0,5,10,20]:
        last=days[bars]
        at=last.tz_localize('America/New_York')+pd.Timedelta(hours=18)
        fixed_clock(monkeypatch,at.isoformat())
        frame=prices(days[:bars+1])
        frame.to_pickle(cache/'EX.pkl');frame.to_pickle(cache/'SPY.pkl')
        engine.update_signal_outcomes(base,cfg)
        outcomes=pd.read_csv(base/'history/signal_outcomes.csv')
        result=research.validation_statistics(pd.read_csv(ledger),outcomes,{'as_of':{'SP500':str(last.date())}})
        assert result['quality']['entry_eligible_rows']==(1 if bars else 0)
        groups=[r for r in result['groups'] if r['dimension']=='market' and r['window']=='all']
        for row in groups: assert row['completed']==int(bars>=row['horizon'])
        if bars>=10:
            row=next(r for r in groups if r['horizon']==10)
            assert row['stop_r_n']==1 and row['mean_stop_r']==pytest.approx(-.03)
        assert ledger.read_bytes()==before


def test_a05_diagnostics_distinguish_technical_eligibility_and_price_alignment_without_mutation():
    frame=pd.DataFrame([dict(sub_market='SP500',stale_bars=1,is_actionable=True),
                        dict(sub_market='SP500',stale_bars=0,is_actionable=False)])
    before=frame.copy(deep=True)
    obs=research.price_observation({'SP500':{'as_of':'2026-11-03'}},frame,'2026-11-05T07:00:00+09:00')
    track=obs['tracks']['SP500']
    assert track['session_unconfirmed'] and track['lagging_candidate_rows']==track['lagging_actionable_rows']==1
    pd.testing.assert_frame_equal(frame,before)


def test_a03_near_extremes_are_close_proximity_counts_not_new_break_counts():
    rows=[dict(above_50dma=True,above_200dma=True,distance_from_high_pct=-.029,
               distance_from_low_pct=.2,strict_minervini=True) for _ in range(30)]
    rows.append(dict(rows[0],distance_from_high_pct=-.2,distance_from_low_pct=.029))
    result=engine.compute_universe_breadth(rows,31)
    assert result['pct_above_50dma']==100. and result['breadth_valid_count']==31
    assert result['nh_nl_label']=='고점 3% 근접 30 / 저점 3% 근접 1'


def test_published_metadata_is_additive_hash_consistent_and_legacy_role_is_explicit(tmp_path,monkeypatch):
    import hashlib,json
    fixed_clock(monkeypatch,'2026-11-05T07:00:00+09:00')
    cfg=deepcopy(engine.DEFAULT_CONFIG);empty=pd.DataFrame()
    regimes={'SP500':dict(as_of='2026-11-03',regime='CAUTION',signal_light='CAUTION')}
    latest=engine.publish_artifacts('2026-11-05',{}, {},regimes,{},empty,empty,cfg,base_dir=tmp_path)
    assert latest is not None
    manifest=json.loads((latest/'manifest.json').read_text());summary=json.loads((latest/'summary.json').read_text())
    assert len(manifest['files'])==7 and engine.validate_published_dir(latest)==[]
    assert summary['price_observation']['tracks']['SP500']['session_unconfirmed'] is True
    assert 'ex-NDX' in summary['metric_definitions']['breadth_sp500']
    assert all(hashlib.sha256((latest/n).read_bytes()).hexdigest()==v['sha256'] for n,v in manifest['files'].items())
    report=json.loads((tmp_path/'analysis/latest.md').read_text().split('````json\n',1)[1].rsplit('\n````',1)[0])
    assert report['source']['run_id']==manifest['run_id'] and report['validation']['quality']['entry_eligible_rows']==0
    assert report['validation']['policy']['authority'].startswith('official')
    # No history/candidate recalculation is used to make the legacy report look verified.
    val=pd.DataFrame([dict(setup_stage='legacy',sample_cnt=1,uniq_cnt=1,win_rate_5d=100.,avg_ret_10d=1.,
        med_ret_10d=1.,avg_ex_10d=2.,win_rate_ex_10d=100.,avg_ex_20d=3.,avg_mae_10d=-1.)])
    md=engine.format_markdown_report('2026-11-05',{}, {},empty,empty,{}, {},empty,val,cfg)
    assert '공식 전략 성과 아님' in md and '종목과 벤치마크의 비교 종료일' in md
    assert '기준선보다 나을 때만' not in md


@pytest.mark.parametrize('value,closed', [('2026-11-05T06:04:00+09:00',False),('2026-11-05T06:05:00+09:00',True),
                                        ('2026-07-07T05:04:00+09:00',False),('2026-07-07T05:05:00+09:00',True)])
def test_a05_us_close_buffer_uses_new_york_clock(value,closed,monkeypatch):
    fixed_clock(monkeypatch,value)
    assert (engine.us_close_check_date() is not None)==closed


def test_a05_us_intraday_bar_is_not_promoted_to_final_close_on_provider_delay(tmp_path,monkeypatch):
    fixed_clock(monkeypatch,'2026-11-05T07:00:00+09:00')
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',tmp_path)
    path=tmp_path/'SPY.pkl';prices(pd.bdate_range('2023-01-02','2026-11-04')).to_pickle(path)
    d=pd.read_pickle(path);d.loc[d.index[-1],'_session_confirmed']=False;d.to_pickle(path)
    stamp=datetime.fromisoformat('2026-11-04T14:00:00-05:00').timestamp();os.utime(path,(stamp,stamp))
    before=path.read_bytes(),path.stat().st_mtime_ns
    monkeypatch.setitem(sys.modules,'yfinance',SimpleNamespace(download=lambda **_:pd.DataFrame()))
    hist=engine.fetch_us_histories(['SPY'],3,deepcopy(engine.DEFAULT_CONFIG))['SPY']
    bench=engine._make_bench_getter(deepcopy(engine.DEFAULT_CONFIG))('SP500','US')
    chart=engine._build_charts(pd.DataFrame([dict(market='US',ticker='SPY',as_of='2026-11-04')]),260,1)
    assert hist.index[-1]==bench.index[-1]==pd.Timestamp('2026-11-03')
    assert chart['US:SPY']['d'][-1]=='2026-11-03'
    assert before==(path.read_bytes(),path.stat().st_mtime_ns)


def test_a05_holiday_clock_does_not_claim_an_exchange_session_or_a_strategy_failure():
    obs=research.price_observation({'SP500':{'as_of':'2026-11-25'}},pd.DataFrame(),'2026-11-27T07:00:00+09:00')
    assert obs['tracks']['SP500']['clock_reference_date']=='2026-11-26'
    assert obs['tracks']['SP500']['session_unconfirmed'] is True
    assert 'does not prove' in obs['scope'] and '휴장/공급 지연' in obs['policy']


@pytest.mark.parametrize('generated,asof',[('2026-11-05T07:00:00+09:00','2026-11-06'),
                                          ('unknown','2026-11-04'),('2026-11-05T07:00:00+09:00',None)])
def test_unknown_or_future_price_dates_are_not_shown_as_confirmed(generated,asof):
    obs=research.price_observation({'SP500':{'as_of':asof}},pd.DataFrame(),generated)
    assert obs['tracks']['SP500']['session_unconfirmed'] is True


def test_invalid_breadth_report_does_not_render_a_full_strength_bar():
    empty=pd.DataFrame();cfg=deepcopy(engine.DEFAULT_CONFIG)
    regimes={'SP500':dict(as_of='2026-11-03',breadth_valid=False,pct_above_50dma=np.nan,pct_above_200dma=np.nan)}
    md=engine.format_markdown_report('2026-11-04',{}, {},empty,empty,regimes,{},empty,empty,cfg)
    assert '| 산출불가 | 산출불가 |' in md
    assert 'nan%' not in md and '`██████` **nan' not in md


def test_html_breadth_discloses_population_and_does_not_display_missing_as_nan(tmp_path):
    out=tmp_path/'viewer.html'
    regimes={'SP500':dict(as_of='2026-11-03',breadth_valid=False,pct_above_50dma=np.nan,pct_above_200dma=np.nan)}
    engine.generate_html_viewer('2026-11-04',{},regimes,pd.DataFrame(),pd.DataFrame(),[out])
    text=out.read_text()
    assert '전략용 모집단' in text and '산출불가' in text and 'nan%' not in text

