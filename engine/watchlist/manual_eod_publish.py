"""Optional manual EOD publication. Never writes strategy caches/latest/history."""
from __future__ import annotations
from collections import deque
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import tempfile
import time

import pandas as pd
import requests

from .manual_catalog import parse_csv,filter_rows,unique_instruments
from .manual_eod_contract import SCHEMA,MAX_BARS,canonical,packed,descriptor,load_snapshot,validate_snapshot,aware,digest
from .manual_eod_provider import YahooDaily,Unavailable,WrongAsset,BASIS,INDEX_BASIS,specification
from .manual_eod_sessions import POLICY,calendar,close_at,latest_session
from .manual_eod_cache import inspect_core_cache,attest_identity,valid_identity
from .manual_eod_kr_by_date import VerifiedKRByDate,SOURCE as KR_BY_DATE_SOURCE,BASIS as KR_BY_DATE_BASIS
from .manual_eod_yfinance import YFinanceBatch, BASIS as YF_BASIS, SOURCE as YF_SOURCE
from .manual_eod_http_meter import observe_yfinance_curl
from .manual_eod_close_fallback import audit_ohlc_coverage,attach_close_only,planned_fallback_reserve
from .manual_eod_gap_audit import audit_missing_history
from .manual_eod_history_coverage import session_coverage
from .manual_macro_review import review_instrument,review_catalog

CATALOG_REPO='example-owner/trend-leader-watchlist'
ENGINE_REPO='example-owner/trading-journal-data'
CATALOG_PATH='watchlists/manual_watchlist.csv'
CORE_PATH='watchlist/published/latest/manifest.json'
UTC=timezone.utc
# Bounded foreign-market bootstrap; every successful chart is retained in the
# verified immutable snapshot for incremental reuse on later core runs.
MAX_PROVIDER_REQUESTS_PER_RUN=64
# Per-run soft reservation for existing valid charts that need a new session.
# Any unused reservation is lent back to missing/uncollected instruments.
DIRECT_REFRESH_RESERVED_REQUESTS=16
# The yfinance transport makes internal HTTP calls that are not accounted for
# by YahooDaily.calls. Keep a smaller cold-start canary while preserving slots
# for existing adjusted-history refreshes and identity-only core cache reuse.
YFINANCE_COLD_PROBE_LIMIT=6
YFINANCE_IDENTITY_SLOTS=4
YFINANCE_RAW_REBASE_SLOTS=2
LAG_RETRY_COOLDOWN=timedelta(hours=2)


def _github(session,url,token,params=None):
    # Token is used only on the fixed GitHub API origin; never in logs/payloads.
    if not url.startswith('https://api.github.com/repos/example-owner/'):raise ValueError('unexpected API origin')
    headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    if token:headers['Authorization']='Bearer '+token
    r=session.get(url,params=params,headers=headers,timeout=(5,25),allow_redirects=False)
    if r.status_code!=200:raise ValueError(f'GITHUB_READ_HTTP_{r.status_code}')
    return r


def fetch_catalog(session,token):
    if not token:raise ValueError('CATALOG_READ_TOKEN_NOT_CONFIGURED')
    url=f'https://api.github.com/repos/{CATALOG_REPO}/contents/{CATALOG_PATH}'
    doc=_github(session,url,token,{'ref':'main'}).json()
    import base64
    if doc.get('type')!='file' or doc.get('encoding')!='base64':raise ValueError('CATALOG_RESPONSE_INVALID')
    raw=base64.b64decode(doc['content']);rows=parse_csv(raw)
    return raw,rows,doc.get('sha')


def resolve_publication(session,event,token,*,now=None):
    run=event.get('workflow_run') or {}
    if (run.get('conclusion')!='success' or run.get('status')!='completed' or run.get('head_branch')!='main'
            or run.get('path','').split('@')[0]!='.github/workflows/watchlist.yml'
            or (event.get('repository') or {}).get('full_name')!=ENGINE_REPO):raise ValueError('UNTRUSTED_CORE_WORKFLOW')
    start,finish=aware(run['created_at']),aware(run['updated_at'])
    if start>finish or finish>(now or datetime.now(UTC))+timedelta(minutes=5):raise ValueError('CORE_TIME_WINDOW_INVALID')
    commits=_github(session,f'https://api.github.com/repos/{ENGINE_REPO}/commits',token,
        {'path':CORE_PATH,'since':start.isoformat(),'until':finish.isoformat(),'per_page':100}).json()
    if not isinstance(commits,list) or len(commits)!=1:raise ValueError('CORE_PUBLICATION_COMMIT_AMBIGUOUS')
    commit=commits[0]['sha']
    if not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('CORE_COMMIT_INVALID')
    import base64
    content=_github(session,f'https://api.github.com/repos/{ENGINE_REPO}/contents/{CORE_PATH}',token,{'ref':commit}).json()
    raw=base64.b64decode(content['content']);core=json.loads(raw)
    generated=aware(core['generated_at'])
    if not start<=generated<=finish or not core.get('run_id'):raise ValueError('CORE_PUBLICATION_OUTSIDE_SUCCESS_WINDOW')
    return core,commit,str(run['id']),finish


def read_core_cache(data_dir,spec,cutoff,*,evidence=None,now=None):
    """Return only eligible prices; unidentified US/ETF caches stay pending."""
    inspected=inspect_core_cache(data_dir,spec,cutoff,evidence=evidence,now=now)
    return (inspected.rows,inspected.basis) if inspected.classification=='reusable' else None


def _array(rows):return {k:[r[k] for r in rows] for k in ('d','o','h','l','c','v')}
def _records(entry):return [dict(zip(entry['bars'],values)) for values in zip(*entry['bars'].values())]
def _same_price(a,b):return all(abs(a[k]-b[k])<=max(abs(a[k]),1)*1e-6 for k in ('o','h','l','c'))


def _merge(previous,new):
    old={r['d']:r for r in _records(previous)}
    if not set(old).intersection(r['d'] for r in new):return None
    for row in new:
        if row['d'] in old and not _same_price(old[row['d']],row):return None
    old.update({r['d']:r for r in new})
    return [old[d] for d in sorted(old)][-MAX_BARS:]


def _balanced_collection_order(rows, run_id, prior):
    """Deterministic, cold-first market round robin; no change to catalog output.

    The previous published snapshot is our persisted, validated international
    price cache. Never-attempted foreign listings must not wait behind every KR
    or US symbol in the user-maintained CSV order. An attempted missing symbol
    is tried after not-yet-attempted ones in its market on the next run.
    """
    buckets={};unsupported=[]
    for row in rows:
        try:market=specification(row)['market']
        except (ValueError,KeyError,TypeError):
            unsupported.append(row);continue
        buckets.setdefault(market,[]).append(row)
    def rank(row):
        old=prior.get(row['symbol']) or {}
        tie=hashlib.sha256((run_id+':'+row['symbol']).encode('utf-8')).hexdigest()
        return (bool(old.get('bars')), old.get('last_attempt_at') or '', tie)
    queues={m:deque(sorted(items,key=rank)) for m,items in buckets.items()}
    markets=sorted(queues,key=lambda market:hashlib.sha256((run_id+':'+market).encode('utf-8')).hexdigest())
    ordered=[]
    while any(queues.values()):
        for market in markets:
            if queues[market]:ordered.append(queues[market].popleft())
    return ordered+unsupported


def _recent_confirmed_lag(old,target,now):
    """Avoid repeated same-session provider-lag probes on consecutive reruns.

    This is not a freshness promotion: the original bar date and 'stale'
    status are preserved. New sessions, forced runs, and genuinely missing
    charts are always eligible to collect.
    """
    if (not old or old.get('status')!='stale' or not old.get('bars')
            or old.get('expected_session')!=target
            or old.get('reason') not in
                ('PROVIDER_BEHIND_FINAL_SESSION','PROVIDER_NO_NEW_FINAL_BARS')):
        return False
    try:
        lag=now-aware(old['last_attempt_at'])
        return timedelta(0)<=lag<LAG_RETRY_COOLDOWN
    except (KeyError,ValueError,TypeError):
        return False


def _cold_yfinance_probes(cold,limit):
    """Use a few real batches on biggest uncovered markets; direct collector
    still performs the fair cross-market bootstrap for all other symbols.
    Avoid one yfinance batch per venue for a single cold ticker.
    """
    from collections import deque
    by_market={}
    first={}
    for spec,metadata_only in cold:
        market=spec['market']
        first.setdefault(market,len(first))
        by_market.setdefault(market,deque()).append((spec,metadata_only))
    markets=sorted(by_market,key=lambda m:(-len(by_market[m]),first[m]))[:3]
    selected=[]
    while len(selected)<limit and any(by_market[m] for m in markets):
        for market in markets:
            if by_market[market] and len(selected)<limit:
                selected.append(by_market[market].popleft())
    return selected


def _yfinance_candidates(rows, prior, inspections, cutoff, limit, *, now=None, force=False):
    """Spend opaque yfinance calls first where price-basis preservation needs it.

    Priorities: verified adjusted series due for refresh, core US cache
    awaiting listing identity, a limited cold-price sample, then existing
    raw-basis rechecks. The direct Yahoo collector handles the remaining
    cold backlog under the unchanged measured 64-HTTP request budget.
    """
    if limit<=0:return []
    now=now or cutoff
    adjusted=[];identity_only=[];cold=[];rebase=[]
    for row in rows:
        try:
            spec=specification(row)
            if spec['market']=='KR' or spec['asset_type']=='index':continue
            target=latest_session(spec['market'],cutoff)
        except (ValueError,KeyError,TypeError,Unavailable):
            continue
        old=prior.get(row['symbol']) or {}
        same=all(old.get(k)==spec.get(k) for k in (
            'symbol','exchange','asset_type','market','currency','exchange_timezone'))
        if same and old.get('bars') and old.get('last_final_session')==target:
            continue
        if not force and same and _recent_confirmed_lag(old,target,now):
            continue
        inspected=(inspections or {}).get(row['symbol'])
        if inspected and inspected.classification=='reusable':
            continue
        meta=bool(inspected and inspected.classification=='identity_pending')
        pair=(spec,meta)
        if same and old.get('bars') and old.get('adjustment')==YF_BASIS:
            adjusted.append(pair)
        elif meta:
            identity_only.append(pair)
        elif not old.get('bars') or not same:
            cold.append(pair)
        else:
            rebase.append(pair)

    # Existing adjusted histories MUST win all available opaque-HTTP slots:
    # the direct Yahoo channel cannot safely refresh their dividend basis.
    # New cold listings still have their independent 64-request direct pool.
    selected=adjusted[:limit]
    spare=max(0,limit-len(selected))
    selected+=identity_only[:min(YFINANCE_IDENTITY_SLOTS,spare)]
    cold_slots=min(YFINANCE_COLD_PROBE_LIMIT,limit-len(selected))
    selected+=_cold_yfinance_probes(cold,cold_slots)
    selected+=rebase[:min(YFINANCE_RAW_REBASE_SLOTS,limit-len(selected))]
    return selected[:limit]

def build_snapshot(rows,*,catalog_sha256,core,core_commit,workflow_run_id,cutoff,provider,
                   previous=None,data_dir=None,clock=lambda:datetime.now(UTC),force=False,cache_inspections=None,
                   max_provider_requests=MAX_PROVIDER_REQUESTS_PER_RUN,yf_batch=None,close_fallback=False,kr_by_date=None):
    if previous:
        validate_snapshot(previous)
        if aware(previous['core_generated_at'])>aware(core['generated_at']):raise ValueError('OLDER_CORE_PUBLICATION')
        if not force and previous['core_run_id']==core['run_id'] and previous['catalog_sha256']==catalog_sha256:return previous
    started=time.monotonic();entries={};targets={};prior=(previous or {}).get('instruments',{})
    request_start=getattr(provider,'calls',0);log_start=len(getattr(provider,'request_log',[]))
    catalog_rows=unique_instruments(filter_rows(rows))
    # Preserve a small slice of the existing 64-call direct quota for
    # *proven* missing/invalid OHLC prices. Regular refresh remains primary;
    # old budget deferrals alone do not qualify for a close-only reservation.
    close_only_reserve=planned_fallback_reserve(
        catalog_rows,prior,max_provider_requests,enabled=close_fallback)
    primary_http_limit=max_provider_requests-close_only_reserve
    # The UI/catalog continues to use the original membership order. Only the
    # acquisition queue is rotated by run ID to avoid repeated market starvation.
    collection_rows=_balanced_collection_order(catalog_rows,core['run_id'],prior)
    # One independent, bounded free pykrx source lane. Give genuinely
    # failed/late KR histories priority over old-current cache basis issues.
    # No inferred adjustment basis and no splice of bulk ticker prices.
    kr_repair_symbols=set()
    if kr_by_date is not None and previous is not None:
        prioritized=[]
        for row in collection_rows:
            symbol=row['symbol']
            if (not symbol.startswith('KRX:') or row['asset_type']!='stock'
                    or symbol not in prior):
                continue
            old=prior[symbol]
            inspected=(cache_inspections or {}).get(symbol)
            mismatch=inspected is not None and inspected.classification=='basis_mismatch'
            is_problem=old.get('status') in ('stale','retained','missing')
            if mismatch or is_problem:
                prioritized.append((0 if is_problem else 1,
                    0 if mismatch else 1,row))
        prioritized.sort(key=lambda item:(item[0],item[1]))
        kr_repair_symbols={x['symbol'] for _,_,x in
            prioritized[:kr_by_date.max_attempts]}
    yf_curl_observation=None
    if yf_batch is not None:
        staged=_yfinance_candidates(collection_rows,prior,cache_inspections,cutoff,
            yf_batch.max_symbols,now=clock(),force=force)
        with observe_yfinance_curl() as curl_meter:
            yf_batch.prepare(staged,cutoff,previous=prior)
        yf_curl_observation=curl_meter.diagnostics()
    # Split actual (not guessed per-symbol) Yahoo HTTP attempts into protected
    # refresh, missing/cold, and leftover-refresh lanes. All lanes still share
    # YahooDaily.max_calls and the global 64-request publisher cap.
    refresh_rows=[r for r in collection_rows if (prior.get(r['symbol']) or {}).get('bars')]
    cold_rows=[r for r in collection_rows if not (prior.get(r['symbol']) or {}).get('bars')]
    def refresh_priority(row):
        """Recover budget-deferred real candles ahead of proven OHLC defects.

        The existing market-balanced order is stable *within* each priority
        tier. The 64-HTTP cap, hard circuit breakers, and independent 8-slot
        close-only reserve remain unchanged. Failed OHLC is never promoted.
        """
        reason=(prior.get(row['symbol']) or {}).get('reason')
        if reason=='PROVIDER_REQUEST_BUDGET_DEFERRED':
            return 0
        if reason in ('INVALID_REAL_OHLC','INVALID_OHLC_VALUE'):
            return 2
        return 1
    refresh_rows.sort(key=refresh_priority)
    def cold_priority(row):
        """Untried and budget-deferred listings precede persistent source errors.

        Stable sorting preserves the existing within-tier market-balanced
        acquisition order. No symbol is removed, no price check is relaxed.
        """
        previous=prior.get(row['symbol']) or {}
        if row['symbol']=='NASDAQ:NBI' and row['asset_type']=='index':
            return -1  # one vetted index-level series before other cold work
        reason=previous.get('reason')
        if not previous or (previous.get('status')=='missing' and
                not previous.get('last_attempt_at') and reason!='PROVIDER_REQUEST_BUDGET_DEFERRED'):
            return 0
        if reason=='PROVIDER_REQUEST_BUDGET_DEFERRED':
            return 1
        if reason in ('PROVIDER_SYMBOL_NOT_FOUND','INVALID_REAL_OHLC'):
            return 3
        return 2
    cold_rows.sort(key=cold_priority)
    phase='refresh_reserved'
    phase_request_limit=min(DIRECT_REFRESH_RESERVED_REQUESTS,primary_http_limit)
    # YahooDaily.fetch may issue multiple provider HTTP calls for a single
    # symbol. Bound it at the transport's own per-call budget too; otherwise
    # a two-host/candidate response could cross the reserved lane limit.
    original_provider_max=getattr(provider,'max_calls',None)
    if isinstance(original_provider_max,int):
        provider.max_calls=min(original_provider_max,request_start+phase_request_limit)
    direct_by_lane={'refresh_reserved':0,'cold':0,'refresh_spare':0}
    def acquire(spec,**kwargs):
        nonlocal phase
        total=getattr(provider,'calls',0)-request_start
        if total>=primary_http_limit:
            raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
        if phase=='refresh_reserved' and total>=phase_request_limit:
            raise Unavailable('REFRESH_RESERVE_DEFERRED')
        before=getattr(provider,'calls',0)
        try:
            return provider.fetch(spec,cutoff,**kwargs)
        except Unavailable as exc:
            if (phase=='refresh_reserved'
                    and str(exc)=='PROVIDER_REQUEST_BUDGET_DEFERRED'
                    and isinstance(original_provider_max,int)
                    and request_start+phase_request_limit<original_provider_max):
                raise Unavailable('REFRESH_RESERVE_DEFERRED') from exc
            raise
        finally:
            direct_by_lane[phase]+=max(0,getattr(provider,'calls',0)-before)
    def _collect(row):
            symbol=row['symbol'];old=prior.get(symbol);spec=None;target=None;listing_identity=None;cache_diagnostic=None
            calls_before=getattr(provider,'calls',0)
            base=dict(symbol=symbol,exchange=symbol.split(':')[0],asset_type=row['asset_type'],market=None,currency=None,
                source_symbol=None,exchange_timezone=None,source=None,adjustment=None,calendar_policy=None,identity_evidence=None,
                bars={},last_final_session=None,last_final_close_at=None,expected_session=None,status='missing',reason='',
                verified_at=None,last_success_at=None,last_attempt_at=(old or {}).get('last_attempt_at'))
            source_review=review_instrument(row) if symbol!='NASDAQ:NBI' else None
            if source_review:base['source_review']=source_review
            try:
                spec=specification(row)
                base.update({k:spec[k] for k in ('exchange','asset_type','market','currency','exchange_timezone')})
                if spec['market'] not in targets:
                    try:targets[spec['market']]=latest_session(spec['market'],cutoff)
                    except (ValueError,KeyError):targets[spec['market']]=None
                target=targets[spec['market']];base['expected_session']=target
                if target is None:raise Unavailable('CALENDAR_COVERAGE_UNVERIFIED')
                same=old and all(old.get(k)==base.get(k) for k in ('symbol','exchange','asset_type','market','currency','exchange_timezone'))
                if not force and same and old.get('last_final_session')==target and (old.get('bars') or
                        spec['asset_type']=='index' and old.get('close_only')):
                    kept=deepcopy(old);kept.update(expected_session=target,reason='NO_NEW_FINAL_SESSION',http_requests=0,acquisition='previous_snapshot')
                    kept.pop('retry_cooldown',None)
                    entries[symbol]=kept;return
                cooldown_due=not force and same and _recent_confirmed_lag(old,target,clock())
                if cooldown_due and not (kr_by_date is not None and symbol in kr_repair_symbols):
                    kept=deepcopy(old)
                    kept.update(status='stale',expected_session=target,http_requests=0,
                        acquisition='previous_snapshot',retry_cooldown=True)
                    entries[symbol]=kept;return
                if spec['asset_type']=='index':
                    # NBI is a standalone verified index level, not an equity
                    # OHLC candle or an input to the tactical strategy.
                    observations,identity,excluded=acquire(spec,close_only=True)
                    observations=[r for r in observations if r['d']<=target][-MAX_BARS:]
                    if len(observations)<2:raise Unavailable('NO_VALID_FINAL_CLOSE_SERIES')
                    last=observations[-1]['d']
                    if same and old.get('close_only') and last<old['close_only']['last_final_session']:
                        raise Unavailable('INDEX_BEHIND_PREVIOUS_SNAPSHOT')
                    stamp=clock().isoformat()
                    source='Yahoo Finance verified index levels'
                    coverage=session_coverage(spec['market'],[r['d'] for r in observations],
                        expected_session=target)
                    base.update(
                        source_symbol=identity['provider_symbol'],source=source,
                        adjustment=INDEX_BASIS,calendar_policy=POLICY,identity_evidence=identity,
                        last_final_session=last,last_final_close_at=close_at(spec['market'],last).isoformat(),
                        expected_session=target,verified_at=stamp,last_success_at=stamp,
                        last_attempt_at=stamp,status='current' if last==target else 'stale',
                        reason='' if last==target else 'PROVIDER_BEHIND_FINAL_SESSION',
                        acquisition='provider_index',excluded_bars=excluded,
                        http_requests=getattr(provider,'calls',0)-calls_before,
                        price_origin_core_run_id=core['run_id'],
                        close_only=dict(d=[r['d'] for r in observations],
                            c=[r['c'] for r in observations],source=source,
                            source_symbol=identity['provider_symbol'],adjustment=INDEX_BASIS,
                            identity_evidence=identity,last_final_session=last,
                            verified_at=stamp,history_coverage=coverage))
                    entries[symbol]=base
                    return
                if cache_inspections is not None and symbol in cache_inspections:
                    # The main entrypoint shares its read-only census with collection;
                    # do not unpickle and calendar-check the same 650 bars twice.
                    cached=deepcopy(cache_inspections[symbol])
                    if cached.identity and cached.identity.get('evidence')=='yahoo-chart-meta-v1' and not valid_identity(spec,cached.identity,clock()):
                        cached.identity=None;cached.checks['identity']=False
                        if cached.classification=='reusable':
                            cached.reason='CACHE_IDENTITY_UNVERIFIED';cached.classification='identity_pending'
                else:
                    cached=inspect_core_cache(data_dir,spec,cutoff,
                        evidence=(old.get('listing_identity') or old.get('identity_evidence')) if same else None,now=clock())
                cache_diagnostic=cached.diagnostic();base['cache_diagnostic']=cache_diagnostic
                # A speculative yfinance batch can return valid adjusted OHLCV
                # without the complete listing metadata needed for publication.
                # Its failure must not disable the independently working direct
                # Yahoo path. Conflict evidence remains fail-closed for that batch.
                staged_yf=yf_batch.get(spec) if yf_batch is not None else None
                yf_attempt=yf_batch.results.get(symbol) if yf_batch is not None else None
                yf_missing_metadata=(yf_attempt is not None and bool(yf_attempt.get('rows'))
                    and yf_attempt.get('reason')=='YFINANCE_LISTING_METADATA_UNAVAILABLE')
                yf_conflicting_metadata=(yf_attempt is not None and bool(yf_attempt.get('rows'))
                    and yf_attempt.get('reason') in (
                        'YFINANCE_SYMBOL_IDENTITY_MISMATCH','YFINANCE_EXCHANGE_MISMATCH',
                        'YFINANCE_TIMEZONE_MISMATCH','YFINANCE_CURRENCY_MISMATCH',
                        'YFINANCE_ASSET_TYPE_MISMATCH'))
                price_rows=None;identity=None;excluded=0;source='Yahoo Finance';adjustment=BASIS
                if cached.classification=='identity_pending':
                    # The core OHLCV is already strictly verified; only its listing
                    # evidence is missing. Use yfinance meta if complete, otherwise
                    # independently verify with the direct chart metadata endpoint.
                    if staged_yf:
                        evidence=staged_yf['identity']
                    else:
                        _,evidence,_=acquire(spec,metadata_only=True)
                    listing_identity=attest_identity(spec,evidence,clock())
                    cached.identity=listing_identity
                    base['listing_identity']=listing_identity
                if cached.rows and cached.checks['fresh'] and cached.identity:
                    price_rows,adjustment,identity=cached.rows,cached.basis,cached.identity
                    listing_identity=listing_identity or (identity if identity.get('evidence')=='yahoo-chart-meta-v1' else None)
                    source='core confirmed KRX cache' if spec['market']=='KR' else 'core confirmed US cache'
                    base['acquisition']='core_cache'
                if price_rows is None and kr_by_date is not None and symbol in kr_repair_symbols:
                    try:
                        # Fully independent adjusted=True source, never mixed
                        # with legacy Yahoo or unverified bulk core records.
                        price_rows,identity,excluded=kr_by_date.fetch(spec,cutoff)
                        adjustment,source=KR_BY_DATE_BASIS,KR_BY_DATE_SOURCE
                        base['acquisition']='kr_verified_by_date'
                    except Unavailable as exc:
                        base['kr_by_date_failure']=str(exc)
                        if cooldown_due:
                            # The original Yahoo retry cooldown remains in
                            # force if the independent KR source also fails.
                            # This never causes a burst of repeated Yahoo calls.
                            raise Unavailable('KR_BY_DATE_FAILED_DURING_YAHOO_COOLDOWN') from exc
                if price_rows is None:
                    if yf_conflicting_metadata:
                        # Do not publish yfinance OHLCV when the metadata explicitly
                        # contradicts the catalog venue, unit, symbol or asset type.
                        raise Unavailable(yf_attempt['reason'])
                    if staged_yf and staged_yf['rows']:
                        price_rows,identity=staged_yf['rows'],staged_yf['identity']
                        listing_identity=attest_identity(spec,identity,clock())
                        adjustment,source=YF_BASIS,YF_SOURCE
                        base['acquisition']='yfinance_batch'
                    elif yf_missing_metadata:
                        # A previously attested Yahoo listing identity is valid for
                        # 7 days, and ONLY for the same native listing. Otherwise
                        # ask the successful direct Yahoo chart path for metadata,
                        # not a second full price history.
                        # Reuse listing evidence already attested while checking
                        # identity_pending core prices; do not request it twice.
                        evidence=listing_identity or valid_identity(spec,
                            (old.get('listing_identity') or old.get('identity_evidence'))
                            if same else None,clock())
                        identity_source=('direct_chart_metadata' if listing_identity else
                            'previous_snapshot' if evidence else 'direct_chart_metadata')
                        if evidence is None:
                            _,raw_identity,_=acquire(spec,metadata_only=True)
                            evidence=attest_identity(spec,raw_identity,clock())
                        listing_identity=evidence
                        staged_yf=yf_batch.accept_verified_listing(
                            spec,listing_identity,identity_source,clock())
                        price_rows,identity=staged_yf['rows'],staged_yf['identity']
                        adjustment,source=YF_BASIS,YF_SOURCE
                        base['acquisition']='yfinance_batch'
                        base['yfinance_identity_source']=identity_source
                    else:
                        # Never swap a trusted adjusted history for raw OHLCV after
                        # a failed yfinance refresh. Retain the old original dates.
                        if yf_batch is not None and same and old.get('bars') and old.get('adjustment')==YF_BASIS:
                            raise Unavailable((yf_attempt or {}).get('reason') or 'YFINANCE_REFRESH_DEFERRED')
                        base['acquisition']='provider'
                        start=None
                        if same and old.get('bars') and old.get('adjustment')==BASIS:
                            start=(pd.Timestamp(old['last_final_session'])-pd.Timedelta(days=10)).date().isoformat()
                        price_rows,identity,excluded=acquire(spec,start=start)
                        listing_identity=attest_identity(spec,identity,clock())
                        if start:
                            merged=_merge(old,price_rows)
                            if merged is None:
                                price_rows,identity,excluded=acquire(spec)
                                listing_identity=attest_identity(spec,identity,clock())
                            else:price_rows=merged
                if (base.get('acquisition')=='yfinance_batch'
                        and yf_attempt and yf_attempt.get('incremental')
                        and same and old.get('bars') and old.get('adjustment')==YF_BASIS):
                    # A yfinance increment is useful only with a verified common
                    # overlap. Never splice dividend-rebased history; on mismatch
                    # retain the old full series and fetch a full replacement on
                    # the next independent EOD run.
                    merged=_merge(old,price_rows)
                    if merged is None:
                        raise Unavailable('YFINANCE_ADJUSTED_SEAM_MISMATCH')
                    price_rows=merged
                price_rows=[r for r in price_rows if r['d']<=target][-MAX_BARS:]
                if not price_rows:raise Unavailable('NO_FINAL_DAILY_BARS')
                # A full fallback can lag behind an already published adjusted
                # cache. Preserve that newer series and its original price basis.
                if same and old.get('bars') and price_rows[-1]['d']<old['last_final_session']:
                    raise Unavailable('PROVIDER_BEHIND_PREVIOUS_SNAPSHOT')
                if same and old.get('bars') and old.get('adjustment')==adjustment and _array(price_rows)==old['bars'] and old.get('last_final_session')!=target:
                    kept=deepcopy(old);kept.update(status='stale',reason='PROVIDER_NO_NEW_FINAL_BARS',expected_session=target,last_attempt_at=clock().isoformat(),
                        acquisition='previous_snapshot',http_requests=getattr(provider,'calls',0)-calls_before,cache_diagnostic=cache_diagnostic)
                    if listing_identity:kept['listing_identity']=listing_identity
                    # A new provider attempt supersedes any skip marker copied
                    # from the old immutable snapshot.
                    kept.pop('retry_cooldown',None)
                    entries[symbol]=kept;return
                stamp=clock().isoformat();last=price_rows[-1]['d']
                # Missing sessions remain gaps, not forward-filled prices.
                expected=calendar(spec['market']).sessions_in_range(price_rows[0]['d'],last)
                gaps=max(0,len(expected)-len(price_rows))
                base.update(bars=_array(price_rows),source_symbol=identity['provider_symbol'],source=source,adjustment=adjustment,
                    calendar_policy=POLICY,identity_evidence=identity,last_final_session=last,
                    last_final_close_at=close_at(spec['market'],last).isoformat(),verified_at=stamp,last_success_at=stamp,
                    status='current' if last==target else 'stale',reason='' if last==target else 'PROVIDER_BEHIND_FINAL_SESSION',
                    excluded_bars=excluded,missing_sessions=gaps,price_origin_core_run_id=core['run_id'])
            except Exception as exc:
                reason=str(exc) if isinstance(exc,Unavailable) else 'OPTIONAL_INSTRUMENT_PROCESSING_FAILED'
                can_keep=old and (old.get('bars') or row['asset_type']=='index' and old.get('close_only')) and old.get('symbol')==symbol and old.get('asset_type')==row['asset_type']
                can_keep=can_keep and (row['asset_type']=='index' or old.get('currency')==row.get('unit')) and old.get('exchange')==symbol.split(':')[0]
                if spec:can_keep=can_keep and all(old.get(k)==spec[k] for k in ('exchange','market','currency','exchange_timezone'))
                if can_keep:
                    base=deepcopy(old);base.update(status='calendar_unverified' if target is None and spec else 'retained',reason=reason,expected_session=target,acquisition='previous_snapshot')
                    base.pop('retry_cooldown',None)
                else:base.update(status='calendar_unverified' if spec and target is None else 'unsupported' if spec is None else 'missing',reason=reason)
            if listing_identity:base['listing_identity']=listing_identity
            if cache_diagnostic:base['cache_diagnostic']=cache_diagnostic
            # Keep the staged failure separate from the final acquisition outcome:
            # a metadata or OHLC failure may have been recovered via direct Yahoo.
            if yf_batch is not None and symbol in yf_batch.results:
                result=yf_batch.results[symbol]
                if result.get('reason'):base['yfinance_failure_reason']=result['reason']
            base['http_requests']=getattr(provider,'calls',0)-calls_before
            if base['http_requests'] or yf_batch is not None and symbol in yf_batch.results:
                base['last_attempt_at']=clock().isoformat()
            if yf_batch is not None and symbol in yf_batch.results:
                base['yfinance_staged']=True
            entries[symbol]=base
    for row in refresh_rows:
        _collect(row)
    phase='cold'
    if isinstance(original_provider_max,int):
        # The cold and refresh-spare lanes MUST NOT use the reserved
        # close-only transport calls; cap at the HTTP transport as well.
        provider.max_calls=min(original_provider_max,request_start+primary_http_limit)
    for row in cold_rows:
        _collect(row)
    phase='refresh_spare'
    # Reserved-space deferrals receive a second chance only after cold entries
    # have used their allocation. Never reattempt a real HTTP/provider failure.
    for row in refresh_rows:
        if entries[row['symbol']].get('reason')=='REFRESH_RESERVE_DEFERRED':
            _collect(row)
    # Restore original transport ceiling for the separately-metered
    # fallback lane, even if the regular lane filled its reduced allocation.
    if isinstance(original_provider_max,int):
        provider.max_calls=original_provider_max
    # Acquisition order must never alter the exact published catalog order.
    entries={row['symbol']:entries[row['symbol']] for row in catalog_rows}
    # Audit every saved OHLC series, including reused snapshots; an isolated
    # missing historic day is not equivalent to a current complete price feed.
    audit_ohlc_coverage(entries)
    close_metrics=dict(candidate_symbols=0,attempted_symbols=0,published_series=0,
        retained_prior_series=0,unavailable_symbols=0,direct_http_requests=0,
        extra_http_cap=0)
    if close_fallback:
        # Any unspent ordinary OHLC quota is also available for proven
        # fallback failures. This preserves the historical cold-start path:
        # even without a previous snapshot, an invalid OHLC response may
        # yield a genuine daily close when actual HTTP budget remains.
        spare_http=max(0,max_provider_requests-
            (getattr(provider,'calls',0)-request_start))
        close_metrics=attach_close_only(entries,catalog_rows,prior,provider,cutoff,
            clock=clock,max_extra_requests=min(16,spare_http))
    if close_fallback:
        direct_by_lane['close_only']=close_metrics['direct_http_requests']
    doc=dict(schema=SCHEMA,run_id=core['run_id']+'-'+catalog_sha256[:12],core_run_id=core['run_id'],
        core_generated_at=core['generated_at'],core_publication_commit=core_commit,workflow_run_id=str(workflow_run_id),
        cutoff_at=cutoff.isoformat(),generated_at=clock().isoformat(),catalog_sha256=catalog_sha256,
        expected_sessions=targets,instruments=entries,core_manifest_sha256=digest(canonical(core)),
        catalog_memberships=len(filter_rows(rows)),catalog_order=list(entries))
    request_log=getattr(provider,'request_log',[])[log_start:]
    doc['collection_metrics']=dict(http_requests=getattr(provider,'calls',0)-request_start,
        requests_by_market={m:{op:sum(r['market']==m and r['operation']==op for r in request_log) for op in ('identity','price')} for m in targets},
        cache_reused=sum(e.get('acquisition')=='core_cache' for e in entries.values()),
        yfinance_batches=yf_batch.batches if yf_batch is not None else 0,
        yfinance_staged=len(yf_batch.attempted) if yf_batch is not None else 0,
        yfinance_verified=sum(bool(v['identity']) and (v['metadata_only'] or bool(v['rows']))
            for v in yf_batch.results.values()) if yf_batch is not None else 0,
        yfinance_published=sum(e.get('acquisition')=='yfinance_batch' for e in entries.values()),
        yfinance_identity_assisted=sum(e.get('yfinance_identity_source') in ('direct_chart_metadata','previous_snapshot')
            for e in entries.values()),
        lag_cooldown_reused=sum(e.get('retry_cooldown') is True for e in entries.values()),
        history_gap_symbols=sum((e.get('history_coverage') or {}).get('missing_between_first_last',0)>0
            for e in entries.values()),
        history_gap_sessions=sum((e.get('history_coverage') or {}).get('missing_between_first_last',0)
            for e in entries.values()),
        close_only=close_metrics,
         verified_index_level_series=sum(e.get('asset_type')=='index' and bool(e.get('close_only')) for e in entries.values()),
        kr_by_date=kr_by_date.diagnostics() if kr_by_date is not None else None,
        kr_by_date_published=sum(e.get('acquisition')=='kr_verified_by_date' for e in entries.values()),
        close_only_reserved_requests=close_only_reserve,
        primary_direct_http_cap=primary_http_limit,
        direct_requests_by_lane=direct_by_lane,
        previous_budget_deferred=sum((prior.get(symbol) or {}).get('reason')=='PROVIDER_REQUEST_BUDGET_DEFERRED'
            for symbol in entries),
        previous_budget_retried=sum((prior.get(symbol) or {}).get('reason')=='PROVIDER_REQUEST_BUDGET_DEFERRED'
            and entry.get('http_requests',0)>0 for symbol,entry in entries.items()),
        previous_budget_recovered=sum((prior.get(symbol) or {}).get('reason')=='PROVIDER_REQUEST_BUDGET_DEFERRED'
            and bool(entry.get('bars')) and entry.get('status') in ('current','stale')
            for symbol,entry in entries.items()),
        previous_stale_recovered=sum((prior.get(symbol) or {}).get('status')=='stale'
            and entry.get('status')=='current' for symbol,entry in entries.items()),
        source_integrity_failures=sum(entry.get('reason')=='INVALID_REAL_OHLC'
            for entry in entries.values()),
        yfinance_curl_observation=yf_curl_observation,
        refresh_reservation=min(DIRECT_REFRESH_RESERVED_REQUESTS,primary_http_limit),
        direct_requests_unused=max(0,max_provider_requests-(getattr(provider,'calls',0)-request_start)),
        direct_responses={str(code):sum(r.get('status')==code for r in request_log)
            for code in sorted({r.get('status') for r in request_log if isinstance(r.get('status'),int)})},
        budget_deferred=sum(e.get('reason')=='PROVIDER_REQUEST_BUDGET_DEFERRED' for e in entries.values()),
        status_by_market={m:{state:sum(e.get('market')==m and e.get('status')==state for e in entries.values())
                             for state in ('current','stale','retained','missing','unsupported','calendar_unverified')}
                          for m in targets},
        direct_failures=sum(e.get('status') in ('missing','retained') and e.get('http_requests',0)>0 for e in entries.values()),
        circuit_skipped=sum(e.get('reason','').endswith('CIRCUIT_OPEN') for e in entries.values()),
        elapsed_seconds=round(time.monotonic()-started,3))
    return validate_snapshot(doc)


def _valid_previous(base):
    root=Path(base)/'manual_eod';invalid=False
    for name in ('latest.json','previous.json'):
        pointer=root/name
        if not pointer.exists():continue
        try:
            raw=pointer.read_bytes();info=json.loads(raw);sha=info['snapshot']['sha256']
            if not re.fullmatch('[a-f0-9]{64}',sha) or info['snapshot']['file']!=f'manual_eod/objects/{sha}.json.gz':raise ValueError('unsafe snapshot path')
            return raw,load_snapshot(info,(Path(base)/info['snapshot']['file']).read_bytes())
        except (ValueError,KeyError,TypeError,OSError):invalid=True
    if invalid:raise ValueError('NO_VALID_PREVIOUS_SNAPSHOT')
    return None,None


def read_previous(base):
    return _valid_previous(base)[1]


def publish_snapshot(base,doc):
    base=Path(base);validate_snapshot(doc)
    current=json.loads((base/'latest/manifest.json').read_bytes())
    if current.get('run_id')!=doc['core_run_id'] or digest(canonical(current))!=doc['core_manifest_sha256']:raise ValueError('CORE_MOVED_DO_NOT_PUBLISH')
    raw=packed(doc);info=descriptor(doc,raw);root=base/'manual_eod';objects=root/'objects';objects.mkdir(parents=True,exist_ok=True)
    pointer=root/'latest.json';previous,_=_valid_previous(base)
    old=json.loads(previous) if previous else None
    if previous==canonical(info):
        load_snapshot(info,(base/info['snapshot']['file']).read_bytes())
        return info
    (base/info['snapshot']['file']).write_bytes(raw)
    load_snapshot(info,raw)
    if previous:(root/'previous.json').write_bytes(previous)
    fd,temp=tempfile.mkstemp(dir=root,prefix='.pointer-')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(canonical(info))
        os.replace(temp,pointer)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    keep={info['snapshot']['sha256']}
    if old:keep.add(old['snapshot']['sha256'])
    for path in objects.glob('*.json.gz'):
        if re.fullmatch(r'[a-f0-9]{64}\.json\.gz',path.name) and path.name[:-8] not in keep:path.unlink()
    return info


def verify_current_core(base,expected):
    if json.loads((Path(base)/'latest/manifest.json').read_bytes()).get('run_id')!=expected:raise ValueError('CORE_MOVED_DO_NOT_PUSH')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--event');parser.add_argument('--published',default='watchlist/published');parser.add_argument('--data',default='watchlist/data');parser.add_argument('--verify-core');parser.add_argument('--diagnostics')
    args=parser.parse_args()
    if args.verify_core:verify_current_core(args.published,args.verify_core);return
    operation_started=time.monotonic()
    event=json.loads(Path(args.event).read_bytes());session=requests.Session()
    core,commit,run,cutoff=resolve_publication(session,event,os.environ.get('GITHUB_TOKEN',''))
    raw,rows,blob=fetch_catalog(session,os.environ.get('MANUAL_CATALOG_READ_TOKEN',''))
    previous=read_previous(args.published)
    diagnostics=Path(args.diagnostics) if args.diagnostics else None;inspections={}
    if diagnostics:
        from .manual_eod_audit import audit_population
        diagnostics.mkdir(parents=True,exist_ok=True)
        cache_hit=os.environ.get('CORE_CACHE_HIT')
        audit=audit_population(rows,data_dir=args.data if cache_hit=='true' else None,cutoff=cutoff,
            previous=previous,now=datetime.now(UTC),source_run_id=run,cache_restored=cache_hit=='true',inspections=inspections)
        audit['catalog_sha256']=digest(raw)
        (diagnostics/'cache-audit.json').write_bytes(canonical(audit))
        macro_review=review_catalog(rows);macro_review['catalog_sha256']=digest(raw)
        (diagnostics/'macro-source-review.json').write_bytes(canonical(macro_review))
    provider=YahooDaily()
    yf_batch=YFinanceBatch()
    kr_by_date=VerifiedKRByDate(max_attempts=20)
    doc=build_snapshot(rows,catalog_sha256=digest(raw),core=core,core_commit=commit,workflow_run_id=run,cutoff=cutoff,
        provider=provider,previous=previous,data_dir=args.data,cache_inspections=inspections,
        yf_batch=yf_batch,close_fallback=True,kr_by_date=kr_by_date)
    snapshot_reused=doc is previous
    cache_restore=dict(exact_key='wl-cache-'+run,hit=os.environ.get('CORE_CACHE_HIT')=='true')
    if not snapshot_reused:
        doc['catalog_source']=dict(repository=CATALOG_REPO,path=CATALOG_PATH,git_blob=blob)
        doc['cache_restore']=cache_restore
    # Snapshot metrics describe its original collection. A replay must report
    # this invocation separately without changing the immutable publication.
    metrics=doc.get('collection_metrics')
    if snapshot_reused:
        metrics=dict(http_requests=0,requests_by_market={m:dict(identity=0,price=0) for m in doc['expected_sessions']},
            cache_reused=0,direct_failures=0,circuit_skipped=0,elapsed_seconds=0)
    if diagnostics:
        # The existing EOD diagnostics upload already includes this directory.
        # Exact dates stay in a short-lived artifact, not in strategy feeds.
        gap_audit=audit_missing_history(doc)
        if (not snapshot_reused and
                gap_audit['missing_historical_session_count']!=
                metrics.get('history_gap_sessions')):
            raise ValueError('HISTORY_GAP_METRICS_MISMATCH')
        (diagnostics/'history-gap-audit.json').write_bytes(canonical(gap_audit))
        report=dict(run_id=doc['run_id'],cache_restore=cache_restore,metrics=metrics,snapshot_reused=snapshot_reused,
            requests=provider.request_log,yfinance={**yf_batch.diagnostics(),
                'curl_observation':metrics.get('yfinance_curl_observation') if metrics else None},
            total_build_seconds=round(time.monotonic()-operation_started,3),
            instruments={k:{f:v.get(f) for f in ('market','asset_type','status','reason','cache_diagnostic',
                'http_requests','acquisition','last_final_session','expected_session',
                'last_attempt_at','retry_cooldown','yfinance_staged','yfinance_failure_reason',
                'yfinance_identity_source','close_only_probe',
                'close_only_probe_http_requests')} for k,v in doc['instruments'].items()})
        if snapshot_reused:
            for entry in report['instruments'].values():
                entry.update(http_requests=0,acquisition='previous_snapshot')
        (diagnostics/'collection.json').write_bytes(canonical(report))
    publish_snapshot(args.published,doc)
    output=os.environ.get('GITHUB_OUTPUT')
    if output:
        with open(output,'a') as stream:stream.write('core_run_id='+doc['core_run_id']+'\n')
    counts={s:sum(r['status']==s for r in doc['instruments'].values()) for s in sorted({r['status'] for r in doc['instruments'].values()})}
    print(json.dumps({'run_id':doc['run_id'],'instruments':len(doc['instruments']),'http_requests':provider.calls,'states':counts,'metrics':metrics,'snapshot_reused':snapshot_reused}))



if __name__=='__main__':main()

