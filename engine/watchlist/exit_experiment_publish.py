"""Optional immutable research publication. No writes to official files/caches."""
from __future__ import annotations
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
from collections import Counter
import argparse
import io
import json
import os
import re
import tempfile
import time
import platform
import numpy as np
import exchange_calendars as xc

import pandas as pd
import requests

from .exit_policy_experiments import RULES,HORIZONS,RULE_VERSION,COST_VERSION,canonical,digest,finite,stamp,run_signal,signal_id,price_context,frame_hash,prepare,simulate
from .exit_policy_statistics import statistics
from .exit_experiment_contract import SCHEMA,VERSION,FILES,pack,unpack,manifest_path,validate_manifest,validate_payload,json_doc
from .manual_eod_publish import resolve_publication,_github,verify_current_core
from .analysis_report import resolve_publications

CONFIG={'rule_version':RULE_VERSION,'rules':list(RULES),'horizons':list(HORIZONS),'target_R':2.,'sma_confirmation_closes':2,
    'atr_period':14,'atr_smoothing':'Wilder_seed_14_consecutive_TR','atr_multiple':3.,'atr_anchor':'since_entry_high',
    'channel_length':10,'channel_shift':1,'intrabar_conflict':'stop_first','same_open_stop_tie':'higher_stop_then_initial',
    'entry_policy':'first_exchange_open_strictly_after_verified_publication_and_asof-v2','cost_model':COST_VERSION,
    'maturity':'entry_session_is_1;intraday_before_expiry_close','expost_large_move_pct':50.,
    'uncertainty':'ticker_x_horizon_entry_session_block_pigeonhole_400_seeded',
    'retrospective':'source_generation_before_first_registry_publication','no_optimization':True}
ROOT='exit_experiments/'+VERSION
MAX_SIGNALS=4000


def registry_for(existing,created_at,cost_pct):
    code_files=('exit_policy_experiments.py','exit_policy_atr.py','exit_policy_donchian.py','exit_policy_statistics.py','exit_experiment_contract.py','exit_experiment_publish.py','session_calendar.py','analysis_report.py','trend_tracking.py')
    calculation_hash=digest(b''.join((Path(__file__).parent/name).read_bytes() for name in code_files))
    config={**CONFIG,'round_trip_cost_pct':float(cost_pct),'calculation_hash':calculation_hash,'runtime_versions':{'python':list(platform.python_version_tuple()[:2]),'pandas':pd.__version__,'numpy':np.__version__,'exchange_calendars':xc.__version__}};hashed=digest(canonical(config))
    if existing:
        if existing.get('schema')!=SCHEMA or existing.get('experiment_version')!=VERSION or existing.get('experiment_config_hash')!=hashed or existing.get('config')!=config:raise ValueError('EXPERIMENT_VERSION_REQUIRES_NEW_REGISTRY')
        stamp(existing['prospective_start_at']);return existing
    return dict(schema=SCHEMA,experiment_version=VERSION,rule_version=RULE_VERSION,experiment_config_hash=hashed,
        prospective_start_at=stamp(created_at).isoformat(),experiment_created_at=stamp(created_at).isoformat(),config=config)


def _minimal_rows(signal,reason):
    return [dict(signal_id=signal_id(signal),source_run_id=signal.get('run_id'),source_config_hash=signal.get('config_hash'),
        source_signal_fingerprint=digest(canonical(signal)),market=signal.get('market'),ticker=signal.get('ticker'),name=signal.get('name'),
        sector=signal.get('sector'),regime=signal.get('regime'),first_seen=signal.get('first_seen'),as_of=signal.get('as_of'),rule_id=rule,
        rule_version=VERSION,horizon=n,eligibility_status='pending' if reason=='ENTRY_PENDING' else 'excluded',completion_status='entry_pending' if reason=='ENTRY_PENDING' else 'excluded',exclusion_reason=reason,
        comparison_eligible=False,return_eligible=False,r_eligible=False,intrabar_ambiguous=False) for n in HORIZONS for rule in RULES]


def public_path(p):
    # One shared OHLC/indicator path per signal, not per strategy. Full warm-up
    # history affects hashes/indicators; only 60 prior sessions are needed to plot.
    dates=p.frame.loc[p.frame.index<p.entry].tail(60).index.append(p.path.index)
    frame=p.frame.reindex(dates)
    result={'d':[d.date().isoformat() for d in dates]}
    for src,dest in (('Open','o'),('High','h'),('Low','l'),('Close','c'),('Volume','v')):
        result[dest]=[finite(v) for v in frame[src]] if src in frame else [None]*len(frame)
    for key,series in p.indicators.items():result[key]=[finite(v) for v in series.reindex(dates)]
    from .exit_policy_experiments import frame_payload
    evidence=frame_payload(p.frame)
    result.update(entry_date=p.meta['entry_date'],price_basis=p.meta['price_basis'],market=p.meta['market'],ticker=p.meta['ticker'],
        input_evidence={k:[row[k] for row in evidence] for k in evidence[0]},atr_seed_policy='14 consecutive TR with valid previous close')
    return result


def build(signals,load_prices,*,core,source_commit,source_signals_sha256,cutoff,created_at,cost_pct,registry=None,previous=None,max_seconds=600):
    registry=registry_for(registry,created_at,cost_pct);cutoff=stamp(cutoff);started=time.monotonic()
    # Reused certified rows and archived evidence belong to the previous
    # evaluation clock. A backdated run must start independently, not inherit
    # future closes/fills from that state (especially when its cache is absent).
    if previous:
        prior_cutoff=(previous.get('manifest') or {}).get('evaluation_cutoff_at')
        if prior_cutoff is None:raise ValueError('PREVIOUS_EVALUATION_CUTOFF_MISSING')
        if cutoff<stamp(prior_cutoff):raise ValueError('EVALUATION_CUTOFF_REGRESSION')
    records=[];paths={};states={};price_cache={};contexts={};price_errors={};grouped={};old_states=(previous or {}).get('states',{})
    old_rows={};old_paths=(previous or {}).get('paths',{})
    for r in (previous or {}).get('results',[]):old_rows.setdefault(r['signal_id'],[]).append(r)
    for raw in signals:
        signal={k:(v.item() if hasattr(v,'item') else v) for k,v in raw.items()}
        signal['ticker']=str(signal.get('ticker','')).zfill(6) if signal.get('market')=='KR' else str(signal.get('ticker','')).upper()
        grouped.setdefault(signal_id(signal),[]).append(signal)
    if len(grouped)>MAX_SIGNALS:raise ValueError('RESEARCH_SIGNAL_BUDGET_EXCEEDED')
    archived={}
    for path in old_paths.values():
        if path.get('input_evidence'):
            frame=pd.DataFrame(path['input_evidence']).set_index('date');frame.index=pd.to_datetime(frame.index)
            archived.setdefault((path.get('market'),path.get('ticker')),[]).append(frame)
    for sid,duplicates in sorted(grouped.items()):
        if time.monotonic()-started>max_seconds:raise ValueError('RESEARCH_TIME_BUDGET_EXCEEDED')
        signal=duplicates[0];unique={digest(canonical(x)) for x in duplicates};p=None
        try:
            vintage='prospective' if stamp(signal['generated_at'])>=stamp(registry['prospective_start_at']) and stamp(signal['published_at'])>=stamp(registry['prospective_start_at']) else 'retrospective'
        except (ValueError,TypeError):vintage='retrospective'
        if len(unique)>1:rows=_minimal_rows(signal,'DUPLICATE_SIGNAL_ID_CONFLICT')
        else:
            key=(signal.get('market'),signal.get('ticker'))
            if key not in price_cache:
                try:
                    price_cache[key]=load_prices(*key)
                    # Restore only older certified history that a rolling cache
                    # aged out. Never fill a gap inside its current date range.
                    if not price_cache[key].empty and key in archived:
                        oldest=pd.Timestamp(price_cache[key].index.min())
                        history=pd.concat(archived[key]).sort_index();history=history[~history.index.duplicated(keep='first')]
                        price_cache[key]=pd.concat([history.loc[history.index<oldest],price_cache[key]]).sort_index()
                    contexts[key]=price_context(price_cache[key],key[0],cutoff)
                except Exception as exc:
                    price_cache[key]=pd.DataFrame();contexts[key]=None
                    price_errors[key]=str(exc) if isinstance(exc,ValueError) and str(exc).isupper() else 'PRICE_CACHE_UNAVAILABLE'
            if key in price_errors:rows=_minimal_rows(signal,price_errors[key])
            else:
                try:
                    p=prepare(signal,price_cache[key],cutoff,cost_pct,contexts[key]);rows=None
                except (ValueError,TypeError,KeyError,IndexError,OverflowError) as exc:
                    rows=_minimal_rows(signal,str(exc) if isinstance(exc,ValueError) else 'SIGNAL_OR_PRICE_STRUCTURE_INVALID')
        previous_state=old_states.get(sid)
        current={'status':'excluded','reason':rows[0].get('exclusion_reason') if rows else None}
        if p is not None:
            signature=digest(canonical({**p.meta,'experiment_config_hash':registry['experiment_config_hash']}))
            changed=None
            if previous_state and previous_state.get('trusted_prefix_hash'):
                if previous_state.get('source_signal_fingerprint')!=p.meta['source_signal_fingerprint'] or previous_state.get('round_trip_cost_pct')!=cost_pct:
                    changed='SOURCE_OR_COST_REVISION_REQUIRES_REVIEW'
                else:
                    first,last=previous_state['trusted_prefix_first_date'],previous_state['trusted_prefix_last_date']
                    old_prefix=p.frame.loc[(p.frame.index>=pd.Timestamp(first))&(p.frame.index<=pd.Timestamp(last))]
                    # Wilder smoothing depends on the entire leading history,
                    # not just the old overlap. A prepended prefix changes the
                    # seed even when every previously certified bar is intact.
                    if p.meta['prefix_first_date']!=first or old_prefix.empty or str(old_prefix.index[0].date())!=first or str(old_prefix.index[-1].date())!=last or frame_hash(old_prefix)!=previous_state['trusted_prefix_hash']:
                        changed='PRICE_REVISION_REQUIRES_REVIEW'
            if changed:
                rows=_minimal_rows(signal,changed);current={**previous_state,'status':'revision_pending','reason':changed,'observed_revision_hash':p.meta['price_path_hash']}
                if sid in old_paths:paths[sid]=old_paths[sid]
            else:
                # Reuse complete result rows only after checking the old trusted
                # prefix, never skip detection merely because a cohort matured.
                if previous_state and previous_state.get('status')=='verified' and previous_state.get('input_signature')==signature and sid in old_rows:
                    rows=deepcopy(old_rows[sid]);paths[sid]=old_paths.get(sid,{})
                else:
                    rows=[simulate(p,rule,n) for n in HORIZONS for rule in RULES]
                    paths[sid]=public_path(p)
                current={'status':'verified','input_signature':signature,'trusted_prefix_hash':p.meta['price_path_hash'],
                    'trusted_prefix_first_date':p.meta['prefix_first_date'],'trusted_prefix_last_date':p.meta['data_as_of'],
                    'source_signal_fingerprint':p.meta['source_signal_fingerprint'],'round_trip_cost_pct':cost_pct}
        # Never clear a revision just because a later partial cache cannot verify it.
        elif previous_state:
            fingerprint=digest(canonical({k:str(signal.get(k,'')) for k in ('first_seen','as_of','market','ticker','run_id','config_hash','engine_version','engine_commit','stop_price','signal_close','published_at','generated_at')}))
            conflict=previous_state.get('source_signal_fingerprint') and previous_state['source_signal_fingerprint']!=fingerprint
            if conflict or rows[0].get('exclusion_reason') in ('SIGNAL_PRICE_BASIS_MISMATCH','SIGNAL_ANCHOR_INVALID','PRICE_BASIS_UNVERIFIED'):
                reason='SOURCE_OR_COST_REVISION_REQUIRES_REVIEW' if conflict else 'PRICE_REVISION_REQUIRES_REVIEW'
                rows=_minimal_rows(signal,reason);current={**previous_state,'status':'revision_pending','reason':reason}
                if sid in old_paths:paths[sid]=old_paths[sid]
            elif previous_state.get('status')=='revision_pending':
                rows=_minimal_rows(signal,previous_state['reason']);current=previous_state
            else:
                current={**previous_state,'status':'input_unavailable','reason':rows[0].get('exclusion_reason')}
                archived_rows={(r['rule_id'],r['horizon']):r for r in old_rows.get(sid,[])}
                cache_unavailable=rows[0].get('exclusion_reason') in ('PRICE_CACHE_MISSING','PRICE_CACHE_UNAVAILABLE','PRICE_CACHE_NO_CLOSED_SESSION')
                for i,row in enumerate(rows):
                    old=archived_rows.get((row['rule_id'],row['horizon']))
                    if cache_unavailable and old and old.get('completion_status')=='complete' and old.get('horizon_complete'):
                        rows[i]={**old,'current_price_revalidation':'cache_unavailable;previous_certified_complete_history'}
                if sid in old_paths:paths[sid]=old_paths[sid]
        for row in rows:
            row.update(vintage=vintage,experiment_config_hash=registry['experiment_config_hash'],experiment_created_at=registry['experiment_created_at'],
                prospective_start_at=registry['prospective_start_at'])
        records.extend(rows);states[sid]=current
    # Store the causal stop trajectory once per signal/policy, sharing its
    # prefixes across 20/60/120 horizons. Constant initial stops need no series.
    for row in records:
        logs=row.pop('stop_path',None)
        if logs and row['signal_id'] in paths:
            traces=paths[row['signal_id']].setdefault('effective_stops',{})
            current=traces.get(row['rule_id'],[])
            if len(logs)>len(current):traces[row['rule_id']]=logs
            elif logs!=current[:len(logs)]:raise ValueError('CAUSAL_STOP_PREFIX_MISMATCH')
            row['stop_path_ref']=row['rule_id'];row['stop_path_length']=len(logs)
    if previous and records==previous.get('results') and 'statistics' in previous and 'pairs' in previous:
        stats,pairs=previous['statistics'],previous['pairs']
    else:stats,pairs=statistics(records)
    files={'results.json.gz':records,'paths.json.gz':paths,'statistics.json.gz':stats,'pairs.json.gz':pairs}
    source=dict(source_run_id=core['run_id'],source_generated_at=core['generated_at'],source_publication_commit=source_commit,
        source_manifest_sha256=digest(canonical(core)),source_signals_sha256=source_signals_sha256,evaluation_cutoff_at=cutoff.isoformat())
    snapshot_id=digest(canonical({'registry':registry,'source':source,'states':states,'results':records}))
    raw={name:pack(data) for name,data in files.items()}
    man=dict(schema=SCHEMA,experiment_version=VERSION,snapshot_id=snapshot_id,rule_version=VERSION,rules=list(RULES),
        price_as_of_by_market={market:max((r['data_as_of'] for r in records if r.get('market')==market and r.get('data_as_of')),default=None) for market in ('KR','US')},
        horizons=list(HORIZONS),experiment_config_hash=registry['experiment_config_hash'],prospective_start_at=registry['prospective_start_at'],
        registry_sha256=digest(canonical(registry)),**source,states=states,
        files={name:{'sha256':digest(content),'bytes':len(content)} for name,content in raw.items()})
    pointer=dict(schema=SCHEMA,experiment_version=VERSION,snapshot_id=snapshot_id,manifest_file=f'{ROOT}/runs/{snapshot_id}/manifest.json',manifest_sha256=digest(canonical(man)))
    validate_manifest(man,pointer,canonical(man));validate_payload(man,files)
    return {'registry':registry,'manifest':man,'pointer':pointer,'raw':raw,'results':records,'paths':paths,'statistics':stats,'pairs':pairs,'states':states}


def load_previous(base):
    base=Path(base);root=base/ROOT;registry=json_doc((root/'registry.json').read_bytes()) if (root/'registry.json').exists() else None
    errors=False
    for name in ('latest.json','previous.json'):
        ptr=root/name
        if not ptr.exists():continue
        try:
            pointer=json_doc(ptr.read_bytes());rel=manifest_path(pointer);raw=(base/rel).read_bytes();man=json_doc(raw);validate_manifest(man,pointer,raw)
            if not registry or digest(canonical(registry))!=man['registry_sha256']:raise ValueError('REGISTRY_INTEGRITY')
            directory=(base/rel).parent;data={file:unpack((directory/file).read_bytes(),meta) for file,meta in man['files'].items()};validate_payload(man,data)
            return registry,{'manifest':man,'pointer':pointer,'results':data['results.json.gz'],'paths':data['paths.json.gz'],'statistics':data['statistics.json.gz'],'pairs':data['pairs.json.gz'],'states':man['states']}
        except (ValueError,TypeError,KeyError,OSError):errors=True
    if errors:raise ValueError('NO_VALID_RESEARCH_PUBLICATION')
    return registry,None


def _atomic(path,raw):
    path.parent.mkdir(parents=True,exist_ok=True);fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.stage-')
    try:
        with os.fdopen(fd,'wb') as stream:stream.write(raw)
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def publish(base,bundle):
    base=Path(base);man=bundle['manifest'];current=json_doc((base/'latest/manifest.json').read_bytes())
    if current.get('run_id')!=man['source_run_id'] or digest(canonical(current))!=man['source_manifest_sha256']:raise ValueError('CORE_MOVED_DO_NOT_PUBLISH_RESEARCH')
    pointer=bundle['pointer'];rel=manifest_path(pointer);directory=(base/rel).parent
    registry,previous=load_previous(base)
    if registry is not None and canonical(registry)!=canonical(bundle['registry']):raise ValueError('IMMUTABLE_REGISTRY_CONFLICT')
    contents={**bundle['raw'],'manifest.json':canonical(man)}
    # All immutable paths are checked before creating any of them.
    for file,raw in contents.items():
        path=directory/file
        if path.exists() and path.read_bytes()!=raw:raise ValueError('IMMUTABLE_RESEARCH_RESULT_CONFLICT')
    validate_payload(man,{file:unpack(raw,man['files'][file]) for file,raw in bundle['raw'].items()})
    root=base/ROOT
    for file,raw in contents.items():
        path=directory/file
        if not path.exists():_atomic(path,raw)
    if registry is None:_atomic(root/'registry.json',canonical(bundle['registry']))
    if previous and previous['pointer']==pointer:return
    if previous:_atomic(root/'previous.json',canonical(previous['pointer']))
    _atomic(root/'latest.json',canonical(pointer))
    # No pruning: older research runs and versions are immutable audit history.


def read_market_inputs(base,core,lookup=None):
    base=Path(base);raw=(base/'history/signals.csv').read_bytes();report=(base/'analysis/latest.md').read_text()
    analysis=json.loads(report.split('````json\n',1)[1].rsplit('\n````',1)[0]);source=analysis['source']
    if source.get('run_id')!=core['run_id'] or not source.get('history_update_ok') or source['history_sources']['signals.csv']['sha256']!=digest(raw):raise ValueError('CORE_SIGNAL_LINEAGE_UNVERIFIED')
    cost=finite(analysis['validation']['policy']['round_trip_cost_pct'])
    if cost is None or cost<0:raise ValueError('SOURCE_COST_UNVERIFIED')
    frame=pd.read_csv(io.BytesIO(raw),dtype=str).fillna('')
    frame['published_at']=None # Successful workflow evidence, not a row's assertion.
    frame,facts=resolve_publications(frame,analysis.get('publication_evidence',{}),lookup,max_calls=4)
    records=[]
    for row in frame.to_dict('records'):
        row['publication_verified']=pd.notna(row.get('published_at'))
        # JSON-safe unknowns stay unknown; no timestamp/cost inference.
        row={k:None if pd.isna(v) else v for k,v in row.items()}
        records.append(row)
    return records,digest(raw),cost


def cached_prices(data,market,ticker):
    if market not in ('KR','US') or not re.fullmatch(r'[A-Z0-9][A-Z0-9.-]{0,30}',ticker):raise ValueError('CACHE_IDENTITY_INVALID')
    file=Path(data)/('kr_history_cache' if market=='KR' else 'us_history_cache')/(ticker+'.pkl')
    if not file.exists():return pd.DataFrame()
    # Only exact trusted-main workflow caches are restored by the separate job.
    return pd.read_pickle(file)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--event');parser.add_argument('--published',default='watchlist/published');parser.add_argument('--data',default='watchlist/data');parser.add_argument('--verify-core');parser.add_argument('--verify-core-hash')
    args=parser.parse_args()
    if args.verify_core:
        verify_current_core(args.published,args.verify_core)
        if args.verify_core_hash and digest(canonical(json_doc((Path(args.published)/'latest/manifest.json').read_bytes())))!=args.verify_core_hash:raise ValueError('CORE_MANIFEST_CHANGED_DO_NOT_PUSH_RESEARCH')
        return
    event=json_doc(Path(args.event).read_bytes());core,commit,run,cutoff=resolve_publication(requests.Session(),event,os.environ.get('GITHUB_TOKEN',''))
    current=json_doc((Path(args.published)/'latest/manifest.json').read_bytes())
    if canonical(current)!=canonical(core):raise ValueError('CORE_MOVED_BEFORE_RESEARCH')
    registry,previous=load_previous(args.published)
    signals,signals_hash,cost=read_market_inputs(args.published,core)
    bundle=build(signals,lambda market,ticker:cached_prices(args.data,market,ticker),core=core,source_commit=commit,source_signals_sha256=signals_hash,
        cutoff=cutoff,created_at=datetime.now(timezone.utc),cost_pct=cost,registry=registry,previous=previous)
    publish(args.published,bundle)
    if os.environ.get('GITHUB_OUTPUT'):
        with open(os.environ['GITHUB_OUTPUT'],'a') as stream:stream.write('core_run_id='+core['run_id']+'\ncore_manifest_hash='+bundle['manifest']['source_manifest_sha256']+'\n')
    print(json.dumps({'experiment_version':VERSION,'snapshot_id':bundle['manifest']['snapshot_id'],'signals':len(bundle['states']),'results':len(bundle['results'])}))


if __name__=='__main__':main()


