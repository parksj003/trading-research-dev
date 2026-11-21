"""Read-only review UI. Exports proposals; never writes with server credentials."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import streamlit as st

FILE_NAME='sector_review.json'
SCHEMA='sector-review-1'
KST=ZoneInfo('Asia/Seoul')
STATUSES={'PENDING':'검토 대기','REVIEW_ONLY':'참고·수동 검토','SUPERSEDED':'새 근거로 교체됨',
          'SCHEDULED':'적용 예정','APPLIED':'분류 반영 이력 있음','REJECTED':'반려',
          'APPROVED_WAITING':'승인됨·정상 분석 대기','APPROVED_BLOCKED':'승인됨·직접 지정 우선'}
REASONS={'EXTERNAL_CATEGORY_CHANGED':'외부 업종·테마 변화',
         'MANUAL_OVERRIDE_LOCKED':'직접 지정한 분류 — 자동 대체 금지',
         'PROVIDER_CHANGED':'공급처·근거 범위 변경 — 분류 변경으로 확정 불가',
         'SOURCE_CHANGED_REVIEW_ONLY':'원천 정보 변화 — 재분류 확정 근거 아님'}


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def aware(value):
    time=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if time.tzinfo is None:raise ValueError('timestamp timezone missing')
    return time


def display_time(value):
    return aware(value).astimezone(KST).strftime('%Y-%m-%d %H:%M KST') if value else '미실시'


def load_sector_review(raw,manifest):
    meta=(manifest.get('files') or {}).get(FILE_NAME)
    if meta is None:return {},{'status':'absent'}
    try:
        if (not isinstance(raw,bytes) or len(raw)>8_000_000 or len(raw)!=meta['bytes']
                or hashlib.sha256(raw).hexdigest()!=meta['sha256']):raise ValueError('checksum mismatch')
        doc=json.loads(raw)
        if doc['schema']!=SCHEMA or doc['run_id']!=manifest['run_id']:raise ValueError('lineage mismatch')
        generated=aware(doc['generated_at'])
        if doc['status'] not in ('ready','waiting','unavailable') or doc['automatic_application'] is not False:
            raise ValueError('review contract mismatch')
        if not isinstance(doc['markets'],dict) or not isinstance(doc['candidates'],list):raise ValueError('invalid review tables')
        for market,info in doc['markets'].items():
            if market not in ('KR','US') or info['status'] not in ('BASELINE_CREATED','CHECKED','NOT_DUE','NOT_REQUESTED','WAITING_FOR_FRESH_SOURCE'):
                raise ValueError('invalid market review status')
            for key in ('observed','blocked'):
                if type(info.get(key)) is not int or info[key]<0:raise ValueError('invalid source coverage')
            if info.get('checked_at') and aware(info['checked_at'])>generated:raise ValueError('future verification')
        seen=set()
        for c in doc['candidates']:
            if c['market'] not in ('KR','US') or not isinstance(c['ticker'],str):raise ValueError('invalid market')
            if c['status'] not in STATUSES or type(c['actionable']) is not bool:raise ValueError('invalid review status')
            for field in ('candidate_id','proposal_hash','source_hash'):
                if not re.fullmatch('[a-f0-9]{64}',c[field]):raise ValueError('invalid evidence reference')
            if c['candidate_id'] in seen:raise ValueError('duplicate proposal')
            seen.add(c['candidate_id'])
            if aware(c['detected_at'])>generated:raise ValueError('future proposal')
            for evidence in (c['before'],c['after']):
                if evidence['market']!=c['market'] or str(evidence['ticker'])!=c['ticker']:raise ValueError('evidence identity mismatch')
                if aware(evidence['retrieved_at'])>generated:raise ValueError('future evidence')
                for url in evidence['source_urls']:
                    parsed=urlsplit(url)
                    if parsed.scheme!='https' or not parsed.hostname or parsed.username:raise ValueError('unsafe source URL')
        return doc,{'status':doc['status'],'run_id':doc['run_id']}
    except (ValueError,TypeError,KeyError,OverflowError,AttributeError):
        return {},{'status':'unavailable','reason':'분류 검토 자료의 무결성·출처·시각을 확인할 수 없습니다.'}


def build_decision(candidate,*,decision,reviewer,reviewed_at,effective_from=''):
    reviewed=aware(reviewed_at)
    if candidate.get('status')!='PENDING' or not candidate.get('actionable'):
        raise ValueError('검토 대기 중인 적용 가능 후보만 선택할 수 있습니다.')
    if decision not in ('approve','reject') or not reviewer.strip() or '\n' in reviewer or len(reviewer)>100:
        raise ValueError('검토자와 검토 결과를 입력하세요.')
    if reviewed<aware(candidate['detected_at']):raise ValueError('관측 이후에 검토해야 합니다.')
    if decision=='approve' and datetime.fromisoformat(effective_from).date()<=reviewed.astimezone(KST).date():
        raise ValueError('적용일은 검토일 다음 날 이후여야 합니다.')
    row=dict(schema='sector-review-decision-1',decision=decision,reviewer=reviewer.strip(),
             reviewed_at=reviewed_at,effective_from=effective_from)
    for field in ('candidate_id','proposal_hash','market','ticker','current_macro_theme','current_sector',
                  'proposed_macro_theme','proposed_sector','source_hash'):row[field]=candidate[field]
    row['decision_id']=hashlib.sha256(canonical(row)).hexdigest()
    return row


def render_sector_review(doc,source):
    if source.get('status')=='absent':return
    with st.expander('🔎 섹터 분류 변경 검토',expanded=False):
        st.caption('기존 수집에서 확보한 원천을 시장별 7일 간격으로 비교합니다. 최초에는 기준점만 만들며 현재 분류를 자동 변경하지 않습니다.')
        if source.get('status')=='unavailable' or not doc:
            st.warning(source.get('reason','검토 상태를 복구할 때까지 재분류를 보류합니다.'));return
        if doc.get('paused'):
            st.info('변경 점검은 일시 중지돼 있습니다. 기존 기준점·분류와 승인 기록은 유지합니다.')
        for market,info in sorted(doc.get('markets',{}).items()):
            status={'BASELINE_CREATED':'최초 기준점 생성','CHECKED':'점검 완료','NOT_DUE':'다음 정기 점검 대기',
                    'WAITING_FOR_FRESH_SOURCE':'최신 원천 확보 대기','NOT_REQUESTED':'이번 실행의 점검 대상 아님'}.get(info.get('status'),info.get('status'))
            st.caption(f"{market} · {status} · 확인 {info.get('observed',0)}종목 · 근거 부족 {info.get('blocked',0)}종목 · 마지막 점검 {display_time(info.get('checked_at'))}")
        rows=doc.get('candidates',[])
        if not rows:
            st.info('현재 등록된 변경 후보가 없습니다. 최초 기준점 생성·자료 부족은 분류 검증 완료를 의미하지 않습니다.');return
        st.caption('아래 분류와 원천은 후보를 발견한 시점의 스냅샷입니다. 최신 분류는 현재 종목표에서 확인하세요.')
        st.dataframe([dict(시장=c['market'],티커=c['ticker'],종목=c.get('name',''),
            발견당시=f"{c['current_macro_theme']} / {c['current_sector']}",
            검토안=f"{c['proposed_macro_theme']} / {c['proposed_sector']}",
            상태=STATUSES[c['status']],사유=REASONS.get(c.get('reason'),c.get('reason')),
            최초발견=display_time(c['detected_at'])) for c in rows],hide_index=True,width='stretch')
        lookup={c['candidate_id']:c for c in rows}
        selected=st.selectbox('근거를 확인할 종목',list(lookup),
            format_func=lambda key:f"{lookup[key]['market']}:{lookup[key]['ticker']} · {lookup[key].get('name','')} · {STATUSES[lookup[key]['status']]}",key='sector_review_selected')
        c=lookup[selected]
        st.write('발견 당시 분류',c['current_macro_theme'],'→',c['current_sector'])
        st.write('추천 분류',c['proposed_macro_theme'],'→',c['proposed_sector'])
        st.caption('ETF 편입·비중과 업종 코드의 변화는 주력 사업 변화의 증명이 아닙니다. 공시·사업 내용도 확인한 뒤 판단하세요.')
        left,right=st.columns(2)
        for column,label,evidence in ((left,'이전 원천',c['before']),(right,'현재 원천',c['after'])):
            with column:
                st.markdown('**'+label+'**')
                st.json({key:evidence.get(key) for key in ('raw_sector','raw_sub_industry','provider','retrieved_at',
                    'source_as_of','etfs','etf_evidence_complete','etf_source')})
                for url in evidence.get('source_urls',[]):st.link_button('원천 출처 열기',url)
        if not c['actionable'] or c['status']!='PENDING':
            st.info('이 항목은 적용 가능한 검토 대기 후보가 아닙니다. 직접 지정한 분류는 기존 사용자 분류표에서 별도 검토하세요.');return
        checked=(doc.get('markets',{}).get(c['market']) or {}).get('checked_at')
        age=datetime.now(timezone.utc)-aware(checked) if checked else None
        if age is None or age<timedelta(0) or age>=timedelta(days=7):
            st.warning('최근 원천 점검이 유효하지 않아 검토안 생성을 보류합니다. 다음 정상 점검 후 다시 확인하세요.');return
        tomorrow=datetime.now(timezone.utc).astimezone(KST).date()+timedelta(days=1)
        with st.form('sector_review_decision_form'):
            reviewer=st.text_input('검토자',key='sector_review_reviewer')
            choice=st.radio('검토 결과',['approve','reject'],format_func=lambda v:'승인안' if v=='approve' else '반려안',horizontal=True)
            effective=st.date_input('승인 적용일',value=tomorrow,min_value=tomorrow)
            confirmed=st.checkbox('이전·현재 근거와 분류 변경 영향을 확인했습니다.')
            submitted=st.form_submit_button('검토안 생성')
        if submitted:
            try:
                if not confirmed:raise ValueError('근거 확인에 동의해야 합니다.')
                decision=build_decision(c,decision=choice,reviewer=reviewer,
                    reviewed_at=datetime.now(timezone.utc).isoformat(),effective_from=effective.isoformat() if choice=='approve' else '')
                st.session_state['sector_review_export']=decision
            except ValueError as exc:st.error(str(exc))
        exported=st.session_state.get('sector_review_export')
        if exported and exported['candidate_id']==selected:
            st.download_button('검토안 JSON 다운로드',canonical(exported),file_name=f"sector-review-{selected[:12]}.json",mime='application/json')
            st.info('다운로드만으로 적용되지 않습니다. 저장소 유지관리자가 검증한 기록을 GitHub에 반영해야 합니다. 공개 앱은 저장소에 쓰지 않습니다.')
            st.code('python -m watchlist.sector_review --base watchlist/published --decision <다운로드한.json>\n'
                    '# 검증 결과를 확인한 뒤 같은 명령에 --apply 추가\n'
                    '# 생성된 watchlist/sector_review_decisions.csv를 GitHub PR로 반영',language='bash')

