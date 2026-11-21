import pandas as pd
import pytest
from watchlist.exit_policy_experiments import prepare,simulate,RULES
from watchlist.exit_policy_donchian import donchian10,sell_stop
from test_exit_policy_core import fixture


def test_exactly_seven_fixed_policies():
    assert RULES==('HOLD','FIXED','TARGET_2R','MA20_2C','MA50_2C','ATR_TRAIL','DONCHIAN_10')


def test_prior_ten_low_excludes_current_and_can_decline_without_ratchet():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');h.loc[entry,'Low']=95
    p=prepare(s,h,cut);line=p.indicators['donchian10']
    assert line.loc[entry]==98 and line.loc[p.path.index[1]]==95
    assert pd.isna(donchian10(h,p.validity).iloc[9])


def test_gap_through_multiple_stops_fills_open_with_deterministic_higher_stop_reason():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');h.loc[entry,'Low']=99
    second=h.index[h.index>entry][0];h.loc[second,['Open','High','Low','Close']]=[85,102,80,100]
    p=prepare(s,h,cut);r=simulate(p,'DONCHIAN_10',20)
    assert r['exit_price']==85 and r['exit_reason']=='gap_donchian_stop'
    assert r['triggered_stops']==['donchian_stop','initial_stop']
    assert r['net_R']< -1


def test_higher_fixed_stop_and_equal_stop_priority_are_explicit():
    assert sell_stop(99,98)==(99,'initial_stop')
    assert sell_stop(98,98)==(98,'initial_stop')
    s,h,cut=fixture();s['stop_price']=99
    p=prepare(s,h,cut);r=simulate(p,'DONCHIAN_10',20)
    assert r['exit_price']==99 and r['exit_reason']=='touch_initial_stop'


def test_entry_channel_at_or_above_open_is_only_donchian_ineligible():
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05');last10=h.index[h.index<entry][-10:];h.loc[last10,'Low']=100
    p=prepare(s,h,cut)
    assert simulate(p,'DONCHIAN_10',20)['exclusion_reason']=='DONCHIAN_ENTRY_ALREADY_BREACHED'
    assert simulate(p,'FIXED',20)['eligibility_status']=='eligible'


def test_channel_touch_uses_prior_low_and_short_history_excludes_only_channel():
    s,h,cut=fixture();p=prepare(s,h,cut);r=simulate(p,'DONCHIAN_10',20)
    assert r['exit_price']==98 and r['exit_reason']=='touch_donchian_stop'
    assert r['stop_path'][0]['active_stop']==p.indicators['donchian10'].loc[p.entry]
    s,h,cut=fixture(pre=5);p=prepare(s,h,cut)
    assert simulate(p,'DONCHIAN_10',20)['exclusion_reason']=='DONCHIAN_WARMUP_INSUFFICIENT'

