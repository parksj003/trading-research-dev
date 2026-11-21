"""Sector review contracts: synthetic public metadata and temporary files only."""
from copy import deepcopy
from datetime import timedelta
import importlib
import json

import pytest

NOW='2026-11-01T12:00:00Z'
WEEK='2026-11-08T12:00:00Z'


@pytest.fixture
def review():
    return importlib.import_module('watchlist.sector_review')


def observation(**changes):
    row=dict(market='US',ticker='SYNTH',name='Synthetic',current_macro_theme='Old',current_sector='Old sub',
        origin='static_map',raw_sector='Old industry',raw_sub_industry='Old category',
        external_macro_theme='Old',external_sector='Old sub',provider='synthetic-public-source',
        source_urls=['https://example.org/industries'],etfs=[],
        retrieved_at=NOW,source_as_of='2026-10-30',policy_hash='rules-v1')
    row.update(changes);return row


def inputs(*rows,stale=False):return {'US':dict(observations=list(rows),stale=stale)}


def changed(**changes):
    return observation(raw_sector='New industry',raw_sub_industry='New category',
        external_macro_theme='New',external_sector='New sub',retrieved_at=WEEK,source_as_of='2026-11-06',**changes)


def baseline(review,tmp_path):
    doc=review.prepare_publication(tmp_path,inputs(observation()),run_id='r1',generated_at=NOW)
    commit(review,tmp_path,doc);return doc


def commit(review,root,doc):
    import hashlib
    latest=root/'latest';latest.mkdir(exist_ok=True)
    raw=review.json_bytes(doc);(latest/review.FILE_NAME).write_bytes(raw)
    (latest/'manifest.json').write_text(json.dumps(dict(run_id=doc['run_id'],files={review.FILE_NAME:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())})))


def test_initial_observation_seeds_baseline_without_reclassifying(review,tmp_path):
    before=observation();doc=baseline(review,tmp_path)
    assert doc['schema']==review.SCHEMA and doc['candidates']==[]
    assert doc['markets']['US']['status']=='BASELINE_CREATED'
    assert before==observation()


def test_static_map_does_not_hide_real_source_change(review,tmp_path):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK)
    candidate=doc['candidates'][0]
    assert candidate['status']=='PENDING' and candidate['actionable']
    assert candidate['current_sector']=='Old sub' and candidate['proposed_sector']=='New sub'
    assert candidate['before']['raw_sector']=='Old industry' and candidate['after']['raw_sector']=='New industry'
    assert candidate['after']['source_urls']==['https://example.org/industries']


def test_weekly_gate_does_not_promote_cached_metadata_to_fresh(review,tmp_path):
    old=baseline(review,tmp_path)
    soon=review.prepare_publication(tmp_path,inputs(changed()),run_id='soon',generated_at='2026-11-02T12:00:00Z')
    assert soon['candidates']==[] and soon['state_ref']==old['state_ref']
    stale=review.prepare_publication(tmp_path,inputs(observation()),run_id='stale',generated_at=WEEK)
    assert stale['candidates']==[] and stale['markets']['US']['status']=='WAITING_FOR_FRESH_SOURCE'


@pytest.mark.parametrize('problem',['stale','empty_source','future','provider_change','manual_lock'])
def test_uncertain_or_locked_information_cannot_be_approved(review,tmp_path,problem):
    baseline(review,tmp_path);row=changed();stale=False
    if problem=='stale':stale=True
    if problem=='empty_source':row.update(raw_sector='',raw_sub_industry='',external_macro_theme='Other',external_sector='Unknown')
    if problem=='future':row['retrieved_at']='2026-11-09T12:00:00Z'
    if problem=='provider_change':row['provider']='different-source'
    if problem=='manual_lock':row['origin']='manual_override'
    doc=review.prepare_publication(tmp_path,inputs(row,stale=stale),run_id='bad',generated_at=WEEK)
    assert not any(c['actionable'] for c in doc['candidates'])


def test_etf_weight_only_and_query_timestamp_changes_do_not_reclassify(review,tmp_path):
    row=observation(etfs=[dict(code='111111',macro_theme='Old',sector='Old sub',weight=10)])
    old=review.prepare_publication(tmp_path,inputs(row),run_id='r1',generated_at=NOW);commit(review,tmp_path,old)
    row.update(retrieved_at=WEEK,source_as_of='2026-11-06');row['etfs'][0]['weight']=11
    doc=review.prepare_publication(tmp_path,inputs(row),run_id='r2',generated_at=WEEK)
    assert doc['candidates']==[]


def test_approval_is_explicit_forward_effective_and_preserves_existing_files(review,tmp_path):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    c=doc['candidates'][0]
    decision=review.make_decision(c,decision='approve',reviewer='Maintainer',reviewed_at=WEEK,effective_from='2026-11-09')
    path=tmp_path/'sector_review_decisions.csv'
    result=review.apply_decision(tmp_path,decision,path,now=WEEK,apply=False)
    assert result['status']=='DRY_RUN' and not path.exists()
    review.apply_decision(tmp_path,decision,path,now=WEEK,apply=True)
    old=path.read_bytes();review.apply_decision(tmp_path,decision,path,now=WEEK,apply=True)
    assert path.read_bytes()==old
    assert review.load_decisions(path,tmp_path,now=WEEK)[0]=={}
    effective,_=review.load_decisions(path,tmp_path,now='2026-11-09T12:00:00Z')
    assert effective=={'US:SYNTH':('New','New sub')}
    assert not (tmp_path/'history/signals.csv').exists()


@pytest.mark.parametrize('problem',['backdate','tamper','missing_reviewer','stale_proposal'])
def test_invalid_approvals_fail_closed(review,tmp_path,problem):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    c=doc['candidates'][0]
    decision=review.make_decision(c,decision='approve',reviewer='Maintainer',reviewed_at=WEEK,effective_from='2026-11-09')
    if problem=='backdate':decision['effective_from']='2026-11-08'
    if problem=='tamper':decision['proposed_sector']='Invented'
    if problem=='missing_reviewer':decision['reviewer']=''
    if problem=='stale_proposal':
        newer=changed();newer.update(raw_sector='Third',external_macro_theme='Third',external_sector='Third')
        latest=review.prepare_publication(tmp_path,inputs(newer),run_id='r3',generated_at='2026-11-15T12:00:00Z');commit(review,tmp_path,latest)
    with pytest.raises(ValueError):review.apply_decision(tmp_path,decision,tmp_path/'decisions.csv',now='2026-11-15T12:00:00Z',apply=True)
    assert not (tmp_path/'decisions.csv').exists()


def test_rejection_is_recorded_without_changing_classification(review,tmp_path):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    decision=review.make_decision(doc['candidates'][0],decision='reject',reviewer='Maintainer',reviewed_at=WEEK)
    path=tmp_path/'decisions.csv';review.apply_decision(tmp_path,decision,path,now=WEEK,apply=True)
    active,decisions=review.load_decisions(path,tmp_path,now=WEEK)
    assert active=={}
    next_doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r3',generated_at=WEEK,decisions=decisions)
    assert next_doc['candidates'][0]['status']=='REJECTED'


def test_corrupt_state_holds_and_can_resume_after_original_is_restored(review,tmp_path):
    old=baseline(review,tmp_path);p=tmp_path/review.OBJECTS/(old['state_ref']+'.json.gz');raw=p.read_bytes()
    p.write_bytes(b'corrupt')
    blocked=review.prepare_publication(tmp_path,inputs(changed()),run_id='bad',generated_at=WEEK)
    assert blocked['status']=='unavailable' and blocked['state_ref']==old['state_ref']
    commit(review,tmp_path,blocked);p.write_bytes(raw)
    resumed=review.prepare_publication(tmp_path,inputs(changed()),run_id='retry',generated_at=WEEK)
    assert resumed['status']=='ready' and len(resumed['candidates'])==1


def test_lost_untrusted_reference_does_not_silently_create_a_new_baseline(review,tmp_path):
    baseline(review,tmp_path);(tmp_path/'latest'/review.FILE_NAME).write_bytes(b'corrupt')
    blocked=review.prepare_publication(tmp_path,inputs(changed()),run_id='bad',generated_at=WEEK)
    assert blocked['status']=='unavailable' and blocked['recovery_required']
    commit(review,tmp_path,blocked)
    assert review.prepare_publication(tmp_path,inputs(changed()),run_id='retry',generated_at=WEEK)['status']=='unavailable'


def test_rules_change_within_week_invalidates_old_pending_proposals(review,tmp_path):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    payload=inputs(changed());payload['US']['policy_hash']='new-rules'
    doc=review.prepare_publication(tmp_path,payload,run_id='new-rules',generated_at='2026-11-09T12:00:00Z')
    assert doc['candidates'][0]['status']=='SUPERSEDED'
    assert doc['markets']['US']['status']=='WAITING_FOR_FRESH_SOURCE'


def test_duplicate_source_rows_cannot_replace_a_committed_baseline(review,tmp_path):
    old=baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed(),changed()),run_id='dup',generated_at=WEEK)
    assert doc['status']=='unavailable' and doc['state_ref']==old['state_ref']
    assert review.previous_publication(tmp_path)['state_ref']==old['state_ref']


@pytest.mark.parametrize('failure',['locked','replace_error','malformed_existing'])
def test_decision_write_failure_preserves_originals(review,tmp_path,monkeypatch,failure):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    decision=review.make_decision(doc['candidates'][0],decision='approve',reviewer='M',reviewed_at=WEEK,effective_from='2026-11-09')
    path=tmp_path/'decisions.csv';path.write_text(','.join(review.FIELDS)+'\n')
    if failure=='locked':path.with_suffix('.csv.lock').write_text('owned by another writer')
    if failure=='replace_error':monkeypatch.setattr(review.os,'replace',lambda *a:(_ for _ in ()).throw(OSError('injected')))
    if failure=='malformed_existing':path.write_bytes(b'bad,header\nkeep,original\n')
    raw=path.read_bytes()
    with pytest.raises((ValueError,OSError)):review.apply_decision(tmp_path,decision,path,now=WEEK,apply=True)
    assert path.read_bytes()==raw


def test_maintainer_cannot_record_a_backdated_effective_date(review,tmp_path):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    decision=review.make_decision(doc['candidates'][0],decision='approve',reviewer='M',reviewed_at=WEEK,effective_from='2026-11-09')
    with pytest.raises(ValueError):review.apply_decision(tmp_path,decision,tmp_path/'decisions.csv',now='2026-11-09T12:00:00Z',apply=True)


def test_raw_capture_keeps_manual_origin_and_does_not_modify_the_universe(tmp_path,monkeypatch):
    from watchlist import trend_watchlist as engine
    import pandas as pd
    f=pd.DataFrame([dict(market='US',ticker='NVDA',name='Synthetic',macro_theme='Manual',sector='Manual sub')])
    before=f.copy(deep=True)
    cfg=dict(_sector_review_capture=True,_sector_review_manual_keys={'US:NVDA'},_sector_review_policy_hash='p')
    out=engine._capture_sector_sources(f,cfg,{'US:NVDA':('Manual','Manual sub')},
        raw_sectors={'NVDA':'Health Care'},sub_industries={'NVDA':'Pharmaceuticals'},
        source_urls={'NVDA':['https://example.org/industry']},source_as_of='2026-11-01')
    evidence=out.iloc[0]['_sector_review_observation']
    assert evidence['origin']=='manual_override' and evidence['current_sector']=='Manual sub'
    assert evidence['external_sector']!='Manual sub' and evidence['raw_sector']=='Health Care'
    pd.testing.assert_frame_equal(f,before)


def test_empty_approval_configuration_preserves_original_mapping_hash(tmp_path,monkeypatch):
    from watchlist import trend_watchlist as engine
    from copy import deepcopy
    cfg=deepcopy(engine.DEFAULT_CONFIG);manual={'US:NVDA':('Manual','Manual sub')}
    before=engine.get_config_version_hash(cfg,manual);monkeypatch.setattr(engine,'ROOT',tmp_path)
    effective=engine._configure_sector_review(cfg,manual,NOW)
    assert effective==manual and engine.get_config_version_hash(cfg,effective)==before
    assert cfg['_sector_review_capture'] and not (tmp_path/'sector_review_decisions.csv').exists()


def test_optional_review_publication_keeps_all_existing_outputs_byte_identical(tmp_path,monkeypatch):
    from test_base_count_publish import install_engine_fixture
    from watchlist import trend_watchlist as engine
    cfg,run,_=install_engine_fixture(tmp_path,monkeypatch)
    seed=b'date,market,level,macro_theme,sector,sector_power\n2020-01-02,US,sector,Prior,Prior sub,50\n'
    for name in ('before','after'):
        history=tmp_path/name/'history';history.mkdir(parents=True)
        (history/'sector_power.csv').write_bytes(seed)
    cfg.pop('base_count');before=run(tmp_path/'before')
    cfg['_sector_review_capture']=True;cfg['_sector_review_decisions']=[]
    after=run(tmp_path/'after')
    assert before is not None and after is not None
    for name in list(engine.CONTRACT_REQUIRED)+['summary.json',engine.CHARTS_FILE]:
        assert (before/name).read_bytes()==(after/name).read_bytes(),name
    for name in ('signals.csv','signal_outcomes.csv','candidates_daily.csv','regime.csv','sector_power.csv'):
        assert (before.parent/'history'/name).read_bytes()==(after.parent/'history'/name).read_bytes(),name
    assert engine.validate_published_dir(after)==[]
    manifest=json.loads((after/'manifest.json').read_bytes())
    assert 'sector_review.json' in manifest['files'] and manifest['contract']['optional_observations']['sector_review']['schema']=='sector-review-1'
    state=manifest['files']['sector_review.json']
    doc=json.loads((after/'sector_review.json').read_bytes())
    cfg['_sector_review_capture']=False
    paused=run(tmp_path/'after')
    next_doc=json.loads((paused/'sector_review.json').read_bytes())
    assert next_doc['paused'] and next_doc['state_ref']==doc['state_ref']


def test_previously_automatic_classification_is_retained_until_approval(review,tmp_path):
    old=review.prepare_publication(tmp_path,inputs(observation(origin='fallback')),run_id='base',generated_at=NOW)
    commit(review,tmp_path,old)
    retained=review.retained_classifications(tmp_path)
    assert retained=={'US:SYNTH':('Old','Old sub')}
    assert review.retained_classifications(tmp_path,static_keys={'US:SYNTH'})=={}
    from watchlist import trend_watchlist as engine
    # A new outside industry cannot directly change an established assignment.
    assert engine.classify_theme_and_sector('US','SYNTH','Health Care','Pharmaceuticals',retained)[:2]==('Old','Old sub')


def test_direct_override_still_wins_over_retained_classification(review,tmp_path,monkeypatch):
    from watchlist import trend_watchlist as engine
    from copy import deepcopy
    root=tmp_path/'published';old=review.prepare_publication(root,inputs(observation(origin='fallback')),run_id='base',generated_at=NOW)
    commit(review,root,old)
    cfg=deepcopy(engine.DEFAULT_CONFIG);monkeypatch.setattr(engine,'ROOT',tmp_path)
    manual={'US:SYNTH':('User','User sub')}
    assert engine._configure_sector_review(cfg,manual,NOW)['US:SYNTH']==manual['US:SYNTH']


def test_effective_date_alone_does_not_claim_an_observed_classification_change(review,tmp_path):
    baseline(review,tmp_path)
    doc=review.prepare_publication(tmp_path,inputs(changed()),run_id='r2',generated_at=WEEK);commit(review,tmp_path,doc)
    decision=review.make_decision(doc['candidates'][0],decision='approve',reviewer='M',reviewed_at=WEEK,effective_from='2026-11-09')
    scheduled=review.prepare_publication(tmp_path,inputs(changed()),run_id='scheduled',generated_at=WEEK,decisions=[decision])
    assert scheduled['candidates'][0]['status']=='SCHEDULED';commit(review,tmp_path,scheduled)
    later='2026-11-09T12:00:00Z'
    row=changed();row['retrieved_at']=later
    waiting=review.prepare_publication(tmp_path,inputs(row),run_id='wait',generated_at=later,decisions=[decision])
    assert waiting['candidates'][0]['status']=='APPROVED_WAITING'
    row.update(current_macro_theme='New',current_sector='New sub',origin='reviewed_override')
    applied=review.prepare_publication(tmp_path,inputs(row),run_id='applied',generated_at=later,decisions=[decision])
    assert applied['candidates'][0]['status']=='APPLIED' and applied['candidates'][0]['applied_at']==later.replace('Z','+00:00')


def test_stale_cache_view_cannot_revert_a_protected_classification_or_retag_source_time():
    from watchlist import trend_watchlist as engine
    import pandas as pd
    evidence=observation(current_macro_theme='Wrong',current_sector='Wrong sub')
    old=pd.DataFrame([dict(market='US',ticker='SYNTH',macro_theme='Wrong',sector='Wrong sub',
        is_stale_cache=True,_sector_review_observation=evidence)])
    cfg=dict(_sector_review_configured=True,_sector_review_approved_keys={'US:SYNTH'})
    out=engine._protect_sector_assignments(old,cfg,{'US:SYNTH':('Approved','Approved sub')})
    assert tuple(out.loc[0,['macro_theme','sector']])==('Approved','Approved sub')
    assert tuple(old.loc[0,['macro_theme','sector']])==('Wrong','Wrong sub')
    assert out.loc[0,'_sector_review_observation']['retrieved_at']==NOW
    assert out.loc[0,'_sector_review_observation']['origin']=='reviewed_override'
    assert evidence['current_sector']=='Wrong sub' and out.loc[0,'is_stale_cache']

