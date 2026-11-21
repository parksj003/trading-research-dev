"""Read-only, date-precise historical manual EOD integrity audit.

The audit is derived from the independently validated immutable snapshot.
It does not use speculative symbols, access the provider, infer suspension
causes, substitute prices, or change any published chart.
"""
from __future__ import annotations

from collections import Counter

from .manual_eod_contract import validate_snapshot
from .manual_eod_sessions import calendar


def _full_missing_dates(market,dates):
    sessions=calendar(market).sessions_in_range(dates[0],dates[-1])
    observed=set(dates)
    return [x.date().isoformat() for x in sessions
            if x.date().isoformat() not in observed]


def audit_missing_history(doc):
    """Return fully enumerated exchange-day gaps and source-error candidates."""
    validate_snapshot(doc)
    gap_items=[];missing_price=[];integrity_price=[];calendar_unverified=[]
    for symbol,item in doc['instruments'].items():
        bars=item.get('bars') or {}
        dates=bars.get('d') or []
        history=item.get('history_coverage') or {}
        count=history.get('missing_between_first_last')
        if item.get('history_coverage_status')=='UNVERIFIED':
            calendar_unverified.append(symbol)
        if dates and count:
            missing=_full_missing_dates(item['market'],dates)
            if len(missing)!=count:
                raise ValueError('HISTORY_GAP_COUNT_MISMATCH')
            gap_items.append(dict(
                symbol=symbol,market=item['market'],status=item['status'],
                first_session=dates[0],last_session=dates[-1],
                observed_bars=len(dates),missing_sessions=len(missing),
                missing_dates=missing,
                likely_cause='UNDETERMINED_PROVIDER_GAP_OR_TRADING_SUSPENSION',
                trailing_missing_sessions=history.get('trailing_missing_sessions',0)))
        if item.get('status')=='missing':
            reason=item.get('reason') or 'NOT_RECORDED'
            missing_price.append(dict(symbol=symbol,market=item.get('market'),
                reason=reason,
                next_review=('CLOSE_ONLY_ELIGIBLE_IF_REAL_CLOSE_EXISTS'
                             if reason in ('INVALID_REAL_OHLC','INVALID_OHLC_VALUE')
                             else 'LISTING_OR_PROVIDER_IDENTITY_REVIEW'),
                close_only_probe=item.get('close_only_probe'),
                close_only_verified_bars=len((item.get('close_only') or {}).get('d') or [])))
        if item.get('reason') in ('INVALID_REAL_OHLC','INVALID_OHLC_VALUE'):
            integrity_price.append(dict(symbol=symbol,market=item.get('market'),
                status=item['status'],reason=item['reason'],
                close_only_probe=item.get('close_only_probe'),
                close_only_verified_bars=len((item.get('close_only') or {}).get('d') or [])))
    # Shared historical gaps are diagnostic candidates for a provider/session
    # incident, NOT trading suspensions and NOT evidence for synthetic bars.
    # Count only actual absent sessions between observed first/last records.
    occurrences=Counter((entry['market'],day) for entry in gap_items
                        for day in entry['missing_dates'])
    common_dates=[]
    for (market,day),count in sorted(occurrences.items(),
                                    key=lambda pair:(-pair[1],pair[0])):
        if count<3:
            continue
        symbols=sorted(entry['symbol'] for entry in gap_items
                       if entry['market']==market and day in entry['missing_dates'])
        common_dates.append(dict(market=market,date=day,affected_symbols=count,
            symbols=symbols,
            review='MULTI_SYMBOL_GAP_SOURCE_COMPARISON_REQUIRED',
            market_calendar_session=True,
            external_stock_price_verified=False))
    return dict(schema='manual-eod-history-integrity-audit-v1',
        run_id=doc['run_id'],core_run_id=doc['core_run_id'],
        catalog_sha256=doc['catalog_sha256'],snapshot_generated_at=doc['generated_at'],
        basis='observed exchange-calendar sessions; no price fill',
        scope='manual EOD OHLC only; close-only provenance separate',
        historical_gaps_exclude_trailing_new_sessions=True,
        gap_symbol_count=len(gap_items),
        missing_historical_session_count=sum(x['missing_sessions'] for x in gap_items),
        calendar_unverified_count=len(calendar_unverified),
        calendar_unverified_symbols=sorted(calendar_unverified),
        missing_price_chart_count=len(missing_price),
        gap_items=gap_items,
        common_gap_dates=common_dates,
        missing_price_charts=missing_price,
        integrity_failures=integrity_price,
        external_source_comparison_performed=False,
        trading_suspension_cause_verified=False,
        published_prices_modified=False)


def main(argv=None):
    import argparse
    from pathlib import Path

    from .manual_eod_contract import canonical
    from .manual_eod_publish import read_previous

    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--published',default='watchlist/published')
    parser.add_argument('--output',required=True)
    args=parser.parse_args(argv)
    root=Path(args.published).resolve()
    output=Path(args.output).resolve()
    if output.is_relative_to(root):
        raise ValueError('HISTORY_AUDIT_CANNOT_EDIT_PUBLISHED_DATA')
    source=read_previous(root)
    if source is None:
        raise ValueError('HISTORY_AUDIT_SNAPSHOT_MISSING')
    report=audit_missing_history(source)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(canonical(report))
    print('read-only history audit: run_id='+report['run_id'])
    print('gap symbols='+str(report['gap_symbol_count'])+
          ', missing historical sessions='+str(report['missing_historical_session_count']))
    print('missing OHLC charts='+str(report['missing_price_chart_count'])+
          ', prices modified: false')


if __name__=='__main__':
    main()

