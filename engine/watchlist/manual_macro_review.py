"""Read-only source candidates, never permission to collect or substitute prices.

Ticker availability is not proof of asset identity, units, or final sessions.
The stock/ETF EOD price contract remains unchanged.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from .manual_catalog import filter_rows, parse_csv, unique_instruments
from .manual_eod_contract import digest

POLICY = 'manual-macro-source-review-v1'
INDEX_CANDIDATES = {
    'KRX:KOSPI': '^KS11', 'KRX:KOSPI200': '^KS200', 'KRX:KOSDAQ': '^KQ11',
    'TVC:DJI': '^DJI', 'NASDAQ:IXIC': '^IXIC', 'SP:SPX': '^GSPC',
    'TVC:DXY': 'DX-Y.NYB', 'TVC:VIX': '^VIX', 'TVC:SOX': '^SOX',
    'TVC:NI225': '^N225', 'TVC:UKX': '^FTSE', 'XETR:DAX': '^GDAXI',
    'TVC:SX5E': '^STOXX50E', 'TVC:SXXP': '^STOXX', 'SPREADEX:CAC': '^FCHI',
    'TVC:HSI': '^HSI', 'SSE:000001': '000001.SS',
    'SZSE:399001': '399001.SZ', 'SSE:000300': '000300.SS',
}
REASONS = {
    'index': 'MACRO_INDEX_DEFINITION_SESSION_UNVERIFIED',
    'yield': 'MACRO_YIELD_DEFINITION_UNIT_UNVERIFIED',
    'fx': 'MACRO_FX_QUOTE_SESSION_UNVERIFIED',
    'future': 'MACRO_FUTURES_CONTRACT_ROLL_UNVERIFIED',
    'commodity': 'MACRO_SPOT_SOURCE_UNVERIFIED',
    'crypto': 'MACRO_CRYPTO_VENUE_UNVERIFIED',
}


def candidate(provider, symbol, relationship='unverified', data_kind='ohlc_candidate'):
    return dict(provider=provider, symbol=symbol, relationship=relationship, data_kind=data_kind)


def review_instrument(row):
    """Describe gaps without fetching data, changing symbols, or approving OHLC."""
    kind = row['asset_type']
    if kind in ('stock', 'etf'):
        return None
    symbol = row['symbol']; candidates = []
    if kind == 'index' and symbol in INDEX_CANDIDATES:
        candidates.append(candidate('Yahoo Finance', INDEX_CANDIDATES[symbol]))
    elif kind == 'yield':
        yahoo = {'TVC:US10Y': '^TNX', 'TVC:US30Y': '^TYX'}.get(symbol)
        fred = {'TVC:US02Y': 'DGS2', 'TVC:US10Y': 'DGS10', 'TVC:US30Y': 'DGS30'}.get(symbol)
        if yahoo: candidates.append(candidate('Yahoo Finance', yahoo, 'yield_definition_unverified'))
        if fred: candidates.append(candidate('FRED', fred, 'related_constant_maturity_series', 'single_observation'))
    elif kind == 'fx':
        yahoo = {'FX_IDC:USDKRW': 'KRW=X', 'FX_IDC:KRWUSD': 'KRW=X',
                 'FX:EURUSD': 'EURUSD=X', 'FX:USDJPY': 'JPY=X', 'FX:USDCNH': 'CNH=X'}.get(symbol)
        if yahoo:
            candidates.append(candidate('Yahoo Finance', yahoo,
                'inverse_quote_not_substitutable' if symbol == 'FX_IDC:KRWUSD' else 'quote_and_venue_unverified'))
    elif kind == 'future':
        yahoo = {'NYMEX:CL1!': 'CL=F', 'ICEEUR:BRN1!': 'BZ=F',
                 'CME_MINI:RTY1!': 'RTY=F', 'MCX:COPPER1!': 'HG=F'}.get(symbol)
        if yahoo:
            candidates.append(candidate('Yahoo Finance', yahoo,
                'different_contract_not_substitutable' if symbol in ('ICEEUR:BRN1!', 'MCX:COPPER1!') else 'roll_policy_unverified'))
    elif kind == 'commodity' and symbol == 'TVC:GOLD':
        candidates.append(candidate('Yahoo Finance', 'GC=F', 'future_not_spot_not_substitutable'))
    elif kind == 'crypto' and symbol == 'BITSTAMP:BTCUSD':
        candidates.append(candidate('Yahoo Finance', 'BTC-USD', 'aggregate_not_venue_not_substitutable'))
    return dict(policy=POLICY, symbol=symbol, asset_type=kind, unit=row['unit'],
        chart_style=row['chart_style'], collection_enabled=False,
        live_availability='not_tested', reason=REASONS.get(kind, 'MACRO_EOD_POLICY_NOT_CONFIGURED'),
        candidates=candidates,
        required_checks=['asset_definition', 'quote_unit', 'source_identity', 'session_finality',
                         'price_basis', 'payload_contract'])


def review_catalog(rows):
    instruments = [review_instrument(row) for row in unique_instruments(filter_rows(rows))
                   if row['asset_type'] not in ('stock', 'etf')]
    return dict(schema=POLICY, http_requests=0, collection_enabled=False,
        counts_by_asset_type=dict(Counter(item['asset_type'] for item in instruments)),
        population=len(instruments), instruments=instruments)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    source = Path(args.catalog); output = Path(args.output)
    if source.resolve() == output.resolve():
        parser.error('output must not overwrite the catalog')
    raw = source.read_bytes(); report = review_catalog(parse_csv(raw))
    report['catalog_sha256'] = digest(raw)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()

