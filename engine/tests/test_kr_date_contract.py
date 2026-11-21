"""Universe metadata is not a price freshness signal. No provider requests."""
from copy import deepcopy
import hashlib
import json

import pandas as pd
import pytest

from watchlist import trend_watchlist as e


@pytest.mark.parametrize('case', ['current', 'index_delay', 'stock_delay', 'unavailable', 'failed', 'unknown_universe'])
def test_universe_alias_preserves_price_health_and_screening_semantics(monkeypatch, tmp_path, case):
    cfg=deepcopy(e.DEFAULT_CONFIG)
    # Deliberately screen out all stocks by price; this is not a processing error.
    cfg['market']['kr_min_price']=1e9
    cfg['runtime']['request_pause_seconds']=0
    universe=pd.DataFrame([dict(ticker=f'{i:06d}',name='Synthetic',sub_market='KOSPI',
        market_cap=2e12,sector='Tech',macro_theme='Tech',universe_as_of='20261105') for i in (1,2)])
    if case=='unknown_universe':universe=universe.drop(columns='universe_as_of')
    def bars(end):
        return pd.DataFrame(dict(Open=[100.]*220,High=[101.]*220,Low=[99.]*220,
            Close=[100.]*220,Volume=[1000.]*220),index=pd.bdate_range(end=end,periods=220))
    def fetch(ticker,*args,is_index=False,**kwargs):
        if is_index:return bars('2026-11-05' if case=='index_delay' else '2026-11-06')
        if ticker=='000002' and case=='failed':raise ConnectionError('fixture provider failure')
        if ticker=='000002' and case=='unavailable':return pd.DataFrame()
        return bars('2026-11-05' if ticker=='000002' and case=='stock_delay' else '2026-11-06')
    monkeypatch.setattr(e,'get_kr_universe',lambda *_:universe)
    monkeypatch.setattr(e,'KR_HISTORY_CACHE_DIR',tmp_path/'cache')
    monkeypatch.setattr(e,'bulk_sync_kr_recent_ohlcv',lambda *_:{})
    monkeypatch.setattr(e,'kr_history_needs_refresh',lambda *_:False)
    monkeypatch.setattr(e,'prefetch_histories',lambda *a,**k:{})
    monkeypatch.setattr(e,'fetch_kr_history',fetch)
    monkeypatch.setattr(e,'trim_incomplete_bar',lambda hist,*_:hist)
    monkeypatch.setattr(e,'kr_close_check_date',lambda:pd.Timestamp('2026-11-06'))
    *_, regimes, meta=e.run_kr_tracks(cfg,{}, {})
    health=meta['health']
    assert health['as_of']==health['kr_universe_as_of']==('-' if case=='unknown_universe' else '20261105')
    assert health['kr_close_check_date']=='2026-11-06'
    assert health['kr_price_status']==('delayed_or_holiday' if case in ('index_delay','stock_delay','unavailable','failed') else 'current')
    assert health['kr_price_delayed_tickers']==int(case=='stock_delay')
    assert health['kr_price_unavailable_tickers']==int(case in ('unavailable','failed'))
    assert health['kr_failed']==int(case=='failed')
    assert health['kr_processed']==health['kr_stale_tickers']==0
    assert health['kr_index_as_of']['KOSPI']==regimes['KOSPI']['as_of']==('2026-11-05' if case=='index_delay' else '2026-11-06')
    assert health['kr_universe']==2  # Delayed 0 does not mean both passed screening.
    if case=='current':
        # Publication and manifest hash consumers tolerate the additive field;
        # US keeps its own prior session and health dictionary unchanged.
        regimes['SP500']={'as_of':'2026-11-05','regime':'CAUTION','signal_light':'CAUTION'}
        us={'us_failed':0}
        latest=e.publish_artifacts('2026-11-06',{}, {},regimes,{'kr_health':health,'us_health':us},
            pd.DataFrame(),pd.DataFrame(),cfg,base_dir=tmp_path/'published')
        assert latest is not None and e.validate_published_dir(latest)==[]
        m=json.loads((latest/'manifest.json').read_text());s=json.loads((latest/'summary.json').read_text())
        assert s['health']['kr']==health and s['health']['us']==us
        assert m['as_of']['KOSPI']=='2026-11-06' and m['as_of']['SP500']=='2026-11-05'
        assert hashlib.sha256((latest/'summary.json').read_bytes()).hexdigest()==m['files']['summary.json']['sha256']

