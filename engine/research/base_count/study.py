"""Frozen-profile, retrospective research. No production files or signals used.

Hindsight adjusted data and a current convenience sample are NOT a point-in-time
strategy backtest. All attempts, unknown counts, missing outcomes and profiles
remain visible. No return-based parameter selection is implemented.
"""
import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import time

import numpy as np
import pandas as pd

from watchlist.base_detector import BaseDetector, DetectorConfig
from watchlist.session_calendar import get_calendar, calendar_policy

STATES=('NO_BASE','BASE_FORMING','BASE_CONFIRMED','BREAKOUT_PENDING','BREAKOUT_CONFIRMED','BASE_FAILED','TREND_RESET','UNKNOWN')
QUALITIES=('UNKNOWN','HISTORY_INSUFFICIENT','ESTIMATED','CONFIRMED')


def canonical(value):
    return json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def load_dataset(plan_path,data_dir):
    plan_raw=Path(plan_path).read_bytes();plan=json.loads(plan_raw);data_dir=Path(data_dir)
    manifest=json.loads((data_dir/'manifest.json').read_bytes())
    if manifest['plan_sha256']!=hashlib.sha256(plan_raw).hexdigest():
        raise ValueError('dataset plan lineage mismatch')
    data={};metadata={}
    for entry in manifest['entries']:
        metadata[entry['symbol']]=entry
        if entry['status']!='available':continue
        # Both raw vendor evidence and normalized research input must match.
        for path_key,hash_key in [('raw_path','raw_sha256'),('csv_path','csv_sha256')]:
            path=(data_dir/entry[path_key]).resolve()
            if not path.is_relative_to(data_dir.resolve()):raise ValueError('data path outside snapshot')
            if hashlib.sha256(path.read_bytes()).hexdigest()!=entry[hash_key]:raise ValueError('research data revision detected')
        data[entry['symbol']]=pd.read_csv(data_dir/entry['csv_path'],index_col='date',parse_dates=True,float_precision='round_trip')
    return plan,data,metadata,manifest


def calendar(market):
    return get_calendar(market)


def segments(frame,market):
    """Independent research islands, never an operational skip/resume policy."""
    cal=calendar(market)
    if frame.empty:return [],dict(rows=0,valid_rows=0,missing_sessions=[],invalid_rows=[],non_sessions=[])
    if not isinstance(frame.index,pd.DatetimeIndex) or frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
        raise ValueError('ordered unique dates required')
    if frame.index.min()<cal.first_session or frame.index.max()>cal.last_session:
        raise ValueError('calendar coverage unknown')
    expected=cal.sessions_in_range(frame.index.min(),frame.index.max())
    numeric=frame[['Open','High','Low','Close','Volume']].apply(pd.to_numeric,errors='coerce')
    valid=pd.Series(np.isfinite(numeric.to_numpy()).all(axis=1),index=frame.index)&numeric.gt(0).all(axis=1)
    valid &= numeric.High.ge(numeric[['Open','Low','Close']].max(axis=1)) & numeric.Low.le(numeric[['Open','High','Close']].min(axis=1))
    for col in ['_raw_open_valid','_raw_ohlc_valid','_session_confirmed']:
        valid &= frame[col].astype(str).str.lower().isin(('true','1','1.0')) if col in frame else False
    if 'IsHalt' in frame:valid &= ~frame.IsHalt.astype(str).str.lower().isin(('true','1','1.0'))
    runs=[];active=[]
    for date in expected:
        if date in valid.index and valid.loc[date]:active.append(date)
        else:
            if active:runs.append(frame.loc[active].copy());active=[]
    if active:runs.append(frame.loc[active].copy())
    report=dict(rows=len(frame),expected_sessions=len(expected),valid_rows=int(valid.loc[valid.index.intersection(expected)].sum()),
        missing_sessions=expected.difference(frame.index).strftime('%Y-%m-%d').tolist(),
        invalid_rows=frame.index[~valid].strftime('%Y-%m-%d').tolist(),
        non_sessions=frame.index.difference(expected).strftime('%Y-%m-%d').tolist(),
        segments=len(runs),segment_lengths=[len(x) for x in runs])
    return runs,report


def detector(config,instrument,revision,segment_id):
    return BaseDetector(DetectorConfig(**config),market=instrument['market'],ticker=instrument['ticker'],
        price_basis='yahoo-chart:adjclose-ratio:research-v1',data_revision=f'{revision}:{segment_id}')


def fold_for(date,folds):
    return next((x for x in folds if x['start']<=date<=x['end']),None)


def assign_nonoverlap(rows,horizon=20):
    """First attempt scheduling uses only event/entry dates, never future return."""
    busy={}
    for row in sorted(rows,key=lambda r:(r['market'],r['ticker'],r['date'],r['event_id'])):
        key=(row['market'],row['ticker'])
        entry=pd.Timestamp(row['entry_date']) if row.get('entry_date') else None
        row['nonoverlap_20d']=bool(entry is not None and (key not in busy or entry>busy[key]))
        if row['nonoverlap_20d']:
            cal=calendar(row['market']);busy[key]=cal.session_offset(entry,horizon-1)
    return rows


def regime_proxy(benchmark,date):
    if benchmark is None or pd.Timestamp(date) not in benchmark.index:return 'UNKNOWN'
    close=benchmark.loc[:date,'Close'].tail(220)
    if len(close)<220 or not np.isfinite(close).all() or not close.gt(0).all():return 'UNKNOWN'
    a50=float(close.tail(50).mean());a200=float(close.tail(200).mean());old=float(close.iloc[:200].mean())
    if close.iloc[-1]>a50>a200 and a200>=old:return 'UP'
    if close.iloc[-1]<a200 and a200<old:return 'DOWN'
    return 'MIXED'


def evaluate_attempt(row,frame,benchmark,plan):
    """Frozen T observation -> T+1 Open, exact N sessions, no successful-hold filter."""
    out=dict(row);cal=calendar(row['market']);signal=pd.Timestamp(row['date'])
    entry=cal.next_session(signal);out['entry_date']=entry.strftime('%Y-%m-%d')
    fold=fold_for(row['date'],plan['folds']);out['fold']=fold['name'] if fold else 'OUTSIDE'
    out['benchmark_price_regime_proxy']=regime_proxy(benchmark,row['date'])
    comparable = row['market']=='US'
    out['benchmark_comparison_quality']='ADJUSTED_ETF_COMPARATOR' if comparable else 'PRICE_INDEX_VS_ADJUSTED_EQUITY'
    for n in plan['horizons']:
        end=cal.session_offset(entry,n-1);key=str(n)
        out['exit_date_'+key]=end.strftime('%Y-%m-%d')
        out['outcome_'+key]='INCOMPLETE';out['net_return_pp_'+key]=None;out['benchmark_excess_pp_'+key]=None
        if not fold or end>pd.Timestamp(fold['end']) or end>pd.Timestamp(plan['cutoff']):
            out['outcome_'+key]='FOLD_BOUNDARY_OR_IMMATURE';continue
        required=cal.sessions_in_range(entry,end)
        if not required.isin(frame.index).all():continue
        prices=frame.loc[required]
        values=prices[['Open','High','Low','Close']].to_numpy(dtype=float)
        if not np.isfinite(values).all() or not (values>0).all():continue
        entry_price=float(prices.Open.iloc[0]);exit_price=float(prices.Close.iloc[-1])
        gross=(exit_price/entry_price-1)*100
        cost=plan['reference_cost_bps']/100
        out['outcome_'+key]='COMPLETE';out['gross_return_pp_'+key]=gross
        out['net_return_pp_'+key]=gross-cost
        out['mfe_pp_'+key]=(float(prices.High.max())/entry_price-1)*100
        out['mae_pp_'+key]=(float(prices.Low.min())/entry_price-1)*100
        for bps in plan['round_trip_cost_bps']:out[f'net_return_pp_{n}_cost_{bps}bps']=gross-bps/100
        if comparable and benchmark is not None and required.isin(benchmark.index).all():
            b=benchmark.loc[required]
            if (np.isfinite(b[['Open','Close']].to_numpy()).all() and b[['Open','Close']].gt(0).all().all()):
                out['benchmark_excess_pp_'+key]=gross-(float(b.Close.iloc[-1])/float(b.Open.iloc[0])-1)*100-cost
    return out


def cluster_interval(rows,value,seed,repetitions=1000):
    eligible=[r for r in rows if r.get(value) is not None and np.isfinite(r[value])]
    tickers=sorted({r['ticker'] for r in eligible})
    if len(tickers)<5 or len(eligible)<20:return None
    grouped={t:np.array([r[value] for r in eligible if r['ticker']==t]) for t in tickers}
    rng=np.random.default_rng(seed);means=[]
    for _ in range(repetitions):
        sample=rng.choice(tickers,len(tickers),replace=True)
        means.append(float(np.concatenate([grouped[t] for t in sample]).mean()))
    return [float(x) for x in np.quantile(means,[.025,.975])]


def run_instrument(args):
    """Independent CPU task; does not call providers or touch operational files."""
    import warnings
    warnings.filterwarnings('ignore',category=DeprecationWarning)
    plan,instrument,frame,benchmark,revision,output=args
    output=Path(output);name=instrument['market']+'_'+instrument['ticker']
    runs,quality=segments(frame,instrument['market']);quality.update(instrument)
    now=datetime.now(timezone.utc).isoformat()
    # Report operational strict replay separately from explicitly segmented research.
    strict=detector(plan['profiles']['reference'],instrument,revision,'strict').update(frame,observed_at=now,mode='REPLAY')
    quality.update(strict_issue=strict.issue,strict_processed=len(strict.observations),
                   strict_coverage=len(strict.observations)/len(frame) if len(frame) else 0)
    arrays={};summaries=[];all_attempts=[];reference_observations=[];reference_events=[];checks=[]
    for profile,config in plan['profiles'].items():
        began=time.perf_counter();observations=[];events=[];attempts=[];lineages=[]
        for segment_id,prices in enumerate(runs):
            model=detector(config,instrument,revision,segment_id)
            result=model.update(prices,observed_at=now,mode='REPLAY')
            if result.issue:raise AssertionError((name,profile,segment_id,result.issue))
            records=[asdict(x) for x in result.observations]
            lookup={x['as_of']:x for x in records};first=set()
            for event in result.events:
                if event.kind!='BREAKOUT' or event.base_id in first:continue
                first.add(event.base_id);snap=lookup[event.date]
                row=dict(asdict(event),profile=profile,market=instrument['market'],ticker=instrument['ticker'],
                    symbol=instrument['symbol'],segment_id=segment_id,ordinal_at_signal=snap['current_base_ordinal'],
                    quality_at_signal=snap['quality'],completed_at_signal=snap['completed_base_count'],
                    sector_current=instrument['sector_current'],benchmark=instrument['benchmark'],
                    snapshot_sha256=digest(snap),source_revision=revision)
                attempts.append(evaluate_attempt(row,prices,benchmark,plan))
            if profile=='reference':
                reference_observations.extend(dict(x,segment_id=segment_id) for x in records)
                reference_events.extend(dict(asdict(x),segment_id=segment_id) for x in result.events)
                # Exact truncated-history causality at three predeclared fractions.
                if len(prices)>=220:
                    for fraction in (.3,.6,.9):
                        count=max(1,int(len(prices)*fraction));cut=prices.iloc[:count]
                        partial=detector(config,instrument,revision,segment_id).update(cut,observed_at=now,mode='REPLAY')
                        assert partial.latest.logical()==result.observations[count-1].logical()
                        assert [e.logical() for e in partial.events]==[e.logical() for e in result.events if e.date<=partial.latest.as_of]
                    # Rolling windows + periodic real JSON checkpoint round trips.
                    resumed=detector(config,instrument,revision,segment_id);rows=[];ev=[]
                    for end in range(21,len(prices)+21,21):
                        end=min(end,len(prices));last=resumed._state['last']
                        start=max(0,prices.index.get_loc(pd.Timestamp(last))-20) if last else 0
                        batch=resumed.update(prices.iloc[start:end],observed_at=now,mode='REPLAY')
                        assert batch.issue is None;rows.extend(batch.observations);ev.extend(batch.events)
                        context=dict(config=DetectorConfig(**config),market=instrument['market'],ticker=instrument['ticker'],
                            price_basis='yahoo-chart:adjclose-ratio:research-v1',data_revision=f'{revision}:{segment_id}')
                        resumed=BaseDetector.restore(json.loads(json.dumps(resumed.checkpoint())),**context)
                    assert [x.logical() for x in rows]==[x.logical() for x in result.observations]
                    assert [x.logical() for x in ev]==[x.logical() for x in result.events]
                    checks.append(dict(segment_id=segment_id,sessions=len(prices),causality_cuts=3,incremental=True))
            observations.extend(records);events.extend(asdict(x) for x in result.events)
        attempts=assign_nonoverlap(attempts);all_attempts.extend(attempts)
        matrix=np.array([[STATES.index(x['status']),QUALITIES.index(x['quality']),
                          x['current_base_ordinal'] if x['current_base_ordinal'] is not None else -1,
                          x['completed_base_count'] if x['completed_base_count'] is not None else -1]
                         for x in observations],dtype=np.int32)
        arrays[profile]=matrix
        summaries.append(dict(market=instrument['market'],ticker=instrument['ticker'],profile=profile,
            sessions=len(observations),events=dict(Counter(x['kind'] for x in events)),
            statuses=dict(Counter(x['status'] for x in observations)),qualities=dict(Counter(x['quality'] for x in observations)),
            absolute_known_sessions=sum(x['completed_base_count'] is not None for x in observations),
            first_attempts=len(attempts),known_ordinal_attempts=sum(x['ordinal_at_signal'] is not None for x in attempts),
            elapsed_seconds=time.perf_counter()-began))
    baseline=arrays['reference']
    for row in summaries:
        other=arrays[row['profile']]
        row['state_agreement_with_reference']=float(np.mean(other[:,0]==baseline[:,0])) if len(baseline) else None
        row['count_agreement_with_reference']=float(np.mean(np.all(other[:,2:]==baseline[:,2:],axis=1))) if len(baseline) else None
        known=(other[:,3]>=0)&(baseline[:,3]>=0)
        row['both_known_sessions']=int(known.sum())
        row['conditional_count_agreement']=float(np.mean(np.all(other[known,2:]==baseline[known,2:],axis=1))) if known.any() else None
    with gzip.open(output/'observations'/f'{name}.jsonl.gz','wt') as stream:
        for record in reference_observations:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
    with gzip.open(output/'events'/f'{name}.jsonl.gz','wt') as stream:
        for record in reference_events:stream.write(json.dumps(record,ensure_ascii=False)+'\n')
    np.savez_compressed(output/'sensitivity'/f'{name}.npz',**arrays)
    return dict(instrument=instrument,quality=quality,summaries=summaries,attempts=all_attempts,checks=checks)


def aggregate(attempts,plan):
    groups=[]
    for profile in plan['profiles']:
        for market in ('US','KR'):
            for fold in [x['name'] for x in plan['folds']]:
                for ordinal in ('1','2','3','4+','UNKNOWN'):
                    rows=[r for r in attempts if r['profile']==profile and r['market']==market and r['fold']==fold
                        and ('UNKNOWN' if r['ordinal_at_signal'] is None else '4+' if r['ordinal_at_signal']>=4 else str(r['ordinal_at_signal']))==ordinal]
                    if not rows:continue
                    for n in plan['horizons']:
                        chosen=[r for r in rows if r['nonoverlap_20d']]
                        complete=[r for r in chosen if r['outcome_'+str(n)]=='COMPLETE']
                        vals=[r['net_return_pp_'+str(n)] for r in complete]
                        excess=[r['benchmark_excess_pp_'+str(n)] for r in complete if r['benchmark_excess_pp_'+str(n)] is not None]
                        groups.append(dict(profile=profile,market=market,fold=fold,ordinal=ordinal,horizon=n,
                            attempts=len(rows),nonoverlap_attempts=len(chosen),complete=len(complete),incomplete=len(chosen)-len(complete),
                            tickers=len({r['ticker'] for r in complete}),mean_net_return_pp=float(np.mean(vals)) if vals else None,
                            median_net_return_pp=float(np.median(vals)) if vals else None,
                            mean_benchmark_excess_pp=float(np.mean(excess)) if excess else None,
                            win_rate=float(np.mean(np.array(vals)>0)) if vals else None,
                            descriptive_cluster_ci=cluster_interval(complete,'net_return_pp_'+str(n),plan['seed'],plan['bootstrap_repetitions']) if profile=='reference' else None,
                            confirmatory_inference_allowed=False))
    return groups


def run(plan_path,data_dir,output,workers=4):
    plan,data,metadata,manifest=load_dataset(plan_path,data_dir)
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    for name in ('observations','events','sensitivity'): (output/name).mkdir()
    began=time.perf_counter();tasks=[];unavailable=[]
    for instrument in plan['instruments']:
        symbol=instrument['symbol']
        if symbol not in data:
            unavailable.append(instrument);continue
        tasks.append((plan,instrument,data[symbol],data.get(instrument['benchmark']),metadata[symbol]['raw_sha256'],output))
    results=[]
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for future in as_completed([pool.submit(run_instrument,arg) for arg in tasks]):
            result=future.result();results.append(result)
            print(json.dumps(dict(symbol=result['instrument']['symbol'],segments=result['quality']['segments'],
                strict_issue=result['quality']['strict_issue'],
                reference_attempts=next(s['first_attempts'] for s in result['summaries'] if s['profile']=='reference')),ensure_ascii=False),flush=True)
    attempts=[x for r in results for x in r['attempts']]
    pd.DataFrame(attempts).to_csv(output/'attempts.csv',index=False)
    pd.DataFrame([x for r in results for x in r['summaries']]).to_csv(output/'sensitivity.csv',index=False)
    summary=dict(schema='base-research-results-1',study_id=plan['study_id'],plan_sha256=manifest['plan_sha256'],
        calendar_policy={market:calendar_policy(market) for market in ('KR','US')},
        generated_at=datetime.now(timezone.utc).isoformat(),elapsed_seconds=time.perf_counter()-began,
        instruments_available=len(results),instruments_unavailable=unavailable,
        quality=[r['quality'] for r in results],checks=[dict(instrument=r['instrument'],segments=r['checks']) for r in results],
        groups=aggregate(attempts,plan),production_changes=False,performance_superiority_established=False,
        limitations=plan['limitations'])
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    sources={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file()}
    (output/'manifest.json').write_text(json.dumps(dict(schema='base-research-artifacts-1',files=sources),indent=2)+'\n')
    print(json.dumps(dict(done=True,instruments=len(results),attempts=len(attempts),elapsed_seconds=summary['elapsed_seconds'])),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--data',required=True)
    parser.add_argument('--output',required=True);parser.add_argument('--workers',type=int,default=4)
    args=parser.parse_args();run(args.plan,args.data,args.output,args.workers)

