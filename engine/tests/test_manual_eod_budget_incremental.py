"""The direct-request ceiling stays 64; use its budget for new listings.

Offline checks for 2026-11-19 EOD evidence: 132 listings deferred, 63/64
direct Yahoo 200 responses, 11/16 verified yfinance, opaque internal HTTP.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import pandas as pd

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_provider import specification, Unavailable
from watchlist.manual_eod_publish import (
    MAX_PROVIDER_REQUESTS_PER_RUN, _yfinance_candidates,
    _recent_confirmed_lag, build_snapshot,
)
from watchlist.manual_eod_yfinance import BASIS, YFinanceBatch

from test_manual_eod_yfinance import CUT, DummyYF, foreign, metadata


def _old(row, basis=BASIS):
    spec=specification(row)
    return {k:spec[k] for k in (
        'symbol','exchange','asset_type','market','currency','exchange_timezone')} | dict(
        bars=dict(d=['2026-11-11'],o=[100.],h=[105.],l=[98.],c=[102.],v=[500.]),
        adjustment=basis,last_final_session='2026-11-11',
        expected_session='2026-11-11',status='current',reason='')


def _many(prefix, number, country='USD'):
    return [foreign(prefix+str(i),country) for i in range(number)]


def test_direct_http_limit_is_still_conservative_64_and_cold_probes_are_six():
    assert MAX_PROVIDER_REQUESTS_PER_RUN==64
    rows = (_many('NYSE:NEW',12) + _many('TSE:7',10,'JPY') +
            _many('SSE:6',8,'CNY') + _many('HKEX:3',3,'HKD') +
            _many('XETR:CODE',2,'EUR') + _many('SIX:CODE',1,'CHF') +
            _many('BME:CODE',1,'EUR'))
    staged=_yfinance_candidates(rows,{},None,CUT,16)
    assert len(staged)==6  # not 16 opaque additional yfinance downloads
    assert {spec['market'] for spec, _ in staged}=={'US','JP','CN'}
    assert {spec['market']:sum(s['market']==spec['market'] for s,_ in staged)
            for spec,_ in staged}=={'US':2,'JP':2,'CN':2}
    assert all(meta is False for _,meta in staged)


def test_certified_adjusted_refresh_and_core_identity_beat_cold_staging():
    adjusted=_many('NYSE:ADJ',10)
    identities=_many('NASDAQ:ID',3)
    cold=_many('TSE:7',10,'JPY')
    prior={row['symbol']:_old(row) for row in adjusted}
    insp={row['symbol']:SimpleNamespace(classification='identity_pending')
          for row in identities}
    rows=adjusted+identities+cold
    staged=_yfinance_candidates(rows,prior,insp,CUT,16)
    symbols={s['symbol'] for s,_ in staged}
    assert len(staged)==16
    assert all(r['symbol'] in symbols for r in adjusted)
    assert all(r['symbol'] in symbols for r in identities)
    assert sum(s['market']=='JP' for s,_ in staged)==3
    assert sum(meta for _,meta in staged)==3


def test_recent_stale_price_does_not_repeat_direct_request_in_same_session():
    item=foreign('NYSE:IBM','USD')
    def snapshot(p, *, previous=None, now=CUT, run='one'):
        return build_snapshot([item],catalog_sha256=digest(csv_bytes([item])),
            core=dict(run_id=run,generated_at=now.isoformat()),core_commit='a'*40,
            workflow_run_id='123',cutoff=now,clock=lambda:now,provider=p,
            previous=previous)
    class Lag:
        def __init__(self):
            self.calls=0
            self.request_log=[]
        def fetch(self,spec,cutoff,**kwargs):
            self.calls+=1
            self.request_log.append(dict(status=200,market=spec['market'],
                symbol=spec['symbol'],operation='price'))
            return [dict(d='2026-11-11',o=100.,h=105.,l=98.,c=102.,v=500.)],dict(
                provider_symbol=spec['candidates'][0],
                provider_exchange=spec['provider_exchanges'][0],
                currency=spec['currency'],timezone=spec['provider_timezones'][0],
                instrument_type='EQUITY'),0
    first=Lag();old=snapshot(first)
    original=old['instruments'][item['symbol']]
    assert first.calls==1 and original['status']=='stale'
    assert original['last_final_session']=='2026-11-11'
    assert _recent_confirmed_lag(original,'2026-11-12',CUT+timedelta(minutes=30))
    second=Lag()
    recent=snapshot(second,previous=old,now=CUT+timedelta(minutes=30),run='two')
    reused=recent['instruments'][item['symbol']]
    assert second.calls==0
    assert reused['status']=='stale' and reused['last_final_session']=='2026-11-11'
    assert reused['bars']==original['bars']
    assert reused['last_success_at']==original['last_success_at']
    assert reused['retry_cooldown'] is True
    assert recent['collection_metrics']['lag_cooldown_reused']==1
    assert recent['collection_metrics']['direct_responses']=={}
    third=Lag()
    later=snapshot(third,previous=recent,now=CUT+timedelta(hours=2,minutes=10),run='three')
    assert third.calls==1
    assert later['collection_metrics']['lag_cooldown_reused']==0
    assert later['collection_metrics']['direct_responses']=={'200':1}


def test_new_confirmed_session_ignores_lag_cooldown():
    old=_old(foreign('NYSE:IBM','USD'))
    old.update(status='stale',reason='PROVIDER_BEHIND_FINAL_SESSION',
        expected_session='2026-11-12',
        last_attempt_at='2026-11-12T23:00:00+00:00')
    assert _recent_confirmed_lag(old,'2026-11-12',CUT+timedelta(minutes=5))
    assert not _recent_confirmed_lag(old,'2026-11-13',CUT+timedelta(minutes=5))


class ThreeDays(DummyYF):
    def __init__(self, lookup, revised=False):
        super().__init__(lookup)
        self.revised=revised

    def download(self, **kwargs):
        self.downloads.append(kwargs)
        values=[102.,103.,104.]
        if self.revised:values=[112.,113.,114.]
        df=pd.DataFrame(dict(Open=[100.,101.,102.],High=[120.,121.,122.],
            Low=[98.,99.,100.],Close=values,Volume=[500.,600.,700.]),
            index=pd.DatetimeIndex(['2026-11-11','2026-11-12','2026-11-13']))
        return pd.concat({symbol:df for symbol in kwargs['tickers']},axis=1)


class StrictNoDirect:
    calls=0
    request_log=[]
    def fetch(self,*args,**kwargs):
        self.calls+=1
        raise AssertionError('verified yfinance must not fetch direct Yahoo')


def _snapshot(item, yf_batch, *, prior=None, cutoff=CUT, run='one'):
    return build_snapshot([item],catalog_sha256=digest(csv_bytes([item])),
        core=dict(run_id=run,generated_at=cutoff.isoformat()),core_commit='a'*40,
        workflow_run_id='123',cutoff=cutoff,clock=lambda:cutoff,
        provider=StrictNoDirect(),previous=prior,yf_batch=yf_batch)


def test_adjusted_yfinance_refresh_fetches_incremental_only_and_merges_overlap():
    row=foreign('NYSE:IBM','USD');spec=specification(row)
    fake=ThreeDays({spec['candidates'][0]:spec})
    first=_snapshot(row,YFinanceBatch(fake.download,fake.ticker,max_symbols=1))
    previous=first['instruments'][row['symbol']]
    assert previous['last_final_session']=='2026-11-12'
    next_cut=datetime(2026,11,13,23,tzinfo=timezone.utc)
    refreshed=ThreeDays({spec['candidates'][0]:spec})
    batch=YFinanceBatch(refreshed.download,refreshed.ticker,max_symbols=1)
    latest=_snapshot(row,batch,prior=first,cutoff=next_cut,run='two')
    entry=latest['instruments'][row['symbol']]
    assert refreshed.downloads[0]['start']=='2026-11-02'
    assert batch.diagnostics()['batch_mix']['US']['incremental']==1
    assert entry['status']=='current'
    assert entry['last_final_session']=='2026-11-13'
    assert entry['adjustment']==BASIS
    assert entry['bars']['d']==['2026-11-11','2026-11-12','2026-11-13']
    assert entry['bars']['c']==[102.,103.,104.]
    assert latest['collection_metrics']['http_requests']==0


def test_adjusted_seam_mismatch_keeps_previous_then_next_run_full_replaces():
    row=foreign('NYSE:IBM','USD');spec=specification(row)
    initial=ThreeDays({spec['candidates'][0]:spec})
    first=_snapshot(row,YFinanceBatch(initial.download,initial.ticker,max_symbols=1))
    old=first['instruments'][row['symbol']]
    next_cut=datetime(2026,11,13,23,tzinfo=timezone.utc)
    revised=ThreeDays({spec['candidates'][0]:spec},revised=True)
    middle=_snapshot(row,YFinanceBatch(revised.download,revised.ticker,max_symbols=1),
        prior=first,cutoff=next_cut,run='two')
    unmerged=middle['instruments'][row['symbol']]
    assert unmerged['status']=='retained'
    assert unmerged['reason']=='YFINANCE_ADJUSTED_SEAM_MISMATCH'
    assert unmerged['bars']==old['bars']
    assert unmerged['adjustment']==BASIS
    repaired=ThreeDays({spec['candidates'][0]:spec},revised=True)
    batch=YFinanceBatch(repaired.download,repaired.ticker,max_symbols=1)
    last=_snapshot(row,batch,prior=middle,cutoff=next_cut+timedelta(hours=1),run='three')
    final=last['instruments'][row['symbol']]
    assert repaired.downloads[0]['start']!='2026-11-02'
    assert batch.diagnostics()['batch_mix']['US']['full']==1
    assert final['status']=='current'
    assert final['bars']['c']==[112.,113.,114.]
    assert final['last_final_session']=='2026-11-13'


def test_forced_yfinance_review_bypasses_same_session_stale_cooldown():
    item=foreign('NYSE:IBM','USD')
    prior=_old(item)
    prior.update(status='stale',reason='PROVIDER_BEHIND_FINAL_SESSION',
        expected_session='2026-11-12',
        last_attempt_at=CUT.isoformat())
    data={item['symbol']:prior}
    now=CUT+timedelta(minutes=20)
    assert _yfinance_candidates([item],data,None,CUT,16,now=now)==[]
    selected=_yfinance_candidates([item],data,None,CUT,16,now=now,force=True)
    assert [spec['symbol'] for spec,_ in selected]==['NYSE:IBM']

