"""Research-only vendor checks and checkpoint compatibility; no operating data."""
from dataclasses import asdict
import copy
import json

import numpy as np
import pandas as pd
import pytest

from test_base_detector import detector,first_base,run,context
from test_base_count_publish import settings,candidates,cached,commit,NOW
from watchlist.base_detector import BaseDetector,StateConflict
from watchlist import base_count_publish as published
from research.base_count import checkpoint_blocks as blocks
from research.base_count.kr_input_audit import parse_naver,compare_frames


def payload(model=None,f=None):
    model=model or detector();batch=run(model,first_base() if f is None else f)
    return dict(schema='base-instrument-1',instrument='US:SYNTH',parent=None,
        checkpoint=model.checkpoint(),observations=[asdict(x) for x in batch.observations],events=[asdict(x) for x in batch.events])


def test_legacy_instrument_is_reconstructed_without_any_changes(tmp_path):
    original=payload();ref=published.write_object(tmp_path,original)
    assert blocks.load_instrument(tmp_path,ref,expected_context=context())==original


@pytest.mark.parametrize('size',[32,128,256,512])
def test_block_checkpoint_is_exact_full_checkpoint_not_a_lossy_summary(tmp_path,size):
    original=payload();before=copy.deepcopy(original)
    ref=blocks.store_instrument(tmp_path,original,block_size=size)
    recovered=blocks.load_instrument(tmp_path,ref,expected_context=context())
    assert recovered==original==before
    restored=BaseDetector.restore(recovered['checkpoint'],**context())
    assert run(restored,first_base()).latest.completed_base_count==1
    assert run(restored,first_base()).events==()


def test_unknown_history_is_not_promoted_to_absolute_base_one(tmp_path):
    original=payload(f=first_base().iloc[:50])
    ref=blocks.store_instrument(tmp_path,original,block_size=128)
    out=blocks.load_instrument(tmp_path,ref,expected_context=context())
    assert out['checkpoint']['payload']['state']['latest']['completed_base_count'] is None


def test_empty_checkpoint_roundtrip(tmp_path):
    original=dict(schema='base-instrument-1',instrument='US:SYNTH',parent=None,
        checkpoint=detector().checkpoint(),observations=[],events=[])
    ref=blocks.store_instrument(tmp_path,original,block_size=128)
    assert blocks.load_instrument(tmp_path,ref,expected_context=context())==original


def test_mixed_v1_to_v2_chain_preserves_old_bytes_and_reuses_complete_blocks(tmp_path):
    f=first_base();model=detector();old=payload(model,f.iloc[:-1]);old_ref=published.write_object(tmp_path,old)
    new_batch=run(model,f)
    child=dict(schema='base-instrument-1',instrument='US:SYNTH',parent=old_ref,checkpoint=model.checkpoint(),
               observations=[asdict(x) for x in new_batch.observations],events=[asdict(x) for x in new_batch.events])
    first=blocks.store_instrument(tmp_path,old,block_size=128)
    existing={p.name:p.read_bytes() for p in (tmp_path/published.OBJECT_DIR).iterdir()}
    second=blocks.store_instrument(tmp_path,child,block_size=128)
    before=published.read_object(tmp_path,first);after=published.read_object(tmp_path,second)
    assert before['seen_blocks'][:-1]==after['seen_blocks'][:-1]
    assert all((tmp_path/published.OBJECT_DIR/name).read_bytes()==raw for name,raw in existing.items())
    assert blocks.load_instrument(tmp_path,second,expected_context=context())==child
    assert blocks.load_instrument(tmp_path,old_ref,expected_context=context())==old


@pytest.mark.parametrize('fault',['missing','corrupt','wrong_count','reordered','duplicate','metadata'])
def test_missing_or_corrupt_blocks_fail_closed_without_state_reset(tmp_path,fault):
    ref=blocks.store_instrument(tmp_path,payload(),block_size=128);root=published.read_object(tmp_path,ref)
    first=root['seen_blocks'][0]
    if fault=='missing':(tmp_path/published.OBJECT_DIR/(first+'.json.gz')).unlink()
    elif fault=='corrupt':(tmp_path/published.OBJECT_DIR/(first+'.json.gz')).write_bytes(b'broken')
    else:
        if fault=='wrong_count':root['seen_count']+=1
        if fault=='reordered':root['seen_blocks'].reverse()
        if fault=='duplicate':root['seen_blocks'].append(first)
        if fault=='metadata':root['legacy_payload_without_seen']['events'][0]['reason']='changed'
        ref=published.write_object(tmp_path,root)
    with pytest.raises(StateConflict):blocks.load_instrument(tmp_path,ref,expected_context=context())


def test_configuration_mismatch_does_not_start_a_new_cycle(tmp_path):
    from test_base_detector import config
    ref=blocks.store_instrument(tmp_path,payload(),block_size=128)
    with pytest.raises(StateConflict):blocks.load_instrument(tmp_path,ref,expected_context=context(config=config(hold_sessions=4)))


def test_reconstructed_old_price_hashes_still_detect_historical_volume_revision(tmp_path):
    original=payload();ref=blocks.store_instrument(tmp_path,original,block_size=128)
    restored=blocks.load_instrument(tmp_path,ref,expected_context=context())
    model=BaseDetector.restore(restored['checkpoint'],**context());before=model.checkpoint()
    f=first_base();f.loc[f.index[0],'Volume']*=2
    assert run(model,f).issue=='HISTORY_REVISION_REQUIRES_REBUILD'
    assert model.checkpoint()==before


def test_legacy_publisher_rejects_experimental_objects_instead_of_faking_counts(tmp_path):
    ref=blocks.store_instrument(tmp_path,payload(),block_size=128)
    generation=published.write_object(tmp_path,dict(schema='base-generation-1',parent=None,run_id='old',
        generated_at=NOW,instruments={'US:SYNTH':ref},observations=[]))
    commit(tmp_path,dict(schema=published.SCHEMA,run_id='old',generated_at=NOW,status='enabled',generation=generation,observations=[]))
    result=published.prepare_publication(tmp_path,candidates(),settings(),lambda m,t:cached(),run_id='legacy',generated_at=NOW)
    assert result['observations'][0]['reason']=='CHECKPOINT_OBJECT_INVALID'
    assert result['observations'][0]['completed_base_count'] is None


def test_failed_publication_cleans_only_new_experimental_objects(tmp_path):
    old=payload();old_ref=published.write_object(tmp_path,old)
    existing={p.name:p.read_bytes() for p in (tmp_path/published.OBJECT_DIR).iterdir()}
    with published.publication_lock(tmp_path):blocks.store_instrument(tmp_path,old,block_size=128)
    assert {p.name:p.read_bytes() for p in (tmp_path/published.OBJECT_DIR).iterdir()}==existing
    assert published.read_object(tmp_path,old_ref)==old


def test_wrong_instrument_label_is_rejected_even_when_object_checksums_are_valid(tmp_path):
    original=payload();original['instrument']='US:OTHER'
    ref=blocks.store_instrument(tmp_path,original,block_size=128)
    with pytest.raises(StateConflict):blocks.load_instrument(tmp_path,ref,expected_context=context())


def test_identical_checkpoint_reuses_verified_objects_without_temporary_rewrites(tmp_path,monkeypatch):
    original=payload();ref=blocks.store_instrument(tmp_path,original,block_size=128)
    def forbidden(*args):pytest.fail('unchanged content must not be rewritten')
    monkeypatch.setattr(blocks,'write_object',forbidden)
    assert blocks.store_instrument(tmp_path,original,block_size=128)==ref


def test_reuse_still_rejects_corrupt_existing_fingerprint_block(tmp_path):
    original=payload();ref=blocks.store_instrument(tmp_path,original,block_size=128)
    block=published.read_object(tmp_path,ref)['seen_blocks'][0]
    (tmp_path/published.OBJECT_DIR/(block+'.json.gz')).write_bytes(b'corrupt')
    with pytest.raises(StateConflict):blocks.store_instrument(tmp_path,original,block_size=128)


@pytest.mark.parametrize('size',[0,-1,True,1.5])
def test_block_size_requires_an_explicit_positive_integer(tmp_path,size):
    with pytest.raises(ValueError):blocks.store_instrument(tmp_path,payload(),block_size=size)


def test_naver_json_like_response_is_parsed_as_literals_not_executed():
    raw="[['날짜','시가','고가','저가','종가','거래량'],['20260102',100,110,90,105,1000],]".encode()
    frame=parse_naver(raw,'2026-01-01','2026-01-31')
    assert frame.index[0]==pd.Timestamp('2026-01-02') and frame.Close.iloc[0]==105
    with pytest.raises((ValueError,SyntaxError)):parse_naver(b"__import__('os').system('false')",'2026-01-01','2026-01-31')


def test_internal_price_errors_are_separate_from_calendar_and_halt_uncertainty():
    index=pd.DatetimeIndex(['2026-01-02','2026-01-05','2026-01-06','2026-01-07'])
    n=pd.DataFrame(dict(Open=100.,High=110.,Low=90.,Close=105.,Volume=1000.),index=index)
    y=n.copy();y.loc[index[1],'High']=1;y=y.drop(index[2]);y.loc[index[3],'Volume']=0;n.loc[index[3],'Volume']=0
    report,exceptions=compare_frames(y,n,n.Close*.9)
    reasons='|'.join(x['reasons'] for x in exceptions)
    assert 'YAHOO_INTERNAL_OHLC_INVALID' in reasons
    assert 'YAHOO_MISSING_NAVER_POSITIVE_TRADE' in reasons
    assert 'BOTH_ZERO_VOLUME_HALT_OR_PADDING_UNVERIFIED' in reasons
    assert report['basis_certified'] is False and report['holiday_cause_confirmed'] is False
    assert report['raw_quote_close_ratio_median']==1


def test_zero_halt_prices_are_not_mislabeled_as_positive_price_ordering_errors():
    ix=pd.to_datetime(['2026-01-02','2026-01-05'])
    n=pd.DataFrame(dict(Open=[100,0],High=[110,0],Low=[90,0],Close=[105,105],Volume=[1000,0]),index=ix)
    _,exceptions=compare_frames(n,n,n.Close)
    second=next(x for x in exceptions if x['date']=='2026-01-05')
    assert 'BOTH_ZERO_VOLUME_HALT_OR_PADDING_UNVERIFIED' in second['reasons']
    assert 'MISSING_OR_NONPOSITIVE_OHLC' in second['reasons']
    assert 'INTERNAL_OHLC_INVALID' not in second['reasons']

