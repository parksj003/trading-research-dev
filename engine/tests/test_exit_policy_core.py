from copy import deepcopy
import pandas as pd
import pytest
from watchlist.exit_policy_experiments import prepare,simulate,run_signal,entry_session,session_time,RULES
from watchlist.session_calendar import get_calendar


def fixture(length=120,pre=80):
    cal=get_calendar('US');entry=pd.Timestamp('2026-01-05');dates=cal.sessions_in_range(cal.session_offset(entry,-pre),cal.session_offset(entry,length-1))
    h=pd.DataFrame(dict(Open=100.,High=102.,Low=98.,Close=100.,Volume=1000.,_raw_ohlc_valid=True,_raw_open_valid=True,_session_confirmed=True,_base_price_basis='yfinance:auto_adjust=True',IsHalt=False,**{'Stock Splits':0.}),index=dates)
    asof=cal.previous_session(entry)
    s=dict(first_seen='2026-01-02',as_of=str(asof.date()),market='US',sub_market='SP500',ticker='SYNTH',name='Synthetic fixture',
        stop_price=90.,signal_close=100.,run_id='source-run',config_hash='config-one',engine_version='test',engine_commit='a'*40,
        generated_at='2026-01-02T22:00:00Z',published_at='2026-01-02T22:05:00Z',publication_verified=True,sector='Synthetic',regime='ATTACK')
    cutoff=session_time('US',dates[-1],'close')+pd.Timedelta('1h')
    return s,h,cutoff


def prep(length=120,pre=80):
    s,h,cutoff=fixture(length,pre);return prepare(s,h,cutoff)


def test_all_base_rules_share_entry_risk_cost_and_entry_day_is_one():
    s,h,cut=fixture();rows,p=run_signal(s,h,cut)
    assert len(rows)==len(RULES)*3
    for n in (20,60,120):
        group=[r for r in rows if r['horizon']==n]
        assert {r['entry_date'] for r in group}=={'2026-01-05'}
        assert {r['entry_price'] for r in group}=={100.} and {r['initial_risk'] for r in group}=={10.}
        assert {r['total_cost'] for r in group}=={.3}
        assert next(r for r in group if r['rule_id']=='HOLD')['holding_sessions']==n
        assert next(r for r in group if r['rule_id']=='HOLD')['exit_fill_date']==str(p.path.index[n-1].date())
        assert all(r['net_R']==pytest.approx(r['net_return_pct']/10) for r in group)


def test_publication_after_open_and_stale_asof_use_first_executable_later_session():
    s,_,_=fixture();s['published_at']='2026-01-05T15:00:00Z'
    assert str(entry_session(s).date())=='2026-01-06'
    s['published_at']='2026-01-12T14:30:00Z';assert str(entry_session(s).date())=='2026-01-13'
    s['publication_verified']=False
    with pytest.raises(ValueError,match='UNVERIFIED'):entry_session(s)


@pytest.mark.parametrize('opening,high,low,expected,reason',[(100,120,99,120,'touch_target_2r'),(100,125,85,90,'touch_initial_stop'),(80,101,75,80,'gap_initial_stop'),(125,130,85,125,'gap_target_2r')])
def test_target_touch_ambiguity_and_gap_order(opening,high,low,expected,reason):
    p=prep();day=p.path.index[1];p.path.loc[day,['Open','High','Low','Close']]=[opening,high,low,100]
    r=simulate(p,'TARGET_2R',20)
    assert r['exit_price']==expected and r['exit_reason']==reason
    assert r['intrabar_ambiguous']==(opening==100 and low==85)
    assert r['net_R']==pytest.approx((expected-100-.3)/10)


def test_expiry_intraday_stop_precedes_close_and_day20_ma_cannot_fill_day21():
    p=prep();last=p.path.index[19];p.path.loc[last,['Low','Close']]=[85,99]
    r=simulate(p,'FIXED',20);assert r['exit_price']==90 and r['exit_fill_date']==str(last.date())
    p=prep();p.path.loc[p.path.index[18:20],'Close']=99
    r=simulate(p,'MA20_2C',20);assert r['exit_reason']=='horizon_close' and r['holding_sessions']==20


@pytest.mark.parametrize('rule',['MA20_2C','MA50_2C'])
def test_ma_two_closes_fill_next_open_and_recovery_resets(rule):
    p=prep();p.path.loc[p.path.index[:4],'Close']=[99,100,99,99]
    p.path.loc[p.path.index[4],'Open']=97
    r=simulate(p,rule,20)
    assert r['exit_reason']=='next_open_ma' and r['holding_sessions']==5 and r['exit_price']==97
    assert r['exit_signal_date']==str(p.path.index[3].date())
    p=prep();r=simulate(p,rule,20);assert r['exit_reason']=='horizon_close' # equality is not below


def test_close_exit_pending_precedes_day_intraday_target_or_stop():
    p=prep();p.path.loc[p.path.index[:2],'Close']=99
    p.path.loc[p.path.index[2],['Open','High','Low','Close']]=[95,200,70,100]
    r=simulate(p,'MA20_2C',20);assert r['exit_reason']=='next_open_ma' and r['exit_price']==95
    assert r['extrema_status']=='exact' and r['mfe_pct']==pytest.approx(2.)


def test_missing_initial_risk_preserves_hold_simple_return_but_excludes_r():
    s,h,cut=fixture();s['stop_price']=None;rows,_=run_signal(s,h,cut)
    hold=[r for r in rows if r['rule_id']=='HOLD'];assert all(r['net_return_pct']==pytest.approx(-.3) and r['net_R'] is None for r in hold)
    assert all(r['eligibility_status']=='excluded' for r in rows if r['rule_id']!='HOLD')


def test_short_ma_history_is_policy_specific_and_no_interpolation():
    s,h,cut=fixture(pre=10);p=prepare(s,h,cut)
    assert simulate(p,'FIXED',20)['eligibility_status']=='eligible'
    assert simulate(p,'MA50_2C',20)['exclusion_reason']=='MA50_WARMUP_INSUFFICIENT'
    s,h,cut=fixture();h=h.drop(h.loc[h.index>'2026-01-05'].index[2]);p=prepare(s,h,cut)
    assert simulate(p,'HOLD',20)['exclusion_reason']=='HOLDING_PATH_INVALID'


def test_intraday_extrema_use_bounds_not_exit_day_afternoon_prices():
    p=prep();p.path.loc[p.entry,['High','Low']]=[200,80]
    r=simulate(p,'FIXED',20)
    assert r['mfe_pct'] is None and r['mae_pct'] is None and r['peak_held_close_net_pct'] is None
    assert r['mfe_lower_pct']==0 and r['mfe_upper_pct']==100
    assert r['mae_lower_pct']==pytest.approx(-20) and r['mae_upper_pct']==pytest.approx(-10)


def test_early_fill_before_maturity_is_not_a_completed_comparison():
    s,h,cut=fixture(length=3);h.loc[h.index[-1],'Low']=85
    rows,p=run_signal(s,h,cut);r=next(r for r in rows if r['rule_id']=='FIXED' and r['horizon']==20)
    assert r['completion_status']=='closed_waiting_maturity' and not r['comparison_eligible']


@pytest.mark.parametrize('change',['halt','split','unconfirmed','ohlc','basis','entry_missing'])
def test_bad_inputs_are_excluded_without_fabrication(change):
    s,h,cut=fixture();entry=pd.Timestamp('2026-01-05')
    if change=='halt':h.loc[entry,'IsHalt']=True
    elif change=='split':h.loc[entry,'Stock Splits']=2
    elif change=='unconfirmed':h.loc[entry,'_session_confirmed']=False
    elif change=='ohlc':h.loc[entry,'High']=50
    elif change=='basis':h.loc[entry,'_base_price_basis']='unmatched'
    else:h=h.drop(entry)
    rows,_=run_signal(s,h,cut);assert all(r['eligibility_status']=='excluded' for r in rows)


