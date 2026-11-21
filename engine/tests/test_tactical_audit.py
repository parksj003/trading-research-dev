"""Independent price/volume oracles. No network or operational writes.

Prior pivot 100, prior close 98, prior 50 volumes 100:
99 after high 104 = intraday excess only; 100 = equality accepted by legacy
daily qualification; 105 = inclusive upper edge; 106 = outside buy zone.
Missing today's volume must never borrow yesterday's surge.
"""
from copy import deepcopy
import numpy as np
import pandas as pd
import pytest
from watchlist import trend_watchlist as e


def prices(close=101., high=None, volume=150.):
    c = np.r_[np.linspace(40., 90., 220), np.full(79, 98.), close]
    h = pd.DataFrame(dict(Open=c, High=np.minimum(c+2, 100.), Low=c-2,
                          Close=c, Volume=100.), index=pd.bdate_range('2024-01-02', periods=300))
    h.iloc[-1, h.columns.get_loc('High')] = high or max(close, 104.)
    h.iloc[-1, h.columns.get_loc('Volume')] = volume
    return h


def feature(h):
    return e.compute_features(h, h.assign(Close=100.), deepcopy(e.DEFAULT_CONFIG))


@pytest.mark.parametrize('close,high,brk,zone', [
    (99.,104.,False,True), (100.,104.,True,True),
    (105.,106.,True,True), (106.,107.,False,False), (97.5,100.,False,True),
    (97.499,100.,False,False),
])
def test_hand_calculated_pivot_and_inclusive_zone(close, high, brk, zone):
    f=feature(prices(close, high))
    assert f['pivot_price']==100.
    assert f['is_breakout'] is brk
    assert f['in_buy_zone'] is zone


def test_missing_current_volume_never_borrows_previous_surge():
    h=prices(volume=np.nan);h.iloc[-2,h.columns.get_loc('Volume')]=1000.
    f=feature(h)
    assert np.isnan(f['volume_ratio']) and np.isnan(f['vol_ratio_50d'])
    assert not f['is_breakout'] and not f['is_actionable']


def test_previous_fifty_sessions_do_not_skip_a_missing_volume():
    h=prices();h.iloc[-5,h.columns.get_loc('Volume')]=np.nan
    assert np.isnan(feature(h)['vol_ratio_50d'])


@pytest.mark.parametrize('volume,expected', [(149.999,False),(150.,True),(0.,False)])
def test_relative_volume_denominator_excludes_today(volume,expected):
    f=feature(prices(volume=volume))
    assert f['vol_ratio_50d']==pytest.approx(volume/100)
    assert f['is_breakout'] is expected


def test_appending_future_bars_cannot_change_prefix_decisions():
    h=prices(99.,104.)
    keys=['is_true_vcp','base_len_days','pivot_price','is_breakout','setup_stage','in_buy_zone','stop_price']
    saved={k:feature(h)[k] for k in keys}
    future=h.iloc[-1:].copy();future.index=[h.index[-1]+pd.offsets.BDay()]
    future[['Open','High','Low','Close']]=[106.,110.,105.,106.]
    full=pd.concat([h,future])
    assert {k:feature(full.loc[:h.index[-1]])[k] for k in keys}==saved
    assert saved['pivot_price']==100. and saved['is_breakout'] is False
    # Extreme current high cannot move the pre-session pivot.
    h.iloc[-1,h.columns.get_loc('High')]=10000.
    assert feature(h)['pivot_price']==100.


def test_track_ties_have_a_stable_identity_order():
    row=dict(feature(prices()),rs_score=90.,pure_rs=.2,tier='Tier A',
             sector='S',macro_theme='M',market='US',sub_market='SP500',name='S')
    rows=[dict(row,ticker=t) for t in ['Z','A','M']]
    cfg=deepcopy(e.DEFAULT_CONFIG);cfg['market']['max_candidates_per_track']=2
    a=e._select_track_candidates(rows,cfg,{},{} )[0]
    b=e._select_track_candidates(rows[::-1],cfg,{},{} )[0]
    assert a.ticker.tolist()==b.ticker.tolist()==['A','M']


def test_daily_breakout_qualification_is_not_a_first_cross_event():
    h=prices(101.)
    first=feature(h)
    tomorrow=h.iloc[-1:].copy();tomorrow.index=[h.index[-1]+pd.offsets.BDay()]
    tomorrow[['Open','High','Low','Close','Volume']]=[102.,104.,101.,103.,200.]
    second=feature(pd.concat([h,tomorrow]))
    assert first['is_breakout'] and second['is_breakout']
    assert first['pivot_price']==second['pivot_price']==100.
    assert first['pivot_inputs_as_of']==str(h.index[-2].date())
    assert second['pivot_inputs_as_of']==str(h.index[-1].date())


@pytest.mark.parametrize('kind,expected', [('contraction',True),('flat',False),('falling',True)])
def test_vcp_window_route_and_its_downtrend_limitation(kind,expected):
    # Three nonoverlapping windows have max drawdowns 12%, 6%, 2.5%.
    # A monotonically falling staircase also passes the detector: trend gating
    # occurs later. These are project rules, not a pattern quality oracle.
    blocks=[np.linspace(100,88,10),np.linspace(100,94,10),np.linspace(100,97.5,10)]
    if kind=='falling':blocks=[blocks[0],blocks[1]*.88,blocks[2]*.8272]
    c=np.r_[np.full(20,101.),np.concatenate(blocks)] if kind!='flat' else np.full(50,100.)
    h=pd.DataFrame(dict(Close=c,High=c+.1,Low=c-.1,Volume=np.linspace(100,30,50)),index=pd.bdate_range('2024-01-02',periods=50))
    result=e._detect_swing_vcp_and_base(h,.02,True,False)
    assert result[0] is expected
    assert e._detect_swing_vcp_and_base(h,.02,True,True)[0] is False


def test_pullback_proximity_does_not_require_a_prior_breakout_or_recovery():
    args=dict(last=120.,sma20=119.,sma50=110.,sma150=100.,sma200=90.,dist_high=-.05,
              is_breakout=False,is_true_vcp=False,range_10d_pct=.15,vol_dry_up=1.3,
              pocket_pivot=False,atr14_pct=.04)
    assert e.classify_alltoo_setup(**args)[0]=='🧲 이평선눌림목'
    assert e.classify_alltoo_setup(**dict(args,sma50=125.,sma150=130.,sma200=140.))[0]=='👀 추세회복'


def test_thirty_eight_candidates_refill_to_fifteen_after_two_missing_prices(tmp_path,monkeypatch):
    monkeypatch.setattr(e,'US_HISTORY_CACHE_DIR',tmp_path)
    monkeypatch.setattr(e,'us_completed_cached_bars',lambda path,h:h)
    h=prices();records=[]
    for i in range(38):
        ticker=f'T{i:03}'
        if i not in (0,3):h.to_pickle(tmp_path/f'{ticker}.pkl')
        records.append(dict(market='US',ticker=ticker,as_of=str(h.index[-1].date())))
    rows=pd.DataFrame(records)
    charts,cov=e._charts_for_publication(pd.DataFrame(),rows,rows,{'chart_scope':'selected','chart_max':15})
    assert list(charts)==[f'US:T{i:03}' for i in range(17) if i not in (0,3)]
    assert cov['reason_counts']=={'cache_missing':2,'limit_reached':21}
    assert len(charts)+len(cov['missing'])==38


def test_chart_preserves_subcent_prices_and_unknown_volume(tmp_path,monkeypatch):
    monkeypatch.setattr(e,'US_HISTORY_CACHE_DIR',tmp_path)
    monkeypatch.setattr(e,'us_completed_cached_bars',lambda path,h:h)
    h=prices(104.99999,105.,np.nan);h.to_pickle(tmp_path/'X.pkl')
    chart=e._build_charts(pd.DataFrame([dict(market='US',ticker='X',as_of=str(h.index[-1].date()),pivot_price=100.)]),260,None)['US:X']
    assert chart['c'][-1]==104.99999 and chart['v'][-1] is None
    assert chart['line_basis']=='current_snapshot_not_historical_signal'


def test_signal_entry_reference_exact_lower_edge_and_history_unchanged(tmp_path):
    row=dict(market='US',ticker='X',last_close=97.5,pivot_price=100.,dist_to_pivot_pct=97.5/100-1,
             as_of='2026-11-13',stop_price=93.)
    assert e.update_signal_ledger(tmp_path,pd.DataFrame([row]),'2026-11-14')==1
    path=tmp_path/'history/signals.csv';original=path.read_bytes()
    assert pd.read_csv(path).entry_ref.iloc[0]==100.
    assert e.update_signal_ledger(tmp_path,pd.DataFrame([row]),'2026-11-15')==0
    assert path.read_bytes()==original


def test_fixed_window_classification_can_change_when_one_old_drop_leaves():
    # Only the first bar of the first ten-bar window changes membership.
    c=np.r_[np.full(20,150.),100.,np.full(9,90.),np.linspace(90.,88.2,10),np.linspace(88.2,87.759,10)]
    h=pd.DataFrame(dict(Close=c,High=c,Low=c,Volume=100.),index=pd.bdate_range('2024-01-02',periods=50))
    before=e._detect_swing_vcp_and_base(h,.02,True,False)
    next_bar=h.iloc[-1:].copy();next_bar.index=[h.index[-1]+pd.offsets.BDay()]
    after=e._detect_swing_vcp_and_base(pd.concat([h,next_bar]),.02,True,False)
    assert before[0] is True and after[0] is False


def test_gap_open_is_not_an_additional_breakout_requirement():
    h=prices();baseline=feature(h)
    h.iloc[-1,h.columns.get_loc('Open')]=103.
    f=feature(h)
    assert f['is_breakout'] is baseline['is_breakout'] is True
    assert f['pivot_price']==baseline['pivot_price']==100.


def test_failure_and_recross_are_daily_conditions_not_new_event_dates():
    h=prices();states=[feature(h)['is_breakout']]
    for close in (99.,101.):
        bar=h.iloc[-1:].copy();bar.index=[h.index[-1]+pd.offsets.BDay()]
        bar[['Open','High','Low','Close','Volume']]=[close,104.,97.,close,200.]
        h=pd.concat([h,bar]);states.append(feature(h)['is_breakout'])
    assert states==[True,False,True]


def test_last_missing_close_cannot_mix_future_high_and_volume_into_old_date():
    h=prices();h.iloc[-1,h.columns.get_loc('Close')]=np.nan
    before=feature(h.iloc[:-1]);after=feature(h)
    for k in ('as_of','pivot_price','vol_ratio_50d','stop_price','is_breakout'):
        assert after[k]==before[k]

