"""Budget lanes and curl request observability: no production HTTP traffic."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_publish import (
    MAX_PROVIDER_REQUESTS_PER_RUN, DIRECT_REFRESH_RESERVED_REQUESTS,
    build_snapshot,
)
from watchlist.manual_eod_http_meter import observe_yfinance_curl

from test_manual_eod_yfinance import foreign

FIRST=datetime(2026,11,12,23,tzinfo=timezone.utc)
SECOND=datetime(2026,11,13,23,tzinfo=timezone.utc)


class DeterministicDirect:
    def __init__(self, last='2026-11-11'):
        self.calls=0
        self.request_log=[]
        self.last=last

    def fetch(self,spec,cutoff,*,start=None,metadata_only=False):
        self.calls+=1
        self.request_log.append(dict(market=spec['market'],symbol=spec['symbol'],
            operation='identity' if metadata_only else 'price',status=200))
        identity=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],
            currency=spec['currency'],
            timezone=spec['provider_timezones'][0],
            instrument_type='EQUITY')
        if metadata_only:return [],identity,0
        rows=[dict(d='2026-11-11',o=100.,h=105.,l=98.,c=102.,v=500.)]
        if self.last=='2026-11-13':
            rows.append(dict(d='2026-11-13',o=101.,h=106.,l=99.,c=103.,v=600.))
        return rows,identity,0


def _snapshot(rows,p,*,prior=None,cutoff=FIRST,run='one'):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=cutoff.isoformat()),
        core_commit='a'*40,workflow_run_id='44',cutoff=cutoff,
        provider=p,previous=prior,clock=lambda:cutoff)


def _listing(prefix,count):
    return [foreign(f'NYSE:{prefix}{n}','USD') for n in range(count)]


def test_measured_direct_lanes_reserve_16_refresh_requests_amid_100_missing():
    assert MAX_PROVIDER_REQUESTS_PER_RUN==64
    assert DIRECT_REFRESH_RESERVED_REQUESTS==16
    old_rows=_listing('PREV',24)
    old=_snapshot(old_rows,DeterministicDirect())
    rows=old_rows+_listing('COLD',100)
    provider=DeterministicDirect('2026-11-13')
    published=_snapshot(rows,provider,prior=old,cutoff=SECOND,run='two')
    allocation=published['collection_metrics']['direct_requests_by_lane']
    assert allocation==dict(refresh_reserved=16,cold=48,refresh_spare=0)
    assert provider.calls==64
    assert published['collection_metrics']['direct_requests_unused']==0
    assert sum(v for v in allocation.values())==published['collection_metrics']['http_requests']
    assert published['collection_metrics']['direct_responses']=={'200':64}
    assert sum(bool(published['instruments'][r['symbol']].get('bars'))
               for r in old_rows)==len(old_rows)
    assert sum(published['instruments'][r['symbol']]['status']=='current'
               for r in old_rows)==16
    assert sum(published['instruments'][r['symbol']]['status']=='current'
               for r in rows[len(old_rows):])==48
    assert list(published['instruments'])==[r['symbol'] for r in rows]


def test_all_unused_refresh_reserve_lent_back_to_missing():
    old_rows=_listing('PREV',3)
    old=_snapshot(old_rows,DeterministicDirect())
    rows=old_rows+_listing('COLD',80)
    provider=DeterministicDirect('2026-11-13')
    doc=_snapshot(rows,provider,prior=old,cutoff=SECOND,run='two')
    lane=doc['collection_metrics']['direct_requests_by_lane']
    assert lane==dict(refresh_reserved=3,cold=61,refresh_spare=0)
    assert doc['collection_metrics']['http_requests']==64


def test_unspent_cold_budget_rolls_back_to_deferred_refresh_without_duplicate_calls():
    old_rows=_listing('PREV',24)
    old=_snapshot(old_rows,DeterministicDirect())
    rows=old_rows+_listing('COLD',5)
    p=DeterministicDirect('2026-11-13')
    doc=_snapshot(rows,p,prior=old,cutoff=SECOND,run='two')
    assert p.calls==29
    lane=doc['collection_metrics']['direct_requests_by_lane']
    assert lane==dict(refresh_reserved=16,cold=5,refresh_spare=8)
    assert doc['collection_metrics']['direct_requests_unused']==35
    assert doc['collection_metrics']['budget_deferred']==0
    assert all(e['status']=='current' for e in doc['instruments'].values())
    assert len({e['symbol'] for e in doc['instruments'].values()})==29


def test_curl_meter_observes_only_scoped_transport_without_leaking_urls(monkeypatch):
    from curl_cffi import requests as curls

    def mock_request(self,method,url,*args,**kwargs):
        if 'fail' in url:raise OSError('simulated')
        return SimpleNamespace(status_code=429 if 'throttle' in url else 200)

    monkeypatch.setattr(curls.Session,'request',mock_request)
    with observe_yfinance_curl() as meter:
        session=curls.Session()
        assert session.request('GET','https://query2.finance.yahoo.com/private?auth=redacted').status_code==200
        assert session.request('GET','https://query1.finance.yahoo.com/throttle').status_code==429
        assert session.request('GET','https://example.net/no-content').status_code==200
        with pytest.raises(OSError):
            session.request('GET','https://query2.finance.yahoo.com/fail?secret=no')
    d=meter.diagnostics()
    assert curls.Session.request is mock_request
    assert d['observed_attempts']==4
    assert d['observed_responses']==3
    assert d['observed_exceptions']==1
    assert d['observed_yahoo_requests']==3
    assert d['observed_other_requests']==1
    assert d['observed_statuses']=={'200':2,'429':1}
    assert d['coverage']=='lower_bound_only' and d['total_http_requests_known'] is False
    assert 'redacted' not in str(d) and 'secret' not in str(d)


def test_provider_multi_request_per_symbol_never_overshoots_reserved_http_cap():
    from watchlist.manual_eod_provider import Unavailable

    old_rows=_listing('PREV',15)
    old=_snapshot(old_rows,DeterministicDirect())
    rows=old_rows+_listing('COLD',100)

    class MultiRequest(DeterministicDirect):
        max_calls=64
        def fetch(self,spec,cutoff,*,start=None,metadata_only=False):
            count=2 if spec['symbol'].startswith('NYSE:PREV') else 1
            for _ in range(count):
                if self.calls>=self.max_calls:
                    raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
                self.calls+=1
                self.request_log.append(dict(market=spec['market'],
                    symbol=spec['symbol'],operation='identity' if metadata_only else 'price',status=200))
            identity=dict(provider_symbol=spec['candidates'][0],
                provider_exchange=spec['provider_exchanges'][0],
                currency=spec['currency'],
                timezone=spec['provider_timezones'][0],instrument_type='EQUITY')
            if metadata_only:return [],identity,0
            return [dict(d=d,o=100.,h=105.,l=98.,c=102.,v=500.)
                    for d in ('2026-11-11','2026-11-13')],identity,0

    provider=MultiRequest('2026-11-13')
    published=_snapshot(rows,provider,prior=old,cutoff=SECOND,run='two')
    lanes=published['collection_metrics']['direct_requests_by_lane']
    assert lanes=={'refresh_reserved':16,'cold':48,'refresh_spare':0}
    assert provider.max_calls==64  # temporary phase cap restored
    assert provider.calls==64
    assert published['collection_metrics']['http_requests']==64
    assert sum(r['status']=='current' for r in published['instruments'].values())==56
    assert published['collection_metrics']['budget_deferred']>0

