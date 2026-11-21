"""NBI index-level acquisition is read-only and cannot forge equity OHLC."""
from datetime import datetime,timezone
import copy
import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest,validate_snapshot
from watchlist.manual_eod_provider import (
    INDEX_BASIS,WrongAsset,Unavailable,result_of,specification,
)
from watchlist.manual_eod_publish import build_snapshot
from test_manual_eod_pipeline import row

CUT=datetime(2026,11,19,2,20,tzinfo=timezone.utc)
NEXT=datetime(2026,11,20,2,20,tzinfo=timezone.utc)

def index_row(**kw):
    result=row(symbol='NASDAQ:NBI',name='Nasdaq Biotechnology Index',
        asset_type='index',unit='지수 포인트',chart_style='line',
        watch_region='글로벌/미국',watch_sector='BIO')
    result.update(kw)
    return result

class NBIProvider:
    def __init__(self,fail=False):
        self.calls=0
        self.max_calls=64
        self.request_log=[]
        self.fail=fail

    def fetch(self,spec,cutoff,*,close_only=False,**kwargs):
        assert close_only and not kwargs
        assert spec['candidates']==('^NBI',)
        self.calls+=1
        self.request_log.append(dict(market='US',symbol='NASDAQ:NBI',
            operation='price',status=503 if self.fail else 200))
        if self.fail:raise Unavailable('PROVIDER_HTTP_503')
        end='2026-11-19' if cutoff==NEXT else '2026-11-18'
        return [dict(d='2026-11-17',c=6000.),
                dict(d='2026-11-18',c=6010.)]+(
                [dict(d=end,c=6020.)] if end!='2026-11-18' else []),dict(
            provider_symbol='^NBI',provider_exchange='NIM',currency='USD',
            instrument_type='INDEX',timezone='America/New_York'),0

def snapshot(provider,*,previous=None,cutoff=CUT,run='one'):
    rows=[index_row()]
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=cutoff.isoformat()),
        core_commit='a'*40,workflow_run_id='123',cutoff=cutoff,
        clock=lambda:cutoff,provider=provider,previous=previous,
        close_fallback=True)

def test_explicit_index_mapping_and_reject_wrong_type_or_unit():
    spec=specification(index_row())
    assert spec['asset_type']=='index'
    assert spec['candidates']==('^NBI',)
    assert spec['currency']=='USD'
    with pytest.raises(Unavailable):
        specification(index_row(unit='USD'))
    with pytest.raises(Unavailable):
        specification(index_row(symbol='NASDAQ:OTHER'))
    meta=dict(symbol='^NBI',instrumentType='ETF',currency='USD',
        exchangeName='NIM',exchangeTimezoneName='America/New_York',
        dataGranularity='1d')
    with pytest.raises(WrongAsset,match='ASSET_TYPE'):
        result_of(dict(chart=dict(error=None,result=[dict(meta=meta)])),
            '^NBI',spec)

def test_nbi_posts_verified_point_series_without_ohlc_or_strategy_cache():
    p=NBIProvider()
    doc=snapshot(p)
    item=doc['instruments']['NASDAQ:NBI']
    assert p.calls==1
    assert item['status']=='current'
    assert item['asset_type']=='index'
    assert item['bars']=={}
    assert item['close_only']['d'][-1]=='2026-11-18'
    assert item['close_only']['c']==[6000.,6010.]
    assert item['close_only']['adjustment']==INDEX_BASIS
    assert item['close_only']['identity_evidence']['instrument_type']=='INDEX'
    assert doc['collection_metrics']['verified_index_level_series']==1
    assert not doc['collection_metrics']['close_only']['candidate_symbols']
    validate_snapshot(doc)
    invalid=copy.deepcopy(doc)
    invalid['instruments']['NASDAQ:NBI']['bars']={'d':['2026-11-18']}
    with pytest.raises(ValueError,match='index must not masquerade'):
        validate_snapshot(invalid)

def test_nbi_reuses_verified_level_then_survives_source_outage():
    old=snapshot(NBIProvider())
    p=NBIProvider(fail=True)
    new=snapshot(p,previous=old,cutoff=NEXT,run='two')
    item=new['instruments']['NASDAQ:NBI']
    assert item['status']=='retained'
    assert item['close_only']==old['instruments']['NASDAQ:NBI']['close_only']
    assert item['reason']=='PROVIDER_HTTP_503'
    assert item['last_final_session']=='2026-11-18'
    assert p.calls==1
    validate_snapshot(new)

