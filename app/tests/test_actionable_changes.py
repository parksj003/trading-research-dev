import copy
import hashlib
import json

import pandas as pd
import pytest
import requests
from streamlit.testing.v1 import AppTest

from actionable_changes import (verified_snapshot, build_changes, compare_track, export_changes, calendar, TRACKS)
from actionable_source import load_public_snapshots, MAX_REQUESTS


def snap(date='2026-11-05', rid='now', tickers=('A','B'), *, market='US',track='NASDAQ100',generated=None, config='fixed'):
    rows=[dict(market=market,sub_market=track,ticker=t,name=t,sector='Tech',as_of=date,is_actionable=True) for t in tickers]
    frame=pd.DataFrame(rows,columns=['market','sub_market','ticker','name','sector','as_of','is_actionable'])
    raw=frame.to_csv(index=False).encode()
    files={'watchlist.csv':raw,'candidates.csv':raw,'summary.json':json.dumps({'regimes':{k:{'as_of':date} for k in TRACKS}}).encode()}
    m={'schema_version':'1.0','run_id':rid,'generated_at':generated or date+'T23:30:00Z',
       'as_of':{k:date for k in TRACKS},'config_hash':config,'engine_version':'v1',
       'files':{n:{'sha256':hashlib.sha256(v).hexdigest()} for n,v in files.items()}}
    sha=hashlib.sha1(rid.encode()).hexdigest()
    return verified_snapshot(json.dumps(m).encode(),files,sha),files


def track(report,key='NASDAQ100'):return next(t for t in report['tracks'] if t['track']==key)


def test_counts_events_reasons_and_no_mutation():
    old,_=snap('2026-11-04','old',('A','B','C'));cur,_=snap(tickers=('B','D'))
    cur.candidates.append(dict(market='US',sub_market='NASDAQ100',ticker='A',name='A',sector='Tech',as_of='2026-11-05',is_actionable=False,exit_signal='경고'))
    before=copy.deepcopy(cur)
    r=track(build_changes(cur,[cur,old]));c=r['counts']
    assert (c['previous'],c['current'],c['net'],c['retained'],c['new'],c['excluded'])==(3,2,-1,1,1,2)
    assert c['retention_pct']==pytest.approx(100/3)
    reasons={e['ticker']:e['reason'] for e in r['events']}
    assert '구체적 제외 사유 확인 불가' in reasons['A']
    assert '미관측' in reasons['C']
    assert cur==before


def test_zero_previous_is_not_zero_retention_or_change_percent():
    old,_=snap('2026-11-04','old',());cur,_=snap()
    c=track(build_changes(cur,[old,cur]))['counts']
    assert c['previous']==0 and c['new']==2
    assert c['change_pct'] is None and c['retention_pct'] is None


def test_same_date_publication_and_previous_session_are_distinct():
    earlier,_=snap(rid='earlier',tickers=('A',),generated='2026-11-05T22:00:00Z');cur,_=snap()
    r=track(build_changes(cur,[cur,earlier],'publication'))
    assert r['counts']['net']==1 and '동일 가격 기준일' in r['reason']
    assert track(build_changes(cur,[cur,earlier]))['status']=='unavailable'
    assert compare_track(cur,cur,'NASDAQ100','publication')['status']=='unavailable'


@pytest.mark.parametrize('day_old,day_now',[('2026-11-06','2026-11-09'),('2026-11-25','2026-11-27')])
def test_weekends_and_us_holiday(day_old,day_now):
    a,_=snap(day_old,'old');b,_=snap(day_now,'new')
    assert track(build_changes(b,[a,b]))['status']=='available'


@pytest.mark.parametrize('date,preclose,postclose',[('2026-03-09','2026-03-09T19:59:00Z','2026-03-09T20:01:00Z'),('2026-11-27','2026-11-27T17:59:00Z','2026-11-27T18:01:00Z')])
def test_dst_and_early_close(date,preclose,postclose):
    old_day=calendar('US').previous_session(date).strftime('%Y-%m-%d')
    a,_=snap(old_day,'old');b,_=snap(date,generated=preclose)
    assert track(build_changes(b,[a,b]))['status']=='unavailable'
    b.manifest['generated_at']=postclose
    assert track(build_changes(b,[a,b]))['status']=='available'


def test_kr_leading_zero_and_previous_holiday():
    # Korea Chuseok calendar lookup rather than assuming a weekday.
    cur,_=snap(market='KR',track='KOSPI',tickers=('5930',))
    previous=calendar('KR').previous_session('2026-11-05').strftime('%Y-%m-%d')
    old,_=snap(previous,'old',('5930',),market='KR',track='KOSPI')
    r=track(build_changes(cur,[old,cur]),'KOSPI')
    assert r['events'][0]['ticker']=='005930' and r['counts']['retained']==1


@pytest.mark.parametrize('kind',['config','engine','stale','missing_date','unknown_date','preclose'])
def test_incomparable_paths_do_not_become_zero(kind):
    a,_=snap('2026-11-04','old');b,_=snap()
    if kind=='config':b.manifest['config_hash']='changed'
    if kind=='engine':b.manifest['engine_version']='changed'
    if kind=='stale':b.watch[0]['as_of']='2026-11-04'
    if kind=='missing_date':b.manifest['as_of'].pop('NASDAQ100')
    if kind=='unknown_date':b.manifest['as_of']['NASDAQ100']='bad'
    if kind=='preclose':b.manifest['generated_at']='2026-11-05T18:00:00Z'
    r=track(build_changes(b,[a,b]));assert r['status']=='unavailable' and r['counts'] is None


def test_skipped_previous_session_is_not_substituted():
    a,_=snap('2026-11-03','old');b,_=snap()
    assert track(build_changes(b,[a,b]))['counts'] is None
    assert track(build_changes(b,[a,b],'publication'))['counts'] is not None


def test_market_identity_deduplicates_a_track_move():
    a,_=snap('2026-11-04','old',('A',));b,_=snap(tickers=(),track='NASDAQ100')
    b.watch=copy.deepcopy(a.watch);b.watch[0].update(sub_market='SP500',as_of='2026-11-05')
    b.candidates=copy.deepcopy(b.watch)
    r=build_changes(b,[a,b]);us=next(m for m in r['markets'] if m['market']=='US')
    assert us['counts']['retained']==1 and us['counts']['new']==us['counts']['excluded']==0
    assert track(r)['counts']['excluded']==1 and track(r,'SP500')['counts']['new']==1


@pytest.mark.parametrize('n',[5,20])
def test_trends_require_every_exchange_session_and_matching_version(n):
    dates=calendar('US').sessions_window('2026-11-05',-n).strftime('%Y-%m-%d')
    snapshots=[snap(d,str(i))[0] for i,d in enumerate(dates)]
    r=build_changes(snapshots[-1],snapshots)
    assert len(r['trends']['NASDAQ100'][str(n)]['points'])==n
    missing=snapshots[:1]+snapshots[2:]
    assert build_changes(snapshots[-1],missing)['trends']['NASDAQ100'][str(n)]['status']=='unavailable'
    snapshots[0].manifest['config_hash']='other'
    assert build_changes(snapshots[-1],snapshots)['trends']['NASDAQ100'][str(n)]['status']=='unavailable'


def test_export_is_allowlisted_market_scoped_and_bound_to_manifest():
    a,_=snap('2026-11-04','old');b,_=snap();r=build_changes(b,[a,b])
    r['private']='secret';track(r)['events'][0]['quantity']='private_quantity'
    us=export_changes(r,b.manifest,('US',))
    assert all(t['market']=='US' for t in us['tracks'])
    assert 'private' not in json.dumps(us)
    assert export_changes(r,b.manifest,('KR',))['trends'].keys()=={'KOSPI','KOSDAQ'}
    altered=copy.deepcopy(b.manifest);altered['files']['watchlist.csv']['sha256']='0'*64
    assert export_changes(r,altered,('US',))['status']=='unavailable'


@pytest.mark.parametrize('kind',['hash','missing_hash','duplicate','bad_identity','missing_column'])
def test_snapshot_verification(kind):
    b,files=snap();m=copy.deepcopy(b.manifest)
    if kind=='hash':files['watchlist.csv']+=b'bad'
    elif kind=='missing_hash':m['files']['watchlist.csv'].pop('sha256')
    else:
        df=pd.read_csv(__import__('io').BytesIO(files['watchlist.csv']),dtype=str)
        if kind=='duplicate':df=pd.concat([df,df.iloc[:1]])
        elif kind=='bad_identity':df['market']='KR'
        else:df=df.drop(columns='ticker')
        files['watchlist.csv']=df.to_csv(index=False).encode()
        m['files']['watchlist.csv']['sha256']=hashlib.sha256(files['watchlist.csv']).hexdigest()
    with pytest.raises(ValueError):verified_snapshot(json.dumps(m).encode(),files,b.commit)


class Response:
    def __init__(self,data):self.data=data
    def raise_for_status(self):pass
    def iter_content(self,chunk_size):yield self.data
    def close(self):pass


def test_loader_uses_only_hashed_public_files_at_fixed_commits():
    a,fa=snap('2026-11-04','old');b,fb=snap();snaps={s.commit:(s,f) for s,f in [(a,fa),(b,fb)]};calls=[]
    def get(url,params,**kwargs):
        calls.append((url,params,kwargs))
        if url.endswith('/commits'):return Response(json.dumps([{'sha':b.commit},{'sha':a.commit}]).encode())
        s,files=snaps[params['ref']];name=url.split('/')[-1]
        assert name in ('manifest.json','summary.json','watchlist.csv','candidates.csv')
        return Response(json.dumps(s.manifest).encode() if name=='manifest.json' else files[name])
    src={'mode':'github','repo':'owner/data','root':'watchlist/published','branch':'main','token':'test-secret'}
    result,warnings=load_public_snapshots(src,'now',get)
    assert not warnings and len(result)==2 and len(calls)<=MAX_REQUESTS
    assert all(c[1].get('ref') in snaps for c in calls[1:])
    assert not load_public_snapshots(src|{'root':'private'},'now',get)[0]
    def fail(*a,**k):raise requests.ConnectionError('secret-url-token')
    r,w=load_public_snapshots(src,'now',fail)
    assert not r and 'secret' not in str(w)


def test_ui_handles_unavailable_without_network_and_displays_exact_computation():
    script='''
import app
import actionable_view as v
from test_actionable_changes import snap
old,_=snap('2026-11-04','old',('A',));cur,_=snap()
v.cached_snapshots=lambda *args:([cur,old],[])
b=app.Bundle(man=cur.manifest,summ={})
v.render_actionable_changes(b,{'mode':'github','repo':'owner/data','root':'watchlist/published'},app.stock_dataframe)
'''
    at=AppTest.from_string(script,default_timeout=60).run()
    assert not at.exception and any('비교 검증 대기' in i.value for i in at.info)
    at.button(key='actionable_load').click().run()
    assert not at.exception
    assert any(m.value=='1' for m in at.metric)
    at.radio(key='actionable_compare_mode').set_value('publication').run()
    assert not at.exception


def test_population_count_changes_are_explicit_and_missing_track_is_unknown():
    a,_=snap('2026-11-04','old');b,_=snap()
    a.summary['health']={'us':{'us_universe':100}}
    b.summary['health']={'us':{'us_universe':99}}
    r=track(build_changes(b,[a,b]))
    assert r['population']['status']=='changed' and '모집단' in r['reason']
    b.summary['regimes'].pop('NASDAQ100')
    assert track(build_changes(b,[a,b]))['counts'] is None


def test_missing_and_stale_candidate_confirmation_blocks_valid_count():
    a,_=snap('2026-11-04','old');b,_=snap()
    b.candidates[0]['is_stale_price']='True'
    assert track(build_changes(b,[a,b]))['status']=='unavailable'
    b.candidates=[]
    assert track(build_changes(b,[a,b]))['status']=='unavailable'


def test_empty_result_and_loading_failures_remain_nonfatal_in_ui():
    at=AppTest.from_string('''
import app
import actionable_view as v
from test_actionable_changes import snap
cur,_=snap()
v.cached_snapshots=lambda *args:([],['공급 자료 미확보'])
v.render_actionable_changes(app.Bundle(man=cur.manifest,summ={}),{'mode':'github','repo':'owner/data','root':'published'},app.stock_dataframe)
''',default_timeout=60).run()
    at.button(key='actionable_load').click().run()
    assert not at.exception and any('미확보' in w.value for w in at.warning)


def test_download_uses_same_comparison_and_rejects_wrong_manifest():
    from gpt_export import build_analysis_export
    from test_gpt_export import inputs,payload
    a,_=snap('2026-11-04','old');b,_=snap();r=build_changes(b,[a,b])
    args=list(inputs());args[0]=b.manifest
    d=payload(build_analysis_export(*args,mode='morning',market='US',change_context=r))
    assert d['actionable_changes']==export_changes(r,b.manifest,('US',))
    r['current_source']['run_id']='wrong'
    assert payload(build_analysis_export(*args,change_context=r))['actionable_changes']['status']=='unavailable'


def test_reload_preserves_the_selected_comparison_widget():
    at=AppTest.from_string('''
import app
import actionable_view as v
from test_actionable_changes import snap
old,_=snap('2026-11-04','old');cur,_=snap()
v.cached_snapshots=lambda *args:([cur,old],[])
v.render_actionable_changes(app.Bundle(man=cur.manifest,summ={}),{'mode':'github','repo':'owner/data','root':'published'},app.stock_dataframe)
''',default_timeout=60).run()
    at.radio(key='actionable_compare_mode').set_value('publication').run()
    at.button(key='actionable_load').click().run()
    at.selectbox(key='actionable_trend_period').set_value(20).run()
    assert not at.exception and at.radio(key='actionable_compare_mode').value=='publication'


def test_invalid_public_metadata_stops_safely():
    b,files=snap();m=copy.deepcopy(b.manifest);m['files']=None
    with pytest.raises(ValueError):verified_snapshot(json.dumps(m).encode(),files,b.commit)
    b.summary['regimes']=[]
    assert compare_track(b,b,'NASDAQ100','publication')['counts'] is None


@pytest.mark.parametrize('observation',[{'session_unconfirmed':True},{'lagging_actionable_rows':1},{'benchmark_as_of':'2026-11-04'}])
def test_source_price_observation_warnings_are_not_promoted(observation):
    a,_=snap('2026-11-04','old');b,_=snap()
    b.summary['price_observation']={'tracks':{'NASDAQ100':observation}}
    r=track(build_changes(b,[a,b]))
    assert r['status']=='unavailable' and r['counts'] is None


def test_loader_budget_and_large_response_stop_without_unbounded_reads():
    b,files=snap();calls=[]
    def get(url,params,**kwargs):
        calls.append(url)
        if url.endswith('/commits'):
            return Response(json.dumps([{'sha':f'{i:040x}'} for i in range(88)]).encode())
        name=url.rsplit('/',1)[-1]
        if name=='manifest.json':return Response(json.dumps(b.manifest).encode())
        return Response(files[name])
    src={'mode':'github','repo':'o/r','root':'published'}
    r,w=load_public_snapshots(src,'now',get,snapshot_limit=44)
    assert len(calls)<=MAX_REQUESTS and len(r)==1
    def huge(*args,**kwargs):return Response(b'x'*300001)
    r,w=load_public_snapshots(src,'now',huge)
    assert not r and w


def test_non_actionable_current_reason_is_not_inferred_for_missing_rows():
    a,_=snap('2026-11-04','old',('A','B'));b,_=snap(tickers=())
    b.candidates=[dict(a.watch[0],as_of='2026-11-05',is_actionable=False,exclusion_reason='원천 사유: 바이존 밖')]
    events=track(build_changes(b,[a,b]))['events']
    assert events[0]['reason']=='원천 사유: 바이존 밖'
    assert '확인 불가' in events[1]['reason']


def test_aggregate_network_bytes_are_bounded(monkeypatch):
    import actionable_source as module
    monkeypatch.setattr(module,'MAX_TOTAL_BYTES',10)
    calls=[]
    def get(*a,**k):calls.append(1);return Response(b' '*11)
    data,warnings=module.load_public_snapshots({'mode':'github','repo':'o/r','root':'published'},'now',get)
    assert not data and warnings and len(calls)==1


def test_expanded_commit_listing_accepts_a_realistic_88_commit_payload():
    b,files=snap();calls=[]
    listing=json.dumps([{'sha':f'{i:040x}','commit':{'message':'x'*5000}} for i in range(88)]).encode()
    assert len(listing)>300_000
    def get(url,params,**kwargs):
        calls.append(url)
        if url.endswith('/commits'):return Response(listing)
        name=url.rsplit('/',1)[-1]
        return Response(json.dumps(b.manifest).encode() if name=='manifest.json' else files[name])
    result,warnings=load_public_snapshots({'mode':'github','repo':'o/r','root':'published'},'now',get,snapshot_limit=44)
    assert len(result)==1 and not warnings and len(calls)<=MAX_REQUESTS


def test_invalid_current_snapshot_does_not_fall_back_to_an_older_same_date_trend():
    dates=calendar('US').sessions_window('2026-11-05',-5).strftime('%Y-%m-%d')
    snapshots=[snap(d,str(i))[0] for i,d in enumerate(dates)]
    current,_=snap(rid='invalid-current')
    current.watch[0]['as_of']='2026-11-04'
    report=build_changes(current,snapshots+[current])
    assert report['trends']['NASDAQ100']['5']['status']=='unavailable'
    assert report['trends']['NASDAQ100']['5']['points']==[]

