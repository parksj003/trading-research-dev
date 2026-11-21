"""Research mechanics, not parameter efficacy. No provider calls."""
from copy import deepcopy
from dataclasses import asdict
import json

import numpy as np
import pandas as pd
import pytest
import exchange_calendars as xc

from research.base_count.fetch import normalize_chart
from research.base_count.study import segments,evaluate_attempt,assign_nonoverlap,cluster_interval,load_dataset,fold_for


def frame(n=50,start='2025-01-02'):
    dates=xc.get_calendar('XNYS').sessions_in_range(start,'2026-11-06')[:n]
    close=np.arange(n,dtype=float)+100
    return pd.DataFrame(dict(Open=close,High=close+2,Low=close-2,Close=close+1,Volume=1000.,
        _raw_open_valid=True,_raw_ohlc_valid=True,_session_confirmed=True),index=dates)


def plan():
    return dict(folds=[dict(name='test',start='2025-01-01',end='2026-11-06')],cutoff='2026-11-06',
        horizons=[5,10,20],reference_cost_bps=30,round_trip_cost_bps=[10,30,50])


def event(f,index=0):
    return dict(date=str(f.index[index].date()),market='US',ticker='EX',event_id=str(index),
        ordinal_at_signal=1,quality_at_signal='CONFIRMED')


def test_adjustment_applies_one_factor_to_all_ohlc_without_filling_bad_bars():
    raw=dict(chart=dict(result=[dict(meta=dict(symbol='EX',exchangeTimezoneName='America/New_York'),
        timestamp=[1735830000,1735916400],indicators=dict(quote=[dict(open=[100,None],high=[110,111],low=[90,91],close=[100,101],volume=[1000,1000])],
        adjclose=[dict(adjclose=[50,50.5])]),events={'splits':{'one':dict(date=1735830000,numerator=2,denominator=1)}})]))
    f,actions=normalize_chart(raw,'EX','US','2026-11-06')
    assert f.iloc[0].Open==50 and f.iloc[0].High==55 and f.iloc[0].Low==45 and f.iloc[0].Close==50
    assert f.iloc[0].Volume==1000
    assert pd.isna(f.iloc[1].Open) and not bool(f.iloc[1]._raw_open_valid)
    assert actions[0]['kind']=='splits' and 'Stock Splits' not in f


@pytest.mark.parametrize('change',['symbol','timezone'])
def test_provider_identity_and_exchange_timezone_must_match(change):
    doc={'chart':{'result':[{'meta':{'symbol':'EX','exchangeTimezoneName':'America/New_York'}}]}}
    if change=='symbol':doc['chart']['result'][0]['meta']['symbol']='OTHER'
    if change=='timezone':doc['chart']['result'][0]['meta']['exchangeTimezoneName']='UTC'
    with pytest.raises(ValueError):normalize_chart(doc,'EX','US','2026-11-06')


@pytest.mark.parametrize('problem',['missing','zero_volume','invalid_ohlc','unconfirmed','raw_open','halt'])
def test_bad_sessions_create_separate_unknown_lineages_not_interpolated_prices(problem):
    f=frame();date=f.index[20]
    if problem=='missing':f=f.drop(date)
    if problem=='zero_volume':f.loc[date,'Volume']=0
    if problem=='invalid_ohlc':f.loc[date,'High']=1
    if problem=='unconfirmed':f.loc[date,'_session_confirmed']=False
    if problem=='raw_open':f.loc[date,'_raw_open_valid']=False
    if problem=='halt':f['IsHalt']=False;f.loc[date,'IsHalt']=True
    before=f.copy(deep=True);runs,report=segments(f,'US')
    assert len(runs)==2 and len(runs[0])==20 and len(runs[1])==29
    assert date not in runs[0].index and date not in runs[1].index
    assert report['valid_rows']==49
    pd.testing.assert_frame_equal(before,f)


def test_non_exchange_rows_are_reported_and_do_not_replace_real_sessions():
    f=frame();extra=f.iloc[:1].copy();extra.index=pd.DatetimeIndex(['2025-01-04'])
    f=pd.concat([f,extra]).sort_index();runs,report=segments(f,'US')
    assert report['non_sessions']==['2025-01-04']
    assert sum(len(r) for r in runs)==50


def test_unknown_provenance_and_duplicate_dates_are_not_silently_certified():
    f=frame().drop(columns='_session_confirmed');runs,report=segments(f,'US')
    assert runs==[] and report['valid_rows']==0
    f=frame();f=pd.concat([f.iloc[:1],f])
    with pytest.raises(ValueError):segments(f,'US')


def test_event_uses_next_open_and_fifth_session_close_not_signal_close():
    f=frame();f.loc[f.index[0],'Close']=10
    result=evaluate_attempt(event(f),f,f,plan())
    assert result['entry_date']==str(f.index[1].date())
    assert result['exit_date_5']==str(f.index[5].date())
    expected=(f.Close.iloc[5]/f.Open.iloc[1]-1)*100-.3
    assert result['net_return_pp_5']==pytest.approx(expected)
    assert result['benchmark_excess_pp_5']==pytest.approx(-.3)
    assert result['net_return_pp_5_cost_50bps']==pytest.approx(expected-.2)


def test_successful_hold_information_is_not_required_to_include_a_breakout_attempt():
    f=frame();row=event(f);row['will_fail_later']=True
    result=evaluate_attempt(row,f,f,plan())
    assert result['outcome_20']=='COMPLETE' and result['ordinal_at_signal']==1
    row.update(ordinal_at_signal=None,quality_at_signal='HISTORY_INSUFFICIENT')
    result=evaluate_attempt(row,f,f,plan())
    assert result['ordinal_at_signal'] is None and result['outcome_20']=='COMPLETE'


def test_incomplete_path_is_retained_as_incomplete_and_never_crosses_a_gap():
    f=frame();row=event(f)
    result=evaluate_attempt(row,f.drop(f.index[3]),f,plan())
    assert result['outcome_5']=='INCOMPLETE' and result['net_return_pp_5'] is None
    result=evaluate_attempt(row,f.iloc[:4],f,plan())
    assert result['outcome_5']=='INCOMPLETE'


def test_temporal_fold_boundaries_are_purged_without_using_later_fold_prices():
    f=frame();p=plan();p['folds'][0]['end']=str(f.index[4].date())
    result=evaluate_attempt(event(f),f,f,p)
    assert result['outcome_5']=='FOLD_BOUNDARY_OR_IMMATURE'
    assert result['net_return_pp_5'] is None


def test_benchmark_missing_dates_never_forward_fills_excess_returns():
    f=frame();benchmark=f.drop(f.index[3])
    result=evaluate_attempt(event(f),f,benchmark,plan())
    assert result['outcome_5']=='COMPLETE' and result['benchmark_excess_pp_5'] is None


def test_nonoverlap_does_not_select_based_on_realized_profit_or_loss():
    f=frame();rows=[]
    for i in [0,5,21]:
        row=event(f,i);row['entry_date']=str(f.index[i+1].date());row['net_return_pp_20']=100 if i==5 else -100
        rows.append(row)
    result=assign_nonoverlap(rows)
    assert [r['nonoverlap_20d'] for r in result]==[True,False,True]
    alternate=deepcopy(rows)
    for r in alternate:r['net_return_pp_20']*=-1
    assert [r['nonoverlap_20d'] for r in assign_nonoverlap(alternate)]==[True,False,True]


def test_insufficient_cluster_sample_has_no_spurious_confidence_interval():
    rows=[dict(ticker='ONE',value=10.) for _ in range(100)]
    assert cluster_interval(rows,'value',1) is None
    rows=[dict(ticker=str(i%5),value=10.) for i in range(20)]
    assert cluster_interval(rows,'value',1)==[10.,10.]


def test_data_manifest_mismatch_blocks_replay(tmp_path):
    p=tmp_path/'plan.json';p.write_text('{}');(tmp_path/'manifest.json').write_text('{"plan_sha256":"bad"}')
    with pytest.raises(ValueError,match='lineage'):load_dataset(p,tmp_path)


def test_research_profiles_are_frozen_separate_from_production_config():
    from pathlib import Path
    from watchlist.base_detector import DetectorConfig
    from watchlist.trend_watchlist import DEFAULT_CONFIG
    p=json.loads((Path(__file__).parents[1]/'research/base_count/plan.json').read_text())
    assert len(p['profiles'])==15 and p['profile_selection']=='ALL_PREDECLARED_NO_WINNER_SELECTION'
    assert 'base_count' not in DEFAULT_CONFIG
    for values in p['profiles'].values():DetectorConfig(**values)


def test_manual_review_is_not_invented_from_algorithm_output():
    from research.base_count.review import score_labels
    labels=pd.DataFrame([dict(case_id='A',label_status='UNREVIEWED',reviewer_code='',manual_state='',manual_ordinal='')])
    key=pd.DataFrame([dict(case_id='A',model_state='BASE_FORMING',model_ordinal=1)])
    result=score_labels(labels,key)
    assert result['reviewed']==0 and result['state_accuracy'] is None and result['ordinal_accuracy'] is None
    labels.loc[0,['label_status','reviewer_code','reviewed_at','manual_state','manual_ordinal']]=['REVIEWED','R01','2026-11-09T12:00:00Z','BASE_FORMING','UNKNOWN']
    result=score_labels(labels,key)
    assert result['state_accuracy']==1 and result['ordinal_accuracy'] is None
    assert result['ordinal_unassessable']==1 and result['population_precision_established'] is False


def test_duplicate_review_ids_are_rejected():
    from research.base_count.review import score_labels
    with pytest.raises(ValueError):score_labels(pd.DataFrame({'case_id':['A','A']}),pd.DataFrame({'case_id':['A']}))


def test_korean_price_index_is_not_treated_as_total_return_benchmark():
    dates=xc.get_calendar('XKRX').sessions_in_range('2025-01-02','2025-06-30')[:50]
    f=frame();f.index=dates
    row=dict(date=str(dates[0].date()),market='KR',ticker='005930',event_id='K',benchmark='^KS11')
    result=evaluate_attempt(row,f,f,plan())
    assert result['outcome_5']=='COMPLETE' and result['net_return_pp_5'] is not None
    assert result['benchmark_excess_pp_5'] is None
    assert result['benchmark_comparison_quality']=='PRICE_INDEX_VS_ADJUSTED_EQUITY'


@pytest.mark.parametrize('market,observed,expected',[
    ('US','2026-11-09T13:00:00Z','2026-11-09'),
    ('US','2026-11-09T15:00:00Z','2026-11-10'),
    ('US','2026-11-09T14:30:00Z','2026-11-10'),
    ('KR','2026-11-09T08:00:00Z','2026-11-10'),
])
def test_forward_research_never_backdates_entry_to_an_already_passed_open(market,observed,expected):
    from research.base_count.prospective import earliest_entry_session
    date,opening=earliest_entry_session(market,observed)
    assert date==expected and pd.Timestamp(opening)>pd.Timestamp(observed)


@pytest.mark.parametrize('observed',['2026-11-18T23:00Z','2026-12-31T12:00Z'])
def test_forward_research_does_not_invent_unreviewed_kr_special_openings(observed):
    from research.base_count.prospective import earliest_entry_session
    with pytest.raises(ValueError,match='separate review'):earliest_entry_session('KR',observed)

