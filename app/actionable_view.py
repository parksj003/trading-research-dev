"""Optional data-page view; computation is shared with GPT export."""
import hashlib
import json
import re

import pandas as pd
import streamlit as st

from actionable_changes import build_changes, fingerprint, TRACKS
from actionable_source import load_public_snapshots, MAX_REQUESTS, MAX_SNAPSHOTS


def context_key(src,run_id):
    public={k:v for k,v in src.items() if k!='token'}
    return 'actionable_compare_'+hashlib.sha256(json.dumps([public,run_id],sort_keys=True).encode()).hexdigest()


@st.cache_data(ttl=600,max_entries=8,show_spinner=False)
def cached_snapshots(key,run_id,limit,_src):
    return load_public_snapshots(_src,run_id,snapshot_limit=limit)


def active_context(src,run_id):
    return st.session_state.get(context_key(src,run_id),{}).get('report')


def render_actionable_changes(b,src,stock_dataframe):
    st.subheader('📊 Actionable 변화')
    st.caption('게시 적격 후보의 폭을 비교합니다. 적격 제외는 추세 이탈·매도 신호와 다릅니다. 후보 누적 이력은 비교 원천으로 사용하지 않습니다.')
    run_id=str(b.man.get('run_id',''));key=context_key(src,run_id)
    mode=st.radio('변화 비교 기준',['previous_session','publication'],horizontal=True,
                  format_func=lambda x:'시장별 직전 확정 거래일 대비' if x=='previous_session' else '최근 게시본 대비',key='actionable_compare_mode')
    period=st.selectbox('검증할 추이 기간',[5,20],key='actionable_trend_period',format_func=lambda n:f'최근 {n}거래일')
    limit=8 if period==5 else 44
    st.caption(f'이전 게시본은 버튼을 누를 때 조회합니다. 최대 {limit}개 게시본·{1+limit*5}회 요청, 10분 캐시. 20거래일 확대 조회는 시간이 걸릴 수 있습니다.')
    if st.button('검증된 이전 게시본 불러오기',key='actionable_load',disabled=not src or b.export_snapshot_verified is False):
        with st.spinner('고정 게시 commit과 SHA256을 확인하고 있습니다.'):
            snapshots,warnings=cached_snapshots(key,run_id,limit,src)
        # Bound session memory to the current source/run; the network cache is bounded too.
        for old in list(st.session_state):
            if re.fullmatch(r'actionable_compare_[a-f0-9]{64}',str(old)) and old!=key:del st.session_state[old]
        st.session_state[key]={'snapshots':snapshots,'warnings':warnings}
    state=st.session_state.get(key,{})
    for warning in state.get('warnings',[]):st.warning(warning)
    snapshots=state.get('snapshots',[])
    current=next((s for s in snapshots if s.manifest['run_id']==run_id),None)
    if current is not None and fingerprint(current.manifest)!=fingerprint(b.man):
        state.pop('report',None);st.warning('현재 화면과 비교 게시본의 manifest가 달라 비교를 중단했습니다. 새로고침하세요.');return
    if current is None:
        st.info('비교 검증 대기: 버튼을 눌러 이전 게시본을 확보하세요. 미확보·검증 실패는 0건을 의미하지 않습니다.')
        st.caption('5/20거래일 자료가 부족하면 추이를 표시하지 않습니다. 20거래일로 바꾼 뒤 버튼을 누르면 조회 범위를 확대합니다.')
        return
    try:report=build_changes(current,snapshots,mode)
    except (ValueError,TypeError,KeyError):
        state.pop('report',None);st.warning('게시 기준일/메타데이터를 해석할 수 없어 비교를 중단했습니다.');return
    state['report']=report;st.session_state[key]=state
    st.caption(f"현재 run_id: {run_id} · 생성: {current.manifest['generated_at']}. 날짜와 가격의 최신성은 별도 확인하세요.")
    for market in report['markets']:
        st.write('**'+market['market']+' 게시 적격 변화**')
        if market['counts'] is None:st.caption(market['reason']);continue
        c=market['counts'];cols=st.columns(3)
        cols[0].metric('이전',c['previous']);cols[1].metric('현재',c['current']);cols[2].metric('순변화',c['net'])
        st.caption(f"유지 {c['retained']} · 신규 {c['new']} · 제외 {c['excluded']} · 유지율 "+(f"{c['retention_pct']:.1f}%" if c['retention_pct'] is not None else '평가 불가'))
    records=[];events=[];sectors=[]
    for t in report['tracks']:
        records.append({'시장':t['market'],'트랙':t['track'],'이전 기준일':t['previous_as_of'],'현재 기준일':t['current_as_of'],
                        '상태':t['status'],'설명':t['reason'],**(t['counts'] or {})})
        events+=t['events']
        sectors += [{'market':t['market'],'track':t['track'],**s} for s in t['sectors']]
    stock_dataframe(pd.DataFrame(records),hide_index=True,width='stretch')
    with st.expander('유지·신규·제외 종목 / 비교 출처'):
        stock_dataframe(pd.DataFrame(events),hide_index=True,width='stretch')
        for t in report['tracks']:
            st.caption(f"{t['track']}: 이전 {t['previous_source']} → 현재 {t['current_source']}")
    with st.expander('트랙·섹터별 게시 적격 변화'):
        stock_dataframe(pd.DataFrame(sectors),hide_index=True,width='stretch')
        st.caption('섹터 재분류나 트랙 이동이 포함될 수 있습니다. 트랙별 건수를 시장 합계에 중복 합산하지 않습니다.')
    for track in TRACKS:
        trend=report['trends'][track][str(period)]
        if trend['status']!='available':st.caption(f'{track}: '+trend['reason']);continue
        frame=pd.DataFrame(trend['points']).set_index('as_of')
        st.write(track);st.line_chart(frame[['count']])
        st.caption('거래소 캘린더상 모든 거래일이 확보된 기록만 연결합니다. 누락일 보간 없음.')
    st.caption('캘린더 마감·게시 기준일 검증이며 원천 가격의 확보 시각/확정값을 독립 검증한 것은 아닙니다.')
    st.caption('모집단 구성·공급 지연·선언된 engine/config 버전 외 코드 동일성은 미검증입니다. 최신 게시본도 현재 시세의 최신성을 보장하지 않습니다. 시장 악화 원인이나 제외 사유를 수치만으로 단정하지 않습니다.')

