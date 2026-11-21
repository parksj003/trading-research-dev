"""Offline regressions for bounded recovery order and common-session gap triage."""
from copy import deepcopy
from datetime import datetime,timezone

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest,validate_snapshot
from watchlist.manual_eod_close_fallback import attach_close_only
from watchlist.manual_eod_provider import Unavailable,specification
from watchlist.manual_eod_publish import build_snapshot
from watchlist.manual_eod_gap_audit import audit_missing_history
from test_manual_eod_pipeline import row
from test_manual_eod_yfinance import foreign

OLD=datetime(2026,11,18,1,tzinfo=timezone.utc)
NEW=datetime(2026,11,19,2,tzinfo=timezone.utc)

class VerifiedPrices:
    def __init__(self,days=('2026-11-17',)):
        self.days=days
        self.calls=0
        self.max_calls=64
        self.seen=[]
        self.request_log=[]

    def fetch(self,spec,cutoff,**kwargs):
        if self.calls>=self.max_calls:raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
        self.seen.append(spec['symbol'])
        self.calls+=1
        self.request_log.append(dict(market=spec['market'],
            symbol=spec['symbol'],operation='price',status=200))
        identity=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],
            currency=spec['currency'],instrument_type='EQUITY',
            timezone=spec['provider_timezones'][0])
        if kwargs.get('close_only'):
            return [dict(d=d,c=101.) for d in self.days],identity,0
        return [dict(d=d,o=100.,h=105.,l=98.,c=101.,v=100.)
                for d in self.days],identity,0

def make(rows,provider,*,previous=None,now=OLD,run='old',budget=64):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=now.isoformat()),
        core_commit='a'*40,workflow_run_id='123',cutoff=now,
        clock=lambda:now,provider=provider,previous=previous,
        max_provider_requests=budget)

def test_deferred_candles_take_reserved_refresh_before_known_invalid_ohlc():
    bad=foreign('NYSE:CHRONIC','USD')
    deferred=foreign('NYSE:DEFERRED','USD')
    rows=[bad,deferred]
    old=make(rows,VerifiedPrices())
    old['instruments'][bad['symbol']].update(status='retained',reason='INVALID_REAL_OHLC')
    old['instruments'][deferred['symbol']].update(
        status='retained',reason='PROVIDER_REQUEST_BUDGET_DEFERRED')
    validate_snapshot(old)
    collector=VerifiedPrices(('2026-11-17','2026-11-18'))
    result=make(rows,collector,previous=old,now=NEW,run='new',budget=1)
    assert collector.seen==['NYSE:DEFERRED']
    assert collector.calls==1
    assert result['instruments'][deferred['symbol']]['status']=='current'
    assert result['instruments'][bad['symbol']]['status']=='retained'
    assert result['collection_metrics']['http_requests']==1
    assert result['collection_metrics']['primary_direct_http_cap']==1
    assert list(result['instruments'])==[r['symbol'] for r in rows]

def test_unserved_close_only_follows_missing_prices_ahead_of_old_recipient():
    existing=foreign('NYSE:PREVIOUS','USD')
    unserved=foreign('NYSE:NEWFAIL','USD')
    rows=[existing,unserved]
    entries={r['symbol']:{**specification(r),'status':'retained',
        'reason':'INVALID_REAL_OHLC'} for r in rows}
    prior=deepcopy(entries)
    prior[existing['symbol']]['close_only']=dict(
        d=['2026-11-16','2026-11-17'],c=[100.,101.],
        last_final_session='2026-11-17',verified_at=OLD.isoformat(),
        source='Yahoo Finance verified close-only',
        source_symbol='PREVIOUS',adjustment='yahoo:split-adjusted-OHLC;cash-dividend-unadjusted',
        identity_evidence=dict(provider_symbol='PREVIOUS',provider_exchange='NYQ',
            currency='USD',instrument_type='EQUITY',timezone='America/New_York'),
        history_coverage={})
    provider=VerifiedPrices(('2026-11-17','2026-11-18'))
    result=attach_close_only(entries,rows,prior,provider,NEW,
        clock=lambda:NEW,max_extra_requests=1)
    assert provider.seen==['NYSE:NEWFAIL']
    assert provider.calls==1
    assert result['published_series']==1 and result['direct_http_requests']==1
    assert entries[unserved['symbol']]['close_only']['last_final_session']=='2026-11-18'
    assert entries[existing['symbol']]['close_only']['last_final_session']=='2026-11-17'

def test_multi_symbol_gap_is_reported_as_unverified_source_incident():
    cutoff=datetime(2025,9,23,1,tzinfo=timezone.utc)
    rows=[row(symbol=code,sort_order=i) for i,code in enumerate(
        ('KRX:005930','KRX:000660','KRX:035420'),1)]
    p=VerifiedPrices(('2025-09-18','2025-09-22'))
    doc=make(rows,p,now=cutoff,run='gap')
    report=audit_missing_history(doc)
    assert report['gap_symbol_count']==3
    assert report['missing_historical_session_count']==3
    assert report['common_gap_dates']==[dict(
        market='KR',date='2025-09-19',affected_symbols=3,
        symbols=sorted(r['symbol'] for r in rows),
        review='MULTI_SYMBOL_GAP_SOURCE_COMPARISON_REQUIRED',
        market_calendar_session=True,external_stock_price_verified=False)]
    assert report['published_prices_modified'] is False
    assert not report['external_source_comparison_performed']

