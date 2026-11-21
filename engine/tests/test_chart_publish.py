import numpy as np
import pandas as pd
import pytest
import copy
import hashlib
import json

from watchlist import trend_watchlist as watchlist


@pytest.mark.parametrize('bars', [130, 260])
def test_chart_window_reuses_cache_aligns_arrays_and_never_includes_future_bars(tmp_path, monkeypatch, bars):
    cache = tmp_path / 'prices'
    cache.mkdir()
    monkeypatch.setattr(watchlist, 'KR_HISTORY_CACHE_DIR', cache)
    index = pd.bdate_range('2024-01-02', periods=500)
    close = np.arange(500, dtype=float) + 1000
    prices = pd.DataFrame({'Open': close, 'High': close + 10, 'Low': close - 10,
                           'Close': close, 'Volume': 1000}, index=index)
    prices['_session_finality_version'] = 2
    prices['_session_confirmed'] = True
    prices.to_pickle(cache / '123456.pkl')
    cutoff = index[-6]
    rows = pd.DataFrame([dict(market='KR', ticker='123456', as_of=cutoff,
                              pivot_price=1400, stop_price=1300)])
    result = watchlist._build_charts(rows, bars, 60)['KR:123456']
    expected = prices.loc[:cutoff].tail(bars)
    assert result['d'] == expected.index.strftime('%Y-%m-%d').tolist()
    assert result['c'] == expected.Close.tolist()
    assert all(len(result[key]) == bars for key in ('d', 'o', 'h', 'l', 'c', 'v', 's20', 's50', 's200'))
    expected_sma = prices.Close.rolling(200).mean().loc[expected.index[0]]
    assert result['s200'][0] == expected_sma  # Preserve calculation precision, including warm-up history.


@pytest.fixture
def chart_cache(tmp_path, monkeypatch):
    for market, attr in [('KR', 'KR_HISTORY_CACHE_DIR'), ('US', 'US_HISTORY_CACHE_DIR')]:
        path = tmp_path / market
        path.mkdir()
        monkeypatch.setattr(watchlist, attr, path)
    # Any provider access fails the test, including calls whose exceptions would be swallowed.
    for name in ('fetch_kr_history', 'fetch_us_histories', 'bulk_sync_kr_recent_ohlcv'):
        monkeypatch.setattr(watchlist, name, lambda *a, **kw: pytest.fail('chart generation must reuse cached prices'))
    return tmp_path


def cached_prices(path, length=500, confirmed=True):
    index = pd.bdate_range(end='2026-11-09', periods=length)
    close = np.linspace(100., 140., length)
    prices = pd.DataFrame({'Open': close, 'High': close + 2, 'Low': close - 2,
                           'Close': close, 'Volume': 1000,
                           '_session_confirmed': confirmed, '_session_finality_version': 3}, index=index)
    prices.to_pickle(path)
    return prices


def chart_candidates(cache, count=75):
    records = []
    for i in range(count):
        market = 'KR' if i % 4 == 0 else 'US'
        ticker = str(100000 + i) if market == 'KR' else f'STK{i:03}'
        cached_prices(cache / market / f'{ticker}.pkl')
        records.append(dict(market=market, ticker=ticker, as_of='2026-11-02',
                            pivot_price=139., stop_price=130.))
    return pd.DataFrame(records)


def test_all_candidates_exceeds_sixty_and_preserves_existing_chart_values_and_inputs(chart_cache):
    candidates = chart_candidates(chart_cache)
    selected = candidates.head(65).copy()
    action = selected.iloc[[9, 2]].copy()
    action['pivot_price'] = 145.
    action['stop_price'] = 131.
    before = [frame.copy(deep=True) for frame in (action, selected, candidates)]
    old = watchlist._build_charts(watchlist._chart_rows(action, selected), 260, 60)
    charts, coverage = watchlist._charts_for_publication(action, selected, candidates, {'chart_max': 60})
    assert len(charts) == 75 and coverage['requested'] == 75
    assert coverage['available'] == 75 and coverage['missing'] == {} and coverage['limit'] is None
    assert {key: charts[key] for key in old} == old
    assert watchlist._gzip_json(old) == watchlist._gzip_json({key: charts[key] for key in old})
    for frame, original in zip((action, selected, candidates), before):
        pd.testing.assert_frame_equal(frame, original)


def test_explicit_selected_scope_reports_limit_and_outside_scope_separately(chart_cache):
    candidates = chart_candidates(chart_cache)
    charts, coverage = watchlist._charts_for_publication(pd.DataFrame(), candidates.head(65), candidates,
        {'chart_scope': 'selected', 'chart_max': 60})
    assert len(charts) == 60 and coverage['requested'] == 75 and coverage['targeted'] == 65
    assert coverage['reason_counts'] == {'limit_reached': 5, 'outside_chart_scope': 10}


def test_selected_limit_counts_successful_charts_not_failed_cache_reads(chart_cache):
    candidates = chart_candidates(chart_cache, 3)
    first = candidates.iloc[0]
    (chart_cache / first.market / f'{first.ticker}.pkl').unlink()
    charts, coverage = watchlist._charts_for_publication(pd.DataFrame(), candidates, candidates,
        {'chart_scope': 'selected', 'chart_max': 1})
    assert len(charts) == 1
    assert coverage['reason_counts'] == {'cache_missing': 1, 'limit_reached': 1}


@pytest.mark.parametrize('case,reason', [
    ('missing', 'cache_missing'), ('corrupt', 'cache_read_error'),
    ('unconfirmed', 'no_confirmed_bars'), ('future', 'no_bars_as_of'),
    ('short', 'insufficient_bars'), ('columns', 'missing_ohlcv'),
    ('invalid_close', 'chart_build_error'),
])
def test_unavailable_prices_have_explicit_reasons_without_fetching(chart_cache, case, reason):
    path = chart_cache / 'US' / 'BAD.pkl'
    if case == 'corrupt':
        path.write_bytes(b'not a pickle')
    elif case != 'missing':
        prices = cached_prices(path, length=20 if case == 'short' else 500, confirmed=case != 'unconfirmed')
        if case == 'columns':
            prices.drop(columns='Volume').to_pickle(path)
        elif case == 'invalid_close':
            prices['Close'] = 'invalid'
            prices.to_pickle(path)
    rows = pd.DataFrame([dict(market='US', ticker='BAD', as_of='2020-01-01' if case == 'future' else '2026-11-09')])
    charts, coverage = watchlist._charts_for_publication(pd.DataFrame(), pd.DataFrame(), rows, {})
    assert charts == {} and coverage['missing'] == {'US:BAD': reason}
    assert coverage['requested'] == 1 and coverage['reason_counts'] == {reason: 1}


def test_identity_normalization_preserves_priority_and_does_not_change_cache_files(chart_cache):
    path = chart_cache / 'KR' / '005930.pkl'
    cached_prices(path)
    before = path.read_bytes()
    action = pd.DataFrame([dict(market='KR', ticker='5930', as_of='2026-11-02', pivot_price=130., stop_price=120.)])
    cand = pd.DataFrame([dict(market='KR', ticker='005930', as_of='2026-11-09', pivot_price=140., stop_price=125.)])
    charts, coverage = watchlist._charts_for_publication(action, pd.DataFrame(), cand, {})
    assert coverage['requested'] == 1 and set(charts) == {'KR:005930'}
    assert charts['KR:005930']['pivot'] == 130. and charts['KR:005930']['stop'] == 120.
    assert charts['KR:005930']['d'][-1] == '2026-11-02'
    assert path.read_bytes() == before


def publication_row(ticker, actionable=False):
    return dict(market='US', sub_market='SP500', ticker=ticker, name=ticker, as_of='2026-11-09',
                sector='S1', macro_theme='M1', tier='A', setup_stage='VCP', status_tag='',
                is_actionable=actionable, in_buy_zone=actionable, is_breakout=actionable,
                is_true_vcp=True, is_leading_sector=True, rs_score=90., rs_change_4w=3.,
                topdown_score=85., sector_power=70., last_close=140., pivot_price=139.,
                dist_to_pivot_pct=.01, stop_price=130., stop_dist_pct=-.07,
                raw_stop_dist_pct=-.07, position_size_pct=10., volume_ratio=1.8,
                distance_from_high_pct=-.01, exit_signal='✅정상')


def test_publication_expands_charts_only_and_validates_coverage_atomically(chart_cache, tmp_path):
    for ticker in ('FIRST', 'SECOND', 'EXTRA'):
        cached_prices(chart_cache / 'US' / f'{ticker}.pkl')
    selected = pd.DataFrame([publication_row('FIRST', True), publication_row('SECOND')])
    candidates = pd.concat([selected, pd.DataFrame([publication_row('EXTRA')])], ignore_index=True)
    originals = [frame.copy(deep=True) for frame in (selected, candidates)]
    regimes = {'SP500': dict(signal_light='🟡 CAUTION', regime='RISK_ON', benchmark='SPY', as_of='2026-11-09')}
    outputs = []
    for scope in ('selected', 'all_candidates'):
        cfg = copy.deepcopy(watchlist.DEFAULT_CONFIG)
        cfg['publish']['chart_scope'] = scope
        out = watchlist._publish_artifacts('2026-11-10', {'SP500': selected}, {'SP500': candidates},
            regimes, {}, pd.DataFrame(), pd.DataFrame(), cfg, base_dir=tmp_path / scope)
        assert out is not None and watchlist.validate_published_dir(out) == []
        outputs.append(out)
    old, new = outputs
    for name in watchlist.CONTRACT_REQUIRED:
        assert (old / name).read_bytes() == (new / name).read_bytes()
    summaries = [json.loads((p / 'summary.json').read_text()) for p in outputs]
    assert summaries[0].pop('chart_coverage')['available'] == 2
    assert summaries[1].pop('chart_coverage')['available'] == 3
    assert summaries[0] == summaries[1]  # Regimes, risk allocations, RS/sector metadata unchanged.
    assert not (new / 'base_counts.json').exists()
    for frame, original in zip((selected, candidates), originals):
        pd.testing.assert_frame_equal(frame, original)
    manifest_before = (new / 'manifest.json').read_bytes()
    cfg['publish']['chart_scope'] = 'invalid'
    assert watchlist._publish_artifacts('2026-11-10', {'SP500': selected}, {'SP500': candidates},
        regimes, {}, pd.DataFrame(), pd.DataFrame(), cfg, base_dir=tmp_path / 'all_candidates') is None
    assert (new / 'manifest.json').read_bytes() == manifest_before
    # Reject inconsistent coverage even when the summary's manifest hash is valid.
    summary = json.loads((new / 'summary.json').read_text())
    summary['chart_coverage']['available'] += 1
    raw = json.dumps(summary).encode()
    (new / 'summary.json').write_bytes(raw)
    manifest = json.loads(manifest_before)
    manifest['files']['summary.json']['sha256'] = hashlib.sha256(raw).hexdigest()
    (new / 'manifest.json').write_text(json.dumps(manifest))
    assert 'chart_coverage 집계 불일치' in watchlist.validate_published_dir(new)

