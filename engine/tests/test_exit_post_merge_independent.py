"""Independent arithmetic oracle for the post-merge exit audit.

The reference reads scalar OHLC values and uses Python arithmetic, without the
production indicator, fill, statistics, or contract helpers. Seeded complete
paths exercise all seven policies at each horizon; the same oracle can replay
the public input_evidence without downloading prices.
"""
import math
import random

import pandas as pd
import pytest

from watchlist.exit_policy_experiments import prepare, simulate, RULES
from test_exit_policy_core import fixture


def reference_indicators(bars, valid=None):
    valid = [True] * len(bars) if valid is None else valid
    atr, ma20, ma50, channel = [], [], [], []
    ranges, previous, smoothed = [], None, None
    for i, (opening, high, low, close) in enumerate(bars):
        if not valid[i]:
            ranges, previous, smoothed = [], None, None
        elif previous is None:
            previous = close
        else:
            ranges.append(max(high - low, abs(high - previous), abs(low - previous)))
            previous = close
            if len(ranges) == 14:
                smoothed = math.fsum(ranges) / 14
            elif len(ranges) > 14:
                smoothed = smoothed * (13 / 14) + ranges[-1] / 14
        atr.append(smoothed)
        for period, output in ((20, ma20), (50, ma50)):
            output.append(math.fsum(b[3] for b in bars[i-period+1:i+1]) / period
                          if i >= period-1 and all(valid[i-period+1:i+1]) else None)
        channel.append(min(b[2] for b in bars[i-10:i])
                       if i >= 10 and all(valid[i-10:i]) else None)
    return dict(atr14=atr, ma20=ma20, ma50=ma50, donchian10=channel)


def reference_trade(bars, start, horizon, rule, initial, cost_pct=.3, valid=None):
    """Return scalar expected fill/held metrics and effective ATR stops."""
    valid = [True] * len(bars) if valid is None else valid
    indicators = reference_indicators(bars, valid)
    entry = bars[start][0]
    risk = entry - initial
    if rule in ('MA20_2C', 'MA50_2C'):
        key = 'ma20' if rule == 'MA20_2C' else 'ma50'
        if indicators[key][start] is None:
            return {'exclusion_reason': key.upper()+'_WARMUP_INSUFFICIENT'}
    if rule == 'ATR_TRAIL' and (start == 0 or indicators['atr14'][start-1] is None):
        return {'exclusion_reason': 'ATR_WARMUP_INSUFFICIENT'}
    if rule == 'DONCHIAN_10':
        if indicators['donchian10'][start] is None:
            return {'exclusion_reason': 'DONCHIAN_WARMUP_INSUFFICIENT'}
        if indicators['donchian10'][start] >= entry:
            return {'exclusion_reason': 'DONCHIAN_ENTRY_ALREADY_BREACHED'}
    count, pending, trail, peak_high = 0, None, initial, -math.inf
    logs = []
    for index in range(start, min(start+horizon, len(bars))):
        if not valid[index]:
            return {"exclusion_reason": "HOLDING_PATH_INVALID"}
        opening, high, low, close = bars[index]
        stop, reason = initial, 'initial_stop'
        if rule == 'HOLD':
            stop = -math.inf
        elif rule == 'ATR_TRAIL':
            stop, reason = trail, 'atr_stop' if trail > initial else 'initial_stop'
            logs.append((index, stop))
        elif rule == 'DONCHIAN_10':
            stop = max(initial, indicators['donchian10'][index])
            reason = 'donchian_stop' if stop > initial else 'initial_stop'
        target = entry + 2*risk if rule == 'TARGET_2R' else math.inf
        # Form the available exits in exchange-time order, then take the first.
        candidates = []
        if pending is not None:
            candidates.append((0, opening, 'next_open_ma', 'open', pending))
        if opening <= stop:
            candidates.append((1, opening, 'gap_'+reason, 'open', None))
        if opening >= target:
            candidates.append((2, opening, 'gap_target_2r', 'open', None))
        if low <= stop:
            candidates.append((3, stop, 'touch_'+reason, 'intraday', None))
        if high >= target:
            candidates.append((4, target, 'touch_target_2r', 'intraday', None))
        if index-start+1 == horizon:
            candidates.append((5, close, 'horizon_close', 'close', None))
        if candidates:
            _, fill, why, timing, signal = min(candidates)
            break
        if rule in ('MA20_2C', 'MA50_2C'):
            average = indicators['ma20' if rule == 'MA20_2C' else 'ma50'][index]
            count = count+1 if close < average else 0
            pending = index if count == 2 else None
        if rule == 'ATR_TRAIL':
            peak_high = max(peak_high, high)
            trail = max(trail, initial, peak_high-3*indicators['atr14'][index])
    else:
        return {'exit_price': None, 'stops': logs}
    # Only fully held days contribute closes. A gap exit has no held intraday
    # prices; a touch day's high and low can only supply bounds.
    complete = bars[start:index+(timing == 'close')]
    held_high = max([entry, fill] + [b[1] for b in complete]
                    + ([opening] if timing == 'intraday' else []))
    held_low = min([entry, fill] + [b[2] for b in complete]
                   + ([opening] if timing == 'intraday' else []))
    pct = lambda price: (price / entry - 1) * 100
    net = pct(fill) - cost_pct
    peak = pct(max(b[3] for b in complete))-cost_pct if complete else None
    return dict(exit_price=fill, exit_reason=why, fill_timing=timing,
                holding_sessions=index-start+1, signal_index=signal,
                gross_return_pct=pct(fill), net_return_pct=net,
                gross_R=(fill-entry)/risk, net_R=(fill-entry-entry*cost_pct/100)/risk,
                mfe_lower_pct=pct(held_high),
                mfe_upper_pct=pct(max(held_high, high)) if timing == 'intraday' else pct(held_high),
                mae_lower_pct=pct(min(held_low, low)) if timing == 'intraday' else pct(held_low),
                mae_upper_pct=pct(held_low), peak_held_close_net_pct=peak,
                profit_giveback_pct=(peak-net)/peak*100 if peak is not None and peak > 0 else None,
                stops=logs)


@pytest.mark.parametrize('seed', range(12))
def test_seven_policies_against_scalar_oracle(seed):
    signal, history, cutoff = fixture()
    rng = random.Random(seed)
    start = history.index.get_loc(pd.Timestamp('2026-01-05'))
    price = 100.
    for i in range(start, len(history)):
        opening = price * (1 + rng.uniform(-.08, .08))
        price = opening * (1 + rng.uniform(-.05, .06))
        high = max(opening, price) * (1 + rng.uniform(0, .07))
        low = min(opening, price) * (1 - rng.uniform(0, .07))
        history.iloc[i, history.columns.get_indexer(['Open', 'High', 'Low', 'Close'])] = [opening, high, low, price]
    prepared = prepare(signal, history, cutoff)
    bars = history[['Open', 'High', 'Low', 'Close']].values.tolist()
    expected_indicators = reference_indicators(bars)
    for key, expected in expected_indicators.items():
        for actual, value in zip(prepared.indicators[key], expected):
            assert pd.isna(actual) if value is None else actual == pytest.approx(value)
    for horizon in (20, 60, 120):
        for rule in RULES:
            actual = simulate(prepared, rule, horizon)
            expected = reference_trade(bars, start, horizon, rule, 90.)
            for key, value in expected.items():
                if key in ('signal_index', 'stops'):
                    continue
                assert (actual[key] == pytest.approx(value) if isinstance(value, float) else actual[key] == value), (seed, horizon, rule, key, actual[key], value)
            if 'exclusion_reason' in expected:
                assert actual['eligibility_status'] == 'excluded'
                continue
            assert actual['exit_fill_date'] == str(history.index[start+expected['holding_sessions']-1].date())
            if rule == 'ATR_TRAIL':
                assert [x['active_stop'] for x in actual['stop_path']] == pytest.approx([x[1] for x in expected['stops']])
                assert all(x['information_as_of'] < x['effective_date'] for x in actual['stop_path'])

