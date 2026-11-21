from copy import deepcopy
from pathlib import Path
import json
import pandas as pd
import pytest

from watchlist.exit_experiment_publish import build,publish,load_previous,registry_for,read_market_inputs
from watchlist.exit_policy_experiments import canonical,digest
from test_exit_policy_core import fixture


def make(signals=None,prices=None,previous=None,registry=None,core=None):
    s,h,cut=fixture();s['publication_verified']=True
    return build(signals or [s],lambda m,t:h if prices is None else prices,core=core or {'run_id':'source-current','generated_at':'2026-01-02T22:00:00Z'},
        source_commit='a'*40,source_signals_sha256='b'*64,cutoff=cut,created_at='2026-11-12T13:00:00Z',cost_pct=.3,registry=registry,previous=previous)


def previous(b):return {'results':b['results'],'paths':b['paths'],'states':b['states'],'manifest':b['manifest'],'pointer':b['pointer'],'statistics':b['statistics'],'pairs':b['pairs']}


def test_bundle_is_deterministic_deduplicates_signals_and_reads_price_once():
    s,h,cut=fixture();calls=[]
    def load(m,t):calls.append((m,t));return h
    kwargs=dict(core={'run_id':'same','generated_at':'2026-01-02T22:00:00Z'},source_commit='a'*40,source_signals_sha256='b'*64,
        cutoff=cut,created_at='2026-11-12T13:00:00Z',cost_pct=.3)
    a=build([s,s],load,**kwargs);b=build([s],load,**kwargs)
    assert len(calls)==2 and a['pointer']==b['pointer'] and a['raw']==b['raw'] and len(a['results'])==21
    again=make(registry=a['registry'],previous=previous(a),core=kwargs['core'])
    assert all(r['entry_price']==100 for r in again['results'])


def test_publication_preserves_official_bytes_and_all_older_research_runs(tmp_path):
    a=make();root=tmp_path/'published';(root/'latest').mkdir(parents=True);(root/'history').mkdir()
    (root/'latest/manifest.json').write_bytes(canonical({'run_id':'source-current','generated_at':'2026-01-02T22:00:00Z'}))
    for name in ('signals.csv','signal_outcomes.csv','trend_tracking.csv','trend_tracking_daily.csv.gz'):(root/'history'/name).write_bytes(b'official fixture unchanged')
    protected={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    publish(root,a);reg,old=load_previous(root);publish(root,a)
    assert reg==a['registry'] and old['results']==a['results']
    assert all((root/path).read_bytes()==raw for path,raw in protected.items())
    s,h,cut=fixture();h.loc[h.index[-1],'High']=103
    b=make(prices=h,previous=old,registry=reg);publish(root,b)
    assert len(list((root/'exit_experiments/exit-research-v2/runs').glob('*/manifest.json')))==2
    assert all(r['exclusion_reason']=='PRICE_REVISION_REQUIRES_REVIEW' for r in b['results'])


def test_same_run_repeated_results_are_reused_and_registry_is_immutable(tmp_path):
    a=make();b=make(previous=previous(a),registry=a['registry']);assert a['raw']==b['raw'] and a['pointer']==b['pointer']
    with pytest.raises(ValueError,match='VERSION'):registry_for(a['registry'],'2026-11-13T00:00:00Z',.5)


def test_price_revision_is_not_silently_cleared_after_retry_or_missing_cache():
    a=make();s,h,cut=fixture();h.loc[h.index[20],'Close']=101
    b=make(prices=h,previous=previous(a),registry=a['registry'])
    c=make(prices=pd.DataFrame(),previous=previous(b),registry=a['registry'])
    d=make(prices=h,previous=previous(c),registry=a['registry'])
    assert all(r.get('exclusion_reason')=='PRICE_REVISION_REQUIRES_REVIEW' for r in d['results'])


def test_aged_out_warmup_uses_archived_real_bars_without_mislabeling_revision():
    a=make();s,h,cut=fixture();h=h.iloc[20:]
    b=make(prices=h,previous=previous(a),registry=a['registry'])
    assert all(r['eligibility_status']=='eligible' for r in b['results'])
    assert a['raw']==b['raw']


def test_missing_cache_keeps_certified_completed_history_and_later_detects_revision():
    a=make();b=make(prices=pd.DataFrame(),previous=previous(a),registry=a['registry'])
    assert all(r['completion_status']=='complete' for r in b['results'])
    assert all('current_price_revalidation' in r for r in b['results'])
    s,h,cut=fixture();h.loc[h.index[20],'Close']=101
    c=make(prices=h,previous=previous(b),registry=a['registry'])
    assert all(r.get('exclusion_reason')=='PRICE_REVISION_REQUIRES_REVIEW' for r in c['results'])


def test_new_core_blocks_research_publication_without_any_core_changes(tmp_path):
    a=make();(tmp_path/'latest').mkdir();raw=canonical({'run_id':'new'});(tmp_path/'latest/manifest.json').write_bytes(raw)
    with pytest.raises(ValueError,match='CORE_MOVED'):publish(tmp_path,a)
    assert (tmp_path/'latest/manifest.json').read_bytes()==raw and not (tmp_path/'exit_experiments').exists()


def test_immutable_result_conflict_does_not_replace_existing_publication(tmp_path):
    a=make();(tmp_path/'latest').mkdir();(tmp_path/'latest/manifest.json').write_bytes(canonical({'run_id':'source-current','generated_at':'2026-01-02T22:00:00Z'}));publish(tmp_path,a)
    path=tmp_path/a['pointer']['manifest_file'];before=path.read_bytes();b=deepcopy(a);b['raw']['results.json.gz']=b'bad'
    with pytest.raises(ValueError):publish(tmp_path,b)
    assert path.read_bytes()==before and load_previous(tmp_path)[1]['results']==a['results']


def test_conflicting_same_signal_id_is_excluded_not_last_row_wins():
    s,h,cut=fixture();other={**s,'config_hash':'changed'}
    a=make(signals=[s,other]);assert all(r['exclusion_reason']=='DUPLICATE_SIGNAL_ID_CONFLICT' for r in a['results'])


def test_research_failure_and_success_do_not_change_real_legacy_outputs(tmp_path,monkeypatch):
    from test_forward_outcomes import prices
    from watchlist import trend_watchlist as engine
    from watchlist.trend_tracking import observe
    from copy import deepcopy
    root=tmp_path/'published';(root/'history').mkdir(parents=True);(tmp_path/'prices').mkdir()
    h=prices(pd.bdate_range('2026-01-02',periods=21))
    pd.DataFrame([dict(first_seen='2026-01-02',as_of='2026-01-02',market='US',sub_market='SP500',ticker='TEST',stop_price=90.,signal_close=100.)]).to_csv(root/'history/signals.csv',index=False)
    h.to_pickle(tmp_path/'prices/TEST.pkl');monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',tmp_path/'prices')
    monkeypatch.setattr(engine,'_make_bench_getter',lambda cfg:lambda *args:h)
    engine.update_signal_outcomes(root,deepcopy(engine.DEFAULT_CONFIG))
    s,history,cut=fixture();before_observe=observe(s,history,cut)
    (root/'history/trend_tracking.csv').write_text(json.dumps(before_observe[0],default=str));(root/'history/trend_tracking_daily.csv.gz').write_bytes(b'certified synthetic legacy daily')
    (root/'latest').mkdir();(root/'latest/manifest.json').write_bytes(canonical({'run_id':'source-current','generated_at':'2026-01-02T22:00:00Z'}))
    protected={str(p.relative_to(root)):p.read_bytes() for p in root.rglob('*') if p.is_file()}
    a=make();publish(root,a)
    broken=deepcopy(a);broken['raw']['results.json.gz']=b'invalid'
    with pytest.raises(ValueError):publish(root,broken)
    assert all((root/p).read_bytes()==raw for p,raw in protected.items())
    assert observe(s,history,cut)==before_observe


def test_source_market_signals_cost_and_success_evidence_are_verified(tmp_path):
    s,h,cut=fixture();s['workflow_run_id']='123';s.pop('publication_verified');raw=pd.DataFrame([s]).to_csv(index=False).encode()
    (tmp_path/'history').mkdir();(tmp_path/'analysis').mkdir();(tmp_path/'history/signals.csv').write_bytes(raw)
    core={'run_id':'current'}
    fact={'id':123,'status':'completed','conclusion':'success','head_branch':'main','path':'.github/workflows/watchlist.yml',
        'created_at':'2026-01-02T21:30:00Z','updated_at':'2026-01-02T22:05:00Z'}
    analysis={'source':{'run_id':'current','history_update_ok':True,'history_sources':{'signals.csv':{'sha256':digest(raw)}}},
        'publication_evidence':{'123':[fact]},'validation':{'policy':{'round_trip_cost_pct':.3}}}
    report=tmp_path/'analysis/latest.md';report.write_text('````json\n'+json.dumps(analysis)+'\n````')
    signals,sha,cost=read_market_inputs(tmp_path,core,lookup=lambda _:pytest.fail('known evidence refetched'))
    assert signals[0]['publication_verified'] and signals[0]['published_at']=='2026-01-02T22:05:00+00:00' and cost==.3 and sha==digest(raw)
    analysis['publication_evidence']['123'][0]['conclusion']='failure';report.write_text('````json\n'+json.dumps(analysis)+'\n````')
    signals,_,_=read_market_inputs(tmp_path,core,lookup=lambda _:fact|{'conclusion':'failure'})
    assert not signals[0]['publication_verified']
    (tmp_path/'history/signals.csv').write_bytes(raw+b'changed')
    with pytest.raises(ValueError,match='LINEAGE'):read_market_inputs(tmp_path,core)


def test_workflow_cache_permission_failure_boundary_and_add_scope_are_isolated():
    import yaml
    root=Path(__file__).resolve().parents[1];doc=yaml.safe_load((root/'tests/fixtures/workflows/exit_experiments.yml').read_text());trigger=doc.get('on',doc.get(True))
    assert list(trigger)==['workflow_run'] and trigger['workflow_run']['workflows']==['watchlist']
    assert doc['concurrency']['group'] not in ('watchlist-data','manual-watchlist-eod')
    assert doc['permissions']=={'contents':'write','actions':'read'}
    steps=doc['jobs']['publish']['steps'];cache=next(s for s in steps if s.get('id')=='core_cache')
    assert cache['with']['key']=='wl-cache-${{ github.event.workflow_run.id }}' and 'restore-keys' not in cache['with']
    script='\n'.join(s.get('run','') for s in steps)
    assert '"$CACHE_HIT" != \'true\'' in script and '"$RUN_ATTEMPT" != \'1\'' in script
    assert 'git add -- watchlist/published/exit_experiments' in script and '--verify-core-hash' in script
    assert 'secrets.' not in str(doc)


def test_same_ticker_multiple_signals_load_common_ohlc_once():
    s,h,cut=fixture();later={**s,'first_seen':'2026-01-03'};calls=[]
    def load(m,t):calls.append((m,t));return h
    b=build([s,later],load,core={'run_id':'x','generated_at':'2026-01-02T22:00:00Z'},source_commit='a'*40,source_signals_sha256='b'*64,
        cutoff=cut,created_at='2026-11-12T13:00:00Z',cost_pct=.3)
    assert len(calls)==1 and len(b['results'])==42


@pytest.mark.parametrize('length,horizon,complete',[(19,20,False),(20,20,True),(59,60,False),(60,60,True),(119,120,False),(120,120,True)])
def test_each_horizon_maturity_waits_for_full_actual_path(length,horizon,complete):
    s,h,cut=fixture(length=length)
    b=build([s],lambda m,t:h,core={'run_id':'horizon','generated_at':'2026-01-02T22:00:00Z'},source_commit='a'*40,source_signals_sha256='b'*64,
        cutoff=cut,created_at='2026-11-12T13:00:00Z',cost_pct=.3)
    summary=next(x for x in b['statistics'] if x['vintage']=='all' and x['market']=='ALL')['horizons'][(20,60,120).index(horizon)]
    assert summary['common_complete_n']==int(complete)
    assert summary['signals']==summary['common_complete_n']+summary['pending_n']+summary['excluded_n']


def test_explicit_new_rule_version_uses_separate_path_and_preserves_old_version(tmp_path,monkeypatch):
    import watchlist.exit_policy_experiments as calc
    import watchlist.exit_experiment_publish as pub
    import watchlist.exit_experiment_contract as contract
    a=make();(tmp_path/'latest').mkdir();(tmp_path/'latest/manifest.json').write_bytes(canonical({'run_id':'source-current','generated_at':'2026-01-02T22:00:00Z'}));publish(tmp_path,a)
    old={str(p.relative_to(tmp_path)):p.read_bytes() for p in (tmp_path/'exit_experiments/exit-research-v2').rglob('*') if p.is_file()}
    monkeypatch.setattr(calc,'RULE_VERSION','exit-research-v3');monkeypatch.setattr(pub,'RULE_VERSION','exit-research-v3');monkeypatch.setattr(pub,'VERSION','exit-research-v3')
    monkeypatch.setattr(pub,'ROOT','exit_experiments/exit-research-v3');monkeypatch.setattr(contract,'VERSION','exit-research-v3')
    monkeypatch.setattr(pub,'CONFIG',{**pub.CONFIG,'rule_version':'exit-research-v3'})
    b=make();pub.publish(tmp_path,b)
    assert (tmp_path/'exit_experiments/exit-research-v3/latest.json').exists()
    assert all((tmp_path/p).read_bytes()==raw for p,raw in old.items())


def test_retrospective_and_prospective_are_frozen_by_registry_not_later_relabelled():
    s,h,cut=fixture();args=dict(core={'run_id':'x','generated_at':'2026-01-02T22:00:00Z'},source_commit='a'*40,source_signals_sha256='b'*64,cutoff=cut,cost_pct=.3)
    retro=build([s],lambda m,t:h,created_at='2026-11-12T13:00:00Z',**args)
    assert all(r['vintage']=='retrospective' for r in retro['results'])
    prospective=build([s],lambda m,t:h,created_at='2025-12-01T00:00:00Z',**args)
    assert all(r['vintage']=='prospective' for r in prospective['results'])
    repeat=build([s],lambda m,t:h,created_at='2026-12-01T00:00:00Z',registry=prospective['registry'],previous=previous(prospective),**args)
    assert all(r['vintage']=='prospective' for r in repeat['results'])


def test_identical_price_hash_skips_policy_simulation_not_just_result_writing(monkeypatch):
    import watchlist.exit_experiment_publish as pub
    a=make()
    monkeypatch.setattr(pub,'simulate',lambda *a,**k:pytest.fail('unchanged input was backtested again'))
    b=make(previous=previous(a),registry=a['registry'])
    assert a['raw']==b['raw']


def test_signal_anchor_price_revision_never_reuses_old_complete_rows_as_current():
    a=make();s,h,cut=fixture();h.loc[pd.Timestamp(s['as_of']),'Close']=101
    b=make(prices=h,previous=previous(a),registry=a['registry'])
    assert all(r['exclusion_reason']=='PRICE_REVISION_REQUIRES_REVIEW' and not r['comparison_eligible'] for r in b['results'])


def test_source_config_revision_excludes_and_preserves_original_trusted_fingerprint():
    a=make();s,h,cut=fixture();s['config_hash']='new-selection-config'
    b=make(signals=[s],previous=previous(a),registry=a['registry'])
    assert all(r['exclusion_reason']=='SOURCE_OR_COST_REVISION_REQUIRES_REVIEW' for r in b['results'])
    sid=next(iter(a['states']));assert b['states'][sid]['source_signal_fingerprint']==a['states'][sid]['source_signal_fingerprint']


