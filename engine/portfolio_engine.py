"""Pure portfolio calculations. No credentials, network, writes or notifications.

Legacy accounting uses buy-before-sell order on the same date and excludes fees.
The private portfolio view applies its fractional policy to a copy only.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import math
import re
import pandas as pd

def compute_realized(ledger: pd.DataFrame) -> dict:
    """원장 기반 이동평균법 실현손익 (수수료·세금 제외).
    반환: {(date, code, currency): {"pnl", "cost", "known"}}  known=False → 원장에 매수 이력 부족.
    계좌×종목별로 보유수량/원가를 따로 추적하며, 같은 날은 매수를 먼저 처리한다."""
    out = {}
    if ledger is None or ledger.empty:
        return out
    df = ledger.copy()
    df["is_buy"] = df["tx_type"].astype(str).str.contains("매수", na=False)
    df["is_sell"] = df["tx_type"].astype(str).str.contains("매도", na=False)
    df = df[df["is_buy"] | df["is_sell"]]
    df = df.assign(ord=df["is_sell"].astype(int)).sort_values(["date", "ord"], kind="stable")
    hold = {}
    for r in df.itertuples(index=False):
        h = hold.setdefault((r.account, r.code, r.currency), [0.0, 0.0])  # [수량, 총원가]
        qty, amt = float(r.qty), float(r.total_amt)
        if r.is_buy:
            h[0] += qty
            h[1] += amt
            continue
        rec = out.setdefault((r.date, r.code, r.currency), {"pnl": 0.0, "cost": 0.0, "known": True})
        if h[0] > 0 and h[0] + 1e-9 >= qty:
            cost = h[1] / h[0] * qty
            rec["pnl"] += amt - cost
            rec["cost"] += cost
            h[0] -= qty
            h[1] -= cost
            if h[0] < 1e-9:
                h[0] = h[1] = 0.0
        else:
            rec["known"] = False
            h[0] = h[1] = 0.0
    return out


def compute_positions(ledger: pd.DataFrame) -> pd.DataFrame:
    """원장 기반 체결별 포지션(이동평균법). 인덱스는 ledger 와 동일.
    열: after_qty(체결 후 계좌별 잔량), after_avg(체결 후 평단), sell_pnl/sell_cost(매도 체결의 실현손익·원가).
    원장에 매수 이력이 부족한 매도는 after_qty/sell_pnl 이 NaN. 같은 날은 매수를 먼저 처리한다."""
    cols = ["after_qty", "after_avg", "sell_pnl", "sell_cost"]
    pos = pd.DataFrame(float("nan"), index=ledger.index, columns=cols)
    if ledger is None or ledger.empty:
        return pos
    df = ledger.copy()
    df["is_buy"] = df["tx_type"].astype(str).str.contains("매수", na=False)
    df["is_sell"] = df["tx_type"].astype(str).str.contains("매도", na=False)
    df = df[df["is_buy"] | df["is_sell"]]
    df = df.assign(_ord=df["is_sell"].astype(int)).sort_values(["date", "_ord"], kind="stable")
    hold = {}
    for r in df.itertuples():
        h = hold.setdefault((r.account, r.code, r.currency), [0.0, 0.0])
        qty, amt = float(r.qty), float(r.total_amt)
        if r.is_buy:
            h[0] += qty
            h[1] += amt
            pos.loc[r.Index, ["after_qty", "after_avg"]] = [h[0], h[1] / h[0] if h[0] else 0.0]
            continue
        if h[0] > 0 and h[0] + 1e-9 >= qty:
            cost = h[1] / h[0] * qty
            h[0] -= qty
            h[1] -= cost
            if h[0] < 1e-9:
                h[0] = h[1] = 0.0
            pos.loc[r.Index, cols] = [h[0], h[1] / h[0] if h[0] else 0.0, amt - cost, cost]
        else:
            h[0] = h[1] = 0.0  # 이력 부족 → 이후 추적은 0에서 다시 시작
    return pos


def holding_risk(item: dict, level: dict = None, *, limits: dict) -> dict:
    """보유 종목 1건의 리스크 지표. stop 이 없으면 default_stop_pct 로 평단 기준 기본 손절가를 쓴다.
    R = (현재가 − 평단) ÷ (평단 − 손절가),  dist = 현재가가 손절가보다 몇 % 위인지(음수면 이탈)."""
    qty = float(item["qty"])
    price, avg = (item["value"] / qty, item["cost"] / qty) if qty else (None, None)
    level = level or {}
    stop, default = level.get("stop"), False
    if stop is None and avg and limits["default_stop_pct"] > 0:
        stop, default = avg * (1 - limits["default_stop_pct"] / 100), True
    r_unit = (avg - stop) if (stop and avg and avg > stop) else None
    dist = (price / stop - 1) * 100 if (stop and price) else None
    status = "none" if stop is None else "breach" if dist <= 0 else "near" if dist <= limits["stop_near_pct"] else "ok"
    return {"price": price, "avg": avg, "stop": stop, "default": default, "target": level.get("target"),
            "R": (price - avg) / r_unit if (r_unit and price) else None, "dist": dist, "status": status,
            "open_risk": qty * max(price - stop, 0.0) if (stop and price) else None}


POSITION_COLUMNS = ['account', 'code', 'currency', 'date', 'tx_type', 'qty', 'total_amt']


def normalize_ledger(ledger):
    """Validate a copy without coercing corrupt source numbers to zero."""
    if not set(POSITION_COLUMNS).issubset(ledger.columns):
        raise ValueError('원장 필수 컬럼 누락')
    df = ledger.copy(deep=True).reset_index(drop=True)
    # Missing group keys must not disappear silently in pandas.groupby.
    for column in ('account', 'code'):
        if df[column].isna().any() or df[column].astype(str).str.strip().eq('').any():
            raise ValueError('원장 계좌·종목 식별자 누락')
    df['code'] = df['code'].astype(str).str.strip().str.upper()
    df['currency'] = df['currency'].astype(str).str.upper()
    if not df['currency'].isin(['KRW', 'USD']).all():
        raise ValueError('지원하지 않는 원장 통화')
    kr = df['currency'].eq('KRW')
    df.loc[kr, 'code'] = df.loc[kr, 'code'].str.removeprefix('A').str.zfill(6)
    df['market'] = df['currency'].map({'KRW': 'KR', 'USD': 'US'})
    df['_date'] = pd.to_datetime(df['date'], errors='coerce')
    if df['_date'].isna().any():
        raise ValueError('원장 날짜 오류')
    df['_sell'] = df['tx_type'].astype(str).str.contains('매도')
    buy = df['tx_type'].astype(str).str.contains('매수')
    if (buy == df['_sell']).any():
        raise ValueError('지원하지 않거나 모호한 거래 구분')
    fractional = []
    for value in df['qty']:
        try:
            q = Decimal(str(value))
        except InvalidOperation:
            raise ValueError('원장 수량 오류') from None
        if not q.is_finite() or q <= 0:
            raise ValueError('원장 수량 오류')
        fractional.append(q != q.to_integral_value())
    df['_fractional'] = fractional
    for col in ('qty', 'total_amt'):
        df[col] = pd.to_numeric(df[col], errors='coerce')
        if not df[col].map(lambda x: math.isfinite(x) and x > 0).all():
            raise ValueError('원장 수치 오류')
    return df


def security_name(code, *values):
    """Prefer available names over ticker placeholders without another API call."""
    for value in values:
        if isinstance(value, str):
            name = value.strip()
            if name and name.casefold() not in {'nan', 'none', '<na>', str(code).casefold()}:
                return name
    return str(code)


def build_snapshot(ledger, *, generated_at, ledger_sha, quotes=None, levels=None,
                   diagnostics=None, fx=None, fx_as_of=None, equity_krw=None,
                   market_source=None, reconciliation=None, limits=None, signal_events=None,
                   equity_as_of=None, names=None):
    """Build a derived private view; never mutate or replace the source ledger.

    quotes[(market, code)] = {price, as_of, source}; unknown/stale marks suppress
    aggregate heat/exposure. Diagnostics must not invent universe RS percentiles.
    """
    quotes, levels, diagnostics = quotes or {}, levels or {}, diagnostics or {}
    names = names or {}
    limits = limits or {'default_stop_pct': 8.0, 'stop_near_pct': 3.0}
    df = normalize_ledger(ledger)
    positions, closed = [], []
    for (account, market, code, currency), group in df.groupby(['account', 'market', 'code', 'currency'], sort=False):
        flags = []
        if group['_fractional'].any():
            flags.append('fractional_excluded')
        view = group[~group['_fractional']].sort_values(['_date', '_sell'], kind='stable')
        if view.groupby('date')['_sell'].nunique().gt(1).any():
            flags.append('order_ambiguous')
        result = compute_positions(view)
        bad = (view['_sell'] & result['sell_pnl'].isna()).any()
        if bad:
            flags += ['insufficient_inventory', 'cost_unknown']
        signed = view['qty'].where(~view['_sell'], -view['qty']).sum()
        qty = float(signed)
        avg = None if bad or view.empty else float(result['after_avg'].iloc[-1])
        pnl = None if bad else float(result['sell_pnl'].sum())
        if qty < 0:
            flags.append('negative_inventory')
        ledger_names = group.sort_values('_date', kind='stable')['name'].tolist()[::-1] if 'name' in group else []
        item = dict(account=str(account), market=market, code=code,
                    name=security_name(code, names.get((market, code)), *ledger_names), currency=currency,
                    qty=qty, avg_cost=avg, realized_pnl=pnl, quality=flags)
        # Same-day ordering affects historical performance, not a flat position's
        # current exposure. Inventory/fractional uncertainties still block risk.
        if qty == 0 and set(flags) <= {'order_ambiguous'}:
            closed.append(item)
            continue
        quote = quotes.get((market, code), {})
        price, as_of = quote.get('price'), quote.get('as_of')
        if price is not None and (not math.isfinite(float(price)) or float(price) <= 0):
            price = None
        if not as_of or price is None:
            flags.append('quote_missing')
        elif not 0 <= (pd.Timestamp(generated_at).date() - pd.Timestamp(as_of).date()).days <= 4:
            flags.append('quote_stale')
        item.update(price=price, quote_as_of=as_of, quote_source=quote.get('source'),
                    diagnostic=diagnostics.get((market, code), {}))
        if not item['diagnostic']:
            flags.append('technical_missing')
        item['value'] = qty * price if price is not None and qty >= 0 else None
        item['unrealized_pnl'] = qty * (price - avg) if price is not None and avg is not None and qty >= 0 else None
        risk = {'stop': None, 'status': 'none', 'open_risk': None}
        if avg is not None and item['value'] is not None and qty > 0:
            risk = holding_risk({'qty': qty, 'value': item['value'], 'cost': qty * avg},
                                levels.get((code, currency)), limits=limits)
        item['risk'] = risk
        item['trend_state'] = trend_state(item)
        if risk['stop'] is None:
            flags.append('stop_missing')
        positions.append(item)
    totals = {}
    for currency in ('KRW', 'USD'):
        rows = [p for p in positions if p['currency'] == currency]
        totals[currency] = {'value': sum(p['value'] for p in rows) if all(p['value'] is not None for p in rows) else None}
    fx_valid = (fx is not None and math.isfinite(float(fx)) and fx > 0 and fx_as_of
                and 0 <= (pd.Timestamp(generated_at).date() - pd.Timestamp(fx_as_of).date()).days <= 4)
    complete = all(not p['quality'] for p in positions)
    usd = any(p['currency'] == 'USD' for p in positions)
    value = risk_sum = None
    if complete and (not usd or fx_valid):
        value = sum(p['value'] * (fx if p['currency'] == 'USD' else 1) for p in positions)
        risk_sum = sum(p['risk']['open_risk'] * (fx if p['currency'] == 'USD' else 1) for p in positions)
    nav = float(equity_krw) if equity_krw and math.isfinite(float(equity_krw)) and equity_krw > 0 else None
    nav_current = False
    try:
        nav_current = nav is not None and equity_as_of is not None and 0 <= (
            pd.Timestamp(generated_at).date() - pd.Timestamp(equity_as_of).date()).days <= 1
    except (ValueError, TypeError):
        pass
    if not nav_current:
        nav = None
    denominator = nav or value
    summary = dict(value_krw=value, heat_krw=risk_sum,
                   heat_pct=100 * risk_sum / denominator if denominator and risk_sum is not None else None,
                   equity_basis='configured_nav' if nav else 'stocks_only', equity_krw=denominator,
                   exposure_pct=100 * value / nav if nav and value is not None else None,
                   complete=bool(complete and (not usd or fx_valid)), by_currency=totals,
                   nav_status='current' if nav_current else 'unconfirmed')
    analytics = trade_analytics(df, signal_events or [])
    summary["equity_as_of"] = equity_as_of
    return dict(analytics=analytics, schema_version='1.0', calculation_policy_version='ma-gross-fractional-excluded-v1',
                generated_at=generated_at, ledger_blob_sha=ledger_sha, market_source=market_source or {},
                fx={'rate': fx, 'as_of': fx_as_of, 'valid': bool(fx_valid)},
                reconciliation=reconciliation or {'status': 'not_checked'},
                positions=positions, closed=closed, summary=summary)


def _utc_timestamp(value):
    """Reject naive availability times instead of guessing publication time."""
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert('UTC') if not pd.isna(stamp) and stamp.tzinfo else None
    except (TypeError, ValueError):
        return None


def match_entry_signal(row, events):
    market = row['market']
    # Collector often has only a date/00:00:00. Use start of the local trading
    # date as a conservative cutoff; never use that day's closing information.
    local_date = str(row['date'])[:10]
    time = str(row.get('time', '') or '')
    zone = 'America/New_York' if market == 'US' else 'Asia/Seoul'
    precise = bool(re.fullmatch(r'\d{2}:\d{2}:\d{2}', time)) and time != '00:00:00'
    try:
        cutoff = pd.Timestamp(f'{local_date} {time if precise else "00:00:00"}').tz_localize(zone).tz_convert('UTC')
    except (ValueError, TypeError):
        return {'status': 'trade_time_unknown'}
    eligible = []
    for event in events:
        if event.get('market') != market or str(event.get('ticker')) != row['code']:
            continue
        available = _utc_timestamp(event.get('published_at'))
        # A finite 30-day horizon avoids attaching an unrelated old setup.
        age_seconds = (cutoff.to_pydatetime() - available.to_pydatetime()).total_seconds() if available is not None else -1
        if 0 <= age_seconds <= 30 * 86400:
            try:
                if pd.Timestamp(event['as_of']).date() > cutoff.tz_convert(zone).date():
                    continue
            except (KeyError, ValueError, TypeError):
                continue
            eligible.append((available, event))
    if not eligible:
        return {'status': 'no_prior_published_signal', 'cutoff': cutoff.isoformat()}
    available, event = max(eligible, key=lambda item: item[0])
    return {'status': 'matched', 'published_at': available.isoformat(),
            'cutoff': cutoff.isoformat(), 'time_basis': 'execution_time' if precise else 'before_trading_date',
            **{k: event.get(k) for k in ('as_of', 'setup_stage', 'regime', 'rs_score', 'run_id', 'workflow_run_id', 'availability_source')}}


def fifo_results(view):
    """FIFO supplementary gross/net-of-recorded-costs P&L, keyed by source index.

    It never changes the moving-average accounting used by journals. Unknown
    inventory invalidates this group's subsequent FIFO results conservatively.
    """
    from collections import deque
    lots, output, invalid = deque(), {}, False
    for idx, row in view.iterrows():
        qty, amount = Decimal(str(row['qty'])), Decimal(str(row['total_amt']))
        costs = Decimal('0')
        for col in ('fee', 'tax'):
            try:
                value = Decimal(str(row.get(col, 0) or 0))
            except InvalidOperation:
                raise ValueError('거래 비용 오류') from None
            if not value.is_finite() or value < 0:
                raise ValueError('거래 비용 오류')
            costs += value
        if not row['_sell']:
            lots.append([qty, amount / qty, costs / qty])
            continue
        if invalid or sum((lot[0] for lot in lots), Decimal('0')) < qty:
            invalid = True
            output[idx] = {'gross': None, 'recorded_costs_net': None, 'known': False}
            continue
        remaining, cost, entry_costs = qty, Decimal('0'), Decimal('0')
        while remaining > 0:
            lot = lots[0]
            used = min(remaining, lot[0])
            cost += used * lot[1]
            entry_costs += used * lot[2]
            remaining -= used
            lot[0] -= used
            if not lot[0]:
                lots.popleft()
        output[idx] = {'gross': float(amount - cost),
                       'recorded_costs_net': float(amount - cost - entry_costs - costs), 'known': True}
    return output


def trade_analytics(df, events):
    """Derived execution/cycle views; source references point to the sole ledger."""
    trades, cycles = [], []
    for key, group in df.groupby(['account', 'market', 'code', 'currency'], sort=False):
        account, market, code, currency = key
        warnings = ['fractional_excluded'] if group['_fractional'].any() else []
        view = group[~group['_fractional']].sort_values(['_date', '_sell'], kind='stable')
        if view.groupby('date')['_sell'].nunique().gt(1).any():
            warnings.append('order_ambiguous')
        ma, fifo = compute_positions(view), fifo_results(view)
        cycle = None
        for idx, row in view.iterrows():
            if cycle is None:
                cycle = {'account': str(account), 'market': market, 'code': code, 'currency': currency,
                         'start': str(row['date']), 'end': None, 'ma_pnl': 0.0, 'fifo_pnl': 0.0,
                         'sold_cost': 0.0, 'quality': list(warnings),
                         'entry_signal': match_entry_signal(row, events) if not row['_sell'] else {'status': 'orphan_sell'}}
            pnl, cost = ma.loc[idx, ['sell_pnl', 'sell_cost']]
            sale = fifo.get(idx, {})
            if row['_sell']:
                if pd.isna(pnl) or not sale.get('known'):
                    if 'cost_unknown' not in cycle['quality']:
                        cycle['quality'].append('cost_unknown')
                    cycle['ma_pnl'] = cycle['fifo_pnl'] = None
                elif cycle['ma_pnl'] is not None:
                    cycle['ma_pnl'] += float(pnl)
                    cycle['fifo_pnl'] += sale['gross']
                    cycle['sold_cost'] += float(cost)
            trades.append({'source_row': int(idx), 'trade_id': str(row.get('trade_id', '')),
                           'account': str(account), 'market': market, 'code': code, 'currency': currency,
                           'date': str(row['date']), 'side': 'sell' if row['_sell'] else 'buy',
                           'qty': float(row['qty']), 'amount': float(row['total_amt']),
                           'ma_realized': None if pd.isna(pnl) else float(pnl),
                           'fifo_realized': sale.get('gross'), 'fifo_recorded_costs_net': sale.get('recorded_costs_net'),
                           'entry_signal': cycle['entry_signal'], 'quality': list(cycle['quality'])})
            after = ma.loc[idx, 'after_qty']
            if row['_sell'] and pd.notna(after) and after == 0:
                cycle['end'] = str(row['date'])
                cycle['return_pct'] = (cycle['ma_pnl'] / cycle['sold_cost'] * 100
                                       if cycle['ma_pnl'] is not None and cycle['sold_cost'] else None)
                cycles.append(cycle)
                cycle = None
        if cycle is not None:
            cycles.append(cycle)
    groups = {}
    for cycle in cycles:
        if not cycle['end'] or cycle['ma_pnl'] is None or cycle['quality']:
            continue
        signal = cycle['entry_signal']
        key = (cycle['currency'], signal.get('setup_stage') or signal['status'], signal.get('regime') or 'unknown')
        agg = groups.setdefault(key, {'currency': key[0], 'setup': key[1], 'regime': key[2],
                                      'count': 0, 'wins': 0, 'ma_pnl': 0.0})
        agg['count'] += 1
        agg['wins'] += int(cycle['ma_pnl'] > 0)
        agg['ma_pnl'] += cycle['ma_pnl']
    return {'trades': trades, 'cycles': cycles, 'signal_performance': list(groups.values())}


def held_budget(snapshot):
    """NAV percentage inputs for the existing allocator; fail closed on gaps."""
    summary = snapshot['summary']
    reasons = []
    if not summary['complete']:
        reasons.append('portfolio_incomplete')
    if summary['equity_basis'] != 'configured_nav' or not summary.get('equity_krw'):
        reasons.append('nav_missing')
    try:
        age = (pd.Timestamp(snapshot['generated_at']).date() - pd.Timestamp(summary['equity_as_of']).date()).days
        if not 0 <= age <= 1:
            reasons.append('nav_stale')
    except (ValueError, KeyError, TypeError):
        reasons.append('nav_date_missing')
    recon = snapshot['reconciliation']
    if recon.get('status') != 'checked' or recon.get('mismatch_count') != 0:
        reasons.append('reconciliation_unconfirmed')
    rows = []
    nav = summary.get('equity_krw') or 1
    for p in snapshot['positions']:
        if p['qty'] <= 0:
            if p['quality']:
                reasons.append('inventory_unknown')
            continue
        d = p.get('diagnostic') or {}
        if d.get('advanced_status') in ('stale', 'unavailable', 'insufficient_history'):
            reasons.append('technical_unconfirmed')
        if not d.get('sector') or not d.get('macro_theme'):
            reasons.append('classification_missing')
        if (p['risk']['status'] in ('breach', 'near') or (d.get('trail') or {}).get('broken')
                or p.get('trend_state') == 'trend_broken'):
            reasons.append('holding_risk_alert')
        rate = snapshot['fx']['rate'] if p['currency'] == 'USD' else 1
        value, risk = p['value'], p['risk'].get('open_risk')
        if rate is None or value is None or risk is None:
            reasons.append('valuation_missing')
            continue
        rows.append({'market': p['market'], 'code': p['code'], 'sector': d.get('sector'),
                     'macro_theme': d.get('macro_theme'), 'exposure_pct': value * rate / nav * 100,
                     'risk_pct': risk * rate / nav * 100})
    return {'complete': not reasons, 'reasons': sorted(set(reasons)), 'positions': rows}


def reentry_watch(snapshot, diagnostics, regimes):
    """Observe a NEW setup after closure, never imply an order or recover losses."""
    last = {}
    for cycle in snapshot.get('analytics', {}).get('cycles', []):
        if cycle['end']:
            key = (cycle['account'], cycle['market'], cycle['code'])
            if key not in last or cycle['end'] > last[key]['end']:
                last[key] = cycle
    active = {(p['account'], p['market'], p['code']) for p in snapshot['positions'] if p['qty'] > 0}
    rows = []
    for key, cycle in last.items():
        if key in active:
            continue
        d = diagnostics.get((cycle['market'], cycle['code']), {})
        row = {**{k: cycle[k] for k in ('account', 'market', 'code', 'end')},
               'status': 'data_unknown', 'as_of': d.get('as_of')}
        if cycle['quality']:
            row['status'] = 'accounting_uncertain'
        elif (d and d.get('as_of') and d['as_of'] > cycle['end']
              and not d.get('is_stale_price') and 0 <=
              (pd.Timestamp(snapshot['generated_at']).date() - pd.Timestamp(d['as_of']).date()).days <= 4):
            market_regimes = [v for k, v in regimes.items() if k in
                              ({'KOSPI', 'KOSDAQ'} if cycle['market'] == 'KR' else {'SP500', 'NASDAQ100'})]
            ready = d.get('is_actionable') is True and (d.get('new_breakout') is True
                    or bool(d.get('setup_started_at') and d['setup_started_at'] > cycle['end']))
            try:
                regime_current = market_regimes and all(0 <= (
                    pd.Timestamp(snapshot['generated_at']).date() - pd.Timestamp(v['as_of']).date()).days <= 4
                    for v in market_regimes)
            except (KeyError, ValueError, TypeError):
                regime_current = False
            if not regime_current:
                row['status'] = 'regime_unknown'
            elif any(v.get('regime') == 'RISK_OFF' for v in market_regimes):
                row['status'] = 'defensive_watch'
            elif ready:
                row['status'] = 'new_setup_watch'
            else:
                row['status'] = 'waiting_new_setup'
        rows.append(row)
    return rows


def trend_state(position):
    """Classify existing exit/trailing/RS evidence without inventing a new score."""
    d, risk = position.get('diagnostic') or {}, position['risk']
    if any(flag in position['quality'] for flag in ('quote_missing', 'quote_stale')):
        return 'data_unknown'
    if risk['status'] == 'breach' or (d.get('trail') or {}).get('broken'):
        return 'trend_broken'
    signal = str(d.get('exit_signal', ''))
    if '🚨' in signal:
        return 'trend_broken'
    if '✂' in signal:
        return 'weakening'
    if '⚠' in signal:
        return 'early_weakening'
    rs = d.get('rs')
    if rs and len(rs) >= 2 and all(isinstance(v, (int, float)) and v < 0 for v in rs[:2]):
        return 'early_weakening'
    if d.get('advanced_status') == 'computed' and signal == '✅정상':
        return 'normal_trend'
    return 'data_unknown'

