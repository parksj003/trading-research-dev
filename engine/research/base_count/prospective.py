"""Local immutable research observation, not a publication or trading signal."""
import argparse
from datetime import datetime,timezone
import gzip
import hashlib
import json
from pathlib import Path

import pandas as pd

from .study import calendar,load_dataset
from watchlist.base_detector import CSAT


def earliest_entry_session(market,observed_at):
    stamp=pd.Timestamp(observed_at)
    if pd.isna(stamp) or stamp.tzinfo is None:raise ValueError('aware observation time required')
    cal=calendar(market);future=cal.schedule.loc[stamp.tz_convert('UTC').tz_localize(None).normalize():]
    future=future[future['open']>stamp]
    if future.empty:raise ValueError('future exchange session coverage unavailable')
    date=future.index[0];opening=future.iloc[0]['open']
    if market=='KR' and (date.year>2026 or date.strftime('%Y-%m-%d') in CSAT):
        raise ValueError('KR special-session opening requires separate review')
    return date.strftime('%Y-%m-%d'),opening.isoformat()


def record(plan_path,data_dir,run_dir,output):
    plan,data,metadata,manifest=load_dataset(plan_path,data_dir)
    now=datetime.now(timezone.utc).isoformat();run_dir=Path(run_dir)
    hashes=json.loads((run_dir/'manifest.json').read_bytes())['files']
    rows=[]
    for instrument in plan['instruments']:
        symbol=instrument['symbol'];name=instrument['market']+'_'+instrument['ticker']
        path=run_dir/'observations'/f'{name}.jsonl.gz'
        if hashlib.sha256(path.read_bytes()).hexdigest()!=hashes[str(path.relative_to(run_dir))]:raise ValueError('observation changed')
        with gzip.open(path,'rt') as stream:last=list(map(json.loads,stream))[-1]
        if pd.Timestamp(metadata[symbol]['retrieved_at'])>pd.Timestamp(now):raise ValueError('future data vintage')
        date,opening=earliest_entry_session(instrument['market'],now)
        rows.append(dict(market=instrument['market'],ticker=instrument['ticker'],symbol=symbol,
            price_as_of=last['as_of'],recorded_at=now,historical_calculation_mode=last['mode'],
            ordinal_observed=last['current_base_ordinal'],completed_observed=last['completed_base_count'],
            state_observed=last['status'],quality_observed=last['quality'],
            source_sha256=metadata[symbol]['raw_sha256'],snapshot=last,
            earliest_counterfactual_entry_date=date,earliest_counterfactual_entry_open_utc=opening,
            actual_trade_authorized=False,future_outcome=None))
    payload=dict(schema='base-research-prospective-observation-1',recorded_at=now,
        plan_sha256=manifest['plan_sha256'],profile='reference',rows=rows,
        publicly_attested_publication=False,production_publication=False,
        retrospective_results_not_counted_as_forward_results=True,
        note='First local research observation only. Next hypothetical entry is strictly after actual observation time, never an already-passed session open.')
    raw=(json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+'\n').encode()
    with Path(output).open('xb') as stream:stream.write(raw)
    print(json.dumps(dict(rows=len(rows),recorded_at=now,sha256=hashlib.sha256(raw).hexdigest(),future_outcomes_available=False),indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--data',required=True)
    parser.add_argument('--run',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();record(args.plan,args.data,args.run,args.output)

