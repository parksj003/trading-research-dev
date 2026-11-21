"""Strictly segregated close-only rescue for manual EOD chart display.

The primary OHLC status/bars and all strategy/backtest datasets are immutable
from this module. Close-only is standalone raw Yahoo pricing, never merged into
an adjusted or partially invalid candle. The same verified identity and
exchange-session policy are required.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from .manual_eod_contract import aware, MAX_BARS
from .manual_eod_provider import BASIS, Unavailable, specification
from .manual_eod_history_coverage import session_coverage
from .manual_eod_sessions import latest_session

MAX_FALLBACK_REQUESTS=16
# Reserve only for already evidenced price-integrity problems, never for a
# merely budget-deferred regular OHLC refresh. No additional Yahoo quota.
MAX_FALLBACK_RESERVED_REQUESTS=8
ELIGIBLE_STATES=frozenset(('stale','retained','missing'))
PRICE_INTEGRITY_REASONS=frozenset((
    # A missing/invalid daily bar is positive evidence. Ordinary provider
    # lag (NO_NEW_FINAL_BARS / BEHIND_FINAL_SESSION) alone is NOT.
    'INVALID_REAL_OHLC','INVALID_OHLC_VALUE',
))
INELIGIBLE_SOURCE_REASONS=frozenset((
    'PROVIDER_SYMBOL_NOT_FOUND','PROVIDER_EXCHANGE_MISMATCH',
    'PROVIDER_ASSET_TYPE_MISMATCH','PROVIDER_CURRENCY_MISMATCH',
    'PROVIDER_TIMEZONE_MISMATCH','PROVIDER_ACCESS_DENIED',
    'PROVIDER_RATE_LIMITED',
))


def _same_listing(entry, spec):
    return all(entry.get(k)==spec[k] for k in (
        'exchange','market','currency','asset_type','exchange_timezone'))



def _close_only_priority(item,old):
    """Return rank only for price-data evidence, not transport deferrals.

    The two truly missing OHLC charts with INVALID_REAL_OHLC receive first
    opportunity; already-verified standalone closes can also refresh.
    No quote/price field is used to infer symbol identity.
    """
    if item.get('asset_type') not in ('stock','etf') or item.get('status') not in ELIGIBLE_STATES:
        return None
    reason=item.get('reason')
    old_reason=(old or {}).get('reason')
    if reason in INELIGIBLE_SOURCE_REASONS:
        return None
    prior_series=(old or {}).get('close_only') or {}
    # Never retry the same first eight invalid symbols forever: missing valid
    # close-only histories win, then the oldest verified histories rotate.
    # Fully verified candles remain unchanged and no speculative close is made.
    gap_rank=(0 if not prior_series else 1,
              prior_series.get('verified_at','') if prior_series else '')
    if reason in PRICE_INTEGRITY_REASONS:
        return (0 if item['status']=='missing' else 1,*gap_rank)
    if old_reason in PRICE_INTEGRITY_REASONS:
        return (0 if item['status']=='missing' else 1,*gap_rank)
    if prior_series:
        return (2,*gap_rank)
    # A source that is only late is not entitled to RESERVED quota.
    # However, with HTTP calls still unused after normal OHLC collection,
    # the old standalone fallback behavior remains useful and safe.
    if reason in ('PROVIDER_BEHIND_FINAL_SESSION','PROVIDER_NO_NEW_FINAL_BARS'):
        return (3,0,'')
    return None


def planned_fallback_reserve(catalog_rows,previous,max_provider_requests,*,enabled):
    """Predict evidenced integrity backlog before spending the 64-call budget.

    This never reserves calls when there is no prior proof of incomplete
    prices; normal OHLC refresh continues to get the original budget.
    """
    if not enabled:
        return 0
    if type(max_provider_requests) is not int or max_provider_requests<0:
        raise ValueError('INVALID_PRIMARY_HTTP_BUDGET')
    targets=0
    for row in catalog_rows:
        old=(previous or {}).get(row['symbol']) or {}
        if (_close_only_priority(old,old) is None or
                (old.get('reason') not in PRICE_INTEGRITY_REASONS
                 and not old.get('close_only'))):
            # Lag may use leftover budget but never steals a reserved call.
            continue
        try:
            spec=specification(row)
        except (ValueError,KeyError,TypeError):
            continue
        if _same_listing(old,spec):
            targets+=1
    # Keep 16 regular refresh slots whenever budget permits; reserve a
    # maximum of 8 within, never on top of, the original request cap.
    floor=min(16,max_provider_requests)
    return min(MAX_FALLBACK_RESERVED_REQUESTS,targets,
               max(0,max_provider_requests-floor))

def audit_ohlc_coverage(entries):
    """Recompute existing candle gaps even for reused/unchanged snapshots."""
    for item in entries.values():
        bars=item.get('bars') or {}
        dates=bars.get('d') or []
        if dates and item.get('market'):
            try:
                coverage=session_coverage(item['market'],dates,
                    expected_session=item.get('expected_session'))
                item['history_coverage']=coverage
                item['missing_sessions']=coverage['missing_between_first_last']
            except (ValueError,KeyError,TypeError):
                # Do not relabel a stale/incomplete candle as current or
                # suppress independent strict validation of its OHLC.
                item['history_coverage_status']='UNVERIFIED'
    return entries


def attach_close_only(entries, catalog_rows, previous, provider, cutoff, *, clock,
                      max_extra_requests=MAX_FALLBACK_REQUESTS):
    """Request at most 16 extra direct HTTP attempts for confirmed daily closes.

    Uses provider's own hard rate-limit and identity circuits; uses only a
    *full-history*, up-to-two-year response (never stitched with a different
    adjustment basis). No price imputation. Previously attested standalone
    closes can be retained if latest HTTP temporarily fails.
    """
    if type(max_extra_requests) is not int or not 0<=max_extra_requests<=MAX_FALLBACK_REQUESTS:
        raise ValueError('CLOSE_ONLY_REQUEST_CAP_INVALID')
    begin=getattr(provider,'calls',0)
    original_limit=getattr(provider,'max_calls',None)
    if isinstance(original_limit,int):
        provider.max_calls=min(original_limit,begin+max_extra_requests)
    attempted=0;published=0;preserved=0;unavailable=0
    ranked=[]
    for i,row in enumerate(catalog_rows):
        item=entries[row['symbol']]
        old=(previous or {}).get(row['symbol']) or {}
        priority=_close_only_priority(item,old)
        if priority is not None:
            ranked.append((priority,i,row))
    ranked.sort(key=lambda pair:(pair[0],pair[1]))
    candidates=len(ranked)
    try:
        for _,_,row in ranked:
            symbol=row['symbol'];item=entries[symbol]
            try:
                spec=specification(row)
                if not _same_listing(item,spec):
                    continue
                old=(previous or {}).get(symbol) or {}
                if old.get('close_only') and _same_listing(old,spec):
                    # The old series was validated by validate_snapshot().
                    item['close_only']=deepcopy(old['close_only'])
                    preserved+=1
                target=latest_session(spec['market'],cutoff)
                prior=item.get('close_only')
                if prior and prior['last_final_session']>=target:
                    continue
                if getattr(provider,'calls',0)-begin>=max_extra_requests:
                    item['close_only_probe']='HTTP_BUDGET_DEFERRED'
                    continue
                attempted+=1
                before=getattr(provider,'calls',0)
                try:
                    rows,identity,excluded=provider.fetch(spec,cutoff,close_only=True)
                    # Direct path required verified exchangeName/instrumentType/
                    # currency/timezone and strict real close + session checks.
                    if any(set(r)!={'d','c'} for r in rows):
                        raise Unavailable('CLOSE_ONLY_PROVIDER_CONTRACT_MISMATCH')
                    rows=rows[-MAX_BARS:]
                    latest=rows[-1]['d']
                    if prior and latest<prior['last_final_session']:
                        raise Unavailable('CLOSE_ONLY_OLDER_THAN_PREVIOUS')
                    coverage=session_coverage(spec['market'],[r['d'] for r in rows],
                        expected_session=target)
                    item['close_only']=dict(
                        d=[r['d'] for r in rows],
                        c=[r['c'] for r in rows],
                        source='Yahoo Finance verified close-only',
                        source_symbol=identity['provider_symbol'],
                        adjustment=BASIS,
                        identity_evidence=identity,
                        last_final_session=latest,
                        verified_at=clock().isoformat(),
                        history_coverage=coverage)
                    item.pop('close_only_probe',None)
                    published+=1
                except Unavailable as exc:
                    # A valid *previous* close-only series remains available;
                    # source failure cannot make up new timestamps or prices.
                    item['close_only_probe']=str(exc)
                    unavailable+=1
                finally:
                    item['close_only_probe_http_requests']=max(
                        0,getattr(provider,'calls',0)-before)
            except (ValueError,TypeError,KeyError):
                item['close_only_probe']='CLOSE_ONLY_VALIDATION_UNAVAILABLE'
                unavailable+=1
    finally:
        if isinstance(original_limit,int):
            provider.max_calls=original_limit
    return dict(candidate_symbols=candidates,attempted_symbols=attempted,
        published_series=published,retained_prior_series=preserved,
        unavailable_symbols=unavailable,
        direct_http_requests=max(0,getattr(provider,'calls',0)-begin),
        extra_http_cap=max_extra_requests)

