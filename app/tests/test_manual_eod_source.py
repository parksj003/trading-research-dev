from copy import deepcopy
import json
import pytest

from manual_eod_fixture import snapshot_for
from manual_eod_contract import canonical,packed,descriptor,load_snapshot,validate_snapshot
from manual_snapshot_source import load_published_eod
from test_manual_catalog import row


def source():
    doc,core=snapshot_for([row()]);raw=packed(doc);ptr=descriptor(doc,raw)
    files={'manual_eod/latest.json':canonical(ptr),ptr['snapshot']['file']:raw,'latest/manifest.json':canonical(core)}
    return doc,files


@pytest.fixture(autouse=True)
def clear_cache():
    load_published_eod.clear()
    yield
    load_published_eod.clear()


def test_read_only_immutable_source_is_cached_and_never_reads_account_paths():
    doc,files=source();paths=[]
    def read(path):paths.append(path);return files[path]
    actual,state=load_published_eod('synthetic',read)
    again,_=load_published_eod('synthetic',lambda p:pytest.fail('cache refetched'))
    assert actual==again==doc and state['status']=='ready'
    assert paths==['manual_eod/latest.json',json.loads(files['manual_eod/latest.json'])['snapshot']['file'],'latest/manifest.json']


def test_bad_latest_hash_uses_previous_good_object():
    doc,files=source();files['manual_eod/previous.json']=files['manual_eod/latest.json']
    bad=json.loads(files['manual_eod/latest.json']);bad['snapshot']['bytes']+=1
    files['manual_eod/latest.json']=canonical(bad)
    actual,state=load_published_eod('synthetic',files.get)
    assert actual==doc and state['status']=='previous' and state['errors']


def test_unsafe_path_is_rejected_before_reading_other_namespace():
    doc,files=source();bad=json.loads(files['manual_eod/latest.json']);bad['snapshot']['file']='../../account.json'
    files={'manual_eod/latest.json':canonical(bad)};paths=[]
    def read(path):paths.append(path);return files.get(path)
    actual,state=load_published_eod('synthetic',read)
    assert actual=={} and state['status']=='unavailable'
    assert paths==['manual_eod/latest.json','manual_eod/previous.json']


@pytest.mark.parametrize('core',[None,{'run_id':'new-run'},b'invalid json'])
def test_missing_newer_or_unreadable_core_retains_verified_prices_as_pending(core):
    doc,files=source();files['latest/manifest.json']=canonical(core) if isinstance(core,dict) else core
    actual,state=load_published_eod('synthetic',files.get)
    assert actual==doc and state['status']=='pending'


def test_same_run_id_changed_manifest_does_not_claim_coherent_snapshot():
    _,files=source();core=json.loads(files['latest/manifest.json']);core['changed']=True;files['latest/manifest.json']=canonical(core)
    actual,state=load_published_eod('synthetic',files.get)
    assert actual=={} and state['status']=='unavailable'


@pytest.mark.parametrize('case',['bad_ohlc','last_date','future_close','fake_current','nan','duplicate_date','missing_provenance','bad_asset'])
def test_contract_rejects_price_and_lineage_corruption(case):
    doc,_=source();doc=deepcopy(doc);item=next(iter(doc['instruments'].values()))
    if case=='bad_ohlc':item['bars']['h'][-1]=1
    elif case=='last_date':item['last_final_session']='2026-11-10'
    elif case=='future_close':item['last_final_close_at']='2026-11-12T21:00:00+00:00'
    elif case=='fake_current':item['expected_session']='2026-11-12'
    elif case=='nan':item['bars']['c'][-1]=float('nan')
    elif case=='duplicate_date':item['bars']['d'][-2]=item['bars']['d'][-1]
    elif case=='missing_provenance':item['identity_evidence']=None
    else:item['asset_type']='yield'
    with pytest.raises(ValueError):validate_snapshot(doc)


def test_gzip_and_descriptor_integrity_are_both_required():
    doc,_=source();raw=packed(doc);ptr=descriptor(doc,raw)
    with pytest.raises(ValueError):load_snapshot(ptr,raw+b'x')
    ptr['run_id']='other'
    with pytest.raises(ValueError):load_snapshot(ptr,raw)


def test_expanded_size_and_duplicate_json_keys_are_rejected(monkeypatch):
    import manual_eod_contract as contract
    doc,_=source();raw=packed(doc);ptr=descriptor(doc,raw)
    monkeypatch.setattr(contract,'MAX_JSON',256)
    with pytest.raises(ValueError,match='expanded'):load_snapshot(ptr,raw)
    for invalid in (b'{"schema":1,"schema":2}',b'{"price":NaN}'):
        with pytest.raises(ValueError):contract.json_doc(invalid)


def test_publication_chronology_cannot_move_before_price_cutoff():
    doc,_=source();doc['generated_at']='2026-11-11T22:59:00+00:00'
    with pytest.raises(ValueError,match='chronology'):validate_snapshot(doc)

