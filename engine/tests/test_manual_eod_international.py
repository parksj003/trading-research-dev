"""International EOD bootstrap, fair collection and immutable snapshot cache tests."""
from collections import Counter
from datetime import datetime, timezone

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_provider import BASIS, RateLimited
from watchlist.manual_eod_publish import build_snapshot
from test_manual_eod_pipeline import row


def item(symbol, currency):
    return row(symbol=symbol, unit=currency, watch_region='overseas')


FOREIGN = [
    ('TSE:7203', 'JPY', 'JP'),
    ('SSE:600000', 'CNY', 'CN'),
    ('SZSE:000001', 'CNY', 'CN'),
    ('HKEX:700', 'HKD', 'HK'),
    ('XETR:SAP', 'EUR', 'DE'),
    ('SIX:NESN', 'CHF', 'CH'),
    ('BME:BBVA', 'EUR', 'ES'),
]


def at(date='2026-11-12'):
    return datetime.fromisoformat(date + 'T23:00:00+00:00')


class Provider:
    def __init__(self, fail_market=None):
        self.calls = 0
        self.events = []
        self.fail_market = fail_market

    def fetch(self, spec, cutoff, **options):
        self.calls += 1
        self.events.append((spec['market'], spec['symbol'], options))
        if self.fail_market == spec['market']:
            raise RateLimited('PROVIDER_RATE_LIMITED')
        dates = ['2026-11-12']
        if cutoff.date().isoformat() > '2026-11-12':
            dates.append('2026-11-13')
        data = [dict(d=day, o=100., h=105., l=98., c=104., v=1000.) for day in dates]
        identity = dict(provider_symbol=spec['candidates'][0],
                        provider_exchange=spec['provider_exchanges'][0],
                        currency=spec['currency'],
                        instrument_type='ETF' if spec['asset_type']=='etf' else 'EQUITY',
                        timezone=spec['provider_timezones'][0])
        return data, identity, 0


def build(rows, provider, *, run='20261112T230000Z-one', cutoff=None, previous=None, budget=64):
    instant = cutoff or at()
    core = dict(run_id=run, generated_at=instant.isoformat())
    return build_snapshot(rows, catalog_sha256=digest(csv_bytes(rows)), core=core,
                          core_commit='a'*40, workflow_run_id='123',
                          cutoff=instant, provider=provider, previous=previous,
                          clock=lambda: instant, max_provider_requests=budget)


def test_all_seven_supported_foreign_venues_keep_real_native_listing_provenance():
    rows = [item(sym, currency) for sym, currency, _ in FOREIGN]
    provider = Provider()
    doc = build(rows, provider)
    assert provider.calls == len(rows)
    assert doc['catalog_order'] == [r['symbol'] for r in rows]
    for sym, currency, market in FOREIGN:
        entry = doc['instruments'][sym]
        assert entry['status'] == 'current'
        assert entry['market'] == market and entry['currency'] == currency
        assert entry['adjustment'] == BASIS
        assert entry['source'] == 'Yahoo Finance'
        assert entry['source_symbol'] == entry['identity_evidence']['provider_symbol']
        assert entry['last_final_session'] == '2026-11-12'
        assert len(entry['bars']['d']) == 1
    assert doc['collection_metrics']['budget_deferred'] == 0
    assert doc['collection_metrics']['status_by_market']['CN']['current'] == 2


def test_country_round_robin_under_small_http_budget_preserves_catalog_order():
    # KR/US and all six overseas market calendars must be given a turn rather
    # than 142 foreign candidates waiting behind dozens of US/JP/CN symbols.
    rows = [item('TSE:' + str(7000+i), 'JPY') for i in range(11)]
    rows += [item('SSE:' + str(600000+i), 'CNY') for i in range(11)]
    rows += [item('NYSE:TEST' + str(i), 'USD') for i in range(11)]
    rows += [item('KRX:' + str(100000+i), 'KRW') for i in range(11)]
    rows += [item(sym, currency) for sym, currency, _ in FOREIGN[3:]]
    provider = Provider()
    first = build(rows, provider, budget=8)
    assert provider.calls == 8
    market_counts = Counter(e[0] for e in provider.events)
    assert set(market_counts) == {'JP','CN','US','KR','HK','DE','CH','ES'}
    assert all(n == 1 for n in market_counts.values())
    assert first['catalog_order'] == [r['symbol'] for r in rows]
    assert list(first['instruments']) == first['catalog_order']
    assert first['collection_metrics']['budget_deferred'] == len(rows) - 8
    assert sum(e['status']=='current' for e in first['instruments'].values()) == 8
    assert all(e['reason']=='PROVIDER_REQUEST_BUDGET_DEFERRED'
               for e in first['instruments'].values() if e['status']=='missing')

    # The next successful core publication is an independent opportunity.
    # Already-current entries must be reused with no additional HTTP calls.
    second_provider = Provider()
    second = build(rows, second_provider, run='20261112T230001Z-two', previous=first, budget=8)
    assert second_provider.calls == 8
    assert sum(e['status']=='current' for e in second['instruments'].values()) == 16
    for symbol, old in first['instruments'].items():
        if old['status'] == 'current':
            new = second['instruments'][symbol]
            assert new['bars'] == old['bars']
            assert new['last_success_at'] == old['last_success_at']
            assert new['acquisition'] == 'previous_snapshot'


def test_international_previous_snapshot_reused_and_incremental_overlapping_ohlcv():
    rows = [item('TSE:7203','JPY'), item('HKEX:700','HKD')]
    first_provider = Provider()
    previous = build(rows, first_provider)
    assert first_provider.calls == 2
    replay = build(rows, Provider(fail_market='JP'), previous=previous)
    assert replay is previous  # same core ID and catalog digest are idempotent

    updated_provider = Provider()
    next_day = build(rows, updated_provider, run='20261113T230000Z-two',
                     cutoff=at('2026-11-13'), previous=previous)
    assert updated_provider.calls == 2
    assert all(ev[2]['start'] == '2026-11-02' for ev in updated_provider.events)
    for sym in ('TSE:7203', 'HKEX:700'):
        entry = next_day['instruments'][sym]
        assert entry['bars']['d'] == ['2026-11-12','2026-11-13']
        assert entry['adjustment'] == BASIS
        assert entry['last_final_session'] == '2026-11-13'


def test_a_foreign_market_failure_keeps_cold_starts_in_other_markets_active():
    rows = [item(sym,currency) for sym,currency,_ in FOREIGN]
    p = Provider(fail_market='JP')
    doc = build(rows,p)
    assert doc['instruments']['TSE:7203']['reason'] == 'PROVIDER_RATE_LIMITED'
    assert doc['instruments']['TSE:7203']['status'] == 'missing'
    assert doc['instruments']['HKEX:700']['status'] == 'current'
    assert doc['instruments']['BME:BBVA']['status'] == 'current'
    assert doc['instruments']['TSE:7203']['last_attempt_at'] == at().isoformat()


def test_unverified_euronext_and_fse_not_silently_aliased_to_another_listing():
    rows = [item('EURONEXT:AI','EUR'),item('FSE:9407','EUR'),item('HKEX:700','HKD')]
    p = Provider()
    doc = build(rows,p)
    assert p.calls == 1
    assert doc['instruments']['EURONEXT:AI']['status'] == 'unsupported'
    assert doc['instruments']['FSE:9407']['status'] == 'unsupported'
    assert doc['instruments']['HKEX:700']['status'] == 'current'

