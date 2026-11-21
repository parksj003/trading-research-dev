"""Independent KR adjusted-by-date rescue: exact source, bounded attempts, no core writes."""
from datetime import datetime,timezone
from pathlib import Path

import pandas as pd
import pytest

from watchlist.manual_eod_contract import digest,validate_snapshot
from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_kr_by_date import VerifiedKRByDate,BASIS,SOURCE
from watchlist.manual_eod_provider import Unavailable,specification
from watchlist.manual_eod_publish import build_snapshot
from test_manual_eod_pipeline import row

CUT=datetime(2026,11,19,18,4,tzinfo=timezone.utc)


def record_frame(*,invalid=False,unfinished=False):
    values=[
        dict(시가=100.,고가=105.,저가=98.,종가=104.,거래량=1000.),
        dict(시가=101.,고가=106.,저가=99.,종가=105.,거래량=1200.),
        dict(시가=102.,고가=107.,저가=100.,종가=106.,거래량=1300.)]
    if invalid:
        values[0]['고가']=103.
    if unfinished:
        values[-1]={k:0. for k in values[-1]}
    return pd.DataFrame(values,index=pd.to_datetime(
        ['2026-11-17','2026-11-18','2026-11-19']))


def stock():return row(symbol='KRX:005930',name='삼성전자',asset_type='stock',unit='KRW')

def test_adjusted_kr_source_never_requires_unverified_bulk_price_basis():
    attempts=[]
    def fetch(start,end,ticker,adjusted=True):
        attempts.append((start,end,ticker,adjusted))
        return record_frame()
    provider=VerifiedKRByDate(fetcher=fetch,max_attempts=2)
    source,identity,excluded=provider.fetch(specification(stock()),CUT)
    assert len(attempts)==1 and attempts[0][2:] == ('005930',True)
    assert attempts[0][0]<='20241115'
    assert source[-1]['d']=='2026-11-19'
    assert source[-1]['o']==102. and source[-1]['c']==106.
    assert identity['evidence']=='pykrx:stock-by-date:adjusted=True'
    assert identity['provider_exchange']=='KRX'
    assert provider.diagnostics()['attempted']==provider.diagnostics()['succeeded']==1
    assert excluded==0


def test_invalid_historical_ohlc_blocks_all_relabeling_and_repairs():
    provider=VerifiedKRByDate(fetcher=lambda *a,**k:record_frame(invalid=True))
    with pytest.raises(Unavailable,match='KR_BY_DATE_OHLC_INTEGRITY_FAILED'):
        provider.fetch(specification(stock()),CUT)
    assert provider.diagnostics()['succeeded']==0
    assert provider.diagnostics()['failed_reasons']=={'KRX:005930':'KR_BY_DATE_OHLC_INTEGRITY_FAILED'}


def test_suspended_zero_price_is_only_excluded_as_a_real_missing_bar():
    provider=VerifiedKRByDate(fetcher=lambda *a,**k:record_frame(unfinished=True))
    source,_,excluded=provider.fetch(specification(stock()),CUT)
    assert [x['d'] for x in source]==['2026-11-17','2026-11-18']
    assert excluded==1


def test_bounded_rescue_only_fixes_interest_eod_not_core_cache(tmp_path):
    r=stock()
    class OldSource:
        calls=0
        max_calls=64
        request_log=[]
        def fetch(self,spec,cutoff,**kwargs):
            self.calls+=1
            return [
                dict(d='2026-11-17',o=100.,h=105.,l=98.,c=104.,v=1000.),
                dict(d='2026-11-18',o=101.,h=106.,l=99.,c=105.,v=1200.)],dict(
                provider_symbol='005930.KS',provider_exchange='KSC',
                currency='KRW',instrument_type='EQUITY',timezone='Asia/Seoul'),0
    initial=OldSource()
    data=tmp_path/'data';data.mkdir()
    kr_cache=data/'kr_history_cache';kr_cache.mkdir()
    (kr_cache/'005930.pkl').write_bytes(b'untouched original cache')
    kwargs=dict(catalog_sha256=digest(csv_bytes([r])),core_commit='a'*40,
        workflow_run_id='123',cutoff=CUT,clock=lambda:CUT,provider=initial,data_dir=None)
    previous=build_snapshot([r],core=dict(run_id='old',generated_at='2026-11-19T17:00:00+00:00'),**kwargs)
    assert previous['instruments'][r['symbol']]['status']=='stale'
    provider=VerifiedKRByDate(fetcher=lambda *a,**k:record_frame(),max_attempts=1)
    new=build_snapshot([r],catalog_sha256=kwargs['catalog_sha256'],
        core=dict(run_id='new',generated_at='2026-11-19T18:00:00+00:00'),
        core_commit='a'*40,workflow_run_id='124',cutoff=CUT,
        clock=lambda:CUT,provider=OldSource(),previous=previous,
        data_dir=None,kr_by_date=provider)
    entry=new['instruments'][r['symbol']]
    assert entry['status']=='current'
    assert entry['last_final_session']=='2026-11-19'
    assert entry['adjustment']==BASIS and entry['source']==SOURCE
    assert entry['acquisition']=='kr_verified_by_date'
    assert entry['bars']['d']==['2026-11-17','2026-11-18','2026-11-19']
    assert new['collection_metrics']['http_requests']==0
    assert new['collection_metrics']['kr_by_date_published']==1
    assert new['collection_metrics']['kr_by_date']['attempted']==1
    assert (kr_cache/'005930.pkl').read_bytes()==b'untouched original cache'
    validate_snapshot(new)


def test_only_verified_kr_stock_is_eligible_and_cap_cannot_expand():
    p=VerifiedKRByDate(fetcher=lambda *a,**k:record_frame(),max_attempts=1)
    with pytest.raises(Unavailable,match='KR_BY_DATE_LISTING_NOT_CERTIFIED'):
        p.fetch(specification(row(symbol='NASDAQ:NBI',asset_type='index',
            unit='지수 포인트',chart_style='line')),CUT)
    assert p.attempted==0
    p.fetch(specification(stock()),CUT)
    with pytest.raises(Unavailable,match='BUDGET_DEFERRED'):
        p.fetch(specification(stock()),CUT)
    assert p.attempted==1

