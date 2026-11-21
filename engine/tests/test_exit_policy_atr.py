from copy import deepcopy
import pandas as pd
import pytest
from watchlist.exit_policy_atr import wilder_atr,next_atr_stop
from watchlist.exit_policy_experiments import prepare,simulate
from test_exit_policy_core import fixture


def test_wilder_seed_uses_14_tr_with_previous_close_and_recurrence_not_ema():
    s,h,cut=fixture();h=h.iloc[:17].copy();h['High']=101.;h['Low']=99.;h['Close']=100.
    h.iloc[14,h.columns.get_loc('High')]=110
    atr=wilder_atr(h)
    assert atr.iloc[:14].isna().all()
    assert atr.iloc[14]==pytest.approx((13*2+11)/14)
    assert atr.iloc[15]==pytest.approx((13*atr.iloc[14]+2)/14)


def test_true_range_includes_gap_and_invalid_history_resets_seed():
    _,h,_=fixture();h=h.iloc[:35].copy();h['High']=101.;h['Low']=99.;h['Close']=100.
    h.iloc[5,h.columns.get_loc('High')]=111;h.iloc[5,h.columns.get_loc('Low')]=109;h.iloc[5,h.columns.get_loc('Close')]=110;h.iloc[5,h.columns.get_loc('Open')]=110
    a=wilder_atr(h);assert a.iloc[14]==pytest.approx((12*2+2*11)/14)
    valid=pd.Series(True,index=h.index);valid.iloc[16]=False
    a=wilder_atr(h,valid);assert a.iloc[16:31].isna().all() and pd.notna(a.iloc[31])


def test_ratchet_never_lowers_or_breaks_initial_protection():
    assert next_atr_stop(90,95,110,20)==95
    assert next_atr_stop(90,90,100,20)==90
    assert next_atr_stop(90,95,120,3)==111


def test_atr_entry_warmup_is_strategy_specific():
    s,h,cut=fixture(pre=10);p=prepare(s,h,cut)
    assert simulate(p,'ATR_TRAIL',20)['exclusion_reason']=='ATR_WARMUP_INSUFFICIENT'
    assert simulate(p,'FIXED',20)['eligibility_status']=='eligible'


def test_today_high_can_only_raise_tomorrow_stop_and_excludes_preentry_high():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');cal=h.index.get_loc(entry)
    # An old 200 high warms ATR but is never the entry-since-high anchor.
    h.iloc[cal-20,h.columns.get_loc('High')]=200
    h.loc[entry,['High','Low','Close']]=[120,95,110]
    h.loc[h.index[cal+1],['Open','Low']]=[95,94]
    p=prepare(s,h,cut);r=simulate(p,'ATR_TRAIL',20)
    assert r['holding_sessions']>=2 and r['stop_path'][0]['active_stop']==90
    assert r['stop_path'][0]['information_as_of']==str(h.index[cal-1].date())
    second=r['stop_path'][1]
    assert second['information_as_of']=='2026-01-05'
    assert second['effective_date']==str(p.path.index[1].date())
    assert second['active_stop']==pytest.approx(max(90,120-3*p.indicators['atr14'].loc[entry]))
    assert r['exit_reason'].startswith('gap_') # new stop already above next open


def test_atr_first_day_low_does_not_trigger_same_day_raised_candidate():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05')
    h.loc[entry,['High','Low','Close']]=[150,95,145]
    p=prepare(s,h,cut);r=simulate(p,'ATR_TRAIL',20)
    assert r['holding_sessions']==2 and r['exit_fill_date']==str(p.path.index[1].date())
    assert r['exit_price']==100 and r['stop_path'][0]['active_stop']==90


def test_future_ohlc_cannot_change_earlier_atr_stops_or_fill():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');h.loc[entry,['High','Low','Close']]=[150,95,145]
    before=simulate(prepare(s,h,cut),'ATR_TRAIL',20)
    h.loc[h.index>pd.Timestamp(before['exit_fill_date']),['High','Low','Close']]=[1000,1,500]
    after=simulate(prepare(s,h,cut),'ATR_TRAIL',20)
    assert before['stop_path']==after['stop_path'] and before['exit_price']==after['exit_price']
    assert before['net_R']==after['net_R']

