"""Compare immutable, hash-checked public publications; never reclassify signals."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import io
import json
import math
import re

import exchange_calendars as xcals
import pandas as pd

TRACKS = {'KOSPI':'KR','KOSDAQ':'KR','NASDAQ100':'US','SP500':'US'}
FIELDS = ('market','sub_market','ticker','name','sector','as_of','is_actionable',
          'exclusion_reason','exit_signal','is_stale_price','portfolio_alloc_note')
MAX_BYTES = 3_000_000


@dataclass
class Snapshot:
    manifest: dict
    summary: dict
    watch: list[dict]
    candidates: list[dict]
    commit: str


def _frame(raw):
    df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    if len(df)>50_000: raise ValueError('게시 행 수 제한 초과')
    required={'market','sub_market','ticker','as_of','is_actionable'}
    if not required.issubset(df.columns): raise ValueError('게시 식별/상태 필드 누락')
    rows=[];seen=set()
    for row in df.to_dict('records'):
        r={k:row[k] for k in FIELDS if k in row}
        if TRACKS.get(r['sub_market'])!=r['market']: raise ValueError('시장/트랙 불일치')
        ticker=r['ticker'].strip().upper()
        if r['market']=='KR' and ticker.isdigit():ticker=ticker.zfill(6)
        if not re.fullmatch(r'[A-Z0-9.^=-]{1,24}',ticker):raise ValueError('티커 형식 오류')
        r['ticker']=ticker
        k=(r['market'],r['sub_market'],ticker)
        if k in seen:raise ValueError('중복 종목 식별자')
        seen.add(k)
        if str(r['is_actionable']).lower() not in ('true','false','1','0'):raise ValueError('적격 상태 확인 불가')
        r['is_actionable']=str(r['is_actionable']).lower() in ('true','1')
        rows.append(r)
    return rows


def verified_snapshot(manifest_raw, files, commit):
    if not re.fullmatch('[a-f0-9]{40}',commit):raise ValueError('고정 commit SHA 필요')
    if len(manifest_raw)>100_000:raise ValueError('manifest 크기 제한 초과')
    m=json.loads(manifest_raw)
    if not isinstance(m,dict) or not isinstance(m.get('as_of'),dict):raise ValueError('manifest 구조 오류')
    if str(m.get('schema_version','')).split('.')[0]!='1':raise ValueError('지원하지 않는 게시 계약 버전')
    if not isinstance(m.get('files'),dict):raise ValueError('manifest 파일 목록 구조 오류')
    if any(not isinstance(m.get(k),str) or not m[k] or len(m[k])>256 for k in ('run_id','generated_at','config_hash','engine_version')):raise ValueError('게시 출처/버전 누락')
    for name in ('summary.json','watchlist.csv','candidates.csv'):
        raw=files.get(name); meta=m.get('files',{}).get(name,{})
        if not isinstance(meta,dict):raise ValueError('파일 검증 메타데이터 오류')
        if raw is None or len(raw)>MAX_BYTES:raise ValueError('게시 파일 누락/크기 제한 초과')
        if not re.fullmatch('[a-f0-9]{64}',str(meta.get('sha256',''))):raise ValueError('SHA256 근거 누락')
        if hashlib.sha256(raw).hexdigest()!=meta['sha256']:raise ValueError('SHA256 불일치')
    watch=_frame(files['watchlist.csv']);cand=_frame(files['candidates.csv'])
    if any(not r['is_actionable'] for r in watch):raise ValueError('게시 적격 목록 상태 불일치')
    summary=json.loads(files['summary.json'])
    if not isinstance(summary,dict):raise ValueError('summary 구조 오류')
    # Hashes bind all three files to this manifest; optional run_id must also agree.
    if summary.get('run_id',m['run_id'])!=m['run_id']:raise ValueError('summary run_id 불일치')
    return Snapshot(m,summary,watch,cand,commit)


@lru_cache(maxsize=2)
def calendar(market):
    return xcals.get_calendar('XKRX' if market=='KR' else 'XNYS')


def day(snapshot, track):
    value=snapshot.manifest.get('as_of',{}).get(track)
    try:return pd.Timestamp(value).strftime('%Y-%m-%d') if value else None
    except (ValueError,TypeError):return None


def fingerprint(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def source(snapshot):
    return {k:snapshot.manifest.get(k) for k in ('run_id','generated_at','config_hash','engine_version')}|{'commit':snapshot.commit,'manifest_sha256':fingerprint(snapshot.manifest)}


def quality(snapshot, track):
    market=TRACKS[track];date=day(snapshot,track)
    regimes=snapshot.summary.get('regimes',{})
    regime=regimes.get(track,{}) if isinstance(regimes,dict) else {}
    if not isinstance(regime,dict) or regime.get('as_of')!=date:return '지수/트랙 기준일 검증 불가'
    try:
        cal=calendar(market)
        if not date or not cal.is_session(date):return '거래일 기준일 확인 불가'
        generated=pd.Timestamp(snapshot.manifest['generated_at'])
        if generated.tzinfo is None:return '생성 시각 시간대 확인 불가'
        if generated < cal.session_close(date):return '장 마감 전 게시본'
    except Exception:return '거래소 캘린더 검증 불가'
    candidates={(r['sub_market'],r['ticker']):r for r in snapshot.candidates}
    observed=snapshot.summary.get('price_observation')
    observed=observed.get('tracks') if isinstance(observed,dict) else None
    observation=observed.get(track) if isinstance(observed,dict) else None
    observation=observation if isinstance(observation,dict) else {}
    if observation and (observation.get('session_unconfirmed') is True
                        or (isinstance(observation.get('lagging_actionable_rows'),(int,float)) and observation['lagging_actionable_rows']>0)
                        or observation.get('benchmark_as_of',date)!=date):
        return '게시 가격 관측 메타데이터 지연/미확정'
    for r in snapshot.watch:
        if r['sub_market']!=track:continue
        c=candidates.get((track,r['ticker']))
        if (r['as_of']!=date or c is None or c['as_of']!=date or not c['is_actionable']
                or str(c.get('is_stale_price','')).lower()=='true'):
            return '적격 종목 가격/후보 상태 지연·불일치'
    return None


def _counts(previous, current):
    a,b=set(previous),set(current);kept=a&b;new=b-a;removed=a-b
    return dict(previous=len(a),current=len(b),net=len(b)-len(a),
                change_pct=(len(b)-len(a))/len(a)*100 if a else None,
                retained=len(kept),new=len(new),excluded=len(removed),
                retention_pct=len(kept)/len(a)*100 if a else None)


def compare_track(current, previous, track, mode):
    result={'market':TRACKS[track],'track':track,'current_as_of':day(current,track),
            'previous_as_of':day(previous,track) if previous else None,
            'current_source':source(current),'previous_source':source(previous) if previous else None,
            'status':'unavailable','reason':'비교 가능한 이전 게시본 없음','counts':None,'events':[], 'sectors':[]}
    if previous is None:return result
    if previous.manifest['run_id']==current.manifest['run_id']:
        result['reason']='동일 run_id';return result
    for snap in (current,previous):
        error=quality(snap,track)
        if error:result['reason']=error;return result
    if any(not current.manifest.get(k) or current.manifest.get(k)!=previous.manifest.get(k)
           for k in ('config_hash','engine_version')):
        result['reason']='계산/설정 버전 비교 불가';return result
    if mode=='previous_session':
        expected=calendar(TRACKS[track]).previous_session(day(current,track)).strftime('%Y-%m-%d')
        if day(previous,track)!=expected:result['reason']='직전 거래일 스냅샷 미확보';return result
    elif day(previous,track)>day(current,track):
        result['reason']='가격 기준일 역전';return result
    a={r['ticker']:r for r in previous.watch if r['sub_market']==track}
    b={r['ticker']:r for r in current.watch if r['sub_market']==track}
    result['price_verification']='캘린더 마감·게시 기준일/상태 검증. 원천 가격 확보 시각·실제 확정값의 독립 검증은 아님'
    result.update(status='available',reason='동일 가격 기준일의 게시본 변화' if day(previous,track)==day(current,track) else '확정 기준일의 게시 적격 목록 비교',counts=_counts(a,b))
    def population(s):
        health=s.summary.get('health')
        h=health.get(TRACKS[track].lower()) if isinstance(health,dict) else None
        value=h.get(TRACKS[track].lower()+'_universe') if isinstance(h,dict) else None
        return value if isinstance(value,(int,float)) and math.isfinite(value) and value>=0 else None
    p,c=population(previous),population(current)
    result['population']={'previous':p,'current':c,'status':'unverified' if p is None or c is None else 'changed' if p!=c else 'same_count_membership_unverified'}
    if p is not None and c is not None and p!=c:result['reason']+=' · 모집단 수 변경 감지'
    candidates={r['ticker']:r for r in current.candidates if r['sub_market']==track}
    for ticker in sorted(set(a)|set(b)):
        kind='retained' if ticker in a and ticker in b else 'new' if ticker in b else 'excluded'
        row=b.get(ticker,a.get(ticker));now=candidates.get(ticker)
        reason=None
        if kind=='excluded':
            if now is None:reason='현재 후보 목록 미관측 — 제외 사유 확인 불가'
            elif str(now.get('is_stale_price','')).lower()=='true' or now['as_of']!=day(current,track):reason='가격 지연/기준일 불일치 — 기술적 제외 단정 불가'
            elif now['is_actionable']:reason='게시 목록 제외 — 후보의 기술적 적격 유지'
            else:reason=now.get('exclusion_reason') or '현재 기술적 비적격 — 구체적 제외 사유 확인 불가'
        result['events'].append({k:row.get(k) for k in ('market','sub_market','ticker','name','sector')}|{
            'event':kind,'reason':reason,'previous_sector':a.get(ticker,{}).get('sector'),
            'current_sector':b.get(ticker,{}).get('sector'),
            'current_exit_signal':now.get('exit_signal') if now else None})
    for sector in sorted({r.get('sector','') for r in list(a.values())+list(b.values())}):
        result['sectors'].append({'sector':sector,**_counts({t for t,r in a.items() if r.get('sector','')==sector},
                                                         {t for t,r in b.items() if r.get('sector','')==sector})})
    return result


def build_changes(current, snapshots, mode='previous_session'):
    if mode not in ('previous_session','publication'):raise ValueError('비교 모드 오류')
    older=sorted([s for s in snapshots if s.manifest['run_id']!=current.manifest['run_id'] and
                  pd.Timestamp(s.manifest['generated_at'])<pd.Timestamp(current.manifest['generated_at'])],
                 key=lambda s:pd.Timestamp(s.manifest['generated_at']),reverse=True)
    tracks=[]
    for track in TRACKS:
        if mode=='publication':prev=next(iter(older),None)
        else:
            try:expected=calendar(TRACKS[track]).previous_session(day(current,track)).strftime('%Y-%m-%d')
            except Exception:expected=None
            prev=next((s for s in older if expected and day(s,track)==expected),None)
        tracks.append(compare_track(current,prev,track,mode))
    markets=[]
    for market in ('KR','US'):
        parts=[t for t in tracks if t['market']==market]
        # Market counts use unique market/ticker identity; track moves are not market entries/exits.
        if not all(t['status']=='available' for t in parts):
            markets.append({'market':market,'status':'unavailable','counts':None,'reason':'일부 트랙 비교 불가'});continue
        if len({t['previous_source']['run_id'] for t in parts})!=1:
            markets.append({'market':market,'status':'unavailable','counts':None,'reason':'트랙별 비교 게시본 상이'});continue
        prev=next(s for s in older if s.manifest['run_id']==parts[0]['previous_source']['run_id'])
        markets.append({'market':market,'status':'available','reason':'시장/티커 중복 제거',
                        'counts':_counts({r['ticker'] for r in prev.watch if r['market']==market},
                                         {r['ticker'] for r in current.watch if r['market']==market})})
    trends={}
    for track in TRACKS:
        entries={}
        for snap in reversed(older+[current]):
            if quality(snap,track) is None and all(current.manifest.get(k) and snap.manifest.get(k)==current.manifest.get(k) for k in ("config_hash","engine_version")):
                date=day(snap,track)
                # Newest publication wins for an unchanged market date.
                if date not in entries or pd.Timestamp(snap.manifest['generated_at'])>pd.Timestamp(entries[date]['generated_at']):
                    entries[date]={'as_of':date,'count':sum(r['sub_market']==track for r in snap.watch),
                                   'run_id':snap.manifest['run_id'],'generated_at':snap.manifest['generated_at']}
        windows={}
        for n in (5,20):
            try:
                cal=calendar(TRACKS[track]);end=day(current,track)
                dates=cal.sessions_window(end,-n).strftime('%Y-%m-%d').tolist()
                available=quality(current,track) is None and len(dates)==n and all(d in entries for d in dates)
            except Exception:dates=[];available=False
            windows[str(n)]={'status':'available' if available else 'unavailable',
                             'reason':None if available else '검증된 연속 거래일 스냅샷 부족',
                             'points':[entries[d] for d in dates] if available else []}
        trends[track]=windows
    return {'schema':'actionable-changes-1.0','current_source':source(current),'mode':mode,
            'definition':'게시 적격 목록 변화이며 매매 신호·추세 이탈 판정이 아님',
            'warnings':['모집단 구성·공급 지연·선언된 engine/config 버전 외의 코드 동일성은 미검증. 게시 건수 변화의 원인을 추정하지 않습니다.'],
            'tracks':tracks,'markets':markets,'trends':trends}


def export_changes(report, manifest, markets):
    """Only locally computed context, projected through fixed public allowlists."""
    if not report or report.get('schema')!='actionable-changes-1.0' or report.get('current_source',{}).get('run_id')!=manifest.get('run_id') or report.get('current_source',{}).get('manifest_sha256')!=fingerprint(manifest):
        return {'schema':'actionable-changes-1.0','status':'unavailable','reason':'현재 run_id와 일치하는 비교 집계 없음'}
    def project(obj,fields):return {k:obj[k] for k in fields if k in obj}
    source_fields=('run_id','generated_at','config_hash','engine_version','commit','manifest_sha256')
    count_fields=('previous','current','net','change_pct','retained','new','excluded','retention_pct')
    tracks=[]
    for t in report.get('tracks',[]):
        if t.get('market') not in markets:continue
        row=project(t,('market','track','current_as_of','previous_as_of','status','reason','price_verification'))
        row['population']=project(t.get('population',{}),('previous','current','status'))
        row['counts']=project(t['counts'],count_fields) if t.get('counts') is not None else None
        for k in ('previous_source','current_source'):row[k]=project(t[k],source_fields) if t.get(k) else None
        row['events']=[project(e,('market','sub_market','ticker','name','sector','event','reason','previous_sector','current_sector','current_exit_signal')) for e in t.get('events',[])[:5000]]
        row['sectors']=[project(e,('sector',)+count_fields) for e in t.get('sectors',[])[:1000]]
        tracks.append(row)
    result={'schema':report['schema'],'status':'available' if any(t['status']=='available' for t in tracks) else 'unavailable',
            'mode':report.get('mode'),'current_source':project(report['current_source'],source_fields),'tracks':tracks,
            'markets':[project(t,('market','status','reason'))|{'counts':project(t['counts'],count_fields) if t.get('counts') else None} for t in report.get('markets',[]) if t.get('market') in markets]}
    # Trend records also have a fixed projection; no raw source/private columns pass through.
    result['definition']='게시 적격 목록의 관측 변화이며 독립적인 매수·매도/추세 이탈 판정이 아닙니다.'
    result['limitations']=['모집단 구성·공급 지연·선언된 engine/config 버전 외 코드 동일성은 미검증입니다.', '현재 게시본의 기준일이 현재 시세의 최신성을 보장하지 않습니다.']
    result['trends']={track:{str(n):{'status':v.get('status'),'reason':v.get('reason'),
        'points':[project(p,('as_of','count','run_id','generated_at')) for p in v.get('points',[])[:20]]}
        for n,v in windows.items() if str(n) in ('5','20')} for track,windows in report.get('trends',{}).items() if TRACKS.get(track) in markets}
    return result

