from copy import deepcopy
import pandas as pd
import pytest
import requests

from watchlist import trend_watchlist as watchlist

OFFICIAL = 'https://api.nasdaq.com/api/quote/list-type/nasdaq100'
SP500 = 'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
NASDAQ_WIKI = 'https://en.wikipedia.org/wiki/Nasdaq-100'


def official_payload(count=101):
    rows = [dict(symbol=f'N{i:03d}', companyName=f'Synthetic Nasdaq Company {i}',
                 sector='Technology', lastSalePrice='$999.00') for i in range(count)]
    return {'status': {'rCode': 200}, 'data': {'totalrecords': str(count), 'offset': 0,
            'data': {'rows': rows}}}


class Response:
    def __init__(self, payload=None, text='', status=200):
        self.payload, self.text, self.status = payload, text, status

    def raise_for_status(self):
        if self.status != 200:
            raise requests.HTTPError('synthetic source failure')

    def json(self):
        return self.payload


@pytest.mark.parametrize('count', [100, 101])
def test_official_membership_is_complete_and_does_not_supply_prices(monkeypatch, count):
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return Response(official_payload(count))
    monkeypatch.setattr(requests, 'get', get)
    table, symbol, company, sector, industry = watchlist._nasdaq100_official_table({'User-Agent': 'synthetic'})
    assert len(table) == count and (symbol, company, sector, industry) == ('symbol', 'companyName', 'sector', None)
    assert list(table.columns) == ['symbol', 'companyName', 'sector']
    assert calls[0][0] == OFFICIAL and calls[0][1]['headers']['Accept'] == 'application/json'
    assert len(calls) == 1


@pytest.mark.parametrize('fault', ['partial_page', 'too_few', 'too_many', 'duplicate', 'bad_symbol',
                                  'missing_name', 'error_status', 'missing_rows', 'offset', 'missing_symbol',
                                  'null_symbol', 'nan_symbol', 'blank_symbol', 'sentinel_symbol'])
def test_http_200_incomplete_or_invalid_official_responses_are_rejected(monkeypatch, fault):
    payload = official_payload()
    rows = payload['data']['data']['rows']
    if fault == 'partial_page': rows.pop()
    elif fault == 'too_few': payload = official_payload(99)
    elif fault == 'too_many': payload = official_payload(111)
    elif fault == 'duplicate': rows[-1]['symbol'] = rows[0]['symbol']
    elif fault == 'bad_symbol': rows[0]['symbol'] = 'BAD/SYMBOL'
    elif fault == 'null_symbol': rows[0]['symbol'] = None
    elif fault == 'nan_symbol': rows[0]['symbol'] = float('nan')
    elif fault == 'blank_symbol': rows[0]['symbol'] = ' '
    elif fault == 'sentinel_symbol': rows[0]['symbol'] = 'NaN'
    elif fault == 'missing_name': rows[0]['companyName'] = None
    elif fault == 'error_status': payload['status']['rCode'] = 500
    elif fault == 'missing_rows': payload['data']['data'] = {}
    elif fault == 'offset': payload['data']['offset'] = 25
    elif fault == 'missing_symbol':
        for row in rows: row.pop('symbol')
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Response(payload))
    with pytest.raises(ValueError):
        watchlist._nasdaq100_official_table({})


@pytest.fixture
def universe_context(tmp_path, monkeypatch):
    cfg = deepcopy(watchlist.DEFAULT_CONFIG)
    monkeypatch.setattr(watchlist, 'UNIVERSE_CACHE_DIR', tmp_path)
    monkeypatch.setattr(watchlist, 'is_cache_fresh', lambda path, *a: path.exists())
    monkeypatch.setattr(watchlist, 'classify_theme_and_sector', lambda *a, **k: ('Theme', 'Sector', []))
    return cfg, tmp_path


def wiki_table(prefix, count):
    return pd.DataFrame({'Symbol': [f'{prefix}{i:03d}' for i in range(count)],
                         'Company': [f'Synthetic Company {i}' for i in range(count)],
                         'GICS Sector': ['Technology'] * count}).to_html(index=False)


def cache_path(cfg, directory):
    sources = '_'.join(sorted(cfg['universe']['us_sources']))
    return directory / f'us_universe_v15_{sources}_{watchlist.get_config_version_hash(cfg)}.pkl'


def complete_cache():
    return pd.DataFrame([dict(ticker=f'S{i:03d}', name='Synthetic S&P Company', in_sp500=True,
                              in_nasdaq100=False) for i in range(503)] +
                        [dict(ticker=f'N{i:03d}', name='Synthetic Nasdaq Company', in_sp500=False,
                              in_nasdaq100=True) for i in range(101)])


def test_collection_uses_official_nasdaq_and_existing_sp500_then_reuses_validated_cache(universe_context, monkeypatch):
    cfg, directory = universe_context
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url == SP500: return Response(text=wiki_table('S', 503))
        if url == OFFICIAL: return Response(official_payload())
        pytest.fail('Working official source must not request the broken Nasdaq Wikipedia table')
    monkeypatch.setattr(requests, 'get', get)
    frame = watchlist.get_us_universe(cfg)
    assert frame.in_sp500.sum() == 503 and frame.in_nasdaq100.sum() == 101
    assert not frame.is_stale_cache.any() and len(frame.ticker.unique()) == 604
    assert set(calls) == {SP500, OFFICIAL} and len(calls) == 2
    assert cache_path(cfg, directory).exists()
    monkeypatch.setattr(requests, 'get', lambda *a, **k: pytest.fail('Fresh valid cache must avoid network calls'))
    cached = watchlist.get_us_universe(cfg)
    assert cached.ticker.tolist() == frame.ticker.tolist() and not cached.is_stale_cache.any()


def test_official_failure_uses_a_complete_wikipedia_table(universe_context, monkeypatch):
    cfg, _ = universe_context
    def get(url, **kwargs):
        if url == OFFICIAL: return Response(status=503)
        return Response(text=wiki_table('S', 503) if url == SP500 else wiki_table('N', 101))
    monkeypatch.setattr(requests, 'get', get)
    frame = watchlist.get_us_universe(cfg)
    assert frame.in_nasdaq100.sum() == 101 and not frame.is_stale_cache.any()


@pytest.mark.parametrize('has_cache', [False, True])
def test_both_sources_incomplete_never_save_partial_data_or_destroy_old_cache(universe_context, monkeypatch, has_cache):
    cfg, directory = universe_context
    old = directory / 'us_universe_previous.pkl'
    before = None
    if has_cache:
        complete_cache().to_pickle(old)
        before = old.read_bytes()
    def get(url, **kwargs):
        if url == OFFICIAL: return Response(official_payload(25))
        return Response(text=wiki_table('S', 503) if url == SP500 else '<table><tr><th>Other</th></tr><tr><td>1</td></tr></table>')
    monkeypatch.setattr(requests, 'get', get)
    if has_cache:
        frame = watchlist.get_us_universe(cfg)
        assert frame.is_stale_cache.all() and frame.in_nasdaq100.sum() == 101
        assert old.read_bytes() == before
    else:
        with pytest.raises(RuntimeError, match='미국 유니버스'):
            watchlist.get_us_universe(cfg)
    assert not cache_path(cfg, directory).exists()


def test_incomplete_fresh_cache_is_refreshed_instead_of_silently_accepted(universe_context, monkeypatch):
    cfg, directory = universe_context
    invalid = complete_cache()
    invalid['in_nasdaq100'] = False
    invalid.to_pickle(cache_path(cfg, directory))
    def get(url, **kwargs):
        return Response(official_payload()) if url == OFFICIAL else Response(text=wiki_table('S', 503))
    monkeypatch.setattr(requests, 'get', get)
    frame = watchlist.get_us_universe(cfg)
    assert frame.in_nasdaq100.sum() == 101 and not frame.is_stale_cache.any()


def test_invalid_old_cache_cannot_bypass_partial_collection_guard(universe_context, monkeypatch):
    cfg, directory = universe_context
    invalid = complete_cache().query('in_sp500')
    old = directory / 'us_universe_incomplete.pkl'
    invalid.to_pickle(old)
    before = old.read_bytes()
    monkeypatch.setattr(requests, 'get', lambda *a, **k: Response(status=503))
    with pytest.raises(RuntimeError, match='미국 유니버스'):
        watchlist.get_us_universe(cfg)
    assert old.read_bytes() == before

