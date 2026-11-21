from copy import deepcopy

import pandas as pd
import pytest

from watchlist import trend_watchlist as engine
from test_forward_outcomes import cached, prices


@pytest.mark.parametrize('tracks', [{}, {'KOSPI': pd.DataFrame(), 'SP500': pd.DataFrame()}, {'KOSPI': None}])
def test_empty_selected_tracks_are_a_valid_zero_result(tracks):
    selected, actionable, meta = engine.build_action_table(tracks, {}, deepcopy(engine.DEFAULT_CONFIG))
    assert selected.empty and actionable.empty
    assert meta['total_alloc_pct'] == 0


def test_selected_without_actionable_is_still_valid():
    selected, actionable, _ = engine.build_action_table(
        {'SP500': pd.DataFrame([{'ticker': 'EX', 'is_actionable': False}]), 'KOSPI': pd.DataFrame()},
        {}, deepcopy(engine.DEFAULT_CONFIG))
    assert len(selected) == 1 and actionable.empty


@pytest.mark.parametrize('missing', ['2026-10-02', '2026-10-06'])
def test_common_cache_gaps_cannot_certify_exchange_sessions(cached, missing):
    _, run = cached
    frame = prices(pd.bdate_range('2026-10-01', periods=8)).drop(pd.Timestamp(missing))
    row = run(frame, frame)
    if missing == '2026-10-02':
        assert not bool(row['entry_session_verified'])
    assert not bool(row['calendar_complete_5d'])


def test_complete_exchange_path_stays_eligible(cached):
    _, run = cached
    row = run(prices(pd.bdate_range('2026-10-01', periods=8)))
    assert bool(row['entry_session_verified']) and bool(row['calendar_complete_5d'])


def test_empty_selection_publishes_valid_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'read_previous_market', lambda base: None)
    monkeypatch.setattr(engine, 'publish_analysis_report', lambda *a, **kw: None)
    regs = {'KOSPI': {'regime':'RISK_OFF','signal_light':'RISK_OFF','as_of':'2026-11-03'}}
    out = engine.publish_artifacts('2026-11-03', {'KOSPI':pd.DataFrame()},
        {'KOSPI':pd.DataFrame()}, regs, {}, pd.DataFrame(), pd.DataFrame(),
        deepcopy(engine.DEFAULT_CONFIG), base_dir=tmp_path/'published')
    assert out is not None
    assert engine.validate_published_dir(out) == []
    assert pd.read_csv(out/'watchlist.csv').empty
    assert pd.read_csv(out/'candidates.csv').empty


def test_previous_candidate_without_observation_is_reported_as_unknown():
    previous={'EX':dict(sub_market='SP500',name='Synthetic',prev_rank=1,prev_rs=90.)}
    row=engine.compute_dropped_candidates(previous,{'SP500':pd.DataFrame()},
                                         {'SP500':pd.DataFrame()},{}).iloc[0]
    assert row['ticker']=='EX' and row['prev_rs']==90.
    assert pd.isna(row['curr_rs'])
    assert '자료' in row['drop_reason'] and '확인불가' in row['exit_signal']


def test_unrequested_market_is_not_reported_as_missing():
    previous={'EX':dict(sub_market='SP500',name='Synthetic',prev_rank=1,prev_rs=90.)}
    result=engine.compute_dropped_candidates(previous,{'KOSPI':pd.DataFrame()},
                                            {'KOSPI':pd.DataFrame()},{})
    assert result.empty


def test_existing_filter_failure_and_retained_selection_keep_their_meaning():
    previous={t:dict(sub_market='SP500',name=t,prev_rank=1,prev_rs=90.) for t in ['FAIL','KEEP']}
    before=deepcopy(previous)
    failures={'FAIL':dict(rs_score=60.,drop_reason='RS미달',exit_signal='기존 경고')}
    result=engine.compute_dropped_candidates(previous,{'SP500':pd.DataFrame()},
        {'SP500':pd.DataFrame([dict(ticker='KEEP')])},failures)
    assert len(result)==1 and result.iloc[0]['ticker']=='FAIL'
    assert result.iloc[0]['drop_reason']=='RS미달'
    assert previous==before

