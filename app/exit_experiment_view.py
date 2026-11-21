"""Read-only optional exit research. No price access or backtest in Streamlit."""
from __future__ import annotations
import json
import math
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st
from exit_experiment_contract import VERSION,SCHEMA,RULE_IDS,canonical,digest,json_doc,manifest_path,validate_manifest,validate_payload,unpack

LABELS={'HOLD':'HOLD · 계속 보유','FIXED':'FIXED · 고정 손절','TARGET_2R':'TARGET_2R · +2R',
    'MA20_2C':'MA20_2C · 20일선','MA50_2C':'MA50_2C · 50일선','ATR_TRAIL':'ATR_TRAIL · Wilder ATR','DONCHIAN_10':'DONCHIAN_10 · 10일 저점'}
COLORS={'HOLD':'#64748B','FIXED':'#DC2626','TARGET_2R':'#D97706','MA20_2C':'#2563EB','MA50_2C':'#7C3AED','ATR_TRAIL':'#0891B2','DONCHIAN_10':'#059669'}
REASONS={'horizon_close':'만기 종가','next_open_ma':'MA 확인 다음 시가','gap_initial_stop':'초기손절 갭시가','touch_initial_stop':'초기손절 도달','gap_target_2r':'목표 초과 갭시가','touch_target_2r':'+2R 도달','gap_atr_stop':'ATR선 갭시가','touch_atr_stop':'ATR선 도달','gap_donchian_stop':'채널 갭시가','touch_donchian_stop':'채널 하한 도달'}
SYMBOLS={'HOLD':'circle','FIXED':'x','TARGET_2R':'diamond','MA20_2C':'triangle-down','MA50_2C':'square','ATR_TRAIL':'star','DONCHIAN_10':'cross'}


@st.cache_data(ttl=300,max_entries=2,show_spinner=False)
def load_exit_publication(source_key,core_run_id,_read):
    del source_key
    errors=[];root='exit_experiments/'+VERSION
    for name in ('latest.json','previous.json'):
        try:
            raw=_read(root+'/'+name)
            if raw is None:continue
            if len(raw)>10000:raise ValueError('POINTER_TOO_LARGE')
            pointer=json_doc(raw);rel=manifest_path(pointer);man_raw=_read(rel)
            if not man_raw or len(man_raw)>4_000_000:raise ValueError('MANIFEST_MISSING_OR_LARGE')
            man=json_doc(man_raw);validate_manifest(man,pointer,man_raw)
            registry_raw=_read(root+'/registry.json')
            if not registry_raw or len(registry_raw)>100000:raise ValueError('REGISTRY_MISSING')
            registry=json_doc(registry_raw)
            if digest(canonical(registry))!=man['registry_sha256'] or registry.get('experiment_config_hash')!=man['experiment_config_hash']:raise ValueError('REGISTRY_CHANGED')
            directory=rel.rsplit('/',1)[0]
            data={file:unpack(_read(directory+'/'+file),info) for file,info in man['files'].items()};validate_payload(man,data)
            paths=data['paths.json.gz']
            # Archived raw input evidence is verified on the publisher. It is not
            # needed for charts and is not retained in the app's memory cache.
            for path in paths.values():path.pop('input_evidence',None)
            return {'manifest':man,'registry':registry,'results':data['results.json.gz'],'paths':paths,
                'statistics':data['statistics.json.gz'],'pairs':data['pairs.json.gz']}, {
                'status':'previous' if name=='previous.json' else 'ready' if man['source_run_id']==core_run_id else 'pending',
                'reason':'이전 정상 연구 게시본' if errors else ''}
        except Exception:errors.append(name+' 검증/조회 실패')
    return {},{'status':'unavailable' if errors else 'absent','reason':' · '.join(errors)}


def number(value,suffix='',digits=2):
    return '—' if value is None or not isinstance(value,(int,float)) or not math.isfinite(value) else f'{value:.{digits}f}'+suffix


def style(fig,theme,height=360):
    dark=theme=='dark';text='#CBD5E1' if dark else '#334155';bg='#0F172A' if dark else '#FFFFFF'
    fig.update_layout(template='plotly_dark' if dark else 'plotly_white',height=height,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor=bg,
        font=dict(color=text,size=11),margin=dict(l=15,r=15,t=45,b=45),legend=dict(orientation='h',y=-.25),
        hoverlabel=dict(bgcolor=bg,font=dict(color=text)))
    return fig


def comparison_figures(summary,pairs,theme='light',pairwise=False):
    rows=summary['policies'];period=summary['horizon'];fig=go.Figure()
    if pairwise:
        rows=[next((r for r in summary['pairwise'] if r['rule_id']==rule and r['baseline']=='FIXED'),{'rule_id':rule,'n':0,'mean_net_R':None,'median_net_R':None}) for rule in RULE_IDS]
    for field,name,color in [('mean_net_R','평균 순R','#2563EB'),('median_net_R','중앙값 순R','#D97706')]:
        fig.add_bar(x=[LABELS[r['rule_id']] for r in rows],y=[r[field] for r in rows],name=name,marker_color=color,
            customdata=[r['n'] for r in rows],hovertemplate='%{x}<br>'+name+' %{y:.2f}R<br>공통 n=%{customdata}<extra></extra>')
    fig.update_layout(barmode='group',title=f'{period}거래일 · '+('쌍별 표본 · 전략별 n 상이' if pairwise else f'7개 공통 n={summary["common_complete_n"]}'),yaxis_title='순 R · 초기 위험 동일')
    dist=go.Figure()
    for rule in RULE_IDS:
        if rule=='FIXED':continue
        selected=[r for r in pairs if r['rule_id']==rule and r['baseline']=='FIXED' and (pairwise or r['seven_common'])]
        if selected:dist.add_histogram(x=[r['delta_net_R'] for r in selected],name=f'{rule} · n={len(selected)}',marker_color=COLORS[rule],opacity=.55,
            hovertemplate=rule+'<br>ΔR %{x:.2f}<br>건수 %{y}<extra></extra>')
    dist.update_layout(barmode='overlay',title=f'{period}거래일 · 동일 신호 FIXED 대비',xaxis_title='Δ 순R',yaxis_title='신호 수')
    dist.add_vline(x=0,line_dash='dash',line_color='#64748B')
    return style(fig,theme),style(dist,theme)


def chart_anchor(rows):
    return next((r for r in rows if r.get('eligibility_status')=='eligible'
                 and r.get('entry_date') and r.get('entry_price') is not None
                 and r.get('horizon_end')),None)


def signal_figure(path,rows,selected,theme='light'):
    if not 1<=len(selected)<=3:raise ValueError('SELECT_ONE_TO_THREE_POLICIES')
    available={r['rule_id']:r for r in rows};anchor=chart_anchor(rows)
    if anchor is None:raise ValueError('UNVERIFIED_SIGNAL_CHART')
    end=anchor['horizon_end']
    indices=[i for i,d in enumerate(path['d']) if end is None or d<=end]
    fig=go.Figure();d=[path['d'][i] for i in indices]
    def series(key):return [path.get(key,[None]*len(path['d']))[i] for i in indices]
    fig.add_candlestick(x=d,open=series('o'),high=series('h'),low=series('l'),close=series('c'),name='원본 OHLC',
        increasing_line_color='#E11D48',decreasing_line_color='#10B981')
    for key,label,color in [('ma20','MA20','#2563EB'),('ma50','MA50','#D97706')]:
        fig.add_scatter(x=d,y=series(key),name=label,mode='lines',line=dict(color=color,width=1.2))
    entry=anchor.get('entry_price');date=anchor.get('entry_date');stop=anchor.get('initial_stop')
    if entry is not None:fig.add_scatter(x=[date],y=[entry],name='공통 진입 시가',mode='markers',marker=dict(symbol='triangle-up',size=12,color='#16A34A'))
    if stop is not None and date:
        held=[day for day in d if day>=date];fig.add_scatter(x=held,y=[stop]*len(held),name='최초 고정 손절',mode='lines',line=dict(color='#DC2626',dash='dash',width=1))
    for rule in selected:
        row=available[rule];color=COLORS[rule];logs=path.get('effective_stops',{}).get(row.get('stop_path_ref'),[])[:row.get('stop_path_length',0)]
        if logs:fig.add_scatter(x=[x['effective_date'] for x in logs],y=[x['active_stop'] for x in logs],mode='lines',
            name=rule+' · 당일 유효 스톱',line=dict(color=color,width=2,shape='hv'),customdata=[x['information_as_of'] for x in logs],
            hovertemplate=rule+'<br>유효일 %{x}<br>손절선 %{y}<br>정보 기준일 %{customdata}<extra></extra>')
        if rule=='DONCHIAN_10' and row.get('eligibility_status')=='eligible':
            until=row.get('exit_fill_date') or end;channel=[i for i in indices if path['d'][i]>=date and (until is None or path['d'][i]<=until)]
            fig.add_scatter(x=[path['d'][i] for i in channel],y=[path['donchian10'][i] for i in channel],mode='lines',name='DONCHIAN_10 · 직전10일 하한',line=dict(color=color,dash='dot'))
        if row.get('exit_price') is not None:
            fig.add_scatter(x=[row['exit_fill_date']],y=[row['exit_price']],mode='markers',name=rule+' · '+REASONS.get(row['exit_reason'],row['exit_reason']),
                marker=dict(color=color,symbol=SYMBOLS[rule],size=13,line=dict(width=1,color=color)),
                hovertemplate=rule+'<br>체결일 %{x}<br>모의 가격 %{y}<extra></extra>')
    fig.update_xaxes(type='category',rangeslider_visible=False,nticks=6);fig.update_yaxes(title_text='동일 조정 기준 가격')
    return style(fig,theme,540)


@st.fragment
def render_exit_experiments(doc=None,source_state=None,theme='light'):
    st.subheader('청산 전략 비교 · 연구용 모의실험')
    if not doc:
        st.caption('연구 게시본이 아직 없거나 확인되지 않았습니다. 기존 공식 검증과 120일 관찰은 위에서 계속 확인할 수 있습니다.')
        if source_state and source_state.get('status')=='unavailable':st.caption(source_state.get('reason',''))
        return
    if (source_state or {}).get('status') in ('pending','previous'):st.info('이전 정상 연구 게시본입니다. 최신 핵심 게시와의 연계 갱신을 기다립니다.')
    man=doc['manifest'];a,b,c=st.columns([1,1.1,1])
    horizon=a.radio('연구 만기 (거래일)',[20,60,120],horizontal=True,key='exit_horizon')
    vintage_label=b.selectbox('연구 자료 구분',['전향적','회고적','전체'],key='exit_vintage',help='최초 연구 게시에 고정한 규칙 확정 시각 이후 생성·게시된 신호가 전향적입니다.')
    vintage={'전향적':'prospective','회고적':'retrospective','전체':'all'}[vintage_label]
    market=c.radio('연구 시장',['ALL','KR','US'],horizontal=True,key='exit_market')
    scope=next((s for s in doc['statistics'] if s['vintage']==vintage and s['market']==market),None)
    if scope is None:st.warning('선택 범위의 검증된 연구 집계가 없습니다.');return
    summary=next(s for s in scope['horizons'] if s['horizon']==horizon)
    if summary['common_complete_n']<20:st.warning(f"공통 완료 {summary['common_complete_n']}건 · 20건 미만: 전략 우열 판단 보류. 쌍별 표본도 같은 순위로 해석하지 않습니다.")
    if vintage=='all':st.caption('회고적·전향적 자료 혼합 범위입니다. 독립적인 전향 검증으로 해석하지 않습니다.')
    price_dates=man.get('price_as_of_by_market',{})
    price_label=' / '.join(k+' '+str(v) for k,v in price_dates.items() if v and (market=='ALL' or k==market)) or '미확인'
    controls=st.columns(6)
    for col,label,value in zip(controls,['대상 신호','공통 완료','진행·만기 대기','적격 제외','가격 확인일','규칙 버전'],
        [summary['signals'],summary['common_complete_n'],summary['pending_n'],summary['excluded_n'],price_label,man['rule_version']]):
        if label in ('가격 확인일','규칙 버전'):
            col.caption(label);col.markdown('**'+str(value).replace(' / ','**  \n**')+'**')
        else:col.metric(label,str(value))
    st.caption('전향적 시작 '+man['prospective_start_at']+' · 평가 cutoff '+man['evaluation_cutoff_at']+' · 가격 거래일은 종목별 별도 표시 · 계좌 수익률·자동 추천 아님')
    cohort=st.radio('비교 표본',['7개 전체 공통','FIXED와 쌍별 · 참고'],horizontal=True,key='exit_cohort');pairwise=cohort!='7개 전체 공통'
    if pairwise:st.caption('각 행의 공통 신호가 다릅니다. FIXED 전체 한 집합으로 비교하거나 전략 순위로 해석하지 않습니다.')
    table=[]
    for rule in RULE_IDS:
        row=next(r for r in summary['policies'] if r['rule_id']==rule)
        delta=row['delta_R_vs_FIXED']
        if pairwise and rule!='FIXED':
            row=next(r for r in summary['pairwise'] if r['rule_id']==rule and r['baseline']=='FIXED');delta=row['delta_mean_R']
        if pairwise and rule=='FIXED':row={'n':None};delta=None
        table.append({'전략':LABELS[rule],'공통 적격 n':row.get('n'),'평균 수익률':number(row.get('mean_net_return_pct'),'%'),
            '평균 R':number(row.get('mean_net_R'),'R'),'중앙값 R':number(row.get('median_net_R'),'R'),
            '승률':number(row.get('win_rate_pct'),'%'),'PF · 순R':number(row.get('profit_factor')),
            '평균 보유일':number(row.get('mean_holding_sessions'),'일',1),
            '평균 이익 반납률':number(row.get('mean_profit_giveback_pct'),'%')+f" (n={row.get('profit_giveback_pct_n',0)})",
            'FIXED 대비 ΔR':number(delta,'R')})
    st.dataframe(table,hide_index=True,width='stretch')
    st.caption('PF는 동일 초기 위험 1R 단위의 순손익 기준입니다. 반납률·정확한 MFE/MAE는 관측 가능한 표본만 사용하며 n이 다를 수 있습니다. 반납률은 보유 중 최고 순종가이익 대비: 음수는 그 이상에 청산, 100% 초과는 이익을 모두 반납하고 손실로 청산한 경우입니다.')
    chosen=[r for r in doc['results'] if r['horizon']==horizon and (vintage=='all' or r['vintage']==vintage) and (market=='ALL' or r['market']==market)]
    pairs=[r for r in doc['pairs'] if r['horizon']==horizon and (vintage=='all' or r['vintage']==vintage) and (market=='ALL' or r['market']==market)]
    if summary['common_complete_n'] or pairwise and pairs:
        first,second=comparison_figures(summary,pairs,theme,pairwise)
        left,right=st.columns(2);left.plotly_chart(first,width='stretch',theme=None,key='exit_mean_median');right.plotly_chart(second,width='stretch',theme=None,key='exit_delta_distribution')
    with st.expander('상세 분포 · 청산 원인 · 시장·국면 · 불확실성',expanded=False):
        detail=st.selectbox('연구 상세 항목',['보유 기간','이익 반납률','청산 원인','시장·국면','개별 신호 성과'],key='exit_detail')
        common_ids={r['signal_id'] for r in pairs if r['seven_common']}
        use=[r for r in chosen if r['signal_id'] in common_ids and r.get('comparison_eligible')]
        fig=go.Figure()
        if detail in ('보유 기간','이익 반납률','개별 신호 성과'):
            field={'보유 기간':'holding_sessions','이익 반납률':'profit_giveback_pct','개별 신호 성과':'net_R'}[detail]
            for rule in RULE_IDS:
                values=[r[field] for r in use if r['rule_id']==rule and r.get(field) is not None]
                if values:fig.add_box(y=values,name=rule,marker_color=COLORS[rule],boxpoints='all')
            fig.update_layout(title=f'{horizon}거래일 공통 n={summary["common_complete_n"]} · '+detail)
        elif detail=='청산 원인':
            for row in summary['policies']:
                for cause,n in row['exit_reasons'].items():fig.add_bar(x=[row['rule_id']],y=[n],name=row['rule_id']+' · '+cause)
            fig.update_layout(title=f'{horizon}거래일 · 청산 원인',barmode='stack')
        else:
            st.dataframe([{'구분':x['dimension'],'집단':x['value'],'공통n':x['n'],'전략':r['rule_id'],'평균순R':r['mean_net_R']} for x in summary['conditional_groups'] for r in x['policies']],hide_index=True,width='stretch')
            st.caption('KR/US 비교는 상단 시장 선택으로도 확인합니다. 집단별 표본 수와 구성 차이가 있습니다.')
        if fig.data:st.plotly_chart(style(fig,theme),width='stretch',theme=None,key='exit_detail_plot')
        st.dataframe([{'전략':r['rule_id'],'기준':r['baseline'],'쌍별n':r['n'],'평균ΔR':r['delta_mean_R'],
            '구간하한':r['uncertainty']['lower'],'구간상한':r['uncertainty']['upper'],'상태':r['uncertainty']['status']} for r in summary['pairwise']],hide_index=True,width='stretch')
        st.caption('불확실성 구간은 n≥100·종목군≥20·진입일 블록≥10일 때 종목×만기 길이 날짜 블록 교차 재표집으로 계산합니다. 블록 경계 의존성과 다중 비교는 남으며 우열을 판정하지 않습니다.')
        st.json(summary['dependence'],expanded=False)
        st.dataframe([{'제외 사유':k,'전략·기간 행수':v} for k,v in summary['exclusion_reasons'].items()],hide_index=True,width='stretch')
    with st.expander('개별 신호 · 공통 OHLC와 청산 시점',expanded=False):
        ids=list(dict.fromkeys(r['signal_id'] for r in chosen))
        if ids:
            labels={r['signal_id']:str(r.get('name') or r.get('ticker'))+' · '+str(r.get('entry_date') or r.get('first_seen')) for r in chosen}
            sid=st.selectbox('연구 신호',ids,format_func=labels.get,key='exit_signal')
            selected=st.multiselect('표시할 청산 전략 · 최대 3개',list(RULE_IDS),default=['ATR_TRAIL'],format_func=LABELS.get,max_selections=3,key='exit_selected_rules')
            rows=[r for r in chosen if r['signal_id']==sid];path=doc['paths'].get(sid)
            if path and chart_anchor(rows) is None:st.warning('현재 신호의 가격·진입 검증이 완료되지 않아 보관된 과거 차트는 표시하지 않습니다. 아래 제외 사유를 확인하세요.')
            elif path and selected and len(selected)<=3:st.plotly_chart(signal_figure(path,rows,selected,theme),width='stretch',theme=None,key='exit_signal_chart')
            elif not selected:st.caption('표시할 전략을 1~3개 선택하세요.')
            elif len(selected)>3:st.warning('전략은 최대 3개까지만 표시합니다.')
            else:st.caption('검증 가능한 원본 가격 경로가 없어 차트를 표시하지 않습니다.')
            st.dataframe([{'전략':r['rule_id'],'거래 적격':r.get('eligibility_status'),'상태':r.get('completion_status'),'비교 적격':r.get('comparison_eligible'),'제외 사유':r.get('exclusion_reason') or r.get('comparison_exclusion_reason'),
                '청산일':r.get('exit_fill_date'),'체결가':r.get('exit_price'),'청산 원인':REASONS.get(r.get('exit_reason'),r.get('exit_reason')),'순수익률':r.get('net_return_pct'),
                '순R':r.get('net_R'),'보유일':r.get('holding_sessions'),'장중충돌':r.get('intrabar_ambiguous'),
                'MFE 범위':f"{number(r.get('mfe_lower_pct'))} ~ {number(r.get('mfe_upper_pct'))}",
                'MAE 범위':f"{number(r.get('mae_lower_pct'))} ~ {number(r.get('mae_upper_pct'))}",
                '가격 기준일':r.get('data_as_of')} for r in rows if r['rule_id'] in selected],hide_index=True,width='stretch')
            st.caption('장중 청산일의 고저가 선후는 알 수 없어 MFE/MAE 범위로 표시합니다. 사후 최고가 포착률은 실시간 판단 지표가 아닙니다.')
    with st.expander('규칙 정의 · 체결·비용·통계 가정',expanded=False):
        st.dataframe([{'전략':LABELS[r],'정의':definition} for r,definition in zip(RULE_IDS,[
            '손절 없이 만기 종가','최초 고정 손절 또는 만기','최초 손절 또는 +2R · 같은 봉 충돌은 손절 우선',
            '종가<SMA20 2회 연속 확인 → 다음 시가','종가<SMA50 2회 연속 확인 → 다음 시가',
            'Wilder ATR14×3 · 진입 이후 최고가 · 다음날 적용 · 하향 금지','직전10일 저가 · 현재 봉 제외 · 초기손절 유지'])],hide_index=True,width='stretch')
        st.caption('게시 시각보다 엄격히 늦은 첫 거래일 시가에 진입합니다. 게시와 개장이 동시라면 다음 거래일로 넘깁니다. 진입일=1봉. 예약 시가청산→갭→장중→만기종가→다음날 주문 순서입니다. 갭은 시가, 모호한 손절/목표 동시 터치는 손절 우선입니다. 비용은 진입금액 기준 왕복 %p이며 R 분모는 최초 위험으로 고정합니다.')
        st.caption('TARGET_2R은 O’Neil의 20~25% 익절과 다르고 MA50은 Weinstein의 30주선 전략이 아닙니다. ATR14·진입 이후 최고가는 전통적 22일 Chandelier와 다르며 Donchian은 Turtle 진입·피라미딩·비중 규칙을 구현하지 않습니다.')
        st.caption('회고적/전향적을 구분하고 7개 공통 또는 쌍별 공통 만기 표본만 비교합니다. 수익률을 누적 계좌 성과로 합산하지 않습니다. 고정 정책을 자동 최적화·선택하지 않습니다.')


