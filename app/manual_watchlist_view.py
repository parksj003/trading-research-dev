"""Independent, user-maintained, published EOD observation grid."""
from __future__ import annotations

import html
import json
import math
import unicodedata
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlencode

import streamlit as st

from manual_catalog import ASSET_LABELS, csv_bytes, filter_rows, normalize_full_symbol, parse_csv, unique_instruments
from manual_chart_data import DISPLAY_PERIODS, load_chart_data, price_figure, PriceUnavailable, source_spec

PAGE_LABEL = '📌 6. 관심종목·매크로'
CATALOG_PATH = Path(__file__).resolve().parent / 'watchlists' / 'manual_watchlist.csv'
EDIT_URL = 'https://github.com/example-owner/trend-leader-watchlist/edit/main/watchlists/manual_watchlist.csv'


def chart_url(symbol):
    return 'https://www.tradingview.com/chart/?' + urlencode({'symbol':normalize_full_symbol(symbol)})


def widget_config(row, *, interval, theme, ma, period='6개월'):
    if interval not in ('D','W','M') or theme not in ('light','dark') or ma not in (0,20,50,200) or period not in DISPLAY_PERIODS:
        raise ValueError('지원하지 않는 차트 설정입니다.')
    if row['chart_style'] not in ('candles','line'):
        raise ValueError('지원하지 않는 차트 스타일입니다.')
    symbol=normalize_full_symbol(row['symbol'])
    months=DISPLAY_PERIODS[period]
    resolution={'D':'1D','W':'1W','M':'1M'}[interval]
    # Advanced Chart's `range` selects a different resolution automatically.
    # Symbol Overview documents an explicit "period|resolution" date range,
    # preserving the user's candle interval as well as candles, volume and MA.
    return dict(autosize=True, width='100%',height='100%',
        symbols=[[symbol,f'{symbol}|{months}M']],dateRanges=[f'{months}m|{resolution}'],
        locale='kr',colorTheme=theme,chartType='candlesticks' if row['chart_style']=='candles' else 'line',
        chartOnly=False,hideDateRanges=True,hideMarketStatus=False,scalePosition='right',scaleMode='Normal',
        showVolume=row['asset_type'] in ('stock','etf','future','crypto'),showMA=bool(ma),maLength=ma or 50,
        maLineColor='#2962FF',maLineWidth=1,changeMode='price-only')


def widget_html(row, *, interval, theme, ma, period='6개월'):
    # Only validated symbols and fixed control values enter executable widget JSON.
    config=json.dumps(widget_config(row,interval=interval,theme=theme,ma=ma,period=period),ensure_ascii=True)
    link=html.escape(chart_url(row['symbol']),quote=True)
    return ('<!doctype html><html><head><meta charset="utf-8"><style>'
        'html,body{margin:0;height:100%;overflow:hidden}.tradingview-widget-container{height:100%;width:100%}'
        '.tradingview-widget-container__widget{height:calc(100% - 28px);width:100%}'
        '.tradingview-widget-copyright{height:28px;font:12px Arial;text-align:center;padding-top:5px;box-sizing:border-box}'
        '</style></head><body><div class="tradingview-widget-container">'
        '<div class="tradingview-widget-container__widget"></div>'
        '<div class="tradingview-widget-copyright"><a href="'+link+'" target="_blank" rel="noopener noreferrer">'
        '<span>TradingView에서 차트 보기</span></a></div>'
        '<script type="text/javascript" src="https://s3.tradingview.com/external-embedding/embed-widget-symbol-overview.js" async>'
        +config+'</script></div></body></html>')


def _options(rows,field):
    return list(dict.fromkeys(row[field] for row in rows if row[field]))


def _reset_page():
    st.session_state['manual_page']=1


def _radio(label,options,key,*,default=None,format_func=None,reset_page=False,help=None):
    fallback=default if default in options else options[0]
    if key in st.session_state and st.session_state[key] not in options:
        st.session_state[key]=fallback
    kwargs={'format_func':format_func} if format_func is not None else {}
    if help is not None:kwargs['help']=help
    if reset_page:kwargs['on_change']=_reset_page
    index=None if key in st.session_state else options.index(fallback)
    return st.radio(label,options,index=index,horizontal=True,key=key,**kwargs)


def _group_label(value):
    return '전체' if value is None else value


def eod_reason(reason):
    labels={'MACRO_EOD_POLICY_NOT_CONFIGURED':'이 자산의 확정 일봉 수집 규칙 미연결',
        'MACRO_INDEX_DEFINITION_SESSION_UNVERIFIED':'지수 정의·확정 거래일 검증 대기',
        'MACRO_YIELD_DEFINITION_UNIT_UNVERIFIED':'금리 정의·단위 검증 대기',
        'MACRO_FX_QUOTE_SESSION_UNVERIFIED':'환율 표시 방향·일봉 마감 검증 대기',
        'MACRO_FUTURES_CONTRACT_ROLL_UNVERIFIED':'선물 계약·만기 교체 기준 검증 대기',
        'MACRO_SPOT_SOURCE_UNVERIFIED':'현물 원본 가격 검증 대기',
        'MACRO_CRYPTO_VENUE_UNVERIFIED':'지정 거래소 가격 검증 대기',
        'LISTING_VENUE_NOT_VERIFIED':'원본 거래소의 가격 출처 미검증',
        'CATALOG_CURRENCY_MISMATCH':'목록의 통화와 거래소 통화 불일치',
        'PROVIDER_SYMBOL_MISMATCH':'원본 종목과 공급자 심볼 불일치',
        'PROVIDER_ASSET_TYPE_MISMATCH':'공급자가 다른 자산 유형을 반환',
        'PROVIDER_CURRENCY_MISMATCH':'공급자 통화 불일치',
        'PROVIDER_EXCHANGE_MISMATCH':'공급자 거래소 불일치',
        'PROVIDER_TIMEZONE_MISMATCH':'공급자 거래소 시간대 불일치',
        'PROVIDER_RATE_LIMITED':'공급자 요청 제한 · 직접 조회 실패',
        'PROVIDER_RATE_LIMIT_CIRCUIT_OPEN':'공급자 요청 제한 · 회로 차단으로 미시도',
        'PROVIDER_ACCESS_DENIED':'공급자 접근 거부 · 직접 조회 실패',
        'PROVIDER_ACCESS_CIRCUIT_OPEN':'공급자 접근 거부 · 회로 차단으로 미시도',
        'PROVIDER_CONNECTION_FAILED':'공급자 연결 실패',
        'PROVIDER_CONNECTION_CIRCUIT_OPEN':'연결 장애 · 회로 차단으로 미시도',
        'PROVIDER_SYMBOL_NOT_FOUND':'공급자에서 원본 심볼 미제공',
        'COLLECTION_TIME_BUDGET_EXCEEDED':'수집 시간 한도 · 미시도',
        'CACHE_NOT_INSPECTED':'엔진 캐시 미확인',
        'CACHE_NOT_SUPPORTED':'해당 시장의 엔진 캐시 재사용 미지원',
        'CACHE_REUSABLE':'검증된 엔진 캐시',
        'CACHE_MISSING':'엔진 캐시 없음',
        'CACHE_IDENTITY_UNVERIFIED':'캐시 거래소·통화·자산 유형 검증 대기',
        'CACHE_ADJUSTMENT_MISMATCH':'캐시 가격 조정 기준 불일치',
        'CACHE_BEHIND_FINAL_SESSION':'캐시 가격 기준일 지연',
        'CACHE_REQUIRED_FIELDS_MISSING':'캐시 검증 필드 부족',
        'CACHE_PRICE_OR_SESSION_INVALID':'캐시 OHLCV·확정 거래일 검증 실패',
        'CACHE_UNREADABLE_OR_INVALID':'캐시 손상 또는 구조 오류',
        'CACHE_NO_FINAL_BARS':'캐시에 확정 일봉 없음',
        'PROVIDER_BEHIND_FINAL_SESSION':'최근 확정 거래일 가격 지연',
        'PROVIDER_BEHIND_PREVIOUS_SNAPSHOT':'공급자 가격이 기존 게시본보다 오래됨 · 이전 자료 유지',
        'PROVIDER_NO_NEW_FINAL_BARS':'새로운 확정 일봉 미확보',
        'CALENDAR_COVERAGE_UNVERIFIED':'거래일 일정 검토 범위 밖',
        'NO_NEW_FINAL_SESSION':'새 확정 거래일 없음',
        'INVALID_REAL_OHLC':'시가·고가·저가·종가 정합성 오류'}
    return labels.get(reason,'확정 가격 확인 실패 · 원본과 수집 작업 확인' if reason else '')


# Streamlit-native proportions. The manual mode uses a slider rather than
# injecting JavaScript or resizing Streamlit's private layout DOM.
PANEL_LAYOUTS={'10:90':10,'20:80':20,'30:70':30,'직접 조절':'custom'}
# The numeric field deliberately has no max_value: taller synchronized panes
# can be selected without a new fixed ceiling.
PANE_HEIGHT_OPTIONS={'600px':600,'900px':900,'1200px':1200,'1600px':1600,'직접 입력':'custom'}
MANUAL_DEFAULTS={'manual_region':None,'manual_scope':'주식·ETF','manual_sector':None,
    'manual_sector_query':'','manual_query':'','manual_cols':2,'manual_size':12,
    'manual_volume':True,'manual_bars':130,'manual_page':1,'manual_layout':'20:80',
    'manual_custom_width':20,'manual_pane_height_mode':'900px','manual_pane_height_custom':1200,
    'manual_panel_open':True}


def _keep_manual_state():
    # Streamlit's supported widget-cleanup interruption: save values under their
    # own keys before widgets are rendered. Hidden panel controls retain filters
    # through fragment/full reruns, without inspecting or mutating browser DOM.
    for key,default in MANUAL_DEFAULTS.items():
        st.session_state[key]=st.session_state.get(key,default)


def _select_sector(sector):
    st.session_state['manual_sector']=sector
    _reset_page()


def _reset_layout():
    st.session_state['manual_layout']='20:80'
    st.session_state['manual_custom_width']=20
    st.session_state['manual_pane_height_mode']='900px'
    st.session_state['manual_pane_height_custom']=1200
    st.session_state['manual_panel_open']=True


def sector_button_key(sector):
    return 'manual_sector_btn_'+sha256(json.dumps(sector,ensure_ascii=False).encode()).hexdigest()


def sector_counts(rows):
    """Unique symbols per membership, preserving the manual CSV order."""
    all_symbols=set();groups={}
    for row in rows:
        all_symbols.add(row['symbol'])
        groups.setdefault(row['watch_sector'],set()).add(row['symbol'])
    return [(None,len(all_symbols))]+[(name,len(symbols)) for name,symbols in groups.items()]


def sector_label(sector,count):
    if sector is None:return f'전체 섹터 · {count}'
    # Truncate only our own label, never the identity/filter. Full text is native
    # button help, accessible by mouse/keyboard; no CSS targeting widget internals.
    units=0;short=[]
    for char in sector:
        units+=2 if unicodedata.east_asian_width(char) in ('W','F') else 1
        if units>18:short.append('…');break
        short.append(char)
    return ''.join(short)+f' · {count}'


def _render_sector_panel(enabled,regional,*,border=True):
    with st.container(border=border):
        st.markdown('**섹터 탐색**')
        st.selectbox('국가·지역',[None]+_options(enabled,'watch_region'),format_func=_group_label,
            key='manual_region',on_change=_reset_page,placeholder='전체')
        st.selectbox('관찰 범위',['주식·ETF','매크로','전체'],key='manual_scope',on_change=_reset_page)
        search=st.text_input('섹터 검색',key='manual_sector_query',placeholder='섹터 이름')
        counts=sector_counts(regional);selected=st.session_state['manual_sector']
        matching=[(name,count) for name,count in counts if name is None or search.strip().casefold() in name.casefold() or name==selected]
        if selected is not None and search.strip() and search.strip().casefold() not in selected.casefold():
            st.caption('선택한 섹터는 검색과 관계없이 유지합니다.')
        for name,count in matching:
            st.button(sector_label(name,count),key=sector_button_key(name),width='stretch',
                type='primary' if name==selected else 'secondary',
                help=f'{name if name is not None else "전체 섹터"} · 고유 심볼 {count}개',
                on_click=_select_sector,args=(name,))
        st.caption('국가·범위 기준 고유 심볼 수 · CSV 순서')


def _render_chart_area(regional,snapshot,state,theme):
    from manual_chart_data import EOD_PERIODS,cached_eod_json
    from manual_eod_contract import digest,canonical
    import plotly.graph_objects as go
    sector=st.session_state['manual_sector']
    st.caption(' / '.join((_group_label(st.session_state['manual_region']),st.session_state['manual_scope'],sector or '전체 섹터')))
    query=st.text_input('종목명·심볼 검색',key='manual_query',on_change=_reset_page)
    selected=unique_instruments(filter_rows(regional,sectors=() if sector is None else (sector,),query=query))
    entries=snapshot.get('instruments',{})
    valid=[];missing=[]
    for row in selected:
        entry=entries.get(row['symbol'])
        if entry and (entry.get('bars') or entry.get('close_only')):
            valid.append((row,entry))
        else:missing.append(dict(종목=row['name'],심볼=row['symbol'],상태=(entry or {}).get('status','게시 대기'),
            사유=eod_reason((entry or {}).get('reason','')) or '다음 EOD 게시 대기',
            캐시진단=eod_reason(((entry or {}).get('cache_diagnostic') or {}).get('reason','')),
            마지막확정일=(entry or {}).get('last_final_session') or '없음',원본=chart_url(row['symbol'])))
    partial_only=sum(bool(entry.get('close_only') and (not entry.get('bars')
        or entry['close_only']['last_final_session']>entry.get('last_final_session','')))
        for _,entry in valid)
    st.caption(f'필터 결과 {len(selected)}개 고유 심볼 · 표시 가능 {len(valid)}개'
        f' (종가 전용 {partial_only}개) · 미제공 {len(missing)}개')
    with st.container(border=True):
        # The chart pane can now be narrow; wrap controls instead of squeezing
        # three nested columns into the smaller right-hand side.
        controls=st.grid(3,min_column_width=210,key='manual_chart_controls')
        c1,c2,c3=controls.cell(),controls.cell(),controls.cell()
        with c1:ncols=_radio('한 줄당 차트 수',[1,2,3,4],'manual_cols',default=2,
            help='선택한 열 수는 최대값입니다. 좁은 영역에서는 차트가 잘리지 않도록 자동 줄바꿈합니다.')
        if st.session_state.get('manual_size',12) not in (8,12,24,60):st.session_state['manual_size']=12
        page_size=c2.selectbox('한 페이지 표시',[8,12,24,60],index=None,key='manual_size',on_change=_reset_page)
        show_volume=c3.checkbox('거래량',key='manual_volume')
        bars=_radio('표시 기간 (거래일)',list(EOD_PERIODS),'manual_bars',default=130,format_func=lambda n:f'{n}봉')
    if missing:
        with st.expander(f'미지원·수집 대기 {len(missing)}개 확인',expanded=not valid):
            st.dataframe(missing,hide_index=True,width='stretch',column_config={'원본':st.column_config.LinkColumn('원본 차트',display_text='TradingView')})
    if not valid:
        st.session_state['manual_page']=1
        if not selected:st.info('현재 필터에 맞는 관심 종목이 없습니다.')
        return
    pages=max(1,math.ceil(len(valid)/page_size));old=st.session_state.get('manual_page',1)
    if type(old) is not int or not 1<=old<=pages:st.session_state['manual_page']=1
    _,pager=st.columns([2,1])
    page=pager.number_input(f'페이지 이동 (1 ~ {pages})',min_value=1,max_value=pages,step=1,key='manual_page')
    first=(page-1)*page_size;visible=valid[first:first+page_size]
    st.caption(f'차트 {len(valid)}개 중 {first+1}~{first+len(visible)}번째 · MA20 파랑 / MA50 주황 / MA200 보라')
    # CSS styles only HTML owned by this module. Streamlit layout/widgets and
    # Plotly sizing use supported native APIs; no JS or internal DOM selectors.
    st.markdown("""<style>
.manual-eod-title{height:2.8em;line-height:1.4em;font-weight:600;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.manual-eod-group{height:1.5em;line-height:1.5em;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.8rem;opacity:.8}
</style>""",unsafe_allow_html=True)
    labels={'current':'게시 기준 정상','stale':'가격 지연','retained':'이전 정상값 · 수집 실패','calendar_unverified':'일정 미검증'}
    cards=st.grid(ncols,min_column_width=240,key='manual_eod_cards')
    for row,entry in visible:
        with cards.cell():
            with st.container(border=True):
                title=html.escape(row['name']+' · '+row['symbol'],quote=True)
                group=html.escape(' / '.join(row[k] for k in ('watch_region','watch_sector','watch_subsector') if row[k])+' · '+str(entry.get('currency','')),quote=True)
                status='이전 게시 · 갱신 대기' if state in ('pending','previous') else labels.get(entry.get('status'),str(entry.get('status','확인 불가')))
                partial=entry.get('close_only') or {}
                close_mode=bool(partial and (not entry.get('bars')
                    or partial['last_final_session']>entry.get('last_final_session','')))
                if close_mode:
                    status=('지수 확정 종가 ' if row['asset_type']=='index' else
                        '종가 전용 ')+partial['last_final_session']+(
                        ' · 포인트 선 차트' if row['asset_type']=='index' else ' · OHLC 미완전')
                else:
                    status='확정 '+str(entry.get('last_final_session','미확인'))+' · '+status
                status=html.escape(status,quote=True)
                summary,source_link=st.columns([3,2],gap='small',vertical_alignment='top')
                with summary:
                    st.markdown(f'<div class="manual-eod-title" title="{title}">{title}</div>'
                        f'<div class="manual-eod-group" title="{group}">{group}</div>'
                        f'<div class="manual-eod-group" title="{status}">{status}</div>',unsafe_allow_html=True)
                with source_link:
                    st.link_button('원본 차트',chart_url(row['symbol']),help='TradingView에서 원본 차트 열기')
                try:
                    price_hash=digest(canonical(entry))
                    spec,missing_ma=cached_eod_json(snapshot['run_id'],row['symbol'],price_hash,bars,ncols,theme,show_volume,entry)
                    st.plotly_chart(go.Figure(json.loads(spec)),width='stretch',theme=None,
                        config={'responsive':True,'displaylogo':False},key='manual_eod_chart_'+row['symbol'])
                    notes=[]
                    shown=partial if close_mode else entry['bars']
                    coverage=(partial.get('history_coverage') if close_mode else
                        entry.get('history_coverage')) or {}
                    if close_mode:
                        notes.append('지수 포인트 · 종가 선 차트 (주식 캔들 아님)' if row['asset_type']=='index'
                            else '종가만 제공 · 캔들/거래량/고저가 분석 불가')
                    if missing_ma:notes.append('MA '+ '/'.join(map(str,missing_ma))+' 이력 부족')
                    if len(shown['d'])<bars:notes.append(f"확보 {len(shown['d'])}봉")
                    if coverage.get('missing_between_first_last'):
                        notes.append(f"중간 누락 {coverage['missing_between_first_last']}거래일 · MA는 유효 봉 기준")
                    elif not close_mode and entry.get('missing_sessions'):
                        notes.append(f"중간 누락 {entry['missing_sessions']}거래일 · MA는 유효 봉 기준")
                    if coverage.get('trailing_missing_sessions'):
                        notes.append(f"미확보 최신 거래일 {coverage['trailing_missing_sessions']}일")
                    if notes:st.caption(' · '.join(notes))
                except Exception:st.warning('이 종목 차트를 그리지 못했습니다. 다른 종목과 원본 링크는 계속 확인할 수 있습니다.')
                with st.expander('메모 · 가격 출처·갱신 상세'):
                    if row['note']:st.text(row['note'])
                    if entry.get('reason'):
                        st.caption('상태 사유: '+eod_reason(entry.get('reason','')))
                        st.caption('관리용 코드: '+entry.get('reason',''))
                    st.caption('마지막 가격 확인: '+str(entry.get('last_success_at','')))
                    st.caption('예상 확정 거래일: '+str(entry.get('expected_session','미검증')))
                    st.caption(str(entry.get('source',''))+' · '+str(entry.get('source_symbol',''))+' · '+str(entry.get('currency','')))
                    st.caption(str(entry.get('adjustment','')))
                    if close_mode:
                        st.caption('별도 검증 NBI 지수 수준 · 주식 전략 데이터와 분리' if row['asset_type']=='index'
                            else '별도 검증 종가 전용 · OHLC 캔들과 조정 가격을 혼합하지 않습니다.')
                        st.caption(str(partial.get('source',''))+' · '+str(partial.get('source_symbol','')))
                        st.caption(str(partial.get('adjustment',''))+
                            ' · 종가 기준 '+str(partial.get('last_final_session','')))
                    if entry.get('history_coverage'):
                        h=entry['history_coverage']
                        st.caption('OHLC 거래일 누락: '+str(h.get('missing_between_first_last',0))+
                            ' · 최근 사례 '+', '.join(h.get('recent_missing_examples',[])))


@st.fragment
def render_manual_watchlist(path=CATALOG_PATH, *, theme='light', snapshot=None, source_state=None):
    from manual_chart_data import EOD_PERIODS,cached_eod_json
    from manual_eod_contract import digest,canonical
    import plotly.graph_objects as go
    _keep_manual_state()
    snapshot=snapshot or {};source_state=source_state or {'status':'absent'}
    st.subheader('📌 관심종목 · 확정 일봉 멀티차트')
    st.caption('수동 관찰 목록 · 워치리스트 성공 게시와 연계한 EOD 자료 · 자동 추천이나 매매 신호를 만들지 않습니다.')
    try:
        raw=Path(path).read_bytes();rows=parse_csv(raw)
    except (OSError,ValueError):
        st.error('관심목록 CSV를 읽지 못했습니다. 파일 형식과 중복 분류를 확인하세요.')
        st.link_button('GitHub에서 목록 확인',EDIT_URL);return
    with st.expander('목록 관리 · 출처·확정 기준'):
        st.markdown('`watchlists/manual_watchlist.csv`에서 추가·삭제하거나 `enabled=false`로 숨깁니다. '
            '사용자 분류와 순서를 보존하며 자동 워치리스트에는 합치지 않습니다.')
        st.link_button('GitHub에서 목록 편집',EDIT_URL)
        st.download_button('전체 관리 CSV 다운로드',csv_bytes(rows),file_name='manual_watchlist.csv',mime='text/csv',key='manual_download')
        st.caption('앱은 게시 파일만 읽습니다. 페이지·열 수·검색·기간 변경으로 가격을 재수집하지 않습니다. '
            '마감·휴장·조기 폐장·시간대와 수집 시점의 증거를 검사한 실제 OHLC 또는 별도 검증한 종가 전용 선차트만 표시하며, 누락 봉은 보간하지 않습니다.')
        if snapshot:
            st.caption('연계 run_id: '+snapshot.get('core_run_id','')+' · 가격 cutoff: '+snapshot.get('cutoff_at',''))
        st.caption('공급자 미지원 또는 동일 자산 확인 실패는 원본 링크만 제공합니다. TradingView 외부 위젯은 EOD 화면에 삽입하지 않습니다.')
    state=source_state.get('status')
    if not snapshot:
        if state == 'unconfigured':
            st.warning('EOD 데이터 소스가 설정되지 않았습니다. 앱의 DATA_DIR 또는 DATA_REPO · GITHUB_TOKEN 설정을 확인하세요.')
        elif state == 'unavailable':
            st.warning('관심종목 EOD 게시본을 읽거나 검증하지 못했습니다. 엔진 게시 파일과 앱의 데이터 읽기 권한을 확인하세요.')
        else:
            st.info('관심종목 EOD 게시 파일을 찾지 못했습니다. 첫 게시 대기 또는 게시 경로·읽기 권한 문제일 수 있습니다.')
        if source_state.get('reason'):st.caption(source_state['reason'])
        with st.expander('EOD 게시 복구 안내'):
            st.markdown('엔진의 **Manual watchlist EOD snapshot** 작업 로그를 확인하세요. '
                '`CATALOG_READ_TOKEN_NOT_CONFIGURED`이면 **trading-journal-data** 저장소의 '
                'Settings → Secrets and variables → Actions에 `MANUAL_CATALOG_READ_TOKEN`을 등록하세요. '
                '토큰 대상은 **trend-leader-watchlist**, 권한은 **Contents: read**입니다. '
                '등록 후 다음 정상 watchlist 작업이 완료되면 EOD 게시가 자동 실행됩니다.')
            st.link_button('EOD 작업 확인', 'https://github.com/example-owner/trading-journal-data/actions/workflows/manual_watchlist_eod.yml')
            st.caption('앱의 관심목록 CSV는 로컬 파일입니다. 엔진의 CSV 조회 토큰과 앱의 엔진 게시본 읽기 권한은 서로 다릅니다. '
                '게시 후 상단 새로고침을 누르세요. 지수·금리·환율·선물 등 매크로 자산은 현재 EOD 수집 미지원으로 원본 링크만 제공합니다.')
    elif state in ('previous','pending'):
        st.warning('이전 정상 EOD 자료를 표시합니다. 새 워치리스트 게시와의 연계 갱신이 대기 중이거나 최신 파일을 확인하지 못했습니다.')
    if snapshot:
        from datetime import datetime,timezone
        from manual_eod_contract import aware
        generated=aware(snapshot['generated_at'])
        hours=max(0,int((datetime.now(timezone.utc)-generated).total_seconds()/3600))
        st.caption('EOD 게시 '+snapshot.get('generated_at','')+f' · 게시 후 {hours}시간 경과 · 가격 거래일은 종목별 표시')
        if digest(raw)!=snapshot.get('catalog_sha256'):
            st.warning('관심목록이 EOD 게시 이후 변경되었습니다. 기존 자료는 심볼별로 유지하며 새 종목은 다음 게시를 기다립니다.')
    enabled=filter_rows(rows)
    regions=[None]+_options(enabled,'watch_region')
    if st.session_state['manual_region'] not in regions:
        st.session_state['manual_region']=None;_reset_page()
    if st.session_state['manual_scope'] not in ('주식·ETF','매크로','전체'):
        st.session_state['manual_scope']='주식·ETF';_reset_page()
    region=st.session_state['manual_region'];scope=st.session_state['manual_scope']
    regional=filter_rows(enabled,regions=() if region is None else (region,),asset_scope={'주식·ETF':'stocks','매크로':'macro','전체':'all'}[scope])
    if st.session_state['manual_sector'] not in [name for name,_ in sector_counts(regional)]:
        st.session_state['manual_sector']=None;_reset_page()
    if st.session_state['manual_layout'] not in PANEL_LAYOUTS:
        st.session_state['manual_layout']='20:80'
    if type(st.session_state['manual_custom_width']) is not int or not 1<=st.session_state['manual_custom_width']<=50:
        st.session_state['manual_custom_width']=20
    if st.session_state['manual_pane_height_mode'] not in PANE_HEIGHT_OPTIONS:
        st.session_state['manual_pane_height_mode']='900px'
    if (type(st.session_state['manual_pane_height_custom']) is not int or
            st.session_state['manual_pane_height_custom']<320):
        st.session_state['manual_pane_height_custom']=1200
    with st.expander('섹터 패널 · 화면 배치'):
        toggle,reset=st.columns([3,1],vertical_alignment='bottom')
        with toggle:st.checkbox('섹터 탐색 패널 표시',key='manual_panel_open')
        with reset:st.button('초기화',key='manual_layout_reset',on_click=_reset_layout)
        ratio_col,height_col=st.columns(2,gap='small',vertical_alignment='top')
        with ratio_col:
            st.selectbox('좌우 비율',list(PANEL_LAYOUTS),key='manual_layout',
                disabled=not st.session_state['manual_panel_open'],
                help='섹터:멀티차트. 프리셋 10:90 / 20:80 / 30:70, 직접 조절 1~50%.')
            if st.session_state['manual_panel_open'] and st.session_state['manual_layout']=='직접 조절':
                pct=st.slider('섹터 너비 (%)',min_value=1,max_value=50,step=1,key='manual_custom_width')
                st.caption(f'{pct}:{100-pct} · 10% 미만은 탐색 패널이 매우 좁습니다.')
        with height_col:
            st.selectbox('양쪽 패널 스크롤 높이',list(PANE_HEIGHT_OPTIONS),key='manual_pane_height_mode',
                disabled=not st.session_state['manual_panel_open'],
                help='섹터 탐색과 멀티차트에 같은 높이를 적용하며, 각 영역은 별도로 스크롤됩니다.')
            if st.session_state['manual_panel_open'] and st.session_state['manual_pane_height_mode']=='직접 입력':
                st.number_input('높이 직접 입력 (px)',min_value=320,step=100,key='manual_pane_height_custom',
                    help='최대 높이를 고정하지 않았습니다. 화면 높이에 맞춰 입력하세요.')
    if not st.session_state['manual_panel_open']:
        _render_chart_area(regional,snapshot,state,theme)
    else:
        configured=PANEL_LAYOUTS[st.session_state['manual_layout']]
        pct=st.session_state['manual_custom_width'] if configured=='custom' else configured
        height_setting=PANE_HEIGHT_OPTIONS[st.session_state['manual_pane_height_mode']]
        pane_height=(st.session_state['manual_pane_height_custom'] if height_setting=='custom'
            else height_setting)
        panel,charts=st.columns([pct,100-pct],gap='small',vertical_alignment='top')
        with panel:
            with st.container(height=pane_height,border=True,key='manual_sector_scroll'):
                _render_sector_panel(enabled,regional,border=False)
        with charts:
            with st.container(height=pane_height,border=True,key='manual_chart_scroll'):
                _render_chart_area(regional,snapshot,state,theme)


