"""Read-only Base Count observations. No engine imports or signal calculations."""
import hashlib
import json
import math
import re

import pandas as pd
import streamlit as st

FILE_NAME = 'base_counts.json'
SCHEMA = 'base-observations-1'
STATUS_LABELS = dict(UNKNOWN='미확인',NO_BASE='현재 후보 없음',BASE_FORMING='형성 중',
    BASE_CONFIRMED='구조 확정',BREAKOUT_PENDING='돌파 유지 확인 중',BREAKOUT_CONFIRMED='돌파 완료',
    BASE_FAILED='실패/만료',TREND_RESET='사이클 종료')
QUALITY_LABELS = dict(UNKNOWN='입력/상태 미확인',HISTORY_INSUFFICIENT='사이클 이력 부족',
                     ESTIMATED='구조 추정',CONFIRMED='규칙 충족')


def load_base_counts(raw, manifest):
    meta = (manifest.get('files') or {}).get(FILE_NAME)
    if meta is None:
        return pd.DataFrame(), {'status':'absent'}
    try:
        if not isinstance(raw,bytes) or len(raw)>8_000_000:
            raise ValueError('file unavailable or too large')
        if len(raw)!=meta['bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:
            raise ValueError('file checksum mismatch')
        doc=json.loads(raw)
        if doc['schema']!=SCHEMA or doc['run_id']!=manifest['run_id']:
            raise ValueError('schema/run mismatch')
        if doc['status'] not in ('enabled','disabled','unavailable') or not isinstance(doc['observations'],list):
            raise ValueError('publication contract invalid')
        generated=pd.Timestamp(doc['generated_at'])
        if pd.isna(generated) or generated.tzinfo is None:
            raise ValueError('publication time missing')
        identities=set();rows=[]
        for row in doc['observations']:
            if not isinstance(row,dict):raise ValueError('invalid observation')
            market,ticker=row['market'],row['ticker']
            if market not in ('KR','US') or not isinstance(ticker,str):raise ValueError('identity missing')
            ticker=ticker.zfill(6) if market=='KR' else ticker.upper()
            if not re.fullmatch(r'[A-Z0-9^][A-Z0-9^._-]*',ticker):raise ValueError('identity invalid')
            key=(market,ticker)
            if key in identities:raise ValueError('duplicate observation')
            identities.add(key)
            date=pd.Timestamp(row['as_of']);recorded=pd.Timestamp(row['recorded_at'])
            if (pd.isna(date) or date.tzinfo is not None or date!=date.normalize()
                or pd.isna(recorded) or recorded.tzinfo is None or recorded>generated
                or row.get('mode') not in ('LIVE','REPLAY')):
                raise ValueError('observation time invalid')
            if row['status'] not in STATUS_LABELS or row['quality'] not in QUALITY_LABELS:
                raise ValueError('unknown state/quality')
            for key in ('completed_base_count','current_base_ordinal'):
                value=row[key]
                if value is not None and (type(value) is not int or value < (1 if key=='current_base_ordinal' else 0)):
                    raise ValueError('invalid absolute count')
                if (row['quality'] in ('UNKNOWN','HISTORY_INSUFFICIENT') or row['status']=='UNKNOWN') and value is not None:
                    raise ValueError('uncertain count must be null')
            for key in ('base_start_date','base_first_observed_date','base_confirmed_date','base_breakout_date',
                        'base_breakout_confirmed_date','base_end_date'):
                if row.get(key) is not None and (pd.isna(pd.Timestamp(row[key])) or pd.Timestamp(row[key])>date):
                    raise ValueError('future structure date')
            rows.append(dict(row,market=market,ticker=ticker,as_of=date.strftime('%Y-%m-%d')))
        return pd.DataFrame(rows),dict(status=doc['status'],generation=doc.get('generation'),run_id=doc['run_id'])
    except (KeyError,ValueError,TypeError,OverflowError,AttributeError):
        return pd.DataFrame(),{'status':'unavailable','reason':'베이스 관측 자료의 무결성·기준일을 확인할 수 없습니다.'}


def _identity(market,ticker):
    m=str(market).upper();t=str(ticker)
    return m+':'+(t.zfill(6) if m=='KR' else t.upper())


def attach_base_counts(frame, observations, source):
    out=frame.copy()
    if out.empty:return out
    lookup={_identity(r['market'],r['ticker']):r for r in observations.to_dict('records')}
    rows=[]
    for row in out.to_dict('records'):
        observation=lookup.get(_identity(row.get('market'),row.get('ticker')))
        try: same_date=observation is not None and pd.Timestamp(row.get('as_of'))==pd.Timestamp(observation['as_of'])
        except (ValueError,TypeError):same_date=False
        if not same_date or source.get('status')!='enabled':
            reason = ('OBSERVATIONS_NOT_PUBLISHED' if source.get('status','absent')=='absent' else
                      'OBSERVATIONS_DISABLED' if source.get('status')=='disabled' else
                      'OBSERVATIONS_UNAVAILABLE' if source.get('status')=='unavailable' else
                      'OBSERVATION_MISSING' if observation is None else 'OBSERVATION_DATE_MISMATCH')
            observation=dict(status='UNKNOWN',quality='UNKNOWN',completed_base_count=None,current_base_ordinal=None,
                             reason=reason)
        rows.append({'bc_'+k:v for k,v in observation.items() if k not in ('market','ticker')})
    extra=pd.DataFrame(rows,index=out.index)
    for col in extra:out[col]=extra[col]
    for col in ('bc_completed_base_count','bc_current_base_ordinal'):
        out[col]=pd.array(out[col],dtype='Int64')
    out['bc_label']=[f'Base {int(n)}' if pd.notna(n) else '순번 미확인' if q in ('UNKNOWN','HISTORY_INSUFFICIENT') else '후보 없음'
                     for n,q in zip(out.bc_current_base_ordinal,out.bc_quality)]
    out['bc_status_label']=out.bc_status.map(STATUS_LABELS)
    out['bc_quality_label']=out.bc_quality.map(QUALITY_LABELS)
    out['bc_display']=[base_display_label(row) for row in out.to_dict('records')]
    return out


def base_display_label(row):
    """One factual table cell; no count inferred from missing/uncertain input."""
    reasons={'OBSERVATIONS_NOT_PUBLISHED':'자료 미게시','OBSERVATIONS_DISABLED':'비활성',
             'OBSERVATIONS_UNAVAILABLE':'자료 확인 불가','OBSERVATION_MISSING':'관측 자료 없음','OBSERVATION_DATE_MISMATCH':'기준일 불일치',
             'PRICE_BASIS_UNVERIFIED':'가격 기준 미확인','CACHE_MISSING':'가격 이력 없음',
             'MARKET_PARAMETERS_NOT_CONFIGURED':'계산 설정 미확인'}
    quality=row.get('bc_quality')
    ordinal=row.get('bc_current_base_ordinal')
    if row.get('bc_status')=='UNKNOWN':
        return '미확인 · '+reasons.get(row.get('bc_reason'),'자료 검증 보류')
    if quality in ('UNKNOWN','HISTORY_INSUFFICIENT'):
        return '미확인 · '+('이력 부족' if quality=='HISTORY_INSUFFICIENT' else '자료 검증 보류')
    if pd.isna(ordinal):
        return '진행 베이스 없음'
    label=f'{int(ordinal)}차 · '+STATUS_LABELS.get(row.get('bc_status'),'상태 미확인')
    return label+' (추정)' if quality=='ESTIMATED' else label


def filter_base_counts(frame, ordinals=(), statuses=(), qualities=()):
    """User-selected view filter only. Default selections retain order and rows."""
    out=frame
    if out.empty:return out.copy()
    if ordinals:
        n=out.get('bc_current_base_ordinal',pd.Series(pd.NA,index=out.index,dtype='Int64'))
        label=n.map(lambda v:'미확인' if pd.isna(v) else '4+' if v>=4 else str(int(v)))
        out=out[label.isin(ordinals)]
    if statuses:out=out[out.bc_status.isin(statuses)]
    if qualities:out=out[out.bc_quality.isin(qualities)]
    return out.copy()


def observation_filters(frame, source):
    if source.get('status','absent')=='absent':return frame
    with st.expander('베이스 단계 필터 · 관측용',expanded=False):
        st.caption('표시할 종목만 좁힙니다. 기존 점수·매매 적격·손절가·배분 계산은 유지됩니다.')
        if source.get('status')!='enabled':
            st.info('베이스 관측이 비활성 상태이거나 자료를 확인할 수 없습니다. 기존 종목 분석은 계속 사용할 수 있습니다.')
        a,b,c=st.columns(3)
        ordinals=a.multiselect('현재 베이스 순번',['1','2','3','4+','미확인'],default=[],key='bc_ordinals')
        statuses=b.multiselect('베이스 상태',list(STATUS_LABELS),default=[],format_func=STATUS_LABELS.get,key='bc_statuses')
        qualities=c.multiselect('베이스 관측 품질',list(QUALITY_LABELS),default=[],format_func=QUALITY_LABELS.get,key='bc_qualities')
    return filter_base_counts(frame,ordinals,statuses,qualities)


def render_base_panel(row):
    if 'bc_status' not in row:return False
    if row.get('bc_reason')=='OBSERVATIONS_NOT_PUBLISHED':
        st.caption('베이스 순번: 미확인 · 계산 결과가 아직 게시되지 않았습니다.')
        return False
    st.markdown('##### 베이스 단계 · 관측용')
    a,b,c=st.columns(3)
    a.metric('현재 순번',row['bc_label'])
    count=row.get('bc_completed_base_count')
    b.metric('완료 베이스 수',str(int(count)) if pd.notna(count) else '미확인')
    c.metric('베이스 상태',row['bc_status_label'])
    st.caption(f"{row['bc_quality_label']} · 베이스 단계의 성과 우위는 미검증이며, 기존 매매 신호에 반영하지 않습니다.")
    with st.expander('베이스 관측 근거',expanded=False):
        labels={'bc_base_start_date':'구조 시작일','bc_base_first_observed_date':'최초 인식일',
                'bc_base_confirmed_date':'구조 확정일','bc_base_breakout_date':'최근 돌파 시도일',
                'bc_base_breakout_confirmed_date':'최초 돌파 완료일','bc_as_of':'관측 기준일',
                'bc_recorded_at':'계산 시각','bc_mode':'기록 유형',
                'bc_base_structural_pivot':'관측 구조 피벗','bc_base_length_days':'관측 구조 길이(거래일)',
                'bc_base_depth_pct':'관측 구조 깊이(%)','bc_base_on_base':'Base-on-Base'}
        st.dataframe(pd.DataFrame([{'항목':label,'값':str(row.get(key)) if pd.notna(row.get(key)) else '미확인'}
                                   for key,label in labels.items()]),hide_index=True,width='stretch')
        st.caption('REPLAY는 사후 재생입니다. 구조 시작일과 실제 인식일은 다르며, 당시 게시한 매매 신호를 뜻하지 않습니다.')
        if row.get('bc_status')=='UNKNOWN':
            reasons={'PRICE_BASIS_UNVERIFIED':'수정주가 기준을 확인할 수 없는 이력이 포함되어 있습니다.',
                     'OBSERVATION_DATE_MISMATCH':'종목 가격과 베이스 관측의 기준일이 일치하지 않습니다.',
                     'CHECKPOINT_LINEAGE_CONFLICT':'계산 설정 또는 가격 이력이 기존 상태와 일치하지 않습니다.'}
            st.info(reasons.get(row.get('bc_reason'),'가격 이력 또는 저장 상태 검증이 보류되어 순번을 표시하지 않습니다.'))
    return row.get('bc_status')!='UNKNOWN'


def add_base_overlay(fig, chart, row, bars):
    """Optional current known structure; leave existing trading pivot/stop intact."""
    if row.get('bc_status','UNKNOWN')=='UNKNOWN':return fig
    dates=chart.get('d',[])[-bars:]
    try:
        as_of=pd.Timestamp(row['bc_as_of']);start=pd.Timestamp(row['bc_base_start_date'])
        end=pd.Timestamp(row.get('bc_base_end_date')) if pd.notna(row.get('bc_base_end_date')) else as_of
        observed=pd.Timestamp(row['bc_base_first_observed_date'])
        if not dates or any(pd.isna(x) for x in (as_of,start,end,observed)) or start>observed or observed>as_of or end>as_of:
            return fig
        visible=[date for date in dates if start<=pd.Timestamp(date)<=min(end,as_of)]
        if not visible:return fig
        fig.add_vrect(x0=visible[0],x1=visible[-1],fillcolor='#7C3AED',opacity=.07,line_width=0,
                      annotation_text=str(row.get('bc_label','순번 미확인'))+' 관측 구간',row=1,col=1)
        pivot=float(row.get('bc_base_structural_pivot'))
        if math.isfinite(pivot) and pivot>0:
            fig.add_hline(y=pivot,line_color='#7C3AED',line_dash='dot',line_width=1,
                          annotation_text='관측 구조 피벗',row=1,col=1)
    except (KeyError,ValueError,TypeError,OverflowError):pass
    return fig

