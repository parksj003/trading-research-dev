"""Regression: 20 deferred US stocks should not queue behind 3 bad listings."""
from datetime import datetime, timedelta, timezone
import copy

import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_provider import (
    InvalidPriceOHLC, YahooDaily, Unavailable, bars_of, specification,
)
from watchlist.manual_eod_publish import build_snapshot
from test_manual_eod_pipeline import row
from test_manual_eod_provider import doc as yahoo_doc
from test_manual_eod_yfinance import foreign

CUT=datetime(2026,11,19,2,20,tzinfo=timezone.utc)
US=[foreign('NYSE:BACK'+str(i),'USD') for i in range(20)]
ANOMALIES=[foreign('AMEX:X','USD'),foreign('HKEX:3931','HKD'),
           row(symbol='KRX:322510',unit='KRW')]


class MockPrice:
    def __init__(self, seed=False, final=True):
        self.seed=seed
        self.final=final
        self.calls=0
        self.request_log=[]
        self.seen=[]

    def fetch(self,spec,cutoff,*,start=None,metadata_only=False):
        symbol=spec['symbol']
        self.seen.append(symbol)
        if self.seed and symbol.startswith('NYSE:BACK'):
            raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
        self.calls+=1
        self.request_log.append(dict(market=spec['market'],symbol=symbol,
            operation='identity' if metadata_only else 'price',status=200))
        if self.seed and symbol.startswith('AMEX:'):
            raise Unavailable('PROVIDER_SYMBOL_NOT_FOUND')
        if self.seed and symbol in ('HKEX:3931','KRX:322510'):
            raise Unavailable('INVALID_REAL_OHLC')
        identity=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],
            currency=spec['currency'],timezone=spec['provider_timezones'][0],
            instrument_type='EQUITY')
        rows=[dict(d='2026-11-17',o=100.,h=105.,l=98.,c=102.,v=500.)]
        if self.final:
            rows.append(dict(d='2026-11-18',o=101.,h=106.,l=99.,c=103.,v=600.))
        return rows,identity,0


def snapshot(rows,provider,*,previous=None,budget=64,run='one',now=CUT):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=now.isoformat()),
        core_commit='a'*40,workflow_run_id='123',cutoff=now,
        clock=lambda:now,provider=provider,previous=previous,
        max_provider_requests=budget)


def test_20_deferred_us_entries_are_collected_before_known_bad_tickers():
    rows=US+ANOMALIES
    seed=MockPrice(seed=True)
    old=snapshot(rows,seed,run='one')
    assert all(old['instruments'][r['symbol']]['reason']=='PROVIDER_REQUEST_BUDGET_DEFERRED'
               for r in US)
    assert old['instruments']['AMEX:X']['reason']=='PROVIDER_SYMBOL_NOT_FOUND'
    assert all(old['instruments'][r]['reason']=='INVALID_REAL_OHLC'
               for r in ('HKEX:3931','KRX:322510'))
    recovery=MockPrice()
    newer=snapshot(rows,recovery,previous=old,budget=4,
                   run='two',now=CUT+timedelta(minutes=10))
    assert recovery.seen==[r['symbol'] for r in US[:4]] or (
        len(recovery.seen)==4 and all(sym.startswith('NYSE:BACK') for sym in recovery.seen))
    assert newer['collection_metrics']['http_requests']==4
    assert newer['collection_metrics']['previous_budget_deferred']==20
    assert newer['collection_metrics']['previous_budget_retried']==4
    assert newer['collection_metrics']['previous_budget_recovered']==4
    assert newer['collection_metrics']['source_integrity_failures']==0
    assert all(newer['instruments'][r['symbol']]['status']=='current'
               for r in US if r['symbol'] in recovery.seen)
    assert all(newer['instruments'][sym]['status']=='missing'
               for sym in ('AMEX:X','HKEX:3931','KRX:322510'))
    assert list(newer['instruments'])==[r['symbol'] for r in rows]


def test_stale_us_chart_promotes_only_on_confirmed_new_final_daily_ohlc():
    item=foreign('NYSE:NVDA','USD')
    provider=MockPrice(final=False)
    old=snapshot([item],provider,run='old')
    entry=old['instruments'][item['symbol']]
    assert entry['status']=='stale' and entry['last_final_session']=='2026-11-17'
    after=CUT+timedelta(hours=2,minutes=5)
    collector=MockPrice(final=True)
    new=snapshot([item],collector,previous=old,run='new',now=after)
    latest=new['instruments'][item['symbol']]
    assert latest['status']=='current'
    assert latest['last_final_session']=='2026-11-18'
    assert latest['bars']['d']==['2026-11-17','2026-11-18']
    assert new['collection_metrics']['previous_stale_recovered']==1
    assert collector.calls==1


def test_invalid_historical_candle_is_rejected_and_date_rule_identified():
    raw=yahoo_doc()
    candles=raw['chart']['result'][0]
    candles['indicators']['quote'][0]['close'][0]=120
    with pytest.raises(InvalidPriceOHLC) as captured:
        bars_of(candles,specification(row()),datetime(2026,11,12,10,tzinfo=timezone.utc))
    err=captured.value
    assert str(err)=='INVALID_REAL_OHLC'
    detail=err.diagnostic('005930.KS')
    assert {k:detail[k] for k in ('date','rule','candidate')}==dict(
        date='2026-11-11',rule='HIGH_BELOW_OPEN_OR_CLOSE',candidate='005930.KS')
    assert detail['source_ohlc']['c']==120
    assert detail['violation_absolute']==14
    assert not hasattr(err,'skipped')


def test_yahoo_request_log_records_real_price_integrity_magnitude_only_in_job_diagnostics():
    class Response:
        status_code=200
        def json(self):
            data=yahoo_doc()
            data['chart']['result'][0]['indicators']['quote'][0]['close'][0]=120
            return data
    class Session:
        def get(self,*args,**kwargs):return Response()
    provider=YahooDaily(session=Session(),pause=0)
    with pytest.raises(InvalidPriceOHLC,match='INVALID_REAL_OHLC'):
        provider.fetch(specification(row()),datetime(2026,11,12,10,tzinfo=timezone.utc))
    request=provider.request_log[0]
    detail=request['price_integrity_failure']
    assert detail['date']=='2026-11-11' and detail['rule']=='HIGH_BELOW_OPEN_OR_CLOSE'
    assert detail['candidate']=='005930.KS'
    assert detail['source_ohlc']['c']==120
    assert detail['violation_absolute']==14
    assert request['status']==200
    assert not any(k in request for k in ('o','h','l','c','v'))

