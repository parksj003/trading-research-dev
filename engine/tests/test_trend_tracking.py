from pathlib import Path
import hashlib
import json

import exchange_calendars as xcals
import numpy as np
import pandas as pd
import pytest
from watchlist.trend_tracking import observe, update_tracking, VERSION


def fixture(n=25, market='US'):
    cal=xcals.get_calendar('XNYS' if market=='US' else 'XKRX')
    dates=cal.sessions_window('2025-11-03',182)
    if market=='KR':
        # Official KRX closures, missing from exchange-calendars 4.13.2.
        dates=dates[~dates.isin(pd.to_datetime(['2026-06-03','2026-07-17']))]
    index=dates[:60+n]
    h=pd.DataFrame({'Open':100.,'High':102.,'Low':98.,'Close':100.,'_raw_ohlc_valid':True,
                    '_raw_open_valid':True,'_session_confirmed':True},index=index)
    asof=index[59];entry=index[60] if n else dates[60]
    s=dict(first_seen=asof.strftime('%Y-%m-%d'),as_of=asof.strftime('%Y-%m-%d'),market=market,
           ticker='005930' if market=='KR' else 'TEST',sub_market='KOSPI' if market=='KR' else 'SP500',
           name='Example',sector='Tech',stop_price=90.,signal_close=100.,
           generated_at=(cal.session_open(entry)-pd.Timedelta(hours=2)).isoformat(),
           published_at=(cal.session_open(entry)-pd.Timedelta(hours=1)).isoformat(),run_id='source',config_hash='fixed')
    now=cal.session_close(index[-1])+pd.Timedelta(minutes=1) if n else cal.session_open(entry)-pd.Timedelta(minutes=1)
    return s,h,now


@pytest.mark.parametrize('n',[0,1,5,10,20,21,60,120])
@pytest.mark.parametrize('market',['KR','US'])
def test_periods_use_actual_sessions(n,market):
    s,h,now=fixture(n,market);r,d=observe(s,h,now)
    if not n:assert r['status']=='진입 대기' and not d;return
    assert r['bars']==n and len(d)==n and r['hold_return_pct']==pytest.approx(-.3)
    assert r['status']==('최대 관찰 기간 도달' if n==120 else '추적 진행')
    assert r.get('ret_20d')==pytest.approx(-.3) if n>=20 else r.get('ret_20d') is None
    assert (r.get('ret_60d') is not None)==(n>=60)


def test_giveback_is_not_high_drawdown_and_nonpositive_peak_is_unknown():
    s,h,now=fixture(25);h.iloc[-3:,h.columns.get_loc('High')]=[201,180,142]
    h.iloc[-3:,h.columns.get_loc('Close')]=[200,175,140]
    r,d=observe(s,h,now,cost_pct=0)
    assert r['peak_close_return_pct']==100 and r['hold_return_pct']==pytest.approx(40)
    assert r['profit_giveback_pct']==pytest.approx(60)
    assert r['high_drawdown_pct']==pytest.approx((140/201-1)*100)
    s,h,now=fixture(5);r,d=observe(s,h,now)
    assert r['profit_giveback_pct'] is None and r['max_profit_giveback_pct'] is None


@pytest.mark.parametrize('gap',[True,False])
def test_stop_frozen_and_price_observation_continues_after_recovery(gap):
    s,h,now=fixture(60)
    at=h.index[62];h.loc[at,'Low']=80
    if gap:h.loc[at,'Open']=85
    h.iloc[-1,h.columns.get_loc('Close')]=150;h.iloc[-1,h.columns.get_loc('High')]=152
    r,d=observe(s,h,now)
    assert r['stop_scenario_return_pct']==pytest.approx(-15.3 if gap else -10.3)
    assert r['hold_return_pct']==pytest.approx(49.7) and r['post_stop_recovered']
    assert len({v['stop_scenario_return_pct'] for v in d[2:]})==1


@pytest.mark.parametrize('bad',['missing','raw','publication','split','entry_missing'])
def test_no_synthetic_values_for_unverified_inputs(bad):
    s,h,now=fixture(25)
    if bad=='missing':h=h.drop(h.index[65])
    if bad=='raw':h.loc[h.index[65],'_raw_ohlc_valid']=False
    if bad=='publication':s['published_at']=''
    if bad=='split':s['signal_close']=200
    if bad=='entry_missing':h=h.drop(h.index[60])
    r,d=observe(s,h,now)
    assert r['status']=='데이터 부족/검증 불가' and not d and r.get('hold_return_pct') is None


def test_ma_warning_and_short_history_unknown():
    s,h,now=fixture(25);h.iloc[-3:,h.columns.get_loc('Close')]=[95,94,93];h.iloc[-3:,h.columns.get_loc('Low')]=[92,92,92]
    r,d=observe(s,h,now)
    assert r['below_ma20'] and r['below_ma50'] and r['confirmed_below_ma50']
    assert r['first_warning_date']==h.index[-3].strftime('%Y-%m-%d')
    s,h,now=fixture(1);r,d=observe(s,h.iloc[59:],now)
    assert r['trend_state']=='평가 불가' and r['below_ma50'] is None


def test_rs_only_actual_recorded_days_and_not_carried_forward():
    s,h,now=fixture(5)
    rows=[dict(market='US',ticker='TEST',sub_market='SP500',as_of=d.strftime('%Y-%m-%d'),date=d.strftime('%Y-%m-%d'),rs_score=90-i*10,rs_change_4w=5-i,sector_power=80-i) for i,d in enumerate(h.index[60:62])]
    r,d=observe(s,h,now,candidate_history=pd.DataFrame(rows))
    assert d[1]['rs_weakening'] and d[1]['sector_power_weakening']
    assert d[2]['rs_score'] is None and d[2]['rs_weakening'] is None


def test_incremental_idempotence_completed_skip_and_fixed_files_unchanged(tmp_path):
    s,h,now=fixture(120);hist=tmp_path/'history';hist.mkdir()
    pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    (hist/'signal_outcomes.csv').write_text('sentinel official outcome\n')
    before={p.name:p.read_bytes() for p in hist.iterdir()}
    calls=[]
    def prices(*key):calls.append(key);return h
    update_tracking(tmp_path,prices,now,run_id='run',generated_at=now.isoformat())
    daily=(hist/'trend_tracking_daily.csv.gz').read_bytes()
    assert len(pd.read_csv(hist/'trend_tracking_daily.csv.gz'))==120
    update_tracking(tmp_path,prices,now,run_id='run',generated_at=now.isoformat())
    assert len(calls)==1 and (hist/'trend_tracking_daily.csv.gz').read_bytes()==daily
    assert all((hist/k).read_bytes()==v for k,v in before.items())
    m=json.loads((hist/'trend_tracking_manifest.json').read_text())
    assert m['run_id']=='run' and m['files']['trend_tracking.csv']['sha256']==hashlib.sha256((hist/'trend_tracking.csv').read_bytes()).hexdigest()


def test_price_revision_and_cost_change_do_not_silently_rewrite_history(tmp_path):
    s,h,now=fixture(21);hist=tmp_path/'history';hist.mkdir();pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    update_tracking(tmp_path,lambda *a:h,now,run_id='a',generated_at=now.isoformat())
    previous=(hist/'trend_tracking_daily.csv.gz').read_bytes()
    changed=h.copy();changed.iloc[65,changed.columns.get_loc('High')]=105
    update_tracking(tmp_path,lambda *a:changed,now,run_id='b',generated_at=now.isoformat())
    assert '수정' in pd.read_csv(hist/'trend_tracking.csv').iloc[0].quality_reason
    assert (hist/'trend_tracking_daily.csv.gz').read_bytes()==previous


def test_twenty_day_completion_does_not_end_long_observation_and_repeats_are_idempotent(tmp_path):
    s,h,now=fixture(21);hist=tmp_path/'history';hist.mkdir();pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    cal=xcals.get_calendar('XNYS');before=cal.session_close(h.index[-2])+pd.Timedelta(minutes=1)
    update_tracking(tmp_path,lambda *a:h.iloc[:-1],before,run_id='a',generated_at=before.isoformat())
    assert pd.read_csv(hist/'trend_tracking.csv').iloc[0].bars==20
    update_tracking(tmp_path,lambda *a:h,now,run_id='b',generated_at=now.isoformat())
    row=pd.read_csv(hist/'trend_tracking.csv').iloc[0]
    assert row.bars==21 and row.status=='추적 진행'
    first=(hist/'trend_tracking_daily.csv.gz').read_bytes()
    update_tracking(tmp_path,lambda *a:h,now,run_id='b',generated_at=now.isoformat())
    assert (hist/'trend_tracking_daily.csv.gz').read_bytes()==first
    assert len(pd.read_csv(hist/'trend_tracking_daily.csv.gz'))==21


def test_lagging_cache_is_not_current_and_split_metadata_is_not_a_fake_stop():
    s,h,now=fixture(5);cal=xcals.get_calendar('XNYS')
    r,d=observe(s,h,cal.session_close(cal.next_session(h.index[-1]))+pd.Timedelta(minutes=1))
    assert r['status']=='데이터 부족/검증 불가' and r['lagging_sessions']==1
    h['Stock Splits']=0.;h.loc[h.index[-1],'Stock Splits']=2.
    r,d=observe(s,h,now)
    assert not d and '주식분할' in r['quality_reason']
    s,h,now=fixture(120)
    r,d=observe(s,h,cal.session_close(cal.next_session(h.index[-1]))+pd.Timedelta(minutes=1))
    assert r['status']=='최대 관찰 기간 도달' and r['lagging_sessions']==0


def test_existing_fixed_horizon_and_signal_calculation_sources_remain_unchanged():
    import ast,hashlib
    from pathlib import Path
    from watchlist import trend_watchlist as engine
    source=Path(engine.__file__).read_text();lines=source.splitlines(keepends=True)
    # Source lock renewed only for the reviewed outcome changes: shared calendar,
    # legacy freeze, additive policy provenance and missing-stage deduplication.
    # Tactical audit: signal entry_ref now includes the exact -2.5% boundary.
    # Only that lock changes; cooldown, historical rows and outcome math are preserved.
    # evidence/verify_contracts.py additionally compares the return/stop AST to v1.
    expected={'compute_forward_validation': '487d61502136b6842677567f8edd72a0e4858937186015631370828f15518d96', 'update_signal_ledger': '9bc373ec9e414de5b1369f8bf8361bdfe7b54232ae3ba704945e5a5f8ab00e44', 'update_signal_outcomes': '446eb7109d43f0c5d95e099a932d77bb38ce64439db81b80e039717c6efabfbe'}
    for node in ast.parse(source).body:
        if isinstance(node,ast.FunctionDef) and node.name in expected:
            assert hashlib.sha256(''.join(lines[node.lineno-1:node.end_lineno]).encode()).hexdigest()==expected[node.name]


def test_initial_entry_gap_below_stop_does_not_invent_a_valid_stop_simulation():
    s,h,now=fixture(5)
    h.loc[h.index[60],'Open']=85.;h.loc[h.index[60],'Low']=84.
    r,d=observe(s,h,now)
    assert r['bars']==5 and r['hold_return_pct']==pytest.approx((100/85-1)*100-.3)
    assert r['first_stop_date']==h.index[60].strftime('%Y-%m-%d')
    assert r['stop_scenario_return_pct'] is None and '비양수' in r['stop_scenario_status']


def test_fixed_outcome_completed_while_long_tracking_continues(tmp_path,monkeypatch):
    from datetime import datetime
    from watchlist import trend_watchlist as engine
    s,h,now=fixture(21);hist=tmp_path/'history';hist.mkdir();prices=tmp_path/'prices';prices.mkdir()
    h['_session_finality_version']=2;h.to_pickle(prices/'TEST.pkl')
    pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',prices)
    monkeypatch.setattr(engine,'_make_bench_getter',lambda cfg:lambda *args:h)
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return now.to_pydatetime().astimezone(tz)
    monkeypatch.setattr(engine,'datetime',Clock)
    engine.update_signal_outcomes(tmp_path,engine.DEFAULT_CONFIG)
    fixed=(hist/'signal_outcomes.csv').read_bytes();out=pd.read_csv(hist/'signal_outcomes.csv').iloc[0]
    assert out.status=='종료' and out.ret_20d==pytest.approx(-.3)
    update_tracking(tmp_path,lambda *a:h,now,run_id='new',generated_at=now.isoformat())
    observed=pd.read_csv(hist/'trend_tracking.csv').iloc[0]
    assert observed.bars==21 and observed.status=='추적 진행' and observed.ret_20d==pytest.approx(out.ret_20d)
    assert observed.fixed_20d_consistency=='일치'
    assert (hist/'signal_outcomes.csv').read_bytes()==fixed


def test_individual_cache_failure_does_not_abort_other_signals(tmp_path):
    s,h,now=fixture(5);hist=tmp_path/'history';hist.mkdir()
    pd.DataFrame([s,{**s,'ticker':'BROKEN'}]).to_csv(hist/'signals.csv',index=False)
    def load(market,ticker):
        if ticker=='BROKEN':raise OSError('synthetic corrupt cache')
        return h
    update_tracking(tmp_path,load,now,run_id='run',generated_at=now.isoformat())
    data=pd.read_csv(hist/'trend_tracking.csv').set_index('ticker')
    assert data.loc['TEST','bars']==5 and data.loc['BROKEN','status']=='데이터 부족/검증 불가'
    assert pd.isna(data.loc['BROKEN','hold_return_pct'])


@pytest.mark.parametrize('change',['cost','prehistory'])
def test_preentry_history_and_cost_changes_require_revalidation(tmp_path,change):
    s,h,now=fixture(21);hist=tmp_path/'history';hist.mkdir();pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    update_tracking(tmp_path,lambda *a:h,now,run_id='a',generated_at=now.isoformat())
    raw=(hist/'trend_tracking_daily.csv.gz').read_bytes()
    if change=='prehistory':h.iloc[20,h.columns.get_loc('High')]=104.
    update_tracking(tmp_path,lambda *a:h,now,cost_pct=.4 if change=='cost' else .3,run_id='b',generated_at=now.isoformat())
    assert (hist/'trend_tracking_daily.csv.gz').read_bytes()==raw
    assert pd.read_csv(hist/'trend_tracking.csv').iloc[0].status=='데이터 부족/검증 불가'


def test_old_calculation_version_is_preserved_but_not_presented_as_new(tmp_path):
    s,h,now=fixture(120);hist=tmp_path/'history';hist.mkdir();pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    update_tracking(tmp_path,lambda *a:h,now,run_id='a',generated_at=now.isoformat())
    old=pd.read_csv(hist/'trend_tracking.csv');old['tracking_version']='legacy-version';old.to_csv(hist/'trend_tracking.csv',index=False)
    daily=(hist/'trend_tracking_daily.csv.gz').read_bytes()
    update_tracking(tmp_path,lambda *a:pytest.fail('no re-evaluation of a different version'),now,run_id='b',generated_at=now.isoformat())
    r=pd.read_csv(hist/'trend_tracking.csv').iloc[0]
    assert r.tracking_version=='legacy-version' and r.status=='데이터 부족/검증 불가'
    assert (hist/'trend_tracking_daily.csv.gz').read_bytes()==daily


def test_backfilled_price_dates_and_actual_recording_time_are_separate(tmp_path):
    s,h,now=fixture(21);hist=tmp_path/'history';hist.mkdir();pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    update_tracking(tmp_path,lambda *a:h,now,run_id='backfill-run',generated_at=now.isoformat())
    d=pd.read_csv(hist/'trend_tracking_daily.csv.gz')
    assert d.recorded_at.nunique()==1 and pd.Timestamp(d.recorded_at.iloc[0])==now
    assert d.evaluation_date.iloc[0]!=d.recorded_at.iloc[0][:10]
    assert set(d.tracking_run_id)=={'backfill-run'}

