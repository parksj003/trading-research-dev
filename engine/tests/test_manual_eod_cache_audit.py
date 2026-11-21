from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from watchlist.manual_eod_audit import audit_population, CLASSIFICATIONS
from watchlist.manual_eod_cache import inspect_core_cache, attest_identity, valid_identity, BASES
from watchlist.manual_eod_provider import specification, YahooDaily, Unavailable
from test_manual_eod_pipeline import row, core
from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_publish import build_snapshot


def cutoff():return datetime(2026,11,13,0,47,tzinfo=timezone.utc)


def build(rows,provider,previous=None,**kwargs):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),core=core(),core_commit='a'*40,
        workflow_run_id='123',cutoff=cutoff(),provider=provider,previous=previous,clock=cutoff,**kwargs)


def us(**kw):
    return row(**dict(dict(symbol='NYSE:IBM',unit='USD',watch_region='미국'),**kw))


def identity(spec):
    return dict(provider_symbol=spec['candidates'][0], provider_exchange=spec['provider_exchanges'][0],
        currency=spec['currency'], instrument_type='ETF' if spec['asset_type']=='etf' else 'EQUITY',
        timezone=spec['provider_timezones'][0])


def cache(root, item, dates=('2026-11-12',)):
    spec=specification(item);market=spec['market'];ticker=item['symbol'].split(':')[1].replace('.','-')
    path=root/('kr_history_cache' if market=='KR' else 'us_history_cache')/(ticker+'.pkl')
    path.parent.mkdir(parents=True,exist_ok=True)
    frame=pd.DataFrame([dict(Open=100.,High=105.,Low=98.,Close=104.,Volume=1000.,
        _raw_ohlc_valid=True,_session_confirmed=True,_session_finality_version=3,
        _base_price_basis=BASES[market]) for _ in dates],index=pd.to_datetime(list(dates)))
    frame.to_pickle(path)
    return frame,path


class Provider:
    def __init__(self, fail=False):self.calls=0;self.operations=[];self.fail=fail
    def fetch(self,spec,instant,**kwargs):
        self.calls+=1;self.operations.append((spec['symbol'],kwargs))
        if self.fail:raise Unavailable('PROVIDER_RATE_LIMITED')
        # Deliberately different dividend-unadjusted OHLC: never compare this to
        # the yfinance auto-adjusted cache or merge it into adjusted history.
        return [dict(d='2026-11-12',o=200,h=210,l=196,c=208,v=None)],identity(spec),0


@pytest.mark.parametrize('item',[row(),us(),us(asset_type='etf'),row(asset_type='etf')])
def test_verified_current_cache_needs_zero_provider_requests_and_is_read_only(tmp_path,item):
    _,path=cache(tmp_path,item);before=path.read_bytes();spec=specification(item)
    previous=build([item],Provider())
    provider=Provider(fail=True)
    doc=build([item,row(**{**item,'watch_sector':'다른 분류'})],provider,previous=previous,data_dir=tmp_path,force=True)
    entry=doc['instruments'][item['symbol']]
    assert provider.calls==0 and entry['bars']['c']==[104.] and entry['adjustment']==BASES[spec['market']]
    assert entry['acquisition']=='core_cache' and doc['collection_metrics']['cache_reused']==1
    assert path.read_bytes()==before


@pytest.mark.parametrize('item',[us(),us(asset_type='etf'),row(asset_type='etf')])
def test_unidentified_cache_bootstraps_metadata_once_without_price_revalidation(tmp_path,item):
    cache(tmp_path,item);p=Provider();doc=build([item],p,data_dir=tmp_path)
    e=doc['instruments'][item['symbol']]
    assert p.operations==[(item['symbol'],{'metadata_only':True})]
    assert e['bars']['c']==[104.] and e['status']=='current'
    assert valid_identity(specification(item),e['listing_identity'],cutoff())
    assert e['cache_diagnostic']['classification']=='identity_pending'


@pytest.mark.parametrize('item',[row(),us()])
@pytest.mark.parametrize('column,value,classification',[
    ('_raw_ohlc_valid',False,'other'),('_session_confirmed',False,'other'),
    ('_session_finality_version',0,'other'),('_base_price_basis','legacy','basis_mismatch'),
    ('Open',-1,'other'),('Close',120,'other'),('High',float('nan'),'other'),
    ('Low',float('inf'),'other'),('Volume',-1,'other')])
def test_invalid_cache_fields_never_get_promoted(tmp_path,item,column,value,classification):
    frame,path=cache(tmp_path,item);frame[column]=value;frame.to_pickle(path)
    result=inspect_core_cache(tmp_path,specification(item),cutoff())
    assert result.rows is None and result.classification==classification


@pytest.mark.parametrize('item',[row(),us()])
@pytest.mark.parametrize('column',['Open','_raw_ohlc_valid','_session_confirmed','_session_finality_version','_base_price_basis'])
def test_missing_evidence_is_not_inferred_from_filename_or_mtime(tmp_path,item,column):
    frame,path=cache(tmp_path,item);frame.drop(columns=column).to_pickle(path)
    result=inspect_core_cache(tmp_path,specification(item),cutoff())
    assert result.reason=='CACHE_REQUIRED_FIELDS_MISSING' and result.rows is None


@pytest.mark.parametrize('item',[row(),us()])
def test_absent_corrupt_stale_duplicate_and_unfinished_cache_are_distinct(tmp_path,item):
    spec=specification(item)
    assert inspect_core_cache(None,spec,cutoff()).classification=='not_inspected'
    assert inspect_core_cache(tmp_path,spec,cutoff()).classification=='no_cache'
    frame,path=cache(tmp_path,item,('2026-11-11',))
    result=inspect_core_cache(tmp_path,spec,cutoff());assert result.classification=='stale' and result.rows
    path.write_bytes(b'corrupt');assert inspect_core_cache(tmp_path,spec,cutoff()).classification=='other'
    pd.concat([frame,frame]).to_pickle(path);assert inspect_core_cache(tmp_path,spec,cutoff()).rows is None
    frame,path=cache(tmp_path,item,('2026-11-12','2026-11-13'))
    frame.loc[pd.Timestamp('2026-11-13'),'_session_confirmed']=False;frame.to_pickle(path)
    result=inspect_core_cache(tmp_path,spec,cutoff())
    assert result.excluded_unfinished==1 and [r['d'] for r in result.rows]==['2026-11-12']


@pytest.mark.parametrize('field,value',[('provider_symbol','WRONG'),('provider_exchange','NMS'),
    ('currency','KRW'),('instrument_type','ETF'),('timezone','Asia/Seoul'),('evidence','untrusted')])
def test_listing_metadata_is_bound_to_exact_venue_type_currency_and_symbol(field,value):
    spec=specification(us());e=attest_identity(spec,identity(spec),cutoff());e[field]=value
    assert valid_identity(spec,e,cutoff()) is None


def test_listing_evidence_expiry_and_future_timestamp_require_verification():
    spec=specification(us());e=attest_identity(spec,identity(spec),cutoff())
    assert valid_identity(spec,e,cutoff()+timedelta(days=7))
    assert valid_identity(spec,e,cutoff()+timedelta(days=7,seconds=1)) is None
    assert valid_identity(spec,e,cutoff()-timedelta(seconds=1)) is None


def test_mixed_adjustment_history_and_wrong_etf_identity_cannot_be_reused(tmp_path):
    item=us(asset_type='etf');frame,path=cache(tmp_path,item,('2026-11-11','2026-11-12'))
    frame.loc[frame.index[0],'_base_price_basis']='yahoo:raw';frame.to_pickle(path)
    assert inspect_core_cache(tmp_path,specification(item),cutoff()).classification=='basis_mismatch'
    cache(tmp_path,item);spec=specification(us());e=attest_identity(spec,identity(spec),cutoff())
    assert inspect_core_cache(tmp_path,specification(item),cutoff(),evidence=e).classification=='identity_pending'


def test_missing_sessions_are_not_filled_and_split_adjusted_prices_are_preserved(tmp_path):
    frame,path=cache(tmp_path,row(),('2026-11-09','2026-11-12'))
    frame.iloc[0,frame.columns.get_indexer(['Open','High','Low','Close'])]=[50,52.5,49,52]
    frame.to_pickle(path);doc=build([row()],Provider(fail=True),data_dir=tmp_path)
    entry=doc['instruments'][row()['symbol']]
    assert entry['bars']['d']==['2026-11-09','2026-11-12'] and entry['bars']['c']==[52.,104.]
    assert entry['missing_sessions']==2


@pytest.mark.parametrize('instant,version,reusable',[
    ('2026-11-19T07:20:00+00:00',3,False),('2026-11-19T08:00:00+00:00',1,False),
    ('2026-11-19T08:00:00+00:00',2,True)])
def test_kr_special_close_is_independently_checked(tmp_path,instant,version,reusable):
    frame,path=cache(tmp_path,row(),('2026-11-19',));frame['_session_finality_version']=version;frame.to_pickle(path)
    result=inspect_core_cache(tmp_path,specification(row()),datetime.fromisoformat(instant))
    assert (result.classification=='reusable')==reusable


@pytest.mark.parametrize('instant,day',[
    ('2026-11-27T18:19:00+00:00','2026-11-25'),('2026-11-27T18:20:00+00:00','2026-11-27'),
    ('2026-07-06T20:19:00+00:00','2026-07-02'),('2026-07-06T20:20:00+00:00','2026-07-06')])
def test_us_dst_holiday_and_early_close_cutoffs(tmp_path,instant,day):
    item=us();cache(tmp_path,item,(day,));spec=specification(item);now=datetime.fromisoformat(instant)
    evidence=attest_identity(spec,identity(spec),now)
    assert inspect_core_cache(tmp_path,spec,now,evidence=evidence).classification=='reusable'


def test_audit_groups_reconcile_and_unknown_cache_is_not_reported_as_missing(tmp_path):
    rows=[row(),us(),us(symbol='NASDAQ:MSFT'),us(symbol='AMEX:SPY',asset_type='etf')]
    cache(tmp_path,rows[0]);cache(tmp_path,rows[1]);cache(tmp_path,rows[2],('2026-11-11',))
    result=audit_population(rows+[row(watch_sector='중복 분류')],data_dir=tmp_path,cutoff=cutoff())
    assert result['groups']['total']['population']==4
    assert result['groups']['total']['classifications']==dict(zip(CLASSIFICATIONS,[1,1,1,0,1,0,0]))
    for group in result['groups'].values():assert sum(group['classifications'].values())==group['population']
    unknown=audit_population(rows,data_dir=None,cutoff=cutoff())['groups']['total']
    assert unknown['classifications']['not_inspected']==4 and unknown['passed']['exists']==0
    assert unknown['unobserved']['exists']==4 and unknown['classifications']['no_cache']==0
    assert unknown['external_verification_or_collection'] is None


@pytest.mark.parametrize('status,first,subsequent',[(429,'PROVIDER_RATE_LIMITED','PROVIDER_RATE_LIMIT_CIRCUIT_OPEN'),
    (401,'PROVIDER_ACCESS_DENIED','PROVIDER_ACCESS_CIRCUIT_OPEN'),(403,'PROVIDER_ACCESS_DENIED','PROVIDER_ACCESS_CIRCUIT_OPEN')])
def test_direct_failure_and_circuit_skipped_are_counted_separately(tmp_path,status,first,subsequent):
    class Response:status_code=status
    class Session:
        def get(self,*args,**kwargs):return Response()
    p=YahooDaily(Session(),pause=0)
    rows=[us(),us(symbol='NASDAQ:MSFT'),row()];cache(tmp_path,rows[-1])
    doc=build(rows,p,data_dir=tmp_path)
    assert doc['instruments'][rows[0]['symbol']]['reason']==first
    assert doc['instruments'][rows[1]['symbol']]['reason']==subsequent
    assert doc['instruments'][row()['symbol']]['status']=='current'
    assert p.calls==1 and doc['collection_metrics']['direct_failures']==1 and doc['collection_metrics']['circuit_skipped']==1
    assert doc['collection_metrics']['requests_by_market']['US']==dict(identity=0,price=1)


def test_only_missing_instrument_fetches_and_old_good_publication_survives(tmp_path):
    rows=[row(),us()];cache(tmp_path,rows[0]);p=Provider()
    doc=build(rows,p,data_dir=tmp_path)
    assert [x[0] for x in p.operations]==['NYSE:IBM'] and doc['collection_metrics']['cache_reused']==1
    old=deepcopy(doc);old['instruments']['NYSE:IBM']['listing_identity']['verified_at']='2026-10-01T00:00:00Z'
    failed=build(rows,Provider(fail=True),previous=old,data_dir=tmp_path,force=True)
    assert failed['instruments']['NYSE:IBM']['bars']==old['instruments']['NYSE:IBM']['bars']
    assert failed['instruments']['NYSE:IBM']['status']=='retained'


def test_three_network_failures_stop_subsequent_requests_without_retries():
    class Session:
        def get(self,*a,**kw):raise requests.ConnectionError('synthetic failure')
    p=YahooDaily(Session(),pause=0)
    doc=build([us(symbol='NYSE:SYNTH'+str(i)) for i in range(5)],p)
    assert p.calls==3 and doc['collection_metrics']['circuit_skipped']==2
    assert doc['collection_metrics']['direct_failures']==3


def test_next_core_session_reuses_persisted_identity_without_force_or_price_calls(tmp_path):
    item=us();old=build([item],Provider());entry=old['instruments'][item['symbol']]
    entry['bars']['d']=['2026-11-11'];entry['last_final_session']='2026-11-11'
    entry['expected_session']='2026-11-11';entry['last_final_close_at']='2026-11-11T21:00:00+00:00'
    cache(tmp_path,item);provider=Provider(fail=True)
    newer=dict(run_id='20261113T004600Z-new',generated_at='2026-11-13T00:46:00Z')
    doc=build_snapshot([item],catalog_sha256=old['catalog_sha256'],core=newer,core_commit='b'*40,
        workflow_run_id='124',cutoff=cutoff(),provider=provider,previous=old,data_dir=tmp_path,clock=cutoff)
    e=doc['instruments'][item['symbol']]
    assert provider.calls==0 and e['last_final_session']=='2026-11-12' and e['status']=='current'
    assert e['price_origin_core_run_id']==newer['run_id'] and e['listing_identity']==entry['listing_identity']


def test_existing_eod_incremental_fetch_is_kept_and_never_spliced_into_adjusted_cache(tmp_path):
    item=us();old=build([item],Provider());e=old['instruments'][item['symbol']]
    e['bars']['d']=['2026-11-11'];e['last_final_session']='2026-11-11'
    e['expected_session']='2026-11-11';e['last_final_close_at']='2026-11-11T21:00:00+00:00'
    class Incremental(Provider):
        def fetch(self,spec,instant,**kwargs):
            result=super().fetch(spec,instant,**kwargs)
            return [dict(result[0][0],d='2026-11-11'),result[0][0]],result[1],0
    p=Incremental();newer=dict(run_id='20261113T004600Z-new',generated_at='2026-11-13T00:46:00Z')
    doc=build_snapshot([item],catalog_sha256=old['catalog_sha256'],core=newer,core_commit='b'*40,
        workflow_run_id='124',cutoff=cutoff(),provider=p,previous=old,data_dir=tmp_path,clock=cutoff)
    assert p.operations==[(item['symbol'],{'start':'2026-11-01'})]
    assert doc['instruments'][item['symbol']]['bars']['d']==['2026-11-11','2026-11-12']


def test_census_and_collection_share_one_read_per_cache(tmp_path,monkeypatch):
    rows=[row(),us()]
    for item in rows:cache(tmp_path,item)
    original=pd.read_pickle;reads=[]
    def counted(path):
        reads.append(path)
        return original(path)
    monkeypatch.setattr(pd,'read_pickle',counted)
    inspections={}
    audit_population(rows,data_dir=tmp_path,cutoff=cutoff(),inspections=inspections)
    p=Provider();doc=build(rows,p,data_dir=tmp_path,cache_inspections=inspections)
    assert len(reads)==2 and p.calls==1 and all(e['status']=='current' for e in doc['instruments'].values())


@pytest.mark.parametrize('item',[us(),us(asset_type='etf'),row(asset_type='etf')])
def test_public_cache_reader_requires_listing_evidence_for_us_and_etfs(tmp_path,item):
    from watchlist.manual_eod_publish import read_core_cache
    cache(tmp_path,item);spec=specification(item)
    assert read_core_cache(tmp_path,spec,cutoff()) is None
    evidence=attest_identity(spec,identity(spec),cutoff())
    assert read_core_cache(tmp_path,spec,cutoff(),evidence=evidence)[0][-1]['c']==104.


@pytest.mark.parametrize('replay_cache_hit',['true','false',''])
def test_entrypoint_writes_read_only_census_and_actual_request_artifact(tmp_path,monkeypatch,replay_cache_hit):
    import json
    import sys
    from watchlist import manual_eod_publish as module
    from watchlist.manual_eod_contract import canonical
    rows=[row(),us()];data=tmp_path/'data';_,cache_path=cache(data,rows[0]);before=cache_path.read_bytes()
    published=tmp_path/'published';(published/'latest').mkdir(parents=True)
    manifest=canonical(core());(published/'latest/manifest.json').write_bytes(manifest)
    event=tmp_path/'event.json';event.write_text('{}');diagnostics=tmp_path/'diagnostics'
    class Logged(Provider):
        def __init__(self):super().__init__();self.request_log=[]
        def fetch(self,spec,instant,**kwargs):
            self.request_log.append(dict(market=spec['market'],symbol=spec['symbol'],operation='price',status=200))
            return super().fetch(spec,instant,**kwargs)
    p=Logged();monkeypatch.setattr(module,'YahooDaily',lambda:p)
    # This test isolates the original direct-provider diagnostics. A separate
    # test module exercises actual yfinance stage integration with mock feeds.
    class NoopYF:
        max_symbols=0
        batches=0
        attempted=[]
        results={}
        def prepare(self,*args,**kwargs):return self
        def get(self,spec):return None
        def diagnostics(self):return dict(symbols_staged=0,batches=0,metadata_checks=0,
                                          verified=0,failure_reasons={})
    monkeypatch.setattr(module,'YFinanceBatch',NoopYF)
    monkeypatch.setattr(module,'resolve_publication',lambda *a,**k:(core(),'a'*40,'123',cutoff()))
    monkeypatch.setattr(module,'fetch_catalog',lambda *a,**k:(csv_bytes(rows),rows,'b'*40))
    monkeypatch.setenv('CORE_CACHE_HIT','true');monkeypatch.delenv('GITHUB_OUTPUT',raising=False)
    monkeypatch.setattr(sys,'argv',['eod','--event',str(event),'--data',str(data),'--published',str(published),'--diagnostics',str(diagnostics)])
    module.main()
    audit=json.loads((diagnostics/'cache-audit.json').read_bytes());report=json.loads((diagnostics/'collection.json').read_bytes())
    assert audit['exact_cache_key']=='wl-cache-123' and audit['groups']['total']['population']==2
    assert audit['groups']['total']['classifications']['no_cache']==1
    assert report['requests']==[dict(market='US',symbol='NYSE:IBM',operation='price',status=200)]
    assert report['metrics']['http_requests']==1 and report['total_build_seconds']>=0
    macro_review=json.loads((diagnostics/'macro-source-review.json').read_bytes())
    assert macro_review['population']==0 and macro_review['http_requests']==0
    assert macro_review['catalog_sha256']==audit['catalog_sha256']
    assert cache_path.read_bytes()==before and (published/'latest/manifest.json').read_bytes()==manifest
    # Replaying the same core publication does no collection. Its artifact must
    # not repeat the requests/cache hits recorded in the original snapshot.
    pointer=(published/'manual_eod/latest.json').read_bytes()
    monkeypatch.setenv('CORE_CACHE_HIT',replay_cache_hit)
    p=Logged();module.main()
    replay=json.loads((diagnostics/'collection.json').read_bytes())
    assert replay['requests']==[] and replay['metrics']['http_requests']==0
    assert replay['snapshot_reused'] is True and replay['metrics']['cache_reused']==0
    assert all(e['http_requests']==0 for e in replay['instruments'].values())
    assert module.read_previous(published)['collection_metrics']['http_requests']==1
    assert (published/'manual_eod/latest.json').read_bytes()==pointer
    assert replay['cache_restore']['hit']==(replay_cache_hit=='true')
    if replay_cache_hit!='true':
        replay_audit=json.loads((diagnostics/'cache-audit.json').read_bytes())
        assert replay_audit['groups']['total']['classifications']['not_inspected']==2
        assert replay_audit['groups']['total']['classifications']['no_cache']==0


@pytest.mark.parametrize('item',[row(),us()])
def test_historical_suspension_is_a_gap_without_discarding_valid_current_cache(tmp_path,item):
    frame,path=cache(tmp_path,item,('2026-11-09','2026-11-10','2026-11-11','2026-11-12'))
    suspended=pd.Timestamp('2026-11-11')
    frame.loc[suspended,['Open','High','Low','Close','Volume']]=0
    frame.loc[suspended,'_raw_ohlc_valid']=False;frame.to_pickle(path);before=path.read_bytes()
    spec=specification(item);evidence=attest_identity(spec,identity(spec),cutoff())
    inspected=inspect_core_cache(tmp_path,spec,cutoff(),evidence=evidence)
    assert inspected.classification=='reusable' and inspected.excluded_unverified==1
    assert [r['d'] for r in inspected.rows]==['2026-11-09','2026-11-10','2026-11-12']
    old=build([item],Provider());p=Provider(fail=True)
    doc=build([item],p,previous=old,data_dir=tmp_path,force=True)
    entry=doc['instruments'][item['symbol']]
    assert p.calls==0 and entry['status']=='current' and entry['missing_sessions']==1
    assert path.read_bytes()==before


@pytest.mark.parametrize('changed',[row(unit='USD'),row(asset_type='etf')])
def test_catalog_currency_or_asset_change_cannot_retain_incompatible_old_prices(changed):
    old=build([row()],Provider())
    doc=build([changed],Provider(fail=True),previous=old,force=True)
    entry=doc['instruments'][changed['symbol']]
    assert not entry['bars'] and entry['status']!='retained' and entry['last_final_session'] is None


@pytest.mark.parametrize('item',[row(),us()])
def test_provider_fallback_cannot_replace_newer_adjusted_cache_publication(tmp_path,item):
    cache(tmp_path,item)
    old=build([item],Provider(),data_dir=tmp_path)
    class Behind(Provider):
        def fetch(self,spec,instant,**kwargs):
            rows,evidence,excluded=super().fetch(spec,instant,**kwargs)
            return [dict(rows[0],d='2026-11-11')],evidence,excluded
    p=Behind()
    newer=dict(run_id='20261114T004600Z-new',generated_at='2026-11-14T00:46:00Z')
    later=cutoff()+timedelta(days=1)
    new=build_snapshot([item],catalog_sha256=old['catalog_sha256'],core=newer,core_commit='b'*40,
        workflow_run_id='124',cutoff=later,provider=p,previous=old,clock=lambda:later)
    before=old['instruments'][item['symbol']];after=new['instruments'][item['symbol']]
    assert p.calls==1 and after['bars']==before['bars']
    assert after['status']=='retained' and after['reason']=='PROVIDER_BEHIND_PREVIOUS_SNAPSHOT'
    for field in ('adjustment','source','identity_evidence','last_final_session','last_success_at','verified_at'):
        assert after[field]==before[field]


def test_cache_basis_diagnostic_pinpoints_exact_dates_without_promoting_prices(tmp_path):
    item=row()
    frame,path=cache(tmp_path,item,('2026-11-09','2026-11-10','2026-11-11','2026-11-12'))
    frame.loc[pd.Timestamp('2026-11-10'),'_base_price_basis']=''
    frame.loc[pd.Timestamp('2026-11-11'),'_base_price_basis']='unknown:raw'
    frame.loc[pd.Timestamp('2026-11-12'),'_base_price_basis']=None
    frame.to_pickle(path)
    before=path.read_bytes()
    spec=specification(item)
    inspection=inspect_core_cache(tmp_path,spec,cutoff())
    assert inspection.classification=='basis_mismatch'
    assert inspection.rows is None
    assert inspection.basis_evidence=={
        'inspected':4,'expected':1,'missing':2,'different':1,
        'missing_dates':['2026-11-10','2026-11-12'],
        'different_dates':['2026-11-11'],
        'last_session_basis_verified':False}
    census=audit_population([item],data_dir=tmp_path,cutoff=cutoff())
    assert census['instruments'][0]['basis_evidence']==inspection.basis_evidence
    assert path.read_bytes()==before


def test_healthy_basis_audit_does_not_spuriously_report_missing_dates(tmp_path):
    item=row()
    _,path=cache(tmp_path,item,('2026-11-11','2026-11-12'))
    inspection=inspect_core_cache(tmp_path,specification(item),cutoff())
    assert inspection.classification=='reusable'
    assert inspection.basis_evidence['missing_dates']==[]
    assert inspection.basis_evidence['different_dates']==[]
    assert inspection.basis_evidence['last_session_basis_verified'] is True

