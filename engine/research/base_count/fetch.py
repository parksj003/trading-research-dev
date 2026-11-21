"""Explicit public research acquisition. Never reads production caches or secrets."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import pandas as pd
import requests


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def normalize_chart(doc, symbol, market, cutoff):
    results = doc.get('chart', {}).get('result') or []
    if len(results) != 1:
        raise ValueError('exactly one chart result required')
    data = results[0]
    if data['meta']['symbol'] != symbol:
        raise ValueError('provider symbol mismatch')
    timezone_name = 'Asia/Seoul' if market == 'KR' else 'America/New_York'
    if data['meta']['exchangeTimezoneName'] != timezone_name:
        raise ValueError('provider exchange timezone mismatch')
    times = pd.to_datetime(data['timestamp'],unit='s',utc=True).tz_convert(timezone_name)
    dates = times.normalize().tz_localize(None)
    values = data['indicators']['quote'][0]
    out = pd.DataFrame({k:values[k.lower()] for k in ('Open','High','Low','Close','Volume')},index=dates,dtype=float)
    adj = pd.Series(data['indicators']['adjclose'][0]['adjclose'],index=dates,dtype=float)
    factor = adj / out.Close
    original = out.copy()
    for col in ('Open','High','Low','Close'):
        out[col] = out[col] * factor
    # Preserve bad input evidence: never synthesize missing O/H/L from Close.
    out['_raw_open_valid'] = original.Open.gt(0) & original.Open.notna()
    out['_raw_ohlc_valid'] = original[['Open','High','Low','Close']].gt(0).all(axis=1)
    out['_session_confirmed'] = dates <= pd.Timestamp(cutoff)
    out = out.loc[out.index <= pd.Timestamp(cutoff)]
    if out.index.has_duplicates or not out.index.is_monotonic_increasing:
        raise ValueError('duplicate/unordered provider dates')
    actions = []
    for kind, entries in data.get('events',{}).items():
        for item in entries.values():
            date = pd.Timestamp(item['date'],unit='s',tz='UTC').tz_convert(timezone_name).strftime('%Y-%m-%d')
            actions.append(dict(kind=kind,date=date,details=item))
    return out, actions


def acquire(plan_path, output):
    plan_raw = Path(plan_path).read_bytes();plan=json.loads(plan_raw)
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    (output/'raw').mkdir();(output/'normalized').mkdir()
    rows=[*plan['instruments'],*[dict(symbol=s,market='US' if s=='SPY' else 'KR') for s in plan['benchmarks']]]
    start=int(pd.Timestamp(plan['start'],tz='UTC').timestamp())
    end=int((pd.Timestamp(plan['cutoff'],tz='UTC')+pd.Timedelta(days=1)).timestamp())
    entries=[]
    for row in rows:
        symbol=row['symbol'];safe=symbol.replace('^','INDEX_')
        url=f'https://query1.finance.yahoo.com/v8/finance/chart/{quote(symbol,safe="")}'
        params=dict(period1=start,period2=end,interval='1d',events='div,splits',includeAdjustedClose='true')
        entry=dict(symbol=symbol,market=row['market'],url=url,params=params,
                   retrieved_at=datetime.now(timezone.utc).isoformat(),status='unavailable')
        try:
            response=requests.get(url,params=params,headers={'User-Agent':'Mozilla/5.0'},timeout=30)
            entry['http_status']=response.status_code
            response.raise_for_status()
            raw=response.content
            raw_path=output/'raw'/f'{safe}.json';raw_path.write_bytes(raw)
            frame,actions=normalize_chart(json.loads(raw),symbol,row['market'],plan['cutoff'])
            csv=frame.to_csv(index_label='date',float_format='%.17g').encode()
            csv_path=output/'normalized'/f'{safe}.csv';csv_path.write_bytes(csv)
            entry.update(status='available',raw_path=str(raw_path.relative_to(output)),raw_sha256=sha(raw),
                csv_path=str(csv_path.relative_to(output)),csv_sha256=sha(csv),bars=len(frame),
                first=str(frame.index[0].date()),last=str(frame.index[-1].date()),corporate_actions=actions,
                price_basis=plan['data_basis'],session_evidence='retrieved closed historical quotes, replay only')
        except (requests.RequestException,ValueError,KeyError,IndexError,TypeError) as exc:
            entry['error_type']=type(exc).__name__
        entries.append(entry)
        print(json.dumps({k:entry.get(k) for k in ('symbol','status','bars','first','last','error_type')},ensure_ascii=False),flush=True)
    manifest=dict(schema='base-research-data-1',plan_sha256=sha(plan_raw),source='Yahoo Finance public chart',
        data_basis=plan['data_basis'],point_in_time=False,production_cache_modified=False,
        normalization='multiply quote OHLC by adjusted-close / quote-close; preserve Volume; corporate actions archived separately; fresh fixed research lineage',entries=entries)
    (output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();acquire(args.plan,args.output)

