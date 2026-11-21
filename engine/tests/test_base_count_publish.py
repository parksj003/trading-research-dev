"""Phase 3 contracts: synthetic cached bars and temporary publications only."""
from dataclasses import asdict
import copy
import json

import pandas as pd
import pytest

from test_base_detector import config, first_base, extend
from watchlist import base_count_publish as publish
from watchlist import trend_watchlist as engine

NOW='2026-11-09T23:00:00Z'


def settings():
    return dict(enabled=True, markets={'US':dict(parameters=asdict(config()),
        price_basis='synthetic-adjusted',data_revision='v1')})


def cached(f=None):
    f=first_base().copy() if f is None else f.copy()
    f['_base_price_basis']='synthetic-adjusted'
    return f


def candidates(f=None):
    f=first_base() if f is None else f
    return pd.DataFrame([dict(market='US',ticker='SYNTH',as_of=str(f.index[-1].date()),sub_market='SP500')])


def prepare(tmp_path,f=None,run_id='run-1',cfg=None):
    f=cached(f)
    return publish.prepare_publication(tmp_path,candidates(f),cfg or settings(),
        lambda m,t:f,run_id=run_id,generated_at=NOW)


def commit(tmp_path,doc):
    latest=tmp_path/'latest';latest.mkdir(exist_ok=True)
    raw=publish.json_bytes(doc);(latest/publish.FILE_NAME).write_bytes(raw)
    import hashlib
    (latest/'manifest.json').write_text(json.dumps(dict(run_id=doc['run_id'],
        files={publish.FILE_NAME:dict(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))})))


def test_new_publication_bootstraps_counts_without_modifying_prices(tmp_path):
    f=cached();before=f.copy(deep=True);doc=prepare(tmp_path,f)
    assert doc['status']=='enabled' and doc['observations'][0]['completed_base_count']==1
    assert publish.validate_publication(doc,'run-1')==[]
    pd.testing.assert_frame_equal(f,before)


def test_failed_publish_does_not_advance_durable_cursor(tmp_path):
    f=first_base();old=prepare(tmp_path,f.iloc[:-2]);commit(tmp_path,old)
    unpublished=prepare(tmp_path,f,run_id='failed-run')
    assert unpublished['generation']!=old['generation']
    assert publish.read_previous(tmp_path)['generation']==old['generation']
    retry=prepare(tmp_path,f,run_id='retry')
    assert retry['observations'][0]['completed_base_count']==1
    assert publish.read_previous(tmp_path)['generation']==old['generation']


def test_retry_noop_is_idempotent_and_old_objects_stay_immutable(tmp_path):
    first=prepare(tmp_path);commit(tmp_path,first)
    objects={p.name:p.read_bytes() for p in (tmp_path/'history/base_count/objects').iterdir()}
    second=prepare(tmp_path,run_id='run-2');commit(tmp_path,second)
    assert second['observations']==first['observations']
    assert all((tmp_path/'history/base_count/objects'/name).read_bytes()==raw for name,raw in objects.items())
    generation=publish.read_object(tmp_path,second['generation'])
    assert generation['instruments']==publish.read_object(tmp_path,first['generation'])['instruments']


@pytest.mark.parametrize('problem',['missing_basis','wrong_basis','missing_flag','internal_unconfirmed','duplicate','gap'])
def test_provenance_or_structural_input_problem_yields_unknown_not_fabricated_counts(tmp_path,problem):
    f=cached()
    if problem=='missing_basis':f=f.drop(columns='_base_price_basis')
    if problem=='wrong_basis':f.loc[f.index[0],'_base_price_basis']='other'
    if problem=='missing_flag':f=f.drop(columns='_raw_ohlc_valid')
    if problem=='internal_unconfirmed':f.loc[f.index[300],'_session_confirmed']=False
    if problem=='duplicate':f=pd.concat([f.iloc[:300],f.iloc[299:]])
    if problem=='gap':f=f.drop(f.index[300])
    doc=publish.prepare_publication(tmp_path,candidates(f),settings(),lambda m,t:f,run_id='bad',generated_at=NOW)
    row=doc['observations'][0]
    assert row['status']=='UNKNOWN' and row['completed_base_count'] is None
    assert row['as_of']==str(f.index[-1].date())


def test_future_cache_bars_are_never_used_for_an_older_candidate(tmp_path):
    f=cached();old=f.iloc[:-2]
    doc=publish.prepare_publication(tmp_path,candidates(old),settings(),lambda m,t:f,
        run_id='old',generated_at=NOW)
    row=doc['observations'][0]
    assert row['status']=='BREAKOUT_PENDING' and row['completed_base_count']==0
    assert row['as_of']==str(old.index[-1].date())


def test_duplicate_tracks_are_processed_once_and_ambiguous_dates_are_unknown(tmp_path):
    f=cached();rows=pd.concat([candidates(f),candidates(f).assign(sub_market='NASDAQ100')])
    doc=publish.prepare_publication(tmp_path,rows,settings(),lambda m,t:f,run_id='dup',generated_at=NOW)
    assert len(doc['observations'])==1 and doc['observations'][0]['completed_base_count']==1
    commit(tmp_path,doc)
    rows.iloc[1,rows.columns.get_loc('as_of')]=str(f.index[-2].date())
    doc=publish.prepare_publication(tmp_path,rows,settings(),lambda m,t:f,run_id='ambiguous',generated_at=NOW)
    assert doc['observations'][0]['reason']=='CONFLICTING_CANDIDATE_DATES'


def test_corrupt_or_lost_state_stays_blocked_and_is_not_reset_to_base_one(tmp_path):
    old=prepare(tmp_path);commit(tmp_path,old)
    p=tmp_path/'history/base_count/objects'/f"{old['generation']}.json.gz"
    original=p.read_bytes();p.write_bytes(b'bad')
    blocked=prepare(tmp_path,run_id='blocked')
    assert blocked['status']=='unavailable' and blocked['generation']==old['generation']
    assert blocked['observations'][0]['completed_base_count'] is None
    p.write_bytes(original)
    assert prepare(tmp_path,run_id='recovered')['observations'][0]['completed_base_count']==1


def test_changed_configuration_does_not_silently_rebuild_existing_instrument(tmp_path):
    old=prepare(tmp_path);commit(tmp_path,old);cfg=settings();cfg['markets']['US']['parameters']['hold_sessions']=3
    doc=prepare(tmp_path,run_id='changed',cfg=cfg)
    assert doc['observations'][0]['reason']=='CHECKPOINT_LINEAGE_CONFLICT'
    assert doc['observations'][0]['completed_base_count'] is None


def test_disabling_preserves_committed_generation_for_later_resume(tmp_path):
    old=prepare(tmp_path);commit(tmp_path,old)
    off=publish.prepare_publication(tmp_path,candidates(),{'enabled':False},
        lambda *args:pytest.fail('disabled must not read cache'),run_id='off',generated_at=NOW)
    assert off['status']=='disabled' and off['generation']==old['generation']
    commit(tmp_path,off)
    assert prepare(tmp_path,run_id='on')['observations'][0]['completed_base_count']==1


def test_local_lock_blocks_concurrent_publisher_and_is_released(tmp_path):
    with publish.publication_lock(tmp_path):
        with pytest.raises(publish.StateConflict):
            with publish.publication_lock(tmp_path):pass
    with publish.publication_lock(tmp_path):pass


def test_unconfigured_feature_has_no_data_reads_or_files(tmp_path):
    assert publish.prepare_publication(tmp_path,candidates(),{},lambda *a:pytest.fail('not configured'),
        run_id='disabled',generated_at=NOW) is None
    assert list(tmp_path.iterdir())==[]


def engine_inputs(f):
    date=str(f.index[-1].date())
    row=dict(market='US',sub_market='SP500',ticker='SYNTH',name='Synthetic',as_of=date,
        sector='S1',macro_theme='M1',tier='Tier A',setup_stage='돌파',status_tag='',
        is_actionable=True,in_buy_zone=True,is_breakout=True,is_true_vcp=False,
        strict_minervini=True,is_leading_sector=True,rs_score=90.,rs_change_4w=3.,
        topdown_score=85.,sector_power=70.,last_close=float(f.Close.iloc[-1]),pivot_price=200.4,
        dist_to_pivot_pct=.01,stop_price=190.,stop_dist_pct=-.06,raw_stop_dist_pct=-.06,
        position_size_pct=15.,volume_ratio=1.8,distance_from_high_pct=-.01,exit_signal='✅정상')
    tracks={'SP500':pd.DataFrame([row])}
    regimes={'SP500':dict(regime='RISK_ON',signal_light='🟢 ATTACK',benchmark='SPY',as_of=date)}
    return date,tracks,regimes


def install_engine_fixture(tmp_path,monkeypatch,f=None):
    from test_independent_audit import fixed_clock
    from types import SimpleNamespace
    import requests
    fixed_clock(monkeypatch,NOW)
    monkeypatch.setattr(engine.uuid,'uuid4',lambda:SimpleNamespace(hex='abc123deadbeef'))
    cache=tmp_path/'cache';cache.mkdir()
    monkeypatch.setattr(engine,'ROOT',tmp_path)
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',cache)
    monkeypatch.setattr(engine,'KR_HISTORY_CACHE_DIR',cache)
    monkeypatch.setattr(requests.sessions.Session,'request',lambda *a,**k:pytest.fail('HTTP forbidden'))
    f=cached(f);f.to_pickle(cache/'SYNTH.pkl')
    cfg=copy.deepcopy(engine.DEFAULT_CONFIG)
    cfg['base_count']=settings()
    date,tracks,regimes=engine_inputs(f)
    def run(base):
        return engine.publish_artifacts(date,tracks,tracks,regimes,{},pd.DataFrame(),pd.DataFrame(),cfg,base_dir=base)
    return cfg,run,cache


def test_real_engine_publication_adds_verified_sidecar_but_legacy_outputs_are_byte_identical(tmp_path,monkeypatch):
    cfg,run,_=install_engine_fixture(tmp_path,monkeypatch)
    cfg.pop('base_count');legacy=run(tmp_path/'legacy')
    cfg['base_count']=settings();current=run(tmp_path/'current')
    assert legacy is not None and current is not None
    assert engine.validate_published_dir(current)==[]
    for name in list(engine.CONTRACT_REQUIRED)+['summary.json',engine.CHARTS_FILE]:
        assert (current/name).read_bytes()==(legacy/name).read_bytes(),name
    for name in ['signals.csv','signal_outcomes.csv','candidates_daily.csv','regime.csv']:
        assert (current.parent/'history'/name).read_bytes()==(legacy.parent/'history'/name).read_bytes(),name
    man=json.loads((current/'manifest.json').read_bytes())
    assert man['schema_version']==engine.SCHEMA_VERSION
    assert publish.FILE_NAME in man['files']
    assert man['contract']['optional_observations']['base_count']['schema']==publish.SCHEMA
    doc=publish.read_previous(current.parent)
    assert doc['observations'][0]['completed_base_count']==1
    assert (current.parent/'daily'/str(first_base().index[-1].date())/publish.FILE_NAME).read_bytes()==(current/publish.FILE_NAME).read_bytes()


def test_real_engine_validation_failure_retains_committed_generation_and_retry_is_safe(tmp_path,monkeypatch):
    cfg,run,_=install_engine_fixture(tmp_path,monkeypatch)
    base=tmp_path/'current';assert run(base) is not None
    old=(base/'latest/manifest.json').read_bytes();head=publish.read_previous(base)['generation']
    archive={p.name:p.read_bytes() for p in (base/publish.OBJECT_DIR).glob('*.json.gz')}
    real_validate=engine.validate_published_dir
    monkeypatch.setattr(engine,'validate_published_dir',lambda path:['injected validation failure'])
    assert run(base) is None
    assert (base/'latest/manifest.json').read_bytes()==old
    assert publish.read_previous(base)['generation']==head
    assert {p.name:p.read_bytes() for p in (base/publish.OBJECT_DIR).glob('*.json.gz')}==archive
    assert not (base/'.base-count-publish.lock').exists()
    monkeypatch.setattr(engine,'validate_published_dir',real_validate)
    assert run(base) is not None
    assert publish.read_previous(base)['observations'][0]['completed_base_count']==1


def test_real_engine_lock_conflict_does_not_replace_existing_publication(tmp_path,monkeypatch):
    _,run,_=install_engine_fixture(tmp_path,monkeypatch);base=tmp_path/'current'
    assert run(base) is not None;before=(base/'latest/manifest.json').read_bytes()
    with publish.publication_lock(base):assert run(base) is None
    assert (base/'latest/manifest.json').read_bytes()==before


@pytest.mark.parametrize('default,index,attested',[(True,False,True),(False,False,False),(True,True,False)])
def test_kr_basis_tags_only_actual_declared_adjusted_default_without_changing_calls(default,index,attested):
    calls=[]
    def provider(start,end,ticker,adjusted=default):calls.append((start,end,ticker,adjusted))
    f=first_base()
    engine._tag_kr_base_price_basis(f,provider,index)
    assert ('_base_price_basis' in f)==attested and calls==[]
    if attested:assert f._base_price_basis.eq(publish.KR_BASIS).all()


def test_historical_revision_is_blocked_without_altering_prior_archive(tmp_path):
    f=cached();old=prepare(tmp_path,f);commit(tmp_path,old)
    generation=publish.read_object(tmp_path,old['generation']);original_id=generation['instruments']['US:SYNTH']
    old_bytes=(tmp_path/publish.OBJECT_DIR/(original_id+'.json.gz')).read_bytes()
    f.loc[f.index[0],'Volume']*=2
    doc=prepare(tmp_path,f,run_id='revised')
    assert doc['observations'][0]['reason']=='HISTORY_REVISION_REQUIRES_REBUILD'
    assert doc['observations'][0]['completed_base_count'] is None
    assert publish.read_object(tmp_path,doc['generation'])['instruments']['US:SYNTH']==original_id
    assert (tmp_path/publish.OBJECT_DIR/(original_id+'.json.gz')).read_bytes()==old_bytes


def test_excluded_candidate_checkpoint_remains_linked_for_future_return(tmp_path):
    old=prepare(tmp_path);commit(tmp_path,old)
    old_instruments=publish.read_object(tmp_path,old['generation'])['instruments']
    empty=publish.prepare_publication(tmp_path,pd.DataFrame(),settings(),lambda *a:pytest.fail('no active candidates'),
        run_id='out',generated_at=NOW)
    assert empty['observations']==[]
    assert publish.read_object(tmp_path,empty['generation'])['instruments']==old_instruments
    commit(tmp_path,empty)
    assert prepare(tmp_path,run_id='return')['observations'][0]['completed_base_count']==1


def test_lost_committed_reference_never_scans_orphans_to_guess_the_latest_state(tmp_path):
    old=prepare(tmp_path);commit(tmp_path,old)
    (tmp_path/'latest'/publish.FILE_NAME).unlink()
    doc=prepare(tmp_path,run_id='lost')
    assert doc['status']=='unavailable' and doc['observations'][0]['completed_base_count'] is None


def test_atomic_object_write_failure_cannot_leave_a_partial_named_object(tmp_path,monkeypatch):
    payload={'schema':'fixture','value':1}
    def fail(*args):raise OSError('injected link failure')
    with monkeypatch.context() as m:
        m.setattr(publish.os,'link',fail)
        with pytest.raises(OSError):publish.write_object(tmp_path,payload)
    assert list((tmp_path/publish.OBJECT_DIR).iterdir())==[]
    digest=publish.write_object(tmp_path,payload)
    assert publish.read_object(tmp_path,digest)==payload


def test_first_publication_failure_cleans_only_its_uncommitted_objects_and_retry_bootstraps(tmp_path,monkeypatch):
    _,run,_=install_engine_fixture(tmp_path,monkeypatch);base=tmp_path/'first-run'
    real_validate=engine.validate_published_dir
    monkeypatch.setattr(engine,'validate_published_dir',lambda path:['injected first-run failure'])
    assert run(base) is None
    assert not list((base/publish.OBJECT_DIR).glob('*.json.gz'))
    monkeypatch.setattr(engine,'validate_published_dir',real_validate)
    assert run(base) is not None
    assert publish.read_previous(base)['observations'][0]['completed_base_count']==1


@pytest.mark.parametrize('version',[0,1,2])
def test_kr_special_session_requires_reviewed_acquisition_version(version):
    f=cached().iloc[:1].copy();f.index=pd.DatetimeIndex(['2024-11-14'])
    f['_session_finality_version']=version
    if version<2:
        with pytest.raises(publish.StateConflict):publish.prepare_prices(f,'2024-11-14','KR','synthetic-adjusted')
    else:
        pd.testing.assert_frame_equal(publish.prepare_prices(f,'2024-11-14','KR','synthetic-adjusted'),f)


def test_passive_basis_metadata_has_no_effect_on_existing_features():
    f=first_base();cfg=copy.deepcopy(engine.DEFAULT_CONFIG)
    before=engine.compute_features(f,f,cfg)
    after=engine.compute_features(cached(f),cached(f),cfg)
    pd.testing.assert_series_equal(pd.Series(before),pd.Series(after))

