from copy import deepcopy
from datetime import datetime
import pandas as pd
import pytest
from watchlist.exit_policy_experiments import prepare,run_signal,simulate,entry_session,session_time
from test_exit_policy_core import fixture


def test_ma_gap_stop_conflict_executes_already_scheduled_open_first():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');dates=h.loc[h.index>=entry].index
    h.loc[dates[:2],'Close']=99;h.loc[dates[2],['Open','Low']]=[85,80]
    r=simulate(prepare(s,h,cut),'MA20_2C',20)
    assert r['exit_reason']=='next_open_ma' and r['exit_price']==85


def test_expiry_close_can_include_its_extrema_and_giveback_can_be_negative():
    s,h,cut=fixture();dates=h.loc[h.index>pd.Timestamp(s['as_of'])].index
    h.loc[dates[0],'Close']=105;h.loc[dates[0],'High']=106
    h.loc[dates[1],['Open','High','Low','Close']]=[125,130,120,128]
    p=prepare(s,h,cut);r=simulate(p,'TARGET_2R',20)
    assert r['giveback_status']=='realized_above_close_peak' and r['profit_giveback_pct']<0
    assert r['peak_held_close_net_pct']==pytest.approx(4.7)
    r=simulate(p,'HOLD',20)
    assert r['extrema_status']=='exact' and r['mfe_pct']==pytest.approx(30)


def test_giveback_can_exceed_100_percent_without_clipping():
    s,h,cut=fixture();dates=h.loc[h.index>pd.Timestamp(s['as_of'])].index
    h.loc[dates[0],['High','Close']]=[111,110];h.loc[dates[1],['Low','Close']]=[80,95]
    r=simulate(prepare(s,h,cut),'FIXED',20)
    assert r['profit_giveback_pct']>100 and r['profit_giveback_amount']>0


def test_zero_nonpositive_close_peak_and_no_held_close_are_not_fake_zero_ratios():
    s,h,cut=fixture();r=simulate(prepare(s,h,cut),'HOLD',20)
    assert r['profit_giveback_pct'] is None and r['giveback_status']=='nonpositive_close_peak'
    h.loc[pd.Timestamp('2026-01-05'),'Low']=85
    r=simulate(prepare(s,h,cut),'FIXED',20)
    assert r['profit_giveback_pct'] is None and r['peak_held_close_net_pct'] is None


def test_prices_after_early_exit_do_not_change_held_extrema_or_giveback():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');dates=h.loc[h.index>=entry].index
    h.loc[dates[1],'Low']=85;p=prepare(s,h,cut);a=simulate(p,'FIXED',20)
    h.loc[dates[2:19],['High','Low','Close']]=[200,50,100]
    b=simulate(prepare(s,h,cut),'FIXED',20)
    for key in ('mfe_lower_pct','mfe_upper_pct','mae_lower_pct','mae_upper_pct','peak_held_close_net_pct','profit_giveback_pct','net_R'):assert a[key]==b[key]
    assert a['hindsight_capture_pct']!=b['hindsight_capture_pct'] # intentionally ex-post only


def test_missing_next_open_is_not_shifted_to_later_available_day():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');h=h.drop(entry)
    rows,_=run_signal(s,h,cut);assert all(r['exclusion_reason']=='ENTRY_OPEN_MISSING' for r in rows)


def test_no_unfinished_day_completes_horizon_and_pending_entry_is_distinct():
    s,h,cut=fixture(length=20);cut=session_time('US',h.index[-1],'close')-pd.Timedelta('1min')
    rows,_=run_signal(s,h,cut);assert not any(r.get('comparison_eligible') for r in rows)
    s,h,_=fixture();cut=pd.Timestamp('2026-01-05T13:00:00Z');rows,_=run_signal(s,h,cut)
    assert all(r['completion_status']=='entry_pending' for r in rows)


def test_kr_special_open_and_us_dst_early_close_use_exchange_clock():
    assert session_time('KR','2026-11-19','open')==pd.Timestamp('2026-11-19T01:00:00Z')
    assert session_time('KR','2026-11-19','close')==pd.Timestamp('2026-11-19T07:30:00Z')
    assert session_time('US','2026-03-06','open').hour==14
    assert session_time('US','2026-03-09','open').hour==13
    assert session_time('US','2026-11-27','close').hour==18
    s=dict(market='KR',as_of='2026-11-18',generated_at='2026-11-18T08:00:00Z',published_at='2026-11-19T00:30:00Z',publication_verified=True)
    assert entry_session(s)==pd.Timestamp('2026-11-19')


def test_initial_stop_equal_entry_is_r_ineligible_but_hold_return_eligible():
    s,h,cut=fixture();s['stop_price']=100
    rows,_=run_signal(s,h,cut);assert all(r.get('net_R') is None for r in rows)
    assert all(r['net_return_pct'] is not None for r in rows if r['rule_id']=='HOLD')

