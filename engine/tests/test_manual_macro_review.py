from copy import deepcopy
import json

import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_macro_review import review_catalog, review_instrument
from test_manual_eod_pipeline import row, build, Provider


@pytest.mark.parametrize('symbol,kind,unit,reason',[
    ('SP:SPX','index','지수 포인트','MACRO_INDEX_DEFINITION_SESSION_UNVERIFIED'),
    ('TVC:US10Y','yield','%','MACRO_YIELD_DEFINITION_UNIT_UNVERIFIED'),
    ('FX_IDC:KRWUSD','fx','USD / KRW','MACRO_FX_QUOTE_SESSION_UNVERIFIED'),
    ('NYMEX:CL1!','future','거래소 계약 단위','MACRO_FUTURES_CONTRACT_ROLL_UNVERIFIED'),
    ('TVC:GOLD','commodity','공급자 표시 단위','MACRO_SPOT_SOURCE_UNVERIFIED'),
    ('BITSTAMP:BTCUSD','crypto','USD','MACRO_CRYPTO_VENUE_UNVERIFIED'),
])
def test_source_candidates_never_enable_unreviewed_price_collection(symbol,kind,unit,reason):
    item=row(symbol=symbol,asset_type=kind,unit=unit,chart_style='line');before=deepcopy(item)
    provider=Provider();doc=build([item],provider);entry=doc['instruments'][symbol]
    assert provider.calls==0 and entry['status']=='unsupported' and not entry['bars']
    assert entry['reason']==reason and entry['source_review']['collection_enabled'] is False
    assert entry['source_review']['live_availability']=='not_tested'
    assert item==before


def test_related_contracts_and_inverse_fx_are_explicitly_not_substitutable():
    cases=[('TVC:GOLD','commodity','GC=F','future_not_spot_not_substitutable'),
           ('MCX:COPPER1!','future','HG=F','different_contract_not_substitutable'),
           ('ICEEUR:BRN1!','future','BZ=F','different_contract_not_substitutable'),
           ('FX_IDC:KRWUSD','fx','KRW=X','inverse_quote_not_substitutable')]
    for symbol,kind,ticker,relationship in cases:
        found=review_instrument(row(symbol=symbol,asset_type=kind))['candidates']
        assert found==[dict(provider='Yahoo Finance',symbol=ticker,relationship=relationship,data_kind='ohlc_candidate')]
    direct=review_instrument(row(symbol='FX_IDC:USDKRW',asset_type='fx',unit='KRW / USD'))
    assert direct['candidates'][0]['relationship']=='quote_and_venue_unverified'


def test_treasury_observations_are_not_ohlc_or_etf_prices():
    for symbol,fred in [('TVC:US02Y','DGS2'),('TVC:US10Y','DGS10'),('TVC:US30Y','DGS30')]:
        review=review_instrument(row(symbol=symbol,asset_type='yield',unit='%'))
        series=next(c for c in review['candidates'] if c['provider']=='FRED')
        assert series['symbol']==fred and series['data_kind']=='single_observation'
        assert series['relationship']=='related_constant_maturity_series'
        assert not {'TLT','IEF','^IRX'} & {c['symbol'] for c in review['candidates']}


def test_census_deduplicates_active_macro_memberships_without_mutating_catalog():
    item=row(symbol='NASDAQ:IXIC',asset_type='index',unit='지수 포인트')
    rows=[row(),row(asset_type='etf',symbol='NASDAQ:QQQ'),item,
          {**item,'watch_sector':'다른 분류'},row(symbol='TVC:VIX',asset_type='index',enabled=False)]
    before=deepcopy(rows);report=review_catalog(rows)
    assert report['population']==1 and report['counts_by_asset_type']=={'index':1}
    assert report['http_requests']==0 and report['instruments'][0]['candidates'][0]['symbol']=='^IXIC'
    assert rows==before and review_instrument(row()) is None
    assert review_instrument(row(asset_type='etf')) is None


def test_unknown_macro_does_not_invent_a_provider_ticker():
    result=review_instrument(row(symbol='TVC:UNREVIEWED',asset_type='index'))
    assert result['candidates']==[] and result['collection_enabled'] is False


def test_cli_is_read_only_and_rejects_overwriting_catalog(tmp_path,monkeypatch):
    import sys
    import requests
    from watchlist.manual_macro_review import main
    monkeypatch.setattr(requests.sessions.Session,'request',lambda *a,**k:pytest.fail('review made HTTP request'))
    path=tmp_path/'catalog.csv';raw=csv_bytes([row(symbol='TVC:US10Y',asset_type='yield',unit='%')]);path.write_bytes(raw)
    output=tmp_path/'review.json'
    monkeypatch.setattr(sys,'argv',['review','--catalog',str(path),'--output',str(output)])
    main();report=json.loads(output.read_bytes())
    assert report['population']==1 and len(report['catalog_sha256'])==64 and path.read_bytes()==raw
    monkeypatch.setattr(sys,'argv',['review','--catalog',str(path),'--output',str(path)])
    with pytest.raises(SystemExit):main()
    assert path.read_bytes()==raw

