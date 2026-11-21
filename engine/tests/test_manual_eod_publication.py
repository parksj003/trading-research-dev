from copy import deepcopy
from datetime import datetime,timezone
import base64,json
from pathlib import Path

import pytest
import yaml

from watchlist.manual_eod_publish import resolve_publication,fetch_catalog,publish_snapshot,read_previous,verify_current_core,_merge
from watchlist.manual_eod_contract import canonical
from watchlist.manual_catalog import csv_bytes
from test_manual_eod_pipeline import row,core,cutoff,Provider,build


class Response:
    status_code=200
    def __init__(self,data):self.data=data
    def json(self):return self.data


class Github:
    def __init__(self,commits=None):self.calls=[];self.commits=commits if commits is not None else [{'sha':'a'*40}]
    def get(self,url,**kw):
        self.calls.append((url,kw))
        if url.endswith('/commits'):return Response(self.commits)
        return Response({'content':base64.b64encode(canonical(core())).decode()})


def event():
    return dict(repository=dict(full_name='example-owner/trading-journal-data'),workflow_run=dict(id=123,status='completed',conclusion='success',
        head_branch='main',path='.github/workflows/watchlist.yml',created_at='2026-11-12T10:00:00Z',updated_at='2026-11-12T10:09:00Z'))


def test_binds_exactly_one_pinned_publication_to_successful_core_window():
    g=Github();doc,sha,run,end=resolve_publication(g,event(),'synthetic-no-real-token',now=cutoff())
    assert doc==core() and sha=='a'*40 and run=='123' and end==cutoff()
    assert g.calls[1][1]['params']=={'ref':'a'*40}
    assert all(k['allow_redirects'] is False for _,k in g.calls)


@pytest.mark.parametrize('commits',[[],[{'sha':'a'*40},{'sha':'b'*40}]])
def test_ambiguous_or_missing_core_publication_fails_closed(commits):
    with pytest.raises(ValueError,match='AMBIGUOUS'):resolve_publication(Github(commits),event(),'test',now=cutoff())


@pytest.mark.parametrize('field,value',[('conclusion','failure'),('head_branch','untrusted'),('path','.github/workflows/other.yml'),('status','in_progress')])
def test_failed_or_untrusted_runs_never_collect_or_publish(field,value):
    e=event();e['workflow_run'][field]=value;g=Github()
    with pytest.raises(ValueError,match='UNTRUSTED'):resolve_publication(g,e,'test')
    assert not g.calls


def test_catalog_permission_is_explicit_read_only_and_fixed_origin():
    class Catalog:
        def __init__(self):self.calls=[]
        def get(self,url,**kw):
            self.calls.append((url,kw))
            return Response(dict(type='file',encoding='base64',content=base64.b64encode(csv_bytes([row()])).decode(),sha='b'*40))
    g=Catalog()
    with pytest.raises(ValueError,match='NOT_CONFIGURED'):fetch_catalog(g,'')
    assert not g.calls
    raw,rows,sha=fetch_catalog(g,'synthetic-only')
    assert len(rows)==1 and sha=='b'*40 and raw==csv_bytes([row()])
    assert g.calls[0][0]=='https://api.github.com/repos/example-owner/trend-leader-watchlist/contents/watchlists/manual_watchlist.csv'
    assert g.calls[0][1]['params']=={'ref':'main'}


def test_corrupt_latest_falls_back_and_next_publish_keeps_good_previous(tmp_path):
    base=tmp_path/'published';(base/'latest').mkdir(parents=True);(base/'latest/manifest.json').write_bytes(canonical(core()))
    first=build([row()],Provider());publish_snapshot(base,first)
    second=deepcopy(first);second['run_id']+='-retry';second['generated_at']='2026-11-12T10:10:00Z'
    publish_snapshot(base,second)
    (base/'manual_eod/latest.json').write_bytes(b'broken')
    assert read_previous(base)==first
    third=deepcopy(second);third['run_id']+='-next';publish_snapshot(base,third)
    previous=json.loads((base/'manual_eod/previous.json').read_bytes())
    assert previous['run_id']==first['run_id'] and read_previous(base)==third
    assert len(list((base/'manual_eod/objects').glob('*')))==2


def test_new_core_blocks_old_publication_and_never_touches_core_files(tmp_path):
    base=tmp_path/'published';(base/'latest').mkdir(parents=True);raw=canonical({**core(),'run_id':'new'});(base/'latest/manifest.json').write_bytes(raw)
    with pytest.raises(ValueError,match='CORE_MOVED'):publish_snapshot(base,build([row()],Provider()))
    with pytest.raises(ValueError,match='CORE_MOVED'):verify_current_core(base,core()['run_id'])
    assert (base/'latest/manifest.json').read_bytes()==raw and not (base/'manual_eod').exists()


def test_revision_or_no_overlap_requires_full_history_instead_of_mixing_bases():
    old=build([row()],Provider())['instruments']['KRX:005930']
    assert _merge(old,[dict(d='2026-11-12',o=50,h=55,l=48,c=52,v=None)]) is None
    assert _merge(old,[dict(d='2026-11-13',o=100,h=105,l=98,c=104,v=None)]) is None
    new=[dict(d='2026-11-12',o=100,h=105,l=98,c=104,v=100),dict(d='2026-11-13',o=100,h=105,l=98,c=104,v=None)]
    assert len(_merge(old,new))==2


def test_optional_workflow_is_isolated_without_core_schedule_or_token_expansion():
    root=Path(__file__).resolve().parents[1]
    doc=yaml.safe_load((root/'tests/fixtures/workflows/manual_watchlist_eod.yml').read_text());trigger=doc.get('on',doc.get(True))
    assert set(trigger)=={'workflow_run'} and trigger['workflow_run']['workflows']==['watchlist']
    assert doc['concurrency']['group']!='watchlist-data'
    steps=doc['jobs']['publish']['steps'];restore=next(s for s in steps if s.get('uses')=='actions/cache/restore@v4')
    assert restore['id']=='core_cache'
    assert restore['with']['key']=='wl-cache-${{ github.event.workflow_run.id }}' and 'restore-keys' not in restore['with']
    assert not any('secrets.KRX_' in str(s) for s in steps)
    scripts='\n'.join(s.get('run','') for s in steps)
    assert 'git add -- watchlist/published/manual_eod' in scripts and '--verify-core' in scripts
    assert "if [[ \"$CORE_CACHE_HIT\" == 'true' ]]" in scripts
    build_step=next(s for s in steps if s.get('id')=='snapshot')
    assert build_step['env']['CORE_CACHE_HIT']=='${{ steps.core_cache.outputs.cache-hit }}'

