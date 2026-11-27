"""Fixed-clock/cache/provider tests. No real KRX, Yahoo or broker requests."""
from copy import deepcopy
from datetime import datetime
import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from watchlist import trend_watchlist as engine


def bars(last='2026-11-03'):
    d = pd.DataFrame(index=pd.bdate_range('2023-01-01', last))
    for k, v in dict(Open=100., High=102., Low=99., Close=100., Volume=1000.).items():
        d[k] = v
    d['_session_finality_version'] = 2
    d['_session_confirmed'] = True
    return d


def raw(d):
    return d.rename(columns=dict(Open='시가', High='고가', Low='저가', Close='종가', Volume='거래량'))


@pytest.fixture
def context(tmp_path, monkeypatch):
    cfg = deepcopy(engine.DEFAULT_CONFIG)
    calls = []
    stock = SimpleNamespace()
    def fetch(start, end, ticker):
        calls.append((start, end, ticker))
        return raw(bars('2026-11-04').loc[start:end])
    stock.get_index_ohlcv_by_date = stock.get_market_ohlcv_by_date = fetch
    monkeypatch.setitem(sys.modules, 'pykrx', SimpleNamespace(stock=stock))
    monkeypatch.setattr(engine, 'KR_HISTORY_CACHE_DIR', tmp_path)
    def clock(value='2026-11-04T17:13:00+09:00'):
        fixed = datetime.fromisoformat(value)
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed.replace(tzinfo=None)
        monkeypatch.setattr(engine, 'datetime', Clock)
    clock()
    monkeypatch.setattr(engine.time, 'time', lambda: engine.datetime.now(engine.KST).timestamp())
    real_pickle = pd.DataFrame.to_pickle
    def fixed_mtime_pickle(frame, path, *args, **kwargs):
        real_pickle(frame, path, *args, **kwargs)
        if isinstance(path, (str, os.PathLike)):
            stamp = engine.time.time()
            os.utime(path, (stamp, stamp))
    monkeypatch.setattr(pd.DataFrame, 'to_pickle', fixed_mtime_pickle)
    def cache(ticker='005930', last='2026-11-03', index=False, age=9):
        p = tmp_path / f"{'idx_' if index else ''}{ticker}.pkl"
        bars(last).to_pickle(p)
        stamp = time.time() - age * 3600
        os.utime(p, (stamp, stamp))
        return p
    return cfg, calls, stock, cache, clock


@pytest.mark.parametrize('ticker,index', [('1001', True), ('2001', True), ('005930', False)])
def test_after_close_refreshes_yesterdays_bar_despite_nine_hour_old_cache(context, ticker, index):
    cfg, calls, _, cache, _ = context
    path = cache(ticker, index=index)
    assert engine.is_cache_fresh(path, 12)
    result = engine.fetch_kr_history(ticker, 3, is_index=index, cfg=cfg)
    assert str(result.index[-1].date()) == '2026-11-04'
    assert calls == [('20261027', '20261104', ticker)]  # Seven-day seam overlap only.


@pytest.mark.parametrize('when', ['2026-11-04T07:00:00+09:00', '2026-11-04T15:34:00+09:00',
                                  '2026-11-07T19:00:00+09:00', '2026-11-08T19:00:00+09:00'])
def test_morning_intraday_and_weekend_do_not_force_refresh(context, when):
    cfg, calls, _, cache, clock = context
    clock(when); path = cache()
    before = path.read_bytes(), path.stat().st_mtime_ns
    result = engine.fetch_kr_history('005930', 3, cfg=cfg)
    assert result.index[-1] == pd.Timestamp('2026-11-03') and calls == []
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_same_day_repeat_does_not_download_again(context):
    cfg, calls, _, cache, _ = context
    cache()
    first = engine.fetch_kr_history('005930', 3, cfg=cfg)
    second = engine.fetch_kr_history('005930', 3, cfg=cfg)
    pd.testing.assert_frame_equal(first, second)
    assert len(calls) == 1


@pytest.mark.parametrize('response', ['empty', 'overlap_only', 'exception'])
def test_provider_delay_keeps_original_cache_and_mtime_without_full_download(context, monkeypatch, response):
    cfg, calls, stock, cache, _ = context
    path = cache('1001', index=True)
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(engine.time, 'sleep', lambda *_: None)
    def fetch(start, end, ticker):
        calls.append((start, end, ticker))
        if response == 'exception': raise ValueError('synthetic provider delay')
        return pd.DataFrame() if response == 'empty' else raw(bars().loc[start:end])
    stock.get_index_ohlcv_by_date = fetch
    result = engine.fetch_kr_history('1001', 3, is_index=True, cfg=cfg)
    assert result.index[-1] == pd.Timestamp('2026-11-03')
    assert len(calls) == (2 if response == 'exception' else 1)
    assert all(start == '20261027' for start, _, _ in calls)
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def bulk_context(context, missing=False):
    cfg, calls, stock, cache, _ = context
    tickers = [f'{i:06d}' for i in range(1, 16)]
    paths = {t: cache(t) for t in tickers}
    daily = []
    def snapshot(date, market, alternative):
        assert market == 'ALL' and alternative is False
        daily.append(date)
        d = pd.DataFrame(index=[f'{i:06d}' for i in range(1, 202)])
        for k, v in dict(시가=100., 고가=102., 저가=99., 종가=100., 거래량=1000.).items(): d[k] = v
        if missing and date == '20261104': d = d.drop(tickers[-1])
        return d
    stock.get_market_ohlcv_by_ticker = snapshot
    return tickers, paths, daily


def test_bulk_uses_two_daily_requests_for_all_fresh_mtime_stale_session_caches(context):
    cfg, calls, _, _, _ = context
    tickers, paths, daily = bulk_context(context)
    sync = engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    assert sync['snapshot_available'] is True and daily == ['20261104', '20261103']
    assert all(pd.read_pickle(p).index[-1] == pd.Timestamp('2026-11-04') for p in paths.values())
    assert not any(engine.kr_history_needs_refresh(p, 12) for p in paths.values())
    for t in tickers: engine.fetch_kr_history(t, 3, cfg=cfg, sync_state=sync)
    assert calls == []
    # Repeated execution after successful update makes no requests at all.
    engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    assert len(daily) == 2


def test_bulk_missing_single_ticker_only_refreshes_that_ticker(context):
    cfg, calls, _, _, _ = context
    tickers, paths, _ = bulk_context(context, missing=True)
    old = paths[tickers[-1]].stat().st_mtime_ns
    sync = engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    needs = [t for t, p in paths.items() if engine.kr_history_needs_refresh(p, 12)]
    assert needs == [tickers[-1]] and paths[tickers[-1]].stat().st_mtime_ns == old
    engine.fetch_kr_history(needs[0], 3, cfg=cfg, sync_state=sync)
    assert len(calls) == 1 and calls[0][2] == tickers[-1]


@pytest.mark.parametrize('fail', [False, True])
def test_bulk_unavailable_checks_once_and_avoids_per_stock_retry_storm(context, fail):
    cfg, calls, stock, _, _ = context
    tickers, paths, daily = bulk_context(context)
    old = {t: (p.read_bytes(), p.stat().st_mtime_ns) for t, p in paths.items()}
    def unavailable(date, **_):
        daily.append(date)
        if fail: raise ValueError('synthetic KRX delay')
        return pd.DataFrame()
    stock.get_market_ohlcv_by_ticker = unavailable
    sync = engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    for t in tickers:
        result = engine.fetch_kr_history(t, 3, cfg=cfg, sync_state=sync)
        assert result.index[-1] == pd.Timestamp('2026-11-03')
    assert daily == ['20261104'] and calls == []
    assert old == {t: (p.read_bytes(), p.stat().st_mtime_ns) for t, p in paths.items()}


def test_missing_cache_still_downloads_history_when_bulk_unavailable(context):
    cfg, calls, _, _, _ = context
    result = engine.fetch_kr_history('999999', 3, cfg=cfg, sync_state={'snapshot_available': False})
    assert not result.empty and len(calls) == 1 and calls[0][0] < '20261027'


def test_bulk_morning_does_not_probe_current_kr_session(context):
    cfg, calls, _, _, clock = context
    tickers, _, daily = bulk_context(context)
    clock('2026-11-04T07:00:00+09:00')
    engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    assert calls == daily == []


def test_session_quality_reports_index_and_stock_delay_independently(context):
    *_, clock = context
    result = engine.kr_price_health({'KOSPI': bars('2026-11-04'), 'KOSDAQ': bars()}, ['2026-11-04', '2026-11-03'])
    assert result['kr_price_status'] == 'delayed_or_holiday' and result['kr_price_delayed_tickers'] == 1
    assert result['kr_close_check_date'] == '2026-11-04' and 'KOSDAQ 2026-11-03' in result['kr_price_message']
    assert '휴장/공급 지연' in result['kr_price_message']
    assert engine.kr_price_health({'KOSPI': bars('2026-11-04')}, ['2026-11-04'])['kr_price_status'] == 'current'
    clock('2026-11-04T07:00:00+09:00')
    assert engine.kr_price_health({'KOSPI': bars()}, ['2026-11-03'])['kr_price_status'] == 'not_due'


@pytest.mark.parametrize('when', ['2026-11-04T07:00:00+09:00', '2026-07-07T07:00:00+09:00'])
def test_us_morning_cache_policy_unaffected_in_standard_and_daylight_time(context, monkeypatch, when):
    cfg, _, _, _, clock = context
    clock(when)
    monkeypatch.setattr(engine, 'US_HISTORY_CACHE_DIR', engine.KR_HISTORY_CACHE_DIR)
    last = str((pd.Timestamp(when[:10]) - pd.offsets.BDay()).date())
    bars(last).to_pickle(engine.US_HISTORY_CACHE_DIR / 'SPY.pkl')
    yf = SimpleNamespace(download=lambda *a, **k: pytest.fail('Fresh US cache must not redownload'))
    monkeypatch.setitem(sys.modules, 'yfinance', yf)
    result = engine.fetch_us_histories(['SPY'], 3, cfg=cfg)
    assert result['SPY'].index[-1] == pd.Timestamp(last)


def test_force_refresh_and_corporate_action_seam_still_allow_full_fetch(context):
    cfg, calls, stock, cache, _ = context
    cache(); cfg['runtime']['refresh_cache'] = True
    engine.fetch_kr_history('005930', 3, cfg=cfg)
    assert len(calls) == 1 and calls[0][0] < '20261027'
    calls.clear(); cfg['runtime']['refresh_cache'] = False; cache()
    def adjusted(start, end, ticker):
        calls.append((start, end, ticker))
        return raw(bars('2026-11-04').loc[start:end] * 2)
    stock.get_market_ohlcv_by_date = adjusted
    result = engine.fetch_kr_history('005930', 3, cfg=cfg)
    assert len(calls) == 2 and calls[0][0] == '20261027' and calls[1][0] < '20261027'
    assert result['Close'].iloc[-1] == 200


def test_missing_stock_prices_cannot_be_reported_as_current(context):
    result = engine.kr_price_health({'KOSPI': bars('2026-11-04')}, [], unavailable_tickers=2)
    assert result['kr_price_status'] == 'delayed_or_holiday'
    assert result['kr_price_unavailable_tickers'] == 2 and '시세 없음 2개' in result['kr_price_message']


def test_kr_tracks_propagates_bulk_delay_without_individual_stock_calls(context, monkeypatch):
    cfg, calls, stock, cache, _ = context
    tickers, _, daily = bulk_context(context)
    cache('1001', index=True); cache('2001', index=True)
    def unavailable(date, **_): daily.append(date); return pd.DataFrame()
    stock.get_market_ohlcv_by_ticker = unavailable
    universe = pd.DataFrame([dict(ticker=t, sub_market='KOSPI' if i % 2 else 'KOSDAQ',
                                 market_cap=2e12, name='Synthetic', sector='Synthetic', macro_theme='Synthetic')
                             for i, t in enumerate(tickers)])
    monkeypatch.setattr(engine, 'get_kr_universe', lambda *_: universe)
    cfg['runtime']['request_pause_seconds'] = 0
    *_, meta = engine.run_kr_tracks(cfg, {}, {})
    assert daily == ['20261104']
    assert {t for _, _, t in calls} == {'1001', '2001'}
    assert meta['health']['kr_price_delayed_tickers'] == len(tickers)
    assert meta['health']['kr_price_status'] == 'delayed_or_holiday'


@pytest.mark.parametrize('index', [False, True])
def test_preclose_same_day_bar_is_refreshed_without_false_corporate_action_full_fetch(context, index):
    cfg, calls, _, cache, _ = context
    ticker = '1001' if index else '005930'
    path = cache(ticker, last='2026-11-04', index=index)
    d = pd.read_pickle(path); d.loc[d.index[-1], 'Close'] = 80.; d.to_pickle(path)
    stamp = datetime.fromisoformat('2026-11-04T14:00:00+09:00').timestamp()
    d = pd.read_pickle(path); d.loc[d.index[-1], '_session_confirmed'] = False; d.to_pickle(path)
    os.utime(path, (stamp, stamp))
    assert engine.kr_history_needs_refresh(path, 12)
    result = engine.fetch_kr_history(ticker, 3, is_index=index, cfg=cfg)
    assert result['Close'].iloc[-1] == 100. and result.index[-1] == pd.Timestamp('2026-11-04')
    assert calls == [('20261027', '20261104', ticker)]


def test_unconfirmed_intraday_cache_is_never_used_as_a_close_on_provider_delay(context):
    cfg, _, stock, cache, _ = context
    path = cache('005930', last='2026-11-04'); cache('1001', last='2026-11-04', index=True)
    stamp = datetime.fromisoformat('2026-11-04T14:00:00+09:00').timestamp()
    for p in [path, engine.KR_HISTORY_CACHE_DIR/'idx_1001.pkl']:
        d=pd.read_pickle(p); d.loc[d.index[-1], '_session_confirmed']=False; d.to_pickle(p); os.utime(p,(stamp,stamp))
    before = path.read_bytes(), path.stat().st_mtime_ns
    stock.get_market_ohlcv_by_date = lambda *_: pd.DataFrame()
    hist = engine.fetch_kr_history('005930', 3, cfg=cfg)
    bench = engine._make_bench_getter(cfg)('KOSPI', 'KR')
    charts = engine._build_charts(pd.DataFrame([dict(market='KR', ticker='005930', as_of='2026-11-04')]), 260, 1)
    assert hist.index[-1] == bench.index[-1] == pd.Timestamp('2026-11-03')
    assert charts['KR:005930']['d'][-1] == '2026-11-03'
    assert before == (path.read_bytes(), path.stat().st_mtime_ns)


def test_bulk_replaces_intraday_rows_without_per_stock_full_download(context):
    cfg, calls, _, _, _ = context
    tickers, paths, daily = bulk_context(context)
    stamp = datetime.fromisoformat('2026-11-04T14:00:00+09:00').timestamp()
    for path in paths.values():
        d = bars('2026-11-04'); d.loc[d.index[-1], 'Close'] = 80.; d.to_pickle(path)
        d = pd.read_pickle(path); d.loc[d.index[-1], '_session_confirmed'] = False; d.to_pickle(path)
    os.utime(path, (stamp, stamp))
    engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    assert daily == ['20261104', '20261103']
    for t, path in paths.items():
        assert pd.read_pickle(path)['Close'].iloc[-1] == 100.
        engine.fetch_kr_history(t, 3, cfg=cfg)
    assert calls == []



def test_bulk_skips_a_missing_middle_session_then_uses_adjusted_date_history(context):
    cfg, calls, stock, _, clock = context
    tickers, paths, _ = bulk_context(context)
    clock('2026-11-06T17:13:00+09:00')
    snapshot = stock.get_market_ohlcv_by_ticker
    def partial(date, **kwargs):
        result = snapshot(date, **kwargs)
        return result.drop(tickers[-1]) if date == '20261105' else result
    stock.get_market_ohlcv_by_ticker = partial
    original = paths[tickers[-1]].read_bytes()
    state = engine.bulk_sync_kr_recent_ohlcv(tickers, cfg)
    assert paths[tickers[-1]].read_bytes() == original and state['snapshot_available']
    assert pd.read_pickle(paths[tickers[0]]).index[-1] == pd.Timestamp('2026-11-06')


def test_incomplete_adjusted_replacement_preserves_old_cache(context):
    cfg, calls, stock, cache, _ = context
    path = cache()
    original = path.read_bytes()
    def truncated(start, end, ticker):
        calls.append(start)
        return raw(bars('2026-11-04').tail(3) * 2)
    stock.get_market_ohlcv_by_date = truncated
    result = engine.fetch_kr_history('005930', 3, cfg=cfg)
    assert len(calls) == 2 and not result.empty and path.read_bytes() == original
