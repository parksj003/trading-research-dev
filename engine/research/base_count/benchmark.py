"""Real-input mechanical benchmark, after research workers finish. No APIs."""
import argparse
from dataclasses import asdict
from datetime import datetime,timezone
import json
from pathlib import Path
import resource
import statistics
import tempfile
import time

from watchlist.base_count_publish import write_object
from watchlist.base_detector import BaseDetector,DetectorConfig
from .study import load_dataset,segments,detector


def measure(operation,repetitions=3):
    seconds=[]
    for _ in range(repetitions):
        began=time.perf_counter();operation();seconds.append(time.perf_counter()-began)
    return dict(median_seconds=statistics.median(seconds),runs=repetitions)


def run(plan_path,data_dir,output):
    plan,data,metadata,_=load_dataset(plan_path,data_dir);now=datetime.now(timezone.utc).isoformat();results=[]
    for symbol in ['AAPL','005930.KS']:
        instrument=next(x for x in plan['instruments'] if x['symbol']==symbol)
        prices=max(segments(data[symbol],instrument['market'])[0],key=len)
        revision=metadata[symbol]['raw_sha256'];config=plan['profiles']['reference']
        new=lambda:detector(config,instrument,revision,'benchmark')
        context=dict(config=DetectorConfig(**config),market=instrument['market'],ticker=instrument['ticker'],
            price_basis='yahoo-chart:adjclose-ratio:research-v1',data_revision=revision+':benchmark')
        # Warm calendar import once; timings are warm-library, in-memory inputs.
        d=new();batch=d.update(prices,observed_at=now,mode='REPLAY');envelope=d.checkpoint()
        row=dict(symbol=symbol,sessions=len(prices),additional_provider_calls=0)
        row['full']=measure(lambda:new().update(prices,observed_at=now,mode='REPLAY'))
        row['state_loss_replay']=measure(lambda:new().update(prices,observed_at=now,mode='REPLAY'))
        for n in (1,5,21):
            old=new();old.update(prices.iloc[:-n],observed_at=now,mode='REPLAY');checkpoint=old.checkpoint()
            def incremental(n=n,checkpoint=checkpoint,window=None):
                model=BaseDetector.restore(checkpoint,**context)
                return model.update(prices.iloc[-(window or n+1):],observed_at=now,mode='REPLAY')
            row[f'incremental_{n}_minimal_overlap']=measure(incremental)
            if n==1:row['incremental_1_with_504_session_input']=measure(lambda:incremental(window=504))
        row['unchanged_full_input_noop']=measure(lambda:BaseDetector.restore(envelope,**context).update(prices,observed_at=now,mode='REPLAY'))
        payload=dict(schema='base-instrument-1',instrument=instrument['market']+':'+instrument['ticker'],parent=None,
            checkpoint=envelope,observations=[asdict(s) for s in batch.observations],events=[asdict(e) for e in batch.events])
        packed=[]
        def durable_write():
            with tempfile.TemporaryDirectory(prefix='base-research-write-') as td:
                digest=write_object(td,payload)
                packed.append((Path(td)/'history/base_count/objects'/f'{digest}.json.gz').stat().st_size)
        row['bootstrap_object_gzip_fsync_write']=measure(durable_write)
        row['bootstrap_object_bytes']=packed[-1]
        row['checkpoint_json_bytes']=len(json.dumps(envelope).encode())
        row['process_peak_rss_bytes']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss*1024
        results.append(row)
    report=dict(schema='base-research-benchmark-1',results=results,production_optimization_applied=False,
        limitations=['single process, 3 warm-library repeats','actual valid islands, not full universe throughput',
        'minimum-overlap speed does not describe normal 2-year cache input','RSS is whole benchmark process, not marginal detector memory',
        'bootstrap storage size does not estimate long-term daily storage volume'])
    Path(output).write_text(json.dumps(report,indent=2)+'\n');print(json.dumps(report,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--data',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();run(args.plan,args.data,args.output)

