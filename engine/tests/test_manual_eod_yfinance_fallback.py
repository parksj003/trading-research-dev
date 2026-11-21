"""Regressions for real 2026-11-19 yfinance failures (12 metadata / 4 OHLC).

The working direct curl_cffi Yahoo collector must remain reachable after a
failed opportunistic yfinance stage. Adjusted prices need independently
attested native listing metadata, never guessed Yahoo ticker mappings.
"""
from datetime import datetime, timezone

import pandas as pd
import pytest

from test_manual_eod_yfinance import CUT, DummyYF, foreign, metadata, prices
from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_provider import BASIS as RAW_BASIS
from watchlist.manual_eod_provider import RateLimited, Unavailable, specification
from watchlist.manual_eod_publish import build_snapshot
from watchlist.manual_eod_yfinance import BASIS as YF_BASIS, YFinanceBatch, _verified_identity


class NoMetadata(DummyYF):
    def ticker(self, symbol):
        class Ticker:
            def get_history_metadata(self):
                # Reproduces the missing/incomplete metadata observed for
                # 12/16 operational batch candidates, without falsifying it.
                return {}
        return Ticker()


class BadCandles(DummyYF):
    def download(self, **kwargs):
        self.downloads.append(kwargs)
        frames=prices(kwargs['tickers'])
        column=(kwargs['tickers'][0], 'High')
        frames.loc[pd.Timestamp('2026-11-12'),column]=0
        return frames


class Direct:
    def __init__(self, fail=False):
        self.calls=0
        self.operations=[]
        self.request_log=[]
        self.fail=fail

    def fetch(self,spec,instant,**kwargs):
        self.calls+=1
        self.operations.append((spec['symbol'],kwargs))
        self.request_log.append(dict(market=spec['market'],symbol=spec['symbol'],
            operation='identity' if kwargs.get('metadata_only') else 'price',status=429 if self.fail else 200))
        if self.fail:
            raise RateLimited('PROVIDER_RATE_LIMITED')
        evidence=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],currency=spec['currency'],
            instrument_type='ETF' if spec['asset_type']=='etf' else 'EQUITY',
            timezone=spec['provider_timezones'][0])
        if kwargs.get('metadata_only'):
            return [],evidence,0
        return [dict(d='2026-11-12',o=200.,h=210.,l=195.,c=205.,v=900.)],evidence,0


def snapshot(rows, provider, yf_batch=None, *, prior=None, cutoff=CUT, run='20261112T230000Z-one'):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=cutoff.isoformat()),core_commit='a'*40,
        workflow_run_id='123',cutoff=cutoff,provider=provider,
        previous=prior,clock=lambda:cutoff,yf_batch=yf_batch)


@pytest.mark.parametrize('symbol,currency',[
    ('NYSE:JPM','USD'), ('TSE:9983','JPY'), ('HKEX:1299','HKD'),
    ('SSE:600859','CNY'), ('XETR:VOW','EUR'),
])
def test_unavailable_yfinance_metadata_recovers_using_direct_identity_only(symbol,currency):
    item=foreign(symbol,currency)
    spec=specification(item)
    fake=NoMetadata({spec['candidates'][0]:spec})
    yf=YFinanceBatch(fake.download,fake.ticker,max_symbols=1)
    provider=Direct()
    result=snapshot([item],provider,yf)
    entry=result['instruments'][symbol]
    assert provider.calls==1
    assert provider.operations==[(symbol,dict(metadata_only=True))]
    assert entry['status']=='current'
    assert entry['acquisition']=='yfinance_batch'
    assert entry['adjustment']==YF_BASIS
    assert entry['bars']['c']==[102.,103.]
    assert entry['source_symbol']==spec['candidates'][0]
    assert entry['listing_identity']['evidence']=='yahoo-chart-meta-v1'
    assert entry['yfinance_identity_source']=='direct_chart_metadata'
    assert result['collection_metrics']['yfinance_identity_assisted']==1
    assert result['collection_metrics']['yfinance_verified']==1
    assert result['collection_metrics']['yfinance_published']==1
    assert result['collection_metrics']['http_requests']==1
    assert yf.diagnostics()['identity_verified_by_direct']==1


def test_recent_previous_listing_identity_attests_yfinance_without_extra_http():
    item=foreign('NYSE:IBM','USD')
    initial=snapshot([item],Direct())
    assert initial['instruments'][item['symbol']]['adjustment']==RAW_BASIS
    later=datetime(2026,11,13,23,tzinfo=timezone.utc)
    spec=specification(item)
    yf=YFinanceBatch(NoMetadata({spec['candidates'][0]:spec}).download,
        NoMetadata({spec['candidates'][0]:spec}).ticker,max_symbols=1)
    direct=Direct(fail=True)
    result=snapshot([item],direct,yf,prior=initial,cutoff=later,run='20261113T230000Z-two')
    entry=result['instruments'][item['symbol']]
    assert direct.calls==0
    assert entry['adjustment']==YF_BASIS
    assert entry['bars']['c']==[102.,103.]
    assert entry['last_final_session']=='2026-11-12'
    assert entry['status']=='stale'
    assert entry['yfinance_identity_source']=='previous_snapshot'
    assert result['collection_metrics']['yfinance_identity_assisted']==1
    assert yf.diagnostics()['identity_verified_by_previous']==1


def test_invalid_yfinance_candle_falls_back_to_real_direct_ohlc_not_missing():
    item=foreign('SSE:601766','CNY')
    spec=specification(item)
    fake=BadCandles({spec['candidates'][0]:spec})
    yf=YFinanceBatch(fake.download,fake.ticker,max_symbols=1)
    direct=Direct()
    doc=snapshot([item],direct,yf)
    e=doc['instruments'][item['symbol']]
    assert e['status']=='current'
    assert e['acquisition']=='provider'
    assert e['adjustment']==RAW_BASIS
    assert e['bars']['c']==[205.]
    assert direct.operations==[(item['symbol'],{'start':None})]
    assert e['yfinance_failure_reason']=='YFINANCE_INVALID_OHLC'
    assert yf.diagnostics()['verified']==0


def test_yfinance_bad_ohlc_and_provider_rate_limit_retains_old_unmodified_prices():
    item=foreign('SSE:601766','CNY')
    old=snapshot([item],Direct())
    original=old['instruments'][item['symbol']]
    spec=specification(item)
    fake=BadCandles({spec['candidates'][0]:spec})
    later=datetime(2026,11,13,23,tzinfo=timezone.utc)
    doc=snapshot([item],Direct(fail=True),
        YFinanceBatch(fake.download,fake.ticker,max_symbols=1),
        prior=old,cutoff=later,run='20261113T230000Z-two')
    new=doc['instruments'][item['symbol']]
    assert new['status']=='retained'
    assert new['reason']=='PROVIDER_RATE_LIMITED'
    assert new['bars']==original['bars']
    assert new['adjustment']==RAW_BASIS
    assert new['last_success_at']==original['last_success_at']
    assert new['last_final_session']==original['last_final_session']
    assert new['yfinance_failure_reason']=='YFINANCE_INVALID_OHLC'


def test_explicit_yfinance_wrong_currency_cannot_be_attested_as_adjusted_prices():
    item=foreign('TSE:7203','JPY')
    spec=specification(item)
    broken=DummyYF({spec['candidates'][0]:spec},
        broken={spec['candidates'][0]:{'currency':'USD'}})
    direct=Direct()
    doc=snapshot([item],direct,YFinanceBatch(broken.download,broken.ticker,max_symbols=1))
    entry=doc['instruments'][item['symbol']]
    assert direct.calls==0
    assert entry['status']=='missing'
    assert entry['reason']=='YFINANCE_CURRENCY_MISMATCH'
    assert not entry['bars']


def test_missing_yfinance_metadata_fields_are_unavailable_not_identity_conflicts():
    spec=specification(foreign('HKEX:1299','HKD'))
    with pytest.raises(Unavailable,match='LISTING_METADATA_UNAVAILABLE'):
        _verified_identity(dict(symbol=spec['candidates'][0],currency='HKD'),spec)
    with pytest.raises(Unavailable,match='CURRENCY_MISMATCH'):
        _verified_identity({**metadata(spec),'currency':'USD'},spec)

