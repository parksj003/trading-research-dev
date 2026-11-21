"""Rate-limit failures must not erase normal cached fundamentals or refresh their age."""
from copy import deepcopy
import sys
import time
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from watchlist import trend_watchlist as engine


class YFRateLimitError(Exception): pass


@pytest.fixture
def cap_context(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'UNIVERSE_CACHE_DIR', tmp_path)
    record = dict(cap=20e9, sector='Technology', peg=1.2, eps_g=25., ts=time.time()-25*3600, ok=True)
    path = tmp_path/'us_caps_sectors_v15.pkl'
    pd.to_pickle({'EX': record.copy()}, path)
    cfg = deepcopy(engine.DEFAULT_CONFIG)
    calls = []
    def provider(fail):
        class Ticker:
            def __init__(self, ticker):
                if fail == 'constructor': raise ValueError('synthetic connection failure')
            @property
            def fast_info(self):
                calls.append('fast_info')
                if fail == 'total': raise YFRateLimitError('synthetic limit')
                return {'marketCap': 22e9}
            def get_info(self):
                calls.append('get_info')
                if fail == 'partial': raise YFRateLimitError('synthetic limit')
                return dict(marketCap=22e9, sector='New Sector', pegRatio=1.5, earningsGrowth=.30)
        monkeypatch.setitem(sys.modules, 'yfinance', SimpleNamespace(Ticker=Ticker))
    return cfg, path, record, calls, provider


@pytest.mark.parametrize('fail', ['total', 'partial', 'constructor'])
def test_total_and_partial_failures_preserve_cap_sector_peg_eps_and_original_age(cap_context, fail):
    cfg, path, record, calls, provider = cap_context
    provider(fail); health = {}
    caps, sectors, pegs, growth = engine.get_us_market_caps(['EX'], 10e9, 0, cfg, health)
    assert caps == {'EX': 20e9} and sectors == {'EX': 'Technology'}
    assert pegs == {'EX': 1.2} and growth == {'EX': 25.}
    assert pd.read_pickle(path)['EX'] == record
    assert health == dict(us_cap_requested=1, us_cap_fallback=1, us_cap_missing=0, us_fundamentals_missing=0)
    if fail == 'total': assert calls == ['fast_info']  # No second rate-limited request.


def test_no_cache_and_no_valid_cap_are_excluded_without_fake_zero(cap_context):
    cfg, path, _, _, provider = cap_context
    path.unlink(); provider('total'); health = {}
    caps, sectors, pegs, growth = engine.get_us_market_caps(['EX'], 10e9, 0, cfg, health)
    assert caps == sectors == {} and pd.isna(pegs['EX']) and pd.isna(growth['EX'])
    assert pd.read_pickle(path) == {} and health['us_cap_missing'] == 1


def test_valid_new_cap_with_failed_fundamentals_is_not_marked_fresh(cap_context):
    cfg, path, _, _, provider = cap_context
    path.unlink(); provider('partial'); health = {}
    caps, _, pegs, growth = engine.get_us_market_caps(['EX'], 10e9, 0, cfg, health)
    assert caps == {'EX': 22e9} and pd.isna(pegs['EX']) and pd.isna(growth['EX'])
    assert pd.read_pickle(path)['EX']['ts'] == 0
    assert health['us_fundamentals_missing'] == 1 and health['us_cap_fallback'] == 0


def test_successful_refresh_replaces_old_record_and_next_run_reuses_it(cap_context):
    cfg, path, record, calls, provider = cap_context
    provider(None); health = {}
    caps, sectors, pegs, growth = engine.get_us_market_caps(['EX'], 10e9, 0, cfg, health)
    assert caps == {'EX': 22e9} and sectors == {'EX': 'New Sector'}
    assert pegs == {'EX': 1.5} and growth == {'EX': 30.}
    assert pd.read_pickle(path)['EX']['ts'] > record['ts'] and health['us_cap_fallback'] == 0
    calls.clear()
    engine.get_us_market_caps(['EX'], 10e9, 0, cfg)
    assert calls == []


def test_new_success_below_cap_floor_does_not_reuse_old_above_floor_cap(cap_context, monkeypatch):
    cfg, path, _, _, _ = cap_context
    class SmallTicker:
        fast_info = {'marketCap': 1e9}
        def get_info(self): pytest.fail('Sub-floor cap needs no fundamentals request')
    monkeypatch.setitem(sys.modules, 'yfinance', SimpleNamespace(Ticker=lambda _: SmallTicker()))
    assert engine.get_us_market_caps(['EX'], 10e9, 0, cfg)[0] == {}
    assert pd.read_pickle(path)['EX']['cap'] == 1e9

