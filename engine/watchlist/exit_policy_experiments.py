"""Independent daily-OHLC exit research. No orders, price downloads or legacy writes.

Entry day counts as session 1. Fixed horizon results never consume a later fill.
Close-triggered exits fill at the next actual session open. Intraday extrema are
bounds, not invented pre-fill paths. Costs follow the official percentage-point
round-trip contract; all R use the original entry-minus-stop price distance.
"""
from __future__ import annotations
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import json
import math

import numpy as np
import pandas as pd
from .session_calendar import get_calendar,calendar_policy
from .trend_tracking import signal_id
from .exit_policy_atr import wilder_atr,next_atr_stop
from .exit_policy_donchian import donchian10,sell_stop

RULE_VERSION='exit-research-v2'
HORIZONS=(20,60,120)
RULES=('HOLD','FIXED','TARGET_2R','MA20_2C','MA50_2C','ATR_TRAIL','DONCHIAN_10')
get_calendar=lru_cache(maxsize=2)(get_calendar)
COST_VERSION='legacy-round-trip-percentage-points-v1'
CSAT={'2021-11-18','2022-11-17','2023-11-16','2024-11-14','2025-11-13','2026-11-19'}


def finite(x):
    try:
        v=float(x)
        return v if math.isfinite(v) else None
    except (ValueError,TypeError):return None


def truth(x):return str(x).lower() in ('true','1','1.0')
def canonical(x):return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def digest(x):return sha256(x).hexdigest()
def stamp(x):
    s=pd.Timestamp(x)
    if pd.isna(s) or s.tzinfo is None:raise ValueError('AWARE_TIME_REQUIRED')
    return s.tz_convert('UTC')


def session_time(market,date,kind):
    date=pd.Timestamp(date).normalize();cal=get_calendar(market)
    if market=='KR' and date.year>2026:raise ValueError('CALENDAR_REVIEW_EXPIRED')
    if market=='KR' and str(date.date()) in CSAT:
        return pd.Timestamp(str(date.date())+(' 10:00' if kind=='open' else ' 16:30'),tz='Asia/Seoul').tz_convert('UTC')
    return pd.Timestamp(cal.session_open(date) if kind=='open' else cal.session_close(date)).tz_convert('UTC')


def entry_session(signal):
    """First executable open after BOTH the price as-of and verified publication."""
    market=signal.get('market')
    if market not in ('KR','US'):raise ValueError('UNSUPPORTED_MARKET')
    if signal.get('publication_verified') is not True:raise ValueError('PUBLICATION_UNVERIFIED')
    published=stamp(signal.get('published_at'));generated=stamp(signal.get('generated_at'))
    if generated>published:raise ValueError('PUBLICATION_CHRONOLOGY')
    asof=pd.Timestamp(signal['as_of']).normalize();cal=get_calendar(market)
    if asof.tzinfo is not None or not cal.is_session(asof):raise ValueError('ASOF_SESSION_INVALID')
    if session_time(market,asof,'close')>generated:raise ValueError('SIGNAL_PRICE_NOT_CLOSED_AT_GENERATION')
    candidate=cal.next_session(asof)
    # A stale price as-of is not the publication date. Never use an open that
    # already occurred, nor shift entry to the next available price record.
    local=published.tz_convert('Asia/Seoul' if market=='KR' else 'America/New_York').tz_localize(None).normalize()
    if local>candidate:candidate=cal.date_to_session(local,direction='next')
    # Publication at the opening timestamp cannot establish an executable
    # order before that auction. Equality must advance as well.
    if session_time(market,candidate,'open')<=published:candidate=cal.next_session(candidate)
    return candidate


def price_frame(frame):
    h=frame.copy();h.index=pd.DatetimeIndex(pd.to_datetime(h.index))
    if h.index.tz is not None:h.index=h.index.tz_localize(None)
    h.index=h.index.normalize()
    if h.index.has_duplicates:raise ValueError('DUPLICATE_PRICE_DATE')
    return h.sort_index()


def row_valid(r):
    vals=[finite(r.get(k)) for k in ('Open','High','Low','Close')]
    if any(x is None or x<=0 for x in vals):return False
    o,h,l,c=vals
    return (h>=max(o,c) and l<=min(o,c) and h>=l and all(truth(r.get(k)) for k in ('_raw_ohlc_valid','_raw_open_valid','_session_confirmed'))
        and not truth(r.get('IsHalt')) and (finite(r.get('Stock Splits',0)) or 0)==0)


def frame_payload(frame):
    fields=('Open','High','Low','Close','Volume','_raw_ohlc_valid','_raw_open_valid','_session_confirmed','_base_price_basis','IsHalt','Stock Splits')
    rows=[]
    for date,r in frame.iterrows():
        item={'date':date.date().isoformat()}
        for key in fields:
            value=r.get(key)
            if key.startswith('_') and key!='_base_price_basis' or key=='IsHalt':item[key]=truth(value)
            elif key=='_base_price_basis':item[key]=str(value) if pd.notna(value) else None
            else:item[key]=finite(value)
        rows.append(item)
    return rows


def frame_hash(frame):return digest(canonical(frame_payload(frame)))


@dataclass
class Prepared:
    signal:dict
    frame:pd.DataFrame
    path:pd.DataFrame
    entry:pd.Timestamp
    cutoff:pd.Timestamp
    indicators:dict
    validity:pd.Series
    meta:dict


@dataclass
class PriceContext:
    market:str
    cutoff:pd.Timestamp
    frame:pd.DataFrame
    validity:pd.Series
    indicators:dict


def price_context(prices,market,cutoff):
    cutoff=stamp(cutoff);cal=get_calendar(market)
    if prices is None or prices.empty:raise ValueError('PRICE_CACHE_MISSING')
    h=price_frame(prices)
    if any(not cal.is_session(d) for d in h.index):raise ValueError('NONSESSION_PRICE')
    local_day=cutoff.tz_convert('Asia/Seoul' if market=='KR' else 'America/New_York').date()
    h=h.loc[[d.date()<=local_day and session_time(market,d,'close')<=cutoff for d in h.index]]
    if h.empty:raise ValueError('PRICE_CACHE_NO_CLOSED_SESSION')
    required={'Open','High','Low','Close','_raw_ohlc_valid','_raw_open_valid','_session_confirmed','_base_price_basis'}
    if not required.issubset(h.columns):raise ValueError('RAW_PRICE_PROVENANCE_MISSING')
    h=h.reindex(cal.sessions_in_range(h.index[0],h.index[-1]))
    valid=h.apply(row_valid,axis=1)&h['_base_price_basis'].isin(('pykrx:date:adjusted=True','yfinance:auto_adjust=True'))
    # Do not smooth across a price-basis transition.
    transition=h['_base_price_basis'].ne(h['_base_price_basis'].shift())
    if len(transition):transition.iloc[0]=False
    valid &= ~transition
    close=pd.to_numeric(h.Close,errors='coerce').where(valid)
    indicators={'ma20':close.rolling(20,min_periods=20).mean(),'ma50':close.rolling(50,min_periods=50).mean(),
        'atr14':wilder_atr(h,valid),'donchian10':donchian10(h,valid)}
    return PriceContext(market,cutoff,h,valid,indicators)


def prepare(signal,prices,cutoff,cost_pct=.3,context=None):
    s=dict(signal);cutoff=stamp(cutoff);entry=entry_session(s);market=s['market'];cal=get_calendar(market)
    if any(not s.get(k) or str(s.get(k)).upper()=='UNKNOWN' for k in ('run_id','config_hash','first_seen','ticker')):raise ValueError('SIGNAL_IDENTITY_UNVERIFIED')
    if stamp(s['published_at'])>cutoff:raise ValueError('PUBLICATION_AFTER_CUTOFF')
    if finite(cost_pct) is None or cost_pct<0:raise ValueError('INVALID_COST')
    context=context or price_context(prices,market,cutoff)
    if context.market!=market or context.cutoff!=cutoff:raise ValueError('PRICE_CONTEXT_IDENTITY')
    end=cal.session_offset(entry,119);h=context.frame.loc[context.frame.index<=end]
    asof=pd.Timestamp(s['as_of']);recorded=finite(s.get('signal_close'))
    if asof not in h.index or recorded is None or recorded<=0 or not math.isclose(float(h.loc[asof,'Close']),recorded,rel_tol=1e-6):raise ValueError('SIGNAL_PRICE_BASIS_MISMATCH')
    if not context.validity.get(asof,False):raise ValueError('SIGNAL_ANCHOR_INVALID')
    if entry not in h.index:
        raise ValueError('ENTRY_PENDING' if cutoff<session_time(market,entry,'close') else 'ENTRY_OPEN_MISSING')
    if finite(h.loc[entry,'Open']) is None or float(h.loc[entry,'Open'])<=0:raise ValueError('ENTRY_OPEN_MISSING')
    if not row_valid(h.loc[entry]):raise ValueError('ENTRY_OHLC_INVALID')
    basis=h.loc[entry,'_base_price_basis']
    if basis not in ('pykrx:date:adjusted=True','yfinance:auto_adjust=True'):raise ValueError('PRICE_BASIS_UNVERIFIED')
    if h.loc[asof,'_base_price_basis']!=basis:raise ValueError('SIGNAL_PRICE_BASIS_MISMATCH')
    valid=context.validity.reindex(h.index)&h['_base_price_basis'].eq(basis)
    indicators={k:v.reindex(h.index) for k,v in context.indicators.items()}
    path=h.loc[h.index>=entry].iloc[:120]
    ep=float(path.iloc[0].Open);stop=finite(s.get('stop_price'));risk=ep-stop if stop is not None else None
    r_ok=risk is not None and risk>0 and stop>0
    fingerprint=digest(canonical({k:str(s.get(k,'')) for k in ('first_seen','as_of','market','ticker','run_id','config_hash','engine_version','engine_commit','stop_price','signal_close','published_at','generated_at')}))
    meta={'signal_id':signal_id(s),'source_run_id':s.get('run_id'),'source_config_hash':s.get('config_hash'),
        'source_signal_fingerprint':fingerprint,'market':market,'ticker':s.get('ticker'),'name':s.get('name'),
        'sector':s.get('sector'),'regime':s.get('regime'),'first_seen':s.get('first_seen'),'as_of':s.get('as_of'),
        'source_published_at':stamp(s['published_at']).isoformat(),'entry_date':entry.date().isoformat(),'entry_price':ep,
        'initial_stop':stop,'initial_risk':risk if r_ok else None,'r_eligible':r_ok,'return_eligible':True,
        'round_trip_cost_pct':float(cost_pct),'total_cost':ep*cost_pct/100,'cost_model_version':COST_VERSION,
        'price_basis':basis,'calendar_policy':calendar_policy(market),'data_as_of':path.index[-1].date().isoformat(),
        'price_path_hash':frame_hash(h),'prefix_first_date':h.index[0].date().isoformat(),
        'entry_session_index':int(((cal.sessions>=pd.Timestamp('2020-01-01'))&(cal.sessions<entry)).sum()),'entry_index_anchor':'2020-01-01'}
    return Prepared(s,h,path,entry,cutoff,indicators,valid,meta)


def _eligible(p,rule):
    if rule!='HOLD' and not p.meta['r_eligible']:return 'INITIAL_RISK_INVALID'
    if rule in ('MA20_2C','MA50_2C'):
        name='ma20' if rule=='MA20_2C' else 'ma50'
        if finite(p.indicators[name].get(p.entry)) is None:return name.upper()+'_WARMUP_INSUFFICIENT'
    if rule=='ATR_TRAIL':
        previous=p.frame.index[p.frame.index.get_loc(p.entry)-1] if p.frame.index.get_loc(p.entry)>0 else None
        if previous is None or finite(p.indicators['atr14'].get(previous)) is None:return 'ATR_WARMUP_INSUFFICIENT'
    if rule=='DONCHIAN_10':
        channel=finite(p.indicators['donchian10'].get(p.entry))
        if channel is None:return 'DONCHIAN_WARMUP_INSUFFICIENT'
        if channel>=p.meta['entry_price']:return 'DONCHIAN_ENTRY_ALREADY_BREACHED'
    return None


def simulate(p,rule,horizon):
    if rule not in RULES or horizon not in HORIZONS:raise ValueError('UNKNOWN_POLICY_OR_HORIZON')
    path=p.path.iloc[:horizon];meta=p.meta
    expected_end=get_calendar(meta['market']).session_offset(p.entry,horizon-1)
    local_day=p.cutoff.tz_convert('Asia/Seoul' if meta['market']=='KR' else 'America/New_York').date()
    maturity_reached=local_day>=expected_end.date() and p.cutoff>=session_time(meta['market'],expected_end,'close')
    horizon_complete=len(path)==horizon and bool(p.validity.reindex(path.index).fillna(False).all())
    base={**meta,'rule_id':rule,'rule_version':RULE_VERSION,'horizon':horizon,'horizon_end':expected_end.date().isoformat(),
        'maturity_reached':bool(maturity_reached),'horizon_complete':bool(horizon_complete),'eligibility_status':'eligible',
        'completion_status':'open','exclusion_reason':None,'exit_signal_date':None,'exit_fill_date':None,'exit_price':None,
        'exit_reason':None,'gross_return_pct':None,'net_return_pct':None,'gross_R':None,'net_R':None,'holding_sessions':None,
        'comparison_exclusion_reason':None if horizon_complete and maturity_reached else 'MATURITY_PATH_INCOMPLETE' if maturity_reached else 'HORIZON_NOT_MATURE',
        'intrabar_ambiguous':False,'ambiguity_count':0,'ambiguity_alternative_exit_price':None,'stop_path':[]}
    reason=_eligible(p,rule)
    if reason:return {**base,'eligibility_status':'excluded','completion_status':'excluded','exclusion_reason':reason}
    entry=meta['entry_price'];initial=meta['initial_stop'];target=entry+2*meta['initial_risk'] if meta['r_eligible'] else None
    pending=None;count=0;exit_data=None;active=initial;highest_high=None;closes=[];complete_highs=[];complete_lows=[]
    for i,(date,row) in enumerate(path.iterrows(),1):
        if not p.validity.get(date,False):
            return {**base,'eligibility_status':'excluded','completion_status':'excluded','exclusion_reason':'HOLDING_PATH_INVALID'}
        day=date.date().isoformat();o,h,l,c=map(float,(row.Open,row.High,row.Low,row.Close))
        if pending:
            exit_data=(i,date,o,'next_open_ma',pending,'open');break
        stop=initial if rule!='HOLD' else None;stop_reason='initial_stop'
        if rule=='ATR_TRAIL':
            stop=active;stop_reason='atr_stop' if active>initial else 'initial_stop'
        if rule=='DONCHIAN_10':
            channel=float(p.indicators['donchian10'].loc[date]);stop,stop_reason=sell_stop(initial,channel)
        if stop is not None and rule in ('ATR_TRAIL','DONCHIAN_10'):base['stop_path'].append({'effective_date':day,'information_as_of':p.frame.index[p.frame.index.get_loc(date)-1].date().isoformat() if p.frame.index.get_loc(date)>0 else None,'active_stop':stop})
        if stop is not None and o<=stop:
            base['triggered_stops']=[name for value,name in sorted([(initial,'initial_stop')]+([(channel,'donchian_stop')] if rule=='DONCHIAN_10' else []),key=lambda x:(-x[0],x[1])) if o<=value]
            exit_data=(i,date,o,'gap_'+stop_reason,None,'open');break
        if rule=='TARGET_2R' and o>=target:
            exit_data=(i,date,o,'gap_target_2r',None,'open');break
        stop_hit=stop is not None and l<=stop;target_hit=rule=='TARGET_2R' and h>=target
        if stop_hit:
            if target_hit:base.update(intrabar_ambiguous=True,ambiguity_count=1,ambiguity_alternative_exit_price=target)
            exit_data=(i,date,stop,'touch_'+stop_reason,None,'intraday');break
        if target_hit:
            exit_data=(i,date,target,'touch_target_2r',None,'intraday');break
        complete_highs.append(h);complete_lows.append(l);closes.append(c)
        if i==horizon:
            exit_data=(i,date,c,'horizon_close',None,'close');break
        if rule in ('MA20_2C','MA50_2C'):
            series=p.indicators['ma20' if rule=='MA20_2C' else 'ma50'];average=finite(series.get(date))
            if average is None:return {**base,'eligibility_status':'excluded','completion_status':'excluded','exclusion_reason':'MA_HISTORY_INVALID'}
            count=count+1 if c<average else 0
            if count==2:pending=day
        if rule=='ATR_TRAIL':
            highest_high=h if highest_high is None else max(highest_high,h)
            active=next_atr_stop(initial,active,highest_high,float(p.indicators['atr14'].loc[date]))
    if exit_data:
        i,date,fill,why,signal_date,kind=exit_data;row=path.loc[date]
        gross=(fill/entry-1)*100;net=gross-meta['round_trip_cost_pct']
        base.update(exit_signal_date=signal_date,exit_fill_date=date.date().isoformat(),exit_price=fill,exit_reason=why,
            gross_return_pct=gross,net_return_pct=net,gross_R=(fill-entry)/meta['initial_risk'] if meta['r_eligible'] else None,
            net_R=(fill-entry-meta['total_cost'])/meta['initial_risk'] if meta['r_eligible'] else None,
            holding_sessions=i,completion_status='complete' if horizon_complete else 'closed_waiting_maturity',fill_timing=kind)
        # Completed closes exclude a gap/touch exit day's close. For close exits
        # that day's full OHLC was appended above before the maturity fill.
        known_high=max([entry,fill]+complete_highs+([float(row.Open)] if kind=='intraday' else []))
        known_low=min([entry,fill]+complete_lows+([float(row.Open)] if kind=='intraday' else []))
        upper_high=max(known_high,float(row.High)) if kind=='intraday' else known_high
        lower_low=min(known_low,float(row.Low)) if kind=='intraday' else known_low
        base.update(mfe_lower_pct=(known_high/entry-1)*100,mfe_upper_pct=(upper_high/entry-1)*100,
            mae_lower_pct=(lower_low/entry-1)*100,mae_upper_pct=(known_low/entry-1)*100,
            extrema_status='intraday_order_unknown' if kind=='intraday' else 'exact',
            mfe_pct=None if kind=='intraday' else (known_high/entry-1)*100,mae_pct=None if kind=='intraday' else (known_low/entry-1)*100)
        peak=(max(closes)/entry-1)*100-meta['round_trip_cost_pct'] if closes else None
        diff=peak-net if peak is not None else None
        base.update(peak_held_close_net_pct=peak,profit_giveback_pp=diff,
            profit_giveback_amount=entry*diff/100 if diff is not None else None,
            profit_giveback_pct=diff/peak*100 if peak is not None and peak>0 else None,
            giveback_status='no_held_close' if peak is None else 'nonpositive_close_peak' if peak<=0 else 'realized_above_close_peak' if diff<0 else 'positive_giveback')
        if base['intrabar_ambiguous']:
            base['ambiguity_net_return_range_pct']=[net,(target/entry-1)*100-meta['round_trip_cost_pct']]
    else:
        base.update(completion_status='open',holding_sessions=len(path),pending_exit_signal_date=pending,
            mark_to_market_net_pct=(float(path.iloc[-1].Close)/entry-1)*100-meta['round_trip_cost_pct'])
    if horizon_complete:
        hindsight=(float(path.High.max())/entry-1)*100
        base.update(hindsight_max_favorable_pct=hindsight,expost_large_move=hindsight>=50,
            hindsight_capture_pct=base['gross_return_pct']/hindsight*100 if base['gross_return_pct'] is not None and hindsight>0 else None,
            hindsight_only=True)
    base['comparison_eligible']=bool(horizon_complete and maturity_reached and base['completion_status']=='complete' and meta['r_eligible'])
    return base


def run_signal(signal,prices,cutoff,cost_pct=.3,context=None):
    try:
        p=prepare(signal,prices,cutoff,cost_pct,context)
        return [simulate(p,rule,n) for n in HORIZONS for rule in RULES],p
    except (ValueError,TypeError,KeyError,IndexError,OverflowError) as exc:
        reason=str(exc) if isinstance(exc,ValueError) else 'SIGNAL_OR_PRICE_STRUCTURE_INVALID'
        return [dict(signal_id=signal_id(signal),source_run_id=signal.get('run_id'),source_config_hash=signal.get('config_hash'),
            market=signal.get('market'),ticker=signal.get('ticker'),name=signal.get('name'),sector=signal.get('sector'),regime=signal.get('regime'),
            rule_id=rule,rule_version=RULE_VERSION,horizon=n,eligibility_status='pending' if reason=='ENTRY_PENDING' else 'excluded',completion_status='entry_pending' if reason=='ENTRY_PENDING' else 'excluded',
            exclusion_reason=reason,comparison_eligible=False,return_eligible=False,r_eligible=False,intrabar_ambiguous=False)
            for n in HORIZONS for rule in RULES],None


