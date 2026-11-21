"""Regression checks for US manual charts without weakening EOD provenance."""
from datetime import datetime, timezone

import requests

from watchlist.manual_eod_cache import (
    MIN_VERIFIED_BASIS_BARS, attest_identity, inspect_core_cache,
)
from watchlist.manual_eod_provider import (
    RateLimited, Unavailable, YahooDaily, specification,
)
from watchlist.manual_eod_sessions import calendar
from test_manual_eod_cache_audit import cache, cutoff, identity, us
from test_manual_eod_pipeline import row


class Response:
    def __init__(self, status_code, doc=None):
        self.status_code = status_code
        self.doc = doc

    def json(self):
        return self.doc


def identity_doc(spec):
    return {'chart': {'error': None, 'result': [{
        'meta': {'symbol': spec['candidates'][0],
                 'exchangeName': spec['provider_exchanges'][0],
                 'exchangeTimezoneName': spec['provider_timezones'][0],
                 'currency': spec['currency'],
                 'instrumentType': 'ETF' if spec['asset_type'] == 'etf' else 'EQUITY',
                 'dataGranularity': '1d'}
    }]}}


def test_korean_429_does_not_skip_us_identity_verification():
    korean = specification(row())
    american = specification(us())

    class Session:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if '005930.' in url:
                return Response(429)
            return Response(200, identity_doc(american))

    session = Session()
    provider = YahooDaily(session, pause=0)
    try:
        provider.fetch(korean, cutoff(), metadata_only=True)
        assert False, 'KR must report a rate limit'
    except RateLimited as exc:
        assert str(exc) == 'PROVIDER_RATE_LIMITED'
    try:
        provider.fetch(korean, cutoff(), metadata_only=True)
        assert False, 'KR circuit must stay open'
    except RateLimited as exc:
        assert str(exc) == 'PROVIDER_RATE_LIMIT_CIRCUIT_OPEN'

    prices, actual, _ = provider.fetch(american, cutoff(), metadata_only=True)
    assert prices == []
    assert actual['provider_symbol'] == 'IBM'
    assert provider.calls == 2
    assert [r['market'] for r in provider.request_log] == ['KR', 'US']
    assert all('query2.finance.yahoo.com' in call[0] for call in session.calls)
    assert all(call[1]['headers']['User-Agent'].startswith('Mozilla/') for call in session.calls)


def test_us_403_does_not_block_korean_market():
    american = specification(us())
    korean = specification(row())

    class Session:
        def get(self, url, **kwargs):
            return Response(403) if '/IBM?' not in url and '/IBM' in url else Response(200, identity_doc(korean))

    provider = YahooDaily(Session(), pause=0)
    try:
        provider.fetch(american, cutoff(), metadata_only=True)
        assert False
    except Unavailable as exc:
        assert str(exc) == 'PROVIDER_ACCESS_DENIED'
    assert provider.fetch(korean, cutoff(), metadata_only=True)[1]['currency'] == 'KRW'
    assert provider.calls == 2


def test_transient_query2_503_tries_query1_once_but_429_never_falls_back():
    spec = specification(us())

    class Session:
        def __init__(self, first):
            self.first = first
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            return Response(self.first) if len(self.urls) == 1 else Response(200, identity_doc(spec))

    session = Session(503)
    provider = YahooDaily(session, pause=0)
    assert provider.fetch(spec, cutoff(), metadata_only=True)[1]['provider_symbol'] == 'IBM'
    assert len(session.urls) == 2
    assert 'query2.finance.yahoo.com' in session.urls[0]
    assert 'query1.finance.yahoo.com' in session.urls[1]
    assert [r['status'] for r in provider.request_log] == [503, 200]

    limited = Session(429)
    provider = YahooDaily(limited, pause=0)
    try:
        provider.fetch(spec, cutoff(), metadata_only=True)
        assert False
    except RateLimited:
        pass
    assert len(limited.urls) == 1


def test_three_us_connection_errors_do_not_disable_korean_cache_lookup():
    us_spec = specification(us())
    kr_spec = specification(row())

    class Session:
        def __init__(self):
            self.calls = 0

        def get(self, url, **kwargs):
            self.calls += 1
            if 'IBM' in url:
                raise requests.ConnectionError('simulated network outage')
            return Response(200, identity_doc(kr_spec))

    session = Session()
    provider = YahooDaily(session, pause=0)
    for _ in range(3):
        try:
            provider.fetch(us_spec, cutoff(), metadata_only=True)
            assert False
        except Unavailable as exc:
            assert str(exc) == 'PROVIDER_CONNECTION_FAILED'
    try:
        provider.fetch(us_spec, cutoff(), metadata_only=True)
        assert False
    except RateLimited as exc:
        assert str(exc) == 'PROVIDER_CONNECTION_CIRCUIT_OPEN'
    assert provider.fetch(kr_spec, cutoff(), metadata_only=True)[1]['currency'] == 'KRW'
    assert session.calls == 4


def test_mixed_us_cache_salvages_only_a_200_bar_verified_trailing_series(tmp_path):
    item = us()
    dates = calendar('US').sessions_in_range('2025-10-01', '2026-11-12').tz_localize(None)[-230:]
    assert len(dates) == 230 and str(dates[-1].date()) == '2026-11-12'
    frame, path = cache(tmp_path, item, dates=dates)
    frame.loc[dates[:20], '_base_price_basis'] = None
    frame.to_pickle(path)
    original = path.read_bytes()
    spec = specification(item)
    evidence = attest_identity(spec, identity(spec), cutoff())

    audit = inspect_core_cache(tmp_path, spec, cutoff(), evidence=evidence)
    assert audit.classification == 'reusable'
    assert audit.checks['basis'] is True
    assert audit.excluded_basis == 20
    assert len(audit.rows) == 210 >= MIN_VERIFIED_BASIS_BARS
    assert {k:audit.basis_evidence[k] for k in ('inspected','expected','missing','different')} == dict(
        inspected=230, expected=210, missing=20, different=0)
    assert len(audit.basis_evidence['missing_dates'])==16
    assert audit.basis_evidence['last_session_basis_verified'] is True
    assert audit.rows[-1]['d'] == '2026-11-12'
    assert path.read_bytes() == original


def test_short_adjusted_suffix_is_not_promoted_or_mixed_with_legacy_prices(tmp_path):
    item = us()
    dates = calendar('US').sessions_in_range('2025-10-01', '2026-11-12').tz_localize(None)[-200:]
    frame, path = cache(tmp_path, item, dates=dates)
    frame.loc[dates[0], '_base_price_basis'] = 'yahoo:raw'
    frame.to_pickle(path)

    spec = specification(item)
    evidence = attest_identity(spec, identity(spec), cutoff())
    result = inspect_core_cache(tmp_path, spec, cutoff(), evidence=evidence)
    assert result.classification == 'basis_mismatch'
    assert result.checks['basis'] is False
    assert result.excluded_basis == 0
    assert result.rows is None
    assert {k:result.basis_evidence[k] for k in ('inspected','expected','missing','different')} == dict(
        inspected=200, expected=199, missing=0, different=1)
    assert result.basis_evidence['missing_dates']==[]
    assert result.basis_evidence['different_dates']==[str(dates[0].date())]

