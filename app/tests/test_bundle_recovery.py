import hashlib
import json

import pytest
from streamlit.testing.v1 import AppTest

import app


def fixture(tmp_path):
    files={'latest/summary.json': b'{"regimes":{}}',
           'latest/watchlist.csv': b'market,ticker\nUS,EX\n',
           'latest/candidates.csv': b'market,ticker\nUS,EX\n',
           'latest/sectors.csv': b'market,sector\nUS,Example\n'}
    for rel,raw in files.items():
        path=tmp_path/rel;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    manifest={'run_id':'same-run','files':{rel.split('/')[-1]:{'sha256':hashlib.sha256(raw).hexdigest()} for rel,raw in files.items()}}
    return files,manifest


@pytest.mark.parametrize('fault',['hash','missing','csv','summary'])
def test_failed_result_and_bad_raw_bytes_do_not_poison_same_run_cache(tmp_path,fault):
    files,man=fixture(tmp_path)
    rel='latest/summary.json' if fault=='summary' else 'latest/candidates.csv'
    path=tmp_path/rel
    if fault=='missing':path.unlink()
    else:
        bad=b'[]' if fault=='summary' else b'"unclosed' if fault=='csv' else b'wrong bytes'
        path.write_bytes(bad)
        if fault in ('csv','summary'):
            man['files'][rel.split('/')[-1]]['sha256']=hashlib.sha256(bad).hexdigest()
    key='recovery-'+str(tmp_path)
    src={'mode':'local','dir':str(tmp_path)}
    app.fetch_file.clear();app.load_bundle_cached.clear()
    first,_=app.load_bundle_cached(key,'same-run',src,man)
    assert first.core_errors and not first.export_snapshot_verified
    path.write_bytes(files[rel])
    man['files'][rel.split('/')[-1]]['sha256']=hashlib.sha256(files[rel]).hexdigest()
    recovered,errors=app.load_bundle_cached(key,'same-run',src,man)
    assert not recovered.core_errors and not errors
    assert recovered.export_snapshot_verified and len(recovered.cand)==1
    app.fetch_file.clear();app.load_bundle_cached.clear()


def test_optional_transport_error_degrades_only_history_and_retries_on_next_call(tmp_path,monkeypatch):
    files,man=fixture(tmp_path);failed=True;calls=[]
    def fetch(key,run,rel,src):
        calls.append(rel)
        if rel=='history/sector_power.csv':
            if failed:raise RuntimeError('synthetic 503')
            return b'date,market,sector\n2026-11-09,US,Example\n'
        return files.get(rel)
    monkeypatch.setattr(app,'fetch_file',fetch)
    app.load_bundle_cached.clear()
    first,_=app.load_bundle_cached('optional-'+str(tmp_path),'same-run',{},man)
    assert first.load_errors and not first.core_errors and len(first.cand)==1
    assert first.h_sector.empty and first.export_snapshot_verified
    failed=False
    recovered,_=app.load_bundle_cached('optional-'+str(tmp_path),'same-run',{},man)
    assert not recovered.load_errors and len(recovered.h_sector)==1
    assert calls.count('history/sector_power.csv')==2
    app.load_bundle_cached.clear()


def test_core_failure_stops_analysis_without_uncaught_exception():
    at=AppTest.from_string('''
import app,streamlit as st
original=app.load_bundle_cached
app.load_bundle_cached=lambda *a:(app.Bundle(man={},summ={},core_errors=['latest/candidates.csv']),[])
try:
 app.load_bundle('fixture',{}, {'run_id':'fixture'})
 st.write('analysis continued')
finally:
 app.load_bundle_cached=original
''').run()
    assert not at.exception
    assert any('분석을 보류' in x.value for x in at.error)
    assert not any(x.value=='analysis continued' for x in at.markdown)

