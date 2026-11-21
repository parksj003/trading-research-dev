"""Independent, cache-only 120-session observation. Never writes fixed outcomes.

Returns include the configured round-trip cost in percentage points. MFE/MAE
use raw intraday highs/lows, gross of costs. Profit giveback uses peak NET CLOSE
profit (not high price); nonpositive peak => unknown, and losses can exceed 100%
of prior profits. High drawdown instead divides price by the observed high.
Only the original fixed stop is simulated; MA/RS warnings never execute an exit.
"""
from __future__ import annotations
import hashlib
import gzip
import json
import math
import os
import pickle
from pathlib import Path

import numpy as np
import pandas as pd

if __package__:
    from .session_calendar import get_calendar, calendar_policy
else:
    from session_calendar import get_calendar, calendar_policy

VERSION='trend-observation-1.0'
LIMIT=120
KEY=['signal_id','evaluation_date']
DAILY_COLS=KEY+['bars','hold_return_pct','peak_close_return_pct','mfe_pct','mae_pct','profit_giveback_pct','max_profit_giveback_pct','high_drawdown_pct','below_ma20','below_ma50','first_stop_date','first_warning_date','stop_scenario_return_pct','rs_score','rs_change_4w','sector_power','tracking_version','rs_peak_score','rs_drawdown_points','rs_weakening','rs_4w_weakening','sector_power_weakening','ma20_down','ma50_down','confirmed_below_ma20','confirmed_below_ma50','trend_state','stop_scenario_status','recorded_at','tracking_run_id']


def finite(value):
    try:
        v=float(value)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None


def truth(value):return str(value).lower() in ('true','1','1.0')


def signal_id(row):
    return hashlib.sha256(json.dumps([str(row.get(k,'')) for k in ('first_seen','market','ticker')]).encode()).hexdigest()[:24]


def price_hash(frame):
    data=frame[['Open','High','Low','Close']].copy()
    for key in ('_raw_ohlc_valid','_raw_open_valid','_session_confirmed','IsHalt'):
        data[key]=frame[key].map(truth) if key in frame else False
    return hashlib.sha256(data.to_csv(float_format='%.12g').encode()).hexdigest()


def prefix_frame(frame,asof,end):
    # MA warnings also depend on pre-entry history; revisions there must not
    # silently change historical first-warning dates.
    previous=frame.loc[frame.index<=asof].tail(51)
    return pd.concat([previous,frame.loc[(frame.index>asof)&(frame.index<=end)]])


def observe(signal, prices, now, cost_pct=.3, candidate_history=None):
    """Return latest and real-date observation rows; no price download/interpolation.

    Analysis-only confirmation is two consecutive closes below MA; it does not
    modify screening/stop rules. First breach and confirmed breach are separate.
    Entry is the next exchange session after signal as_of, original raw Open.
    Publication must be evidenced before that session opens; no entry shifting.
    """
    s=dict(signal);market=s.get('market');sid=signal_id(s)
    base={k:s.get(k) for k in ('first_seen','as_of','market','sub_market','ticker','name','sector','setup_stage','tier','regime','run_id','config_hash','engine_version','engine_commit','generated_at','published_at','workflow_run_id')}
    base['signal_fingerprint']=hashlib.sha256(json.dumps({k:str(s.get(k,'')) for k in ('first_seen','as_of','market','ticker','run_id','config_hash','engine_version','engine_commit','stop_price','signal_close')},sort_keys=True).encode()).hexdigest()
    base.update(signal_id=sid,tracking_version=VERSION,calendar_policy=calendar_policy(market or ''),reference_only=True,max_sessions=LIMIT,cost_pct=cost_pct)
    def invalid(reason,status='데이터 부족/검증 불가'):
        return {**base,'status':status,'quality_reason':reason,'bars':0,'evaluation_date':None,'hold_return_pct':None,'mfe_pct':None,'mae_pct':None,'first_warning_date':None,'trend_state':'평가 불가'},[]
    if market not in ('KR','US'):return invalid('시장 확인 불가')
    try:
        now=pd.Timestamp(now)
        if now.tzinfo is None:return invalid('평가 시각 시간대 누락')
        asof=pd.Timestamp(s['as_of']).normalize()
        cal=get_calendar(market)
        if not cal.is_session(asof):return invalid('신호 가격 기준일이 거래일 아님')
        entry=cal.next_session(asof)
        if now<cal.session_close(entry):return invalid('진입일 확정 봉 대기','진입 대기')
        published=pd.Timestamp(s.get('published_at'))
        generated=pd.Timestamp(s.get('generated_at'))
        if pd.isna(published) or published.tzinfo is None:return invalid('게시 시각 근거 없음')
        if published>cal.session_open(entry) or (pd.notna(generated) and (generated.tzinfo is None or generated>published)):
            return invalid('게시 전 진입 또는 시각 순서 오류')
    except (ValueError,TypeError,KeyError,OverflowError):return invalid('거래일/게시 시각 검증 불가')
    if prices is None or prices.empty:return invalid('확정 가격 캐시 없음')
    h=prices.copy();h.index=pd.to_datetime(h.index)
    if h.index.tz is not None:h.index=h.index.tz_localize(None)
    h=h[~h.index.duplicated(keep='last')].sort_index()
    required=['Open','High','Low','Close','_raw_ohlc_valid','_raw_open_valid','_session_confirmed']
    if not set(required).issubset(h):return invalid('원본 OHLC/확정성 출처 누락')
    h=h[h.index<=pd.Timestamp(now.tz_convert('Asia/Seoul' if market=='KR' else 'America/New_York').date())]
    h=h.loc[[d for d in h.index if cal.is_session(d) and now>=cal.session_close(d)]]
    if entry not in h.index:return invalid('실제 다음 거래일 진입 봉 미확보')
    signal_close=finite(s.get('signal_close'))
    if asof not in h.index or signal_close is None or signal_close<=0 or not math.isclose(float(h.loc[asof,'Close']),signal_close,rel_tol=1e-6):
        return invalid('신호 종가 가격 기준 불일치/분할·조정 여부 미확인')
    path=h.loc[h.index>=entry].iloc[:LIMIT]
    if path.empty:return invalid('진입 봉 대기','진입 대기')
    if 'Stock Splits' in path and pd.to_numeric(path['Stock Splits'],errors='coerce').fillna(0).ne(0).any():return invalid('관찰 경로의 주식분할 감지 — 가격/손절 기준 재검증 필요')
    expected=cal.sessions_in_range(entry,path.index[-1])
    if path.index.tolist()!=expected.tolist():return invalid('관찰 경로 거래일 누락')
    # No sanitized OHLC or synthetic highs/lows are accepted as path evidence.
    nums=path[['Open','High','Low','Close']].apply(pd.to_numeric,errors='coerce')
    valid=np.isfinite(nums).all(axis=1)&nums.gt(0).all(axis=1)
    valid &= nums.High.ge(nums[['Open','Close']].max(axis=1))&nums.Low.le(nums[['Open','Close']].min(axis=1))
    valid &= path['_raw_ohlc_valid'].map(truth)&path['_raw_open_valid'].map(truth)&path['_session_confirmed'].map(truth)
    if 'IsHalt' in path:valid &= ~path.IsHalt.map(truth)
    if not valid.all():return invalid('실제 OHLC/거래정지/일봉 확정성 검증 실패')
    ep=float(path.Open.iloc[0]);stop=finite(s.get('stop_price'))
    if stop is not None and stop<=0:stop=None
    stop_model_valid=stop is not None and stop<ep
    base.update(entry_date=entry.strftime('%Y-%m-%d'),entry_price=ep,stop_price=stop,publication_verified=True)
    # A moving average requires the preceding consecutive exchange sessions too.
    calendar_index=cal.sessions_in_range(h.index[0],h.index[-1])
    close=pd.to_numeric(h.Close,errors='coerce').where(h['_raw_ohlc_valid'].map(truth)&h['_session_confirmed'].map(truth)).reindex(calendar_index)
    ma20=close.rolling(20,min_periods=20).mean();ma50=close.rolling(50,min_periods=50).mean()
    rs={}
    if candidate_history is not None and not candidate_history.empty:
        c=candidate_history.copy()
        if {'market','ticker','as_of','date','sub_market'}.issubset(c):
            c=c[(c.market==market)&(c.ticker.astype(str)==str(s['ticker']))&(c.sub_market==s.get('sub_market'))]
            # Only actual same-date recorded observations, never carry-forward RS.
            c=c[c['as_of'].astype(str).eq(c['date'].astype(str))]
            for r in c.to_dict('records'):rs[str(r['as_of'])]=r
    out=[];peak=None;hi=ep;lo=ep;max_giveback=None;exit_date=None;exit_ret=None;exit_reason=None
    warning_date=None;warning_close=None;first20=None;first50=None;prev_below20=None;prev_below50=None;prev_rs=None;rs_peak=None
    for i,(date,row) in enumerate(path.iterrows(),1):
        name=date.strftime('%Y-%m-%d');cl=float(row.Close)
        ret=(cl/ep-1)*100-cost_pct;peak=ret if peak is None else max(peak,ret)
        hi=max(hi,float(row.High));lo=min(lo,float(row.Low))
        giveback=(peak-ret)/peak*100 if peak>0 else None
        if giveback is not None:max_giveback=giveback if max_giveback is None else max(max_giveback,giveback)
        if exit_date is None and stop is not None:
            fill=float(row.Open) if float(row.Open)<=stop else stop if float(row.Low)<=stop else None
            if fill is not None:
                exit_date=name;exit_ret=(fill/ep-1)*100-cost_pct if stop_model_valid else None
                exit_reason='갭하락 시가' if float(row.Open)<=stop else '장중 고정 손절가'
        def average(n,series):
            return finite(series.get(date)),finite(series.shift(1).get(date))
        a20,p20=average(20,ma20);a50,p50=average(50,ma50)
        below20=cl<a20 if a20 is not None else None;below50=cl<a50 if a50 is not None else None
        if below20 and first20 is None:first20=name
        if below50 and first50 is None:first50=name
        obs=rs.get(name);rs_score=finite(obs.get('rs_score')) if obs else None
        rs_4w=finite(obs.get('rs_change_4w')) if obs else None;power=finite(obs.get('sector_power')) if obs else None
        if rs_score is not None:rs_peak=rs_score if rs_peak is None else max(rs_peak,rs_score)
        rs_weak=rs_score<prev_rs['rs_score'] if prev_rs and rs_score is not None and prev_rs['rs_score'] is not None else None
        delta_weak=rs_4w<prev_rs['rs_change_4w'] if prev_rs and rs_4w is not None and prev_rs['rs_change_4w'] is not None else None
        power_weak=power<prev_rs['sector_power'] if prev_rs and power is not None and prev_rs['sector_power'] is not None else None
        warned=bool(below20 or below50 or rs_weak or delta_weak or power_weak or (a20 is not None and p20 is not None and a20<p20) or (a50 is not None and p50 is not None and a50<p50))
        if warned and warning_date is None:warning_date=name;warning_close=cl
        state='중기 훼손' if below50 else '단기 약화' if below20 else '추세 양호' if a20 is not None and a50 is not None else '평가 불가'
        r={**base,'evaluation_date':name,'bars':i,'status':'최대 관찰 기간 도달' if i==LIMIT else '추세 경고 발생' if warned else '추적 진행',
           'trend_state':state,'quality_reason':None,'hold_return_pct':ret,'peak_close_return_pct':peak,
           'mfe_pct':(hi/ep-1)*100,'mae_pct':(lo/ep-1)*100,'profit_giveback_pct':giveback,'max_profit_giveback_pct':max_giveback,
           'high_drawdown_pct':(cl/hi-1)*100,'below_ma20':below20,'below_ma50':below50,
           'ma20_down':a20<p20 if a20 is not None and p20 is not None else None,'ma50_down':a50<p50 if a50 is not None and p50 is not None else None,
           'confirmed_below_ma20':bool(below20 and prev_below20) if below20 is not None and prev_below20 is not None else None,
           'confirmed_below_ma50':bool(below50 and prev_below50) if below50 is not None and prev_below50 is not None else None,
           'first_below_ma20':first20,'first_below_ma50':first50,'first_warning_date':warning_date,
           'post_warning_return_pct':(cl/warning_close-1)*100 if warning_close else None,
           'first_stop_date':exit_date,'stop_scenario_return_pct':exit_ret,'stop_exit_reason':exit_reason,
           'stop_scenario_status':'모의 최초 청산 적용' if stop_model_valid and exit_date else '청산 미발생' if stop_model_valid else '원 손절 누락/진입 손절거리 비양수 — 청산 성과 미평가',
           'post_stop_recovered':cl>=ep if exit_date else None,'rs_score':rs_score,'rs_change_4w':rs_4w,
           'sector_power':power,'rs_peak_score':rs_peak,'rs_drawdown_points':rs_score-rs_peak if rs_score is not None else None,'rs_weakening':rs_weak,'rs_4w_weakening':delta_weak,'sector_power_weakening':power_weak,
           'ret_20d':out[19]['hold_return_pct'] if len(out)>=20 else ret if i==20 else None,
           'ret_60d':out[59]['hold_return_pct'] if len(out)>=60 else ret if i==60 else None,
           'ret_120d':ret if i==120 else None}
        out.append(r);prev_below20=below20;prev_below50=below50
        if obs:prev_rs={'rs_score':rs_score,'rs_change_4w':rs_4w,'sector_power':power}
    last=out[-1].copy();last['price_prefix_hash']=price_hash(prefix_frame(h,asof,path.index[-1]))
    reference=cal.date_to_session(pd.Timestamp(now.tz_convert('Asia/Seoul' if market=='KR' else 'America/New_York').date()),direction='previous')
    if now<cal.session_close(reference):reference=cal.previous_session(reference)
    last['lagging_sessions']=max(0,len(cal.sessions_in_range(path.index[-1],reference))-1) if len(path)<LIMIT else 0
    if last['lagging_sessions'] and len(path)<LIMIT:
        last.update(status='데이터 부족/검증 불가',quality_reason='후속 거래일 확정 가격 미확보')
    return last,out


def _write(path,raw):
    if path.exists() and path.read_bytes()==raw:return
    temp=path.with_suffix(path.suffix+'.tmp');temp.write_bytes(raw);os.replace(temp,path)


def update_tracking(base,load_prices,now,cost_pct=.3,*,run_id,generated_at):
    """Cache-only incremental I/O; terminal rows frozen. No outcomes/ledger rewrite."""
    if __package__:
        from .analysis_report import resolve_publications
    else:
        from analysis_report import resolve_publications
    base=Path(base);hist=base/'history';hist.mkdir(parents=True,exist_ok=True)
    signals=hist/'signals.csv'
    if not signals.exists():return 0
    sig=pd.read_csv(signals,dtype=str).fillna('')
    facts={};report=base/'analysis/latest.md'
    if report.exists():
        try:facts=json.loads(report.read_text().split('````json\n',1)[1].rsplit('\n````',1)[0]).get('publication_evidence',{})
        except (ValueError,IndexError):pass
    sig,_=resolve_publications(sig,facts,max_calls=0)
    latest_path=hist/'trend_tracking.csv';daily_path=hist/'trend_tracking_daily.csv.gz'
    old=pd.read_csv(latest_path,dtype={'ticker':str}) if latest_path.exists() else pd.DataFrame()
    daily=pd.read_csv(daily_path,dtype={'ticker':str}) if daily_path.exists() else pd.DataFrame()
    previous={r['signal_id']:r for r in old.to_dict('records')};rows=[];updates=[];cache={}
    fixed_path=hist/'signal_outcomes.csv'
    fixed=pd.read_csv(fixed_path,dtype={'ticker':str}) if fixed_path.exists() else pd.DataFrame()
    fixed_lookup={signal_id(r):r for r in fixed.to_dict('records')} if {'first_seen','market','ticker'}.issubset(fixed) else {}
    candidates=hist/'candidates_daily.csv';cand=pd.read_csv(candidates,dtype={'ticker':str}).fillna('') if candidates.exists() else pd.DataFrame()
    changed=0
    for signal in sig.to_dict('records'):
        market=signal.get('market');ticker=str(signal.get('ticker','')).zfill(6) if market=='KR' else str(signal.get('ticker','')).upper()
        signal['ticker']=ticker;sid=signal_id(signal);prev=previous.get(sid)
        if prev and prev.get('tracking_version')!=VERSION:
            rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':'이전 계산 버전 — 보존본과 분리한 명시적 재검증 필요'});continue
        if prev and prev.get('status')=='최대 관찰 기간 도달':rows.append(prev);continue
        if prev and market=='KR' and prev.get('calendar_policy')!=calendar_policy(market):
            rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':'이전 KR 달력 정책 — 기존 관측 보존, 별도 계보 재검증 필요'});continue
        try:
            core=hashlib.sha256(json.dumps({k:str(signal.get(k,'')) for k in ('first_seen','as_of','market','ticker','run_id','config_hash','engine_version','engine_commit','stop_price','signal_close')},sort_keys=True).encode()).hexdigest()
            if prev and pd.notna(prev.get('price_prefix_hash')) and (prev.get('signal_fingerprint')!=core or finite(prev.get('cost_pct'))!=cost_pct):
                rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':'신호 원본/비용 설정 변경 — 재검증 필요'});continue
            key=(market,ticker)
            if key not in cache:cache[key]=load_prices(market,ticker)
            prices=cache[key]
            if prev and pd.notna(prev.get('price_prefix_hash')):
                end=pd.Timestamp(prev['evaluation_date']);asof=pd.Timestamp(signal['as_of'])
                if prices.empty or asof not in prices.index or end not in prices.index:
                    rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':'기존 가격 경로 미확보'});continue
                if price_hash(prefix_frame(prices,asof,end))!=prev['price_prefix_hash']:
                    rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':'과거 가격 수정/조정 감지 — 재검증 필요'});continue
            if prev and prev.get('status') in ('추적 진행','추세 경고 발생'):
                cal=get_calendar(market)
                stamp=pd.Timestamp(now);local=pd.Timestamp(stamp.tz_convert('Asia/Seoul' if market=='KR' else 'America/New_York').date())
                reference=cal.date_to_session(local,direction='previous')
                if stamp<cal.session_close(reference):reference=cal.previous_session(reference)
                available=prices.loc[prices.index<=reference]
                if not available.empty and available.index[-1]==pd.Timestamp(prev.get('evaluation_date')):
                    lag=max(0,len(cal.sessions_in_range(available.index[-1],reference))-1)
                    rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':'후속 거래일 확정 가격 미확보','lagging_sessions':lag} if lag else prev)
                    continue
            history=cand
            if not daily.empty and 'signal_id' in daily:
                recorded=daily[daily.signal_id==sid].copy()
                if not recorded.empty:
                    recorded['date']=recorded['evaluation_date'];recorded['as_of']=recorded['evaluation_date']
                    for field in ('market','ticker','sub_market'):recorded[field]=signal.get(field)
                    history=pd.concat([cand,recorded],ignore_index=True)
            latest,path=observe(signal,prices,now,cost_pct,history)
            latest['recorded_at']=pd.Timestamp(now).isoformat();latest['tracking_run_id']=run_id
            for point in path:
                point['recorded_at']=pd.Timestamp(now).isoformat();point['tracking_run_id']=run_id
            anchor=fixed_lookup.get(sid)
            latest['fixed_20d_consistency']='미평가/기준 미확보'
            if anchor and finite(anchor.get('ret_20d')) is not None and finite(latest.get('ret_20d')) is not None:
                aligned=(str(anchor.get('entry_date'))==str(latest.get('entry_date'))
                         and finite(anchor.get('entry_price')) is not None
                         and math.isclose(float(anchor['entry_price']),latest['entry_price'],rel_tol=1e-9)
                         and math.isclose(float(anchor['ret_20d']),latest['ret_20d'],abs_tol=1e-6))
                latest['fixed_20d_consistency']='일치' if aligned else '불일치 — 재검증 필요'
                if not aligned:
                    latest.update(status='데이터 부족/검증 불가',quality_reason='기존 고정20일 평가와 진입/수익 기준 차이')
                    path=[]
            # Previously verified paths are not overwritten by partial cache failures.
            if prev and int(prev.get('bars',0))>int(latest.get('bars',0)):
                rows.append({**prev,'status':'데이터 부족/검증 불가','quality_reason':latest.get('quality_reason')});continue
            rows.append(latest);updates.extend(path);changed+=1
        except (ValueError,KeyError,TypeError,IndexError,OSError,EOFError,pickle.UnpicklingError):
            rows.append({**(prev or {k:signal.get(k) for k in ('first_seen','as_of','market','sub_market','ticker','name','sector','setup_stage','run_id','config_hash')}),'signal_id':sid,'tracking_version':VERSION,'status':'데이터 부족/검증 불가','quality_reason':'신호별 입력 검증 실패','evaluation_date':prev.get('evaluation_date') if prev else None,'bars':prev.get('bars',0) if prev else 0,'hold_return_pct':prev.get('hold_return_pct') if prev else None,'mfe_pct':prev.get('mfe_pct') if prev else None,'mae_pct':prev.get('mae_pct') if prev else None,'trend_state':'평가 불가'})
    latest=pd.DataFrame(rows)
    if updates:daily=pd.concat([daily,pd.DataFrame(updates)],ignore_index=True).drop_duplicates(KEY,keep='first')
    daily=daily.reindex(columns=DAILY_COLS)
    for path,frame in ((latest_path,latest),(daily_path,daily)):
        if frame.empty and not len(frame.columns):frame=pd.DataFrame(columns=['signal_id','evaluation_date','status'])
        raw=frame.to_csv(index=False,float_format='%.12g').encode()
        _write(path,gzip.compress(raw,mtime=0) if path==daily_path else raw)
    descriptor={'schema':'trend-tracking-1.0','tracking_version':VERSION,'run_id':run_id,'generated_at':generated_at,
                'calendar_policy':{market:calendar_policy(market) for market in ('KR','US')},
                'max_sessions':LIMIT,'confirmation_closes':2,'files':{p.name:{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in (latest_path,daily_path)}}
    _write(hist/'trend_tracking_manifest.json',json.dumps(descriptor,ensure_ascii=False,indent=2).encode())
    return changed

