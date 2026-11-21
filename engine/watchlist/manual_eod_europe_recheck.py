"""Read-only, bounded EOD recheck for stale DE/CH/ES watchlist charts.

This command cannot publish, mutate the chart cache, change the catalog,
or promote a stale bar. It only verifies whether the normal YahooDaily
provider now supplies a genuine confirmed session, recording European
source-date evidence on a separate short-lived GitHub Actions artifact.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .manual_eod_contract import aware, canonical, validate_snapshot
from .manual_eod_provider import Unavailable, YahooDaily, specification
from .manual_eod_publish import read_previous
from .manual_eod_sessions import latest_session
from .manual_eod_source_diagnostics import EUROPE_MARKETS

RECHECK_DELAY = timedelta(hours=2)
MAX_RECHECK_REQUESTS = 16
MAX_RECHECK_SYMBOLS = 16


def eligible_stale(snapshot):
    """Work from the original attested snapshot, never the catalog aliases."""
    validate_snapshot(snapshot)
    return [(sym, item) for sym, item in snapshot['instruments'].items()
            if item.get('market') in EUROPE_MARKETS
            and item.get('status') == 'stale'
            and bool(item.get('bars'))]


def _source_evidence(log, symbol):
    for item in reversed(log):
        if item.get('symbol') == symbol and item.get('europe_source_days'):
            return item['europe_source_days']
    return None


def recheck_stale_europe(snapshot, provider, *, now, max_requests=MAX_RECHECK_REQUESTS,
                         max_symbols=MAX_RECHECK_SYMBOLS):
    """Investigate prices without writing or building any EOD publication.

    Every actual provider attempt uses YahooDaily's existing exchange/asset/
    currency/interval/real-OHLC validation and per-market 429 circuit. Missing
    or invalid dates are NEVER fabricated. The real publisher remains the only
    place capable of promoting a chart from stale to current.
    """
    if now.tzinfo is None:
        raise ValueError('AWARE_RECHECK_TIME_REQUIRED')
    if type(max_requests) is not int or not 1 <= max_requests <= MAX_RECHECK_REQUESTS:
        raise ValueError('RECHECK_REQUEST_CAP_INVALID')
    if type(max_symbols) is not int or not 1 <= max_symbols <= MAX_RECHECK_SYMBOLS:
        raise ValueError('RECHECK_SYMBOL_CAP_INVALID')
    candidates = eligible_stale(snapshot)
    results = []
    calls_at_start = getattr(provider, 'calls', 0)
    for sym, old in candidates[:max_symbols]:
        evidence = dict(symbol=sym, market=old['market'],
            prior_status=old['status'], prior_last_final_session=old['last_final_session'],
            prior_expected_session=old.get('expected_session'),
            prior_last_attempt_at=old.get('last_attempt_at'),
            request_count=0)
        results.append(evidence)
        try:
            spec=specification(dict(symbol=sym, asset_type=old['asset_type'],
                                    unit=old['currency']))
            if (spec['market'] != old['market'] or spec['currency'] != old['currency']
                or spec['exchange'] != old['exchange']):
                evidence.update(outcome='LISTING_DEFINITION_CHANGED')
                continue
            target=latest_session(spec['market'],now)
            evidence['target_session']=target
            if old['last_final_session']>=target:
                evidence['outcome']='NO_NEW_CONFIRMED_SESSION'
                continue
            last_attempt=old.get('last_attempt_at')
            if last_attempt:
                next_allowed=aware(last_attempt).astimezone(timezone.utc)+RECHECK_DELAY
                if now < next_allowed:
                    evidence.update(outcome='WAIT_FOR_COOLDOWN',
                                    next_eligible_utc=next_allowed.isoformat())
                    continue
            if getattr(provider, 'calls', 0)-calls_at_start>=max_requests:
                evidence['outcome']='RECHECK_HTTP_BUDGET_DEFERRED'
                continue
            before=getattr(provider, 'calls', 0)
            try:
                # Fetch a small overlapping window only: do not download the
                # entire 2-year history for a read-only diagnostic.
                since=(datetime.fromisoformat(old['last_final_session']).date()
                       -timedelta(days=10)).isoformat()
                bars, identity, excluded=provider.fetch(spec,now,start=since)
                latest=bars[-1]['d'] if bars else None
                evidence.update(
                    outcome=('VALID_FINAL_BAR_NOW_AVAILABLE'
                        if latest==target else 'VALID_PRICES_STILL_BEHIND'),
                    verified_listing=True, latest_verified_session=latest,
                    excluded_unfinalized_or_incomplete=excluded)
            except Unavailable as exc:
                evidence.update(outcome='PROVIDER_REJECTED',reason=str(exc))
            finally:
                evidence['request_count']=getattr(provider,'calls',0)-before
                source=_source_evidence(getattr(provider,'request_log',[]),sym)
                if source is not None:
                    evidence['source_day_evidence']=source
        except (ValueError,KeyError,TypeError) as exc:
            # Keep invalid source definitions out of the price collection.
            evidence.update(outcome='RECHECK_VALIDATION_UNAVAILABLE',
                            reason=type(exc).__name__)
    statuses={}
    for entry in results:
        label=entry.get('outcome','UNKNOWN')
        statuses[label]=statuses.get(label,0)+1
    return dict(schema='manual-eod-europe-recheck-v1',
        source_snapshot_run_id=snapshot['run_id'],
        source_core_run_id=snapshot['core_run_id'],
        source_catalog_sha256=snapshot['catalog_sha256'],
        observed_at_utc=now.astimezone(timezone.utc).isoformat(),
        market_scope=sorted(EUROPE_MARKETS),
        diagnostic_only=True, published_prices_modified=False,
        direct_http_cap=max_requests, attempted_direct_http=max(
            0,getattr(provider,'calls',0)-calls_at_start),
        candidate_count=len(candidates),
        inspected_count=len(results), outcomes=statuses,
        instruments=results)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--published',default='watchlist/published')
    parser.add_argument('--output',required=True)
    parser.add_argument('--max-requests',type=int,default=MAX_RECHECK_REQUESTS)
    parser.add_argument('--max-symbols',type=int,default=MAX_RECHECK_SYMBOLS)
    args=parser.parse_args(argv)
    root=Path(args.published).resolve()
    output=Path(args.output).resolve()
    if output.is_relative_to(root):
        raise ValueError('DIAGNOSTIC_MUST_NOT_TOUCH_PUBLISHED_NAMESPACE')
    snapshot=read_previous(root)
    if snapshot is None:
        raise ValueError('MANUAL_EOD_SNAPSHOT_NOT_PRESENT')
    provider=YahooDaily(max_calls=MAX_RECHECK_REQUESTS,
                        max_seconds=240)
    report=recheck_stale_europe(snapshot,provider,
        now=datetime.now(timezone.utc),max_requests=args.max_requests,
        max_symbols=args.max_symbols)
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_bytes(canonical(report))
    print('read-only European EOD recheck: '+str(report['outcomes']))
    print('HTTP calls: '+str(report['attempted_direct_http'])+
          ', published charts modified: false')


if __name__=='__main__':
    main()

