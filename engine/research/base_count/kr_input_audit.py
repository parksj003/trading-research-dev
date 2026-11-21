"""Research-only Korean vendor comparison. Never repairs/attests operating data."""
import argparse
import ast
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import re

import exchange_calendars as xc
import numpy as np
import pandas as pd
import requests


def sha(raw):return hashlib.sha256(raw).hexdigest()


def parse_naver(raw,start,end):
    if len(raw)>10_000_000:raise ValueError('response too large')
    text=raw.decode('utf-8-sig').strip()
    try:rows=json.loads(text)
    except json.JSONDecodeError:rows=ast.literal_eval(text)
    required=['날짜','시가','고가','저가','종가','거래량']
    if not isinstance(rows,list) or not rows or not isinstance(rows[0],list):raise ValueError('invalid table')
    if not all(x in rows[0] for x in required):raise ValueError('price headers missing')
    positions=[rows[0].index(x) for x in required];data=[]
    for row in rows[1:]:
        if not isinstance(row,list) or len(row)<=max(positions):raise ValueError('invalid row')
        values=[row[i] for i in positions]
        if not re.fullmatch(r'\d{8}',str(values[0])):raise ValueError('date invalid')
        data.append(values)
    frame=pd.DataFrame(data,columns=['date','Open','High','Low','Close','Volume'])
    frame['date']=pd.to_datetime(frame.date,format='%Y%m%d',errors='raise');frame=frame.set_index('date')
    for col in frame:frame[col]=pd.to_numeric(frame[col],errors='coerce')
    if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:raise ValueError('duplicate/unordered source')
    return frame.loc[start:end].copy()


def yahoo_quotes(raw):
    result=json.loads(raw)['chart']['result'][0]
    dates=pd.to_datetime(result['timestamp'],unit='s',utc=True).tz_convert('Asia/Seoul').normalize().tz_localize(None)
    values=result['indicators']['quote'][0]
    frame=pd.DataFrame({x:values[x.lower()] for x in ['Open','High','Low','Close','Volume']},index=dates,dtype=float)
    adj=pd.Series(result['indicators']['adjclose'][0]['adjclose'],index=dates,dtype=float)
    return frame,adj,result.get('events',{})


def validity(frame):
    f=frame[['Open','High','Low','Close','Volume']]
    finite=pd.Series(np.isfinite(f[['Open','High','Low','Close']].to_numpy(dtype=float)).all(axis=1),index=f.index)
    ohlc=finite & f[['Open','High','Low','Close']].gt(0).all(axis=1)
    ohlc &= f.High.ge(f[['Open','Low','Close']].max(axis=1)) & f.Low.le(f[['Open','High','Close']].min(axis=1))
    return ohlc,ohlc & f.Volume.gt(0) & np.isfinite(f.Volume)


def compare_frames(yahoo,naver,adjclose):
    start=max(yahoo.index.min(),naver.index.min());end=min(yahoo.index.max(),naver.index.max())
    y=yahoo.loc[start:end];n=naver.loc[start:end]
    cal=xc.get_calendar('XKRX');sessions=cal.sessions_in_range(start,end)
    common=y.index.intersection(n.index);yo,yt=validity(y);no,nt=validity(n)
    exceptions=[]
    for date in sessions.union(y.index).union(n.index):
        name=date.strftime('%Y-%m-%d');in_y=date in y.index;in_n=date in n.index;expected=date in sessions
        reasons=[]
        if expected and not in_y and in_n and nt.loc[date]:reasons.append('YAHOO_MISSING_NAVER_POSITIVE_TRADE')
        if expected and not in_n and in_y and yt.loc[date]:reasons.append('NAVER_MISSING_YAHOO_POSITIVE_TRADE')
        if expected and not in_y and not in_n:reasons.append('BOTH_MISSING_EXPECTED_CALENDAR_SESSION')
        for prefix,frame,present,valid in [('YAHOO',y,in_y,yo),('NAVER',n,in_n,no)]:
            if present and not valid.loc[date]:
                prices=frame.loc[date,['Open','High','Low','Close']].to_numpy(dtype=float)
                reasons.append(prefix+('_INTERNAL_OHLC_INVALID' if np.isfinite(prices).all() and (prices>0).all()
                                       else '_MISSING_OR_NONPOSITIVE_OHLC'))
        if in_y and in_n and y.loc[date,'Volume']==0 and n.loc[date,'Volume']==0:reasons.append('BOTH_ZERO_VOLUME_HALT_OR_PADDING_UNVERIFIED')
        if in_y and y.loc[date,'Volume']==0 and in_n and nt.loc[date]:reasons.append('YAHOO_ZERO_NAVER_POSITIVE_TRADE')
        if not expected and ((in_y and yt.loc[date]) or (in_n and nt.loc[date])):reasons.append('POSITIVE_TRADE_OUTSIDE_CALENDAR_VERIFY_OFFICIAL')
        if not expected and in_y and y.loc[date,'Volume']==0 and not in_n:reasons.append('YAHOO_ZERO_PADDING_ON_CALENDAR_HOLIDAY')
        if reasons:
            exceptions.append(dict(date=name,reasons='|'.join(reasons),calendar_expected=expected,
                yahoo_present=in_y,naver_present=in_n,yahoo_volume=float(y.loc[date,'Volume']) if in_y else None,
                naver_volume=float(n.loc[date,'Volume']) if in_n else None))
    usable=common[yt.reindex(common).to_numpy() & nt.reindex(common).to_numpy()]
    differences=[]
    for field in ['Open','High','Low','Close','Volume']:
        delta=(y.loc[usable,field]-n.loc[usable,field]).abs()
        differences.append(dict(field=field,n=len(usable),exact_with_float_tolerance=int(delta.le(1e-6).sum()),
            within_one_unit=int(delta.le(1).sum()),median_absolute_difference=float(delta.median()) if len(delta) else None,
            maximum_absolute_difference=float(delta.max()) if len(delta) else None))
    ratio=n.loc[usable,'Close']/y.loc[usable,'Close']
    adjusted_ratio=n.loc[usable,'Close']/adjclose.reindex(usable)
    report=dict(comparison_start=str(start.date()),comparison_end=str(end.date()),common_dates=len(common),
        jointly_positive_valid_dates=len(usable),yahoo_rows=len(yahoo),naver_rows=len(naver),
        yahoo_first=str(yahoo.index.min().date()),naver_first=str(naver.index.min().date()),
        raw_quote_close_ratio_median=float(ratio.median()) if len(ratio) else None,
        dividend_adjusted_close_ratio_median=float(adjusted_ratio.median()) if len(adjusted_ratio) else None,
        field_comparisons=differences,basis_certified=False,holiday_cause_confirmed=False,
        label_note='Different vendor responses do not establish independent upstream provenance or official market truth')
    return report,exceptions


def acquire_and_compare(plan_path,yahoo_dir,output):
    plan_raw=Path(plan_path).read_bytes();plan=json.loads(plan_raw);yahoo_dir=Path(yahoo_dir)
    manifest=json.loads((yahoo_dir/'manifest.json').read_bytes())
    if manifest['plan_sha256']!=sha(plan_raw):raise ValueError('parent plan mismatch')
    entries={x['symbol']:x for x in manifest['entries']};output=Path(output);output.mkdir(parents=True,exist_ok=False)
    (output/'raw').mkdir();(output/'comparisons').mkdir();reports=[];evidence=[];all_exceptions=[]
    for instrument in [x for x in plan['instruments'] if x['market']=='KR']:
        ticker=instrument['ticker'];symbol=instrument['symbol'];entry=entries[symbol]
        raw=(yahoo_dir/entry['raw_path']).read_bytes()
        if sha(raw)!=entry['raw_sha256']:raise ValueError('Yahoo source changed')
        url='https://api.finance.naver.com/siseJson.naver'
        params=dict(symbol=ticker,requestType='1',startTime=plan['start'].replace('-',''),endTime=plan['cutoff'].replace('-',''),timeframe='day')
        source=dict(ticker=ticker,url=url,params=params,retrieved_at=datetime.now(timezone.utc).isoformat(),status='unavailable')
        try:
            response=requests.get(url,params=params,timeout=30);source['http_status']=response.status_code
            response.raise_for_status();body=response.content
            (output/'raw'/f'{ticker}.txt').write_bytes(body);source['sha256']=sha(body)
            naver=parse_naver(body,plan['start'],plan['cutoff']);yahoo,adj,actions=yahoo_quotes(raw)
            yahoo=yahoo.loc[plan['start']:plan['cutoff']]
            report,exceptions=compare_frames(yahoo,naver,adj)
            report.update(ticker=ticker,symbol=symbol,corporate_actions=actions,yahoo_source_sha256=sha(raw),naver_source_sha256=sha(body))
            pd.DataFrame(exceptions).to_csv(output/'comparisons'/f'{ticker}-exceptions.csv',index=False)
            reports.append(report);all_exceptions.extend(dict(x,ticker=ticker) for x in exceptions)
            source.update(status='available',rows=len(naver))
        except (requests.RequestException,ValueError,KeyError,IndexError,TypeError,SyntaxError) as exc:
            source['error_type']=type(exc).__name__
        evidence.append(source);print(json.dumps({k:source.get(k) for k in ['ticker','status','rows','error_type']}),flush=True)
    pd.DataFrame(all_exceptions).to_csv(output/'all-exceptions.csv',index=False)
    result=dict(schema='base-kr-vendor-audit-1',plan_sha256=sha(plan_raw),sources=evidence,comparisons=reports,
        data_repaired=False,production_cache_modified=False,official_market_calendar_verified=False,
        conclusion='Vendor comparison only; no automatic substitution, basis attestation, or calendar override')
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


def reanalyze_snapshot(plan_path,yahoo_dir,snapshot,output):
    """Reclassify preserved evidence in a NEW folder; no provider calls or repair."""
    plan_raw=Path(plan_path).read_bytes();plan=json.loads(plan_raw);snapshot=Path(snapshot);yahoo_dir=Path(yahoo_dir)
    prior_raw=(snapshot/'summary.json').read_bytes();prior=json.loads(prior_raw)
    if prior['plan_sha256']!=sha(plan_raw):raise ValueError('source plan mismatch')
    yahoo_index={e['symbol']:e for e in json.loads((yahoo_dir/'manifest.json').read_bytes())['entries']}
    sources={e['ticker']:e for e in prior['sources']};output=Path(output);output.mkdir(parents=True,exist_ok=False)
    (output/'comparisons').mkdir();reports=[];all_exceptions=[]
    for instrument in [i for i in plan['instruments'] if i['market']=='KR']:
        ticker=instrument['ticker'];entry=sources[ticker]
        if entry['status']!='available':continue
        body=(snapshot/'raw'/f'{ticker}.txt').read_bytes()
        ymeta=yahoo_index[instrument['symbol']];raw=(yahoo_dir/ymeta['raw_path']).read_bytes()
        if sha(body)!=entry['sha256'] or sha(raw)!=ymeta['raw_sha256']:raise ValueError('vendor snapshot changed')
        naver=parse_naver(body,plan['start'],plan['cutoff']);yahoo,adj,actions=yahoo_quotes(raw)
        report,exceptions=compare_frames(yahoo.loc[plan['start']:plan['cutoff']],naver,adj)
        report.update(ticker=ticker,symbol=instrument['symbol'],corporate_actions=actions,yahoo_source_sha256=sha(raw),naver_source_sha256=sha(body))
        reports.append(report);all_exceptions.extend(dict(x,ticker=ticker) for x in exceptions)
        pd.DataFrame(exceptions).to_csv(output/'comparisons'/f'{ticker}-exceptions.csv',index=False)
    pd.DataFrame(all_exceptions).to_csv(output/'all-exceptions.csv',index=False)
    result=dict(schema='base-kr-vendor-audit-1',plan_sha256=sha(plan_raw),sources=prior['sources'],comparisons=reports,
        parent_summary_sha256=sha(prior_raw),classification_revision='separate zero/missing prices from positive OHLC ordering violations',
        data_repaired=False,production_cache_modified=False,official_market_calendar_verified=False)
    (output/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--yahoo',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();acquire_and_compare(args.plan,args.yahoo,args.output)

