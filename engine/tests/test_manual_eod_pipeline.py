from datetime import datetime,timezone
from pathlib import Path
import hashlib,json

import pandas as pd
import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_provider import specification


def row(**kw):
    x=dict(enabled=True,watch_region='한국',watch_sector='반도체',watch_subsector='',symbol='KRX:005930',name='삼성전자',asset_type='stock',unit='KRW',chart_style='candles',sort_order=1,note='');x.update(kw);return x


def core():return dict(run_id='20261112T100800Z-abcdef',generated_at='2026-11-12T19:08:00+09:00')
def cutoff():return datetime(2026,11,12,10,9,tzinfo=timezone.utc)
def bars(date='2026-11-12'):return [dict(d=date,o=100,h=105,l=98,c=104,v=None)]


class Provider:
    def __init__(self,fail=False):self.calls=0;self.fail=fail
    def fetch(self,spec,instant,**kwargs):
        from watchlist.manual_eod_provider import Unavailable
        self.calls+=1
        if self.fail:raise Unavailable('SYNTHETIC_PROVIDER_DOWN')
        return bars(),dict(provider_symbol='005930.KS',provider_exchange='KSC',currency='KRW',instrument_type='EQUITY',timezone='Asia/Seoul'),0


def build(rows,provider,previous=None,**kw):
    from watchlist.manual_eod_publish import build_snapshot
    return build_snapshot(rows,catalog_sha256=hashlib.sha256(csv_bytes(rows)).hexdigest(),core=core(),core_commit='a'*40,
        workflow_run_id='123',cutoff=cutoff(),provider=provider,previous=previous,clock=cutoff,**kw)


def test_duplicate_memberships_are_fetched_once_and_never_change_core():
    p=Provider();source=core().copy()
    doc=build([row(),row(watch_sector='내 별도 목록')],p)
    assert p.calls==1 and len(doc['instruments'])==1 and core()==source
    entry=doc['instruments']['KRX:005930'];assert entry['last_final_session']=='2026-11-12' and entry['status']=='current'
    assert entry['bars']['v']==[None] and entry['source_symbol']=='005930.KS'


def test_same_core_run_and_catalog_are_idempotent_without_refetch():
    p=Provider();rows=[row()];doc=build(rows,p);again=build(rows,p,previous=doc)
    assert p.calls==1 and again==doc


def test_provider_failure_keeps_last_success_and_actual_price_date():
    good=build([row()],Provider());bad=build([row()],Provider(fail=True),previous=good,force=True)
    item=bad['instruments']['KRX:005930'];old=good['instruments']['KRX:005930']
    assert item['status']=='retained' and item['bars']==old['bars']
    assert item['last_success_at']==old['last_success_at'] and item['last_final_session']==old['last_final_session']
    assert item['reason']=='SYNTHETIC_PROVIDER_DOWN'


def test_macro_never_substitutes_a_related_stock_etf_or_futures():
    p=Provider();doc=build([row(symbol='TVC:US10Y',asset_type='yield',unit='%',chart_style='line')],p)
    item=doc['instruments']['TVC:US10Y'];assert p.calls==0 and item['status']=='unsupported' and not item['bars']


def test_publish_only_writes_manual_namespace_and_preserves_existing_core(tmp_path):
    from watchlist.manual_eod_publish import publish_snapshot,read_previous
    base=tmp_path/'published';(base/'latest').mkdir(parents=True);(base/'history').mkdir()
    (base/'latest/manifest.json').write_text(json.dumps(core()));(base/'history/signals.csv').write_bytes(b'untouched')
    before={str(p.relative_to(base)):p.read_bytes() for p in base.rglob('*') if p.is_file()}
    doc=build([row()],Provider());publish_snapshot(base,doc)
    assert read_previous(base)==doc
    assert all((base/name).read_bytes()==raw for name,raw in before.items())
    with pytest.raises(ValueError):publish_snapshot(base,{**doc,'schema':'broken'})
    assert read_previous(base)==doc


def test_cache_with_filled_or_unconfirmed_ohlc_is_not_reused(tmp_path):
    from watchlist.manual_eod_publish import read_core_cache
    root=tmp_path/'data';p=root/'kr_history_cache/005930.pkl';p.parent.mkdir(parents=True)
    frame=pd.DataFrame(dict(Open=[100],High=[100],Low=[100],Close=[100],Volume=[1000],
        _raw_ohlc_valid=[False],_session_confirmed=[True],_session_finality_version=[3],_base_price_basis=['pykrx:date:adjusted=True']),index=pd.to_datetime(['2026-11-12']))
    frame.to_pickle(p);assert read_core_cache(root,specification(row()),cutoff()) is None
    frame['_raw_ohlc_valid']=True;frame['_session_confirmed']=False;frame.to_pickle(p)
    assert read_core_cache(root,specification(row()),cutoff()) is None
    frame['_session_confirmed']=True;frame.to_pickle(p)
    assert read_core_cache(root,specification(row()),cutoff()) is not None



def test_new_core_run_without_new_final_session_does_not_refetch_or_retimestamp():
    from watchlist.manual_eod_publish import build_snapshot
    p=Provider();rows=[row()];old=build(rows,p)
    newer={**core(),'run_id':'20261112T100900Z-new','generated_at':'2026-11-12T10:09:00Z'}
    doc=build_snapshot(rows,catalog_sha256=old['catalog_sha256'],core=newer,core_commit='b'*40,workflow_run_id='124',cutoff=cutoff(),
        provider=p,previous=old,clock=cutoff)
    assert p.calls==1 and doc['core_run_id']==newer['run_id']
    entry=doc['instruments']['KRX:005930'];before=old['instruments']['KRX:005930']
    assert entry['last_success_at']==before['last_success_at'] and entry['bars']==before['bars']
    assert entry['price_origin_core_run_id']==old['core_run_id']

