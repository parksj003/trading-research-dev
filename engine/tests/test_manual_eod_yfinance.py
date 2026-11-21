"""Offline regressions for bounded yfinance manual EOD acquisition."""
from datetime import datetime, timezone
import pandas as pd
import pytest

from watchlist.manual_eod_cache import inspect_core_cache
from watchlist.manual_eod_contract import digest
from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_provider import Unavailable, specification
from watchlist.manual_eod_publish import build_snapshot
from watchlist.manual_eod_yfinance import (
    BASIS, YFinanceBatch, _ticker_frame, _verified_identity, _final_bars,
)
from test_manual_eod_cache_audit import cache as core_cache
from test_manual_eod_pipeline import row


CUT=datetime(2026,11,12,23,tzinfo=timezone.utc)


def foreign(symbol, unit):
    return row(symbol=symbol,unit=unit,watch_region='해외')


def metadata(spec):
    return dict(symbol=spec['candidates'][0],exchangeName=spec['provider_exchanges'][0],
                exchangeTimezoneName=spec['provider_timezones'][0],
                currency=spec['currency'],instrumentType='ETF' if spec['asset_type']=='etf' else 'EQUITY')


def prices(tickers, *, multi=True):
    dates=pd.DatetimeIndex(['2026-11-11','2026-11-12'])
    df=pd.DataFrame(dict(Open=[100.,101.],High=[105.,106.],Low=[98.,99.],
                         Close=[102.,103.],Volume=[500,600]),index=dates)
    if not multi: return df
    return pd.concat({t:df for t in tickers},axis=1)


class DummyYF:
    def __init__(self, lookup=None, broken=None):
        self.specs=lookup or {}
        self.broken=broken or {}
        self.downloads=[]
        self.metadatas=[]

    def download(self, **kwargs):
        symbols=kwargs['tickers']
        self.downloads.append(kwargs)
        assert kwargs['auto_adjust'] is True
        assert kwargs['interval']=='1d'
        assert kwargs['group_by']=='ticker'
        assert kwargs['threads'] is True
        return prices(symbols)

    def ticker(self, symbol):
        outer=self
        class Ticker:
            def get_history_metadata(self):
                outer.metadatas.append(symbol)
                result=metadata(outer.specs[symbol])
                result.update(outer.broken.get(symbol,{}))
                return result
        return Ticker()


class NoDirectYahoo:
    calls=0
    request_log=[]
    def fetch(self,*args,**kwargs):
        self.calls+=1
        raise AssertionError('direct Yahoo must not run for a staged yfinance ticker')


def snapshot(rows,yf,*,previous=None,cache_inspections=None,data_dir=None,run='20261112T230000Z-one'):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=CUT.isoformat()),core_commit='a'*40,
        workflow_run_id='1',cutoff=CUT,provider=NoDirectYahoo(),
        previous=previous,clock=lambda:CUT,data_dir=data_dir,
        cache_inspections=cache_inspections,yf_batch=yf)


@pytest.mark.parametrize('sym,unit',[
    ('NYSE:IBM','USD'),('NASDAQ:NVDA','USD'),
    ('TSE:7203','JPY'),('SSE:600000','CNY'),('SZSE:000001','CNY'),
    ('HKEX:700','HKD'),('XETR:SAP','EUR'),('SIX:NESN','CHF'),
    ('BME:BBVA','EUR'),
])
def test_all_reviewed_foreign_markets_have_strict_metadata_and_real_ohlc(sym,unit):
    item=foreign(sym,unit)
    spec=specification(item)
    fake=DummyYF({spec['candidates'][0]:spec})
    batch=YFinanceBatch(fake.download,fake.ticker,max_symbols=1)
    batch.prepare([(spec,False)],CUT)
    result=batch.get(spec)
    assert result['identity']['provider_symbol']==spec['candidates'][0]
    assert len(result['rows'])==2
    assert result['rows'][-1]==dict(d='2026-11-12',o=101.,h=106.,l=99.,c=103.,v=600.)
    assert batch.batches==1 and batch.identity_checks==1


@pytest.mark.parametrize('field,bad,reason',[
    ('symbol','WRONG','SYMBOL'),
    ('currency','USD','CURRENCY'),
    ('exchangeName','NYQ','EXCHANGE'),
    ('instrumentType','ETF','ASSET_TYPE'),
    ('exchangeTimezoneName','America/New_York','TIMEZONE'),
])
def test_metadata_identity_mismatch_fail_closed(field,bad,reason):
    spec=specification(foreign('TSE:7203','JPY'))
    identity=metadata(spec)
    identity[field]=bad
    with pytest.raises(Unavailable,match=reason):
        _verified_identity(identity,spec)


def test_ticker_extraction_supports_only_exact_multicolumn_and_no_ambiguous_fallback():
    df=prices(['IBM','SAP.DE'])
    assert _ticker_frame(df,'IBM',2)['Close'].tolist()==[102.,103.]
    assert _ticker_frame(df.swaplevel(0,1,axis=1),'SAP.DE',2)['Close'].tolist()==[102.,103.]
    assert _ticker_frame(df,'AMD',2) is None
    assert _ticker_frame(prices(['IBM'],multi=False),'IBM',1)['High'].tolist()==[105.,106.]


def test_unfinalized_session_not_published_and_ohlc_not_filled():
    spec=specification(foreign('NYSE:IBM','USD'))
    frame=prices(['IBM'],multi=False)
    early=datetime(2026,11,12,20,59,tzinfo=timezone.utc)
    assert [x['d'] for x in _final_bars(frame,spec,early)]==['2026-11-11']
    frame.loc[pd.Timestamp('2026-11-12'),'Low']=None
    assert [x['d'] for x in _final_bars(frame,spec,CUT)]==['2026-11-11']
    frame.loc[pd.Timestamp('2026-11-12'),'Low']=110.
    with pytest.raises(Unavailable,match='OHLC'):
        _final_bars(frame,spec,CUT)


def test_publisher_uses_yfinance_adjusted_basis_and_no_direct_http_for_staged():
    rows=[foreign('NYSE:IBM','USD'),foreign('TSE:7203','JPY'),
          foreign('HKEX:700','HKD')]
    specs=[specification(x) for x in rows]
    fake=DummyYF({s['candidates'][0]:s for s in specs})
    yf=YFinanceBatch(fake.download,fake.ticker,max_symbols=3)
    doc=snapshot(rows,yf)
    assert doc['catalog_order']==[r['symbol'] for r in rows]
    assert doc['collection_metrics']['yfinance_batches']==3
    assert doc['collection_metrics']['yfinance_staged']==3
    assert doc['collection_metrics']['yfinance_published']==3
    assert doc['collection_metrics']['http_requests']==0
    for row_ in rows:
        e=doc['instruments'][row_['symbol']]
        assert e['status']=='current'
        assert e['adjustment']==BASIS
        assert e['source']=='yfinance adjusted daily batch'
        assert e['acquisition']=='yfinance_batch'
        assert e['last_final_session']=='2026-11-12'
        assert e['identity_evidence']['provider_symbol']==e['source_symbol']
        assert e['listing_identity']['evidence']=='yahoo-chart-meta-v1'


def test_verified_core_us_cache_needs_only_yfinance_identity_not_prices(tmp_path):
    item=foreign('NYSE:IBM','USD')
    core_cache(tmp_path,item)
    spec=specification(item)
    inspection=inspect_core_cache(tmp_path,spec,CUT)
    assert inspection.classification=='identity_pending'
    fake=DummyYF({spec['candidates'][0]:spec})
    def no_download(**kwargs):
        raise AssertionError('valid core OHLC must not be re-downloaded')
    yf=YFinanceBatch(no_download,fake.ticker,max_symbols=1)
    doc=snapshot([item],yf,cache_inspections={item['symbol']:inspection},data_dir=tmp_path)
    e=doc['instruments'][item['symbol']]
    assert e['status']=='current'
    assert e['acquisition']=='core_cache'
    assert e['bars']['c']==[104.]
    assert e['adjustment']==BASIS
    assert fake.metadatas==['IBM']
    assert yf.batches==0
    assert doc['collection_metrics']['http_requests']==0


def test_missing_listing_evidence_never_promotes_downloaded_prices():
    item=foreign('TSE:7203','JPY')
    spec=specification(item)
    fake=DummyYF({spec['candidates'][0]:spec},
                 broken={spec['candidates'][0]:{'currency':'USD'}})
    yf=YFinanceBatch(fake.download,fake.ticker,max_symbols=1)
    doc=snapshot([item],yf)
    e=doc['instruments'][item['symbol']]
    assert e['status']=='missing' and not e['bars']
    assert e['reason']=='YFINANCE_CURRENCY_MISMATCH'
    assert e['yfinance_staged'] is True
    assert e['last_attempt_at']==CUT.isoformat()
    assert doc['collection_metrics']['http_requests']==0


def test_bounded_stage_does_not_ask_yfinance_to_download_every_watchlist_name():
    rows=[foreign('NYSE:SIM'+str(i),'USD') for i in range(30)]
    specs=[specification(x) for x in rows]
    fake=DummyYF({s['candidates'][0]:s for s in specs})
    yf=YFinanceBatch(fake.download,fake.ticker,max_symbols=4,batch_size=4)
    class Direct:
        calls=0
        request_log=[]
        def fetch(self,*args,**kw):
            self.calls+=1
            raise Unavailable('PROVIDER_RATE_LIMITED')
    provider=Direct()
    doc=build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id='20261112T230000Z-a',generated_at=CUT.isoformat()),
        core_commit='a'*40,workflow_run_id='1',cutoff=CUT,provider=provider,
        clock=lambda:CUT,yf_batch=yf,max_provider_requests=1)
    assert yf.batches==1
    assert len(yf.attempted)==4
    assert doc['collection_metrics']['yfinance_staged']==4
    assert doc['collection_metrics']['yfinance_published']==4
    assert sum(e['status']=='current' for e in doc['instruments'].values())==4
    assert provider.calls==1
    assert doc['catalog_order']==[r['symbol'] for r in rows]


def test_same_core_run_replay_does_not_stage_or_download():
    item=foreign('NYSE:IBM','USD');s=specification(item)
    first=YFinanceBatch(DummyYF({s['candidates'][0]:s}).download,
                       DummyYF({s['candidates'][0]:s}).ticker,max_symbols=1)
    old=snapshot([item],first)
    class ShouldNotDownload:
        def prepare(self,*a,**kw): raise AssertionError('must not prepare')
    replay=snapshot([item],ShouldNotDownload(),previous=old)
    assert replay is old


def test_adjusted_batch_replaces_raw_price_history_without_cross_basis_merge():
    from watchlist.manual_eod_provider import BASIS as RAW_BASIS
    item=foreign('NYSE:IBM','USD')
    spec=specification(item)
    class Raw:
        calls=0
        request_log=[]
        def fetch(self,spec,cutoff,**kwargs):
            self.calls+=1
            return [dict(d='2026-11-12',o=200.,h=210.,l=195.,c=205.,v=700.)],dict(
                provider_symbol=spec['candidates'][0],provider_exchange=spec['provider_exchanges'][0],
                currency=spec['currency'],instrument_type='EQUITY',timezone=spec['provider_timezones'][0]),0
    old=build_snapshot([item],catalog_sha256=digest(csv_bytes([item])),
        core=dict(run_id='20261112T230000Z-old',generated_at=CUT.isoformat()),
        core_commit='a'*40,workflow_run_id='1',cutoff=CUT,provider=Raw(),clock=lambda:CUT)
    assert old['instruments'][item['symbol']]['adjustment']==RAW_BASIS
    # Newly verified yfinance prices have different historical scale and must
    # REPLACE rather than splice the dividend-unadjusted raw Yahoo history.
    fake=DummyYF({spec['candidates'][0]:spec})
    batch=YFinanceBatch(fake.download,fake.ticker,max_symbols=1)
    next_cut=datetime(2026,11,13,23,tzinfo=timezone.utc)
    updated=build_snapshot([item],catalog_sha256=digest(csv_bytes([item])),
        core=dict(run_id='20261113T230000Z-new',generated_at=next_cut.isoformat()),
        core_commit='b'*40,workflow_run_id='2',cutoff=next_cut,
        provider=NoDirectYahoo(),previous=old,clock=lambda:next_cut,yf_batch=batch)
    entry=updated['instruments'][item['symbol']]
    assert entry['adjustment']==BASIS
    assert entry['bars']['c']==[102.,103.]
    assert 205. not in entry['bars']['c']
    assert entry['last_final_session']=='2026-11-12'
    assert entry['status']=='stale'


def test_failed_next_day_yfinance_staging_preserves_previous_adjusted_chart():
    item=foreign('NYSE:IBM','USD')
    spec=specification(item)
    fake=DummyYF({spec['candidates'][0]:spec})
    old=snapshot([item],YFinanceBatch(fake.download,fake.ticker,max_symbols=1))
    old_entry=old['instruments'][item['symbol']]
    broken=DummyYF({spec['candidates'][0]:spec},
                   broken={spec['candidates'][0]:{'exchangeName':'KOE'}})
    next_cut=datetime(2026,11,13,23,tzinfo=timezone.utc)
    updated=build_snapshot([item],catalog_sha256=digest(csv_bytes([item])),
        core=dict(run_id='20261113T230000Z-next',generated_at=next_cut.isoformat()),
        core_commit='b'*40,workflow_run_id='2',cutoff=next_cut,
        provider=NoDirectYahoo(),previous=old,clock=lambda:next_cut,
        yf_batch=YFinanceBatch(broken.download,broken.ticker,max_symbols=1))
    entry=updated['instruments'][item['symbol']]
    assert entry['status']=='retained'
    assert entry['reason']=='YFINANCE_EXCHANGE_MISMATCH'
    assert entry['bars']==old_entry['bars']
    assert entry['adjustment']==BASIS
    assert entry['last_success_at']==old_entry['last_success_at']
    assert entry['last_final_session']==old_entry['last_final_session']

