"""Hash-bound optional observation artifacts, separate from official outcomes."""
import gzip
import hashlib
import io
import json

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

FILES=('trend_tracking.csv','trend_tracking_daily.csv.gz')
METRICS=('hold_return_pct','mfe_pct','mae_pct','peak_close_return_pct','profit_giveback_pct','max_profit_giveback_pct','high_drawdown_pct','stop_scenario_return_pct')
VALID_STATUSES=('추적 진행','추세 경고 발생','최대 관찰 기간 도달')
INVALID_STATUS='데이터 부족/검증 불가'
STATUS_LABELS={
    '추적 진행':'현재 정상 추적 중',
    '추세 경고 발생':'추세 경고 발생',
    '진입 대기':'진입 대기',
    INVALID_STATUS:'데이터 부족·검증 불가',
    '최대 관찰 기간 도달':'최대 관찰 기간 도달',
}


def tracking_status_counts(frame):
    """Mutually exclusive current states, including missing/future engine states."""
    states=frame.get('status',pd.Series(index=frame.index,dtype=object))
    counts={state:int(states.eq(state).sum()) for state in STATUS_LABELS}
    counts['기타·미분류']=int((~states.isin(STATUS_LABELS)).sum())
    return counts


def invalid_reason_counts(frame):
    """Exact source reasons; no inferred causes or lossy semantic grouping."""
    invalid=frame.loc[frame.get('status',pd.Series(index=frame.index,dtype=object)).eq(INVALID_STATUS)]
    reasons=invalid.get('quality_reason',pd.Series(index=invalid.index,dtype=object))
    reasons=reasons.where(reasons.notna() & reasons.astype(str).str.strip().ne(''),'사유 미기록')
    counts=reasons.value_counts().rename_axis('quality_reason').reset_index(name='count')
    counts['pct']=counts['count']/len(invalid)*100 if len(invalid) else pd.Series(dtype=float)
    return counts


def metric_statistics(frame,keys=METRICS):
    """Each metric's own finite denominator, restricted to verified states."""
    good=frame.loc[frame.get('status',pd.Series(index=frame.index,dtype=object)).isin(VALID_STATUSES)]
    result={}
    for key in keys:
        values=pd.to_numeric(good.get(key,pd.Series(dtype=float)),errors='coerce').replace([np.inf,-np.inf],np.nan).dropna()
        result[key]={'n':len(values),'mean':float(values.mean()) if len(values) else None}
    return result


def maturity_statistics(latest,daily):
    """Observed exact 20/60/120th closes; no new return calculation or maturity guess."""
    good=latest.loc[latest.get('status',pd.Series(index=latest.index,dtype=object)).isin(VALID_STATUSES)]
    result={}
    for period in (20,60,120):
        eligible=good.loc[pd.to_numeric(good.get('bars',pd.Series(index=good.index,dtype=float)),errors='coerce').ge(period)]
        if {'signal_id','bars','hold_return_pct','evaluation_date'}.issubset(daily):
            points=daily.loc[daily.signal_id.isin(eligible.signal_id) & pd.to_numeric(daily.bars,errors='coerce').eq(period)]
            points=points.sort_values('evaluation_date').drop_duplicates('signal_id',keep='last')
            values=pd.to_numeric(points.hold_return_pct,errors='coerce').replace([np.inf,-np.inf],np.nan).dropna()
        else:values=pd.Series(dtype=float)
        result[period]={'n':len(values),'mean':float(values.mean()) if len(values) else None}
    return result


def _render_status_summary(frame, plot):
    """Compact and exhaustive status summary; no invalid returns are counted."""
    counts = tracking_status_counts(frame)
    total = len(frame)
    active = counts['추적 진행'] + counts['추세 경고 발생']
    cols = st.columns(3)
    cols[0].metric('전체 추적 신호 수', str(total))
    cols[1].metric('추적 진행·경고', str(active))
    cols[2].metric('데이터 부족·검증 불가', str(counts[INVALID_STATUS]))

    # A single horizontal bar replaces a second row of status KPI cards while
    # preserving all mutually exclusive status categories and exact counts.
    palette = {
        '추적 진행': '#16A34A', '추세 경고 발생': '#F59E0B',
        '진입 대기': '#60A5FA', INVALID_STATUS: '#94A3B8',
        '최대 관찰 기간 도달': '#6366F1', '기타·미분류': '#64748B',
    }
    fig = go.Figure()
    for state, label in [*STATUS_LABELS.items(), ('기타·미분류', '기타·미분류')]:
        count = counts[state]
        if count:
            fig.add_bar(
                x=[count], y=['현재 상태'], name=label, orientation='h',
                marker_color=palette[state],
                text=[f'{count}건' if count / max(total, 1) >= 0.13 else ''],
                textposition='inside',
                hovertemplate=f'{label}: {count}건 ({count/max(total,1):.1%})<extra></extra>',
            )
    if total:
        fig.update_layout(
            barmode='stack', height=100, margin=dict(l=8, r=8, t=2, b=2),
            xaxis=dict(visible=False, range=[0, total]),
            yaxis=dict(visible=False), showlegend=False, bargap=0.2,
        )
        plot(fig, key='trend_status_bar')
    legend = ' · '.join(
        f'{label} {counts[state]}' for state, label in
        [*STATUS_LABELS.items(), ('기타·미분류', '기타·미분류')]
        if counts[state] or state in STATUS_LABELS
    )
    st.caption(legend + ' · 각 상태는 중복 집계되지 않습니다.')
    if counts['기타·미분류']:
        st.info('기타·미분류 상태는 신호 건수에 포함되지만, 유효 성과에는 포함되지 않습니다.')


def _render_invalid_details(frame, stock_dataframe):
    reasons = invalid_reason_counts(frame)
    if reasons.empty:
        return
    with st.expander(f'ⓘ 검증 불가 사유 {int(reasons["count"].sum())}건 · 상세 보기', expanded=False):
        st.caption('원본 사유 그대로 표시합니다. 분모는 현재 필터 범위의 검증 불가 신호 수입니다.')
        st.dataframe(
            reasons.rename(columns={'quality_reason':'검증 불가 사유','count':'건수','pct':'비율 (%)'}),
            hide_index=True, width='stretch',
            column_config={'비율 (%)':st.column_config.NumberColumn(format='%.1f%%')},
        )
        invalid = frame.loc[frame.status.eq(INVALID_STATUS)].copy()
        reason = st.selectbox(
            '사유 선택', [None] + list(reasons.quality_reason),
            format_func=lambda v:'전체' if v is None else v, key='trend_reason',
        )
        if reason is not None:
            raw = invalid.get('quality_reason', pd.Series(index=invalid.index, dtype=object))
            mask = (raw.isna() | raw.astype(str).str.strip().eq('')) if reason=='사유 미기록' else raw.eq(reason)
            invalid = invalid.loc[mask]
        keys = list(dict.fromkeys(zip(invalid.market.astype(str), invalid.ticker.astype(str))))
        labels = {(str(r.market),str(r.ticker)):
                  f'{r.market}:{r.ticker} · {getattr(r,"name","")}' for r in invalid.itertuples()}
        stock = st.selectbox('검증 불가 종목', [None] + keys,
                             format_func=lambda v:'전체' if v is None else labels[v],
                             key='trend_invalid_stock')
        if stock is not None:
            invalid = invalid.loc[
                invalid.market.astype(str).eq(stock[0]) &
                invalid.ticker.astype(str).eq(stock[1])
            ]
        columns = {'market':'시장','name':'종목명','ticker':'티커','first_seen':'신호일',
                   'entry_date':'진입일','bars':'경과 거래일(관측 봉)',
                   'evaluation_date':'최신 평가일','status':'검증 상태','quality_reason':'검증 불가 사유'}
        detail = invalid.reindex(columns=columns).copy()
        detail['bars'] = pd.to_numeric(detail.bars, errors='coerce').where(lambda v:v.gt(0))
        detail = detail.sort_values(['quality_reason','market','ticker'], na_position='last')
        detail = detail.astype(object).where(detail.notna(), '확인 불가')
        stock_dataframe(detail.rename(columns=columns), hide_index=True, width='stretch')
        st.caption('가격·일봉 검증이 없는 관측 수는 추정하지 않습니다. 실제 무효 사유도 원본 그대로 유지합니다.')


def load_tracking(run_id,fetch):
    try:
        raw=fetch('history/trend_tracking_manifest.json')
        if not raw:return pd.DataFrame(),pd.DataFrame(),{'status':'unavailable','reason':'중장기 추적 게시 자료 없음'}
        if len(raw)>100_000:raise ValueError('descriptor size')
        m=json.loads(raw)
        if not isinstance(m,dict):raise ValueError('descriptor shape')
        if m.get('schema')!='trend-tracking-1.0' or m.get('run_id')!=run_id:raise ValueError('source mismatch')
        frames=[]
        for name in FILES:
            meta=m['files'][name]
            if not isinstance(meta.get('bytes'),int) or not 0<=meta['bytes']<=10_000_000:raise ValueError('file size')
            content=fetch('history/'+name)
            if content is None or len(content)!=meta['bytes'] or hashlib.sha256(content).hexdigest()!=meta['sha256']:raise ValueError('hash mismatch')
            if name.endswith('.gz'):
                with gzip.GzipFile(fileobj=io.BytesIO(content)) as stream:content=stream.read(64_000_001)
                if len(content)>64_000_000:raise ValueError('decompression size')
            df=pd.read_csv(io.BytesIO(content),dtype={'ticker':str,'signal_id':str})
            if len(df)>250_000:raise ValueError('row limit')
            if not {'signal_id','evaluation_date'}.issubset(df):raise ValueError('schema')
            if name==FILES[0] and df.signal_id.duplicated().any():raise ValueError('duplicate signal')
            if name==FILES[1] and df.duplicated(['signal_id','evaluation_date']).any():raise ValueError('duplicate observation')
            frames.append(df)
        return *frames,{'status':'available','run_id':run_id,'tracking_version':m.get('tracking_version')}
    except (ValueError,TypeError,KeyError,OSError):
        return pd.DataFrame(),pd.DataFrame(),{'status':'unavailable','reason':'추적 자료 run_id·SHA256·크기/구조 검증 실패'}


def horizon_frame(latest,daily,horizon):
    """Metrics capped at selected observation count; never use later observations."""
    rows=[]
    for r in latest.to_dict('records'):
        row=dict(r);row['observed_bars_total']=r.get('bars')
        if pd.to_numeric(r.get('bars',0),errors='coerce')>horizon:
            points=daily[(daily.signal_id==r['signal_id'])&(pd.to_numeric(daily.bars,errors='coerce')<=horizon)].sort_values('bars')
            if points.empty:
                row.update({k:None for k in METRICS});row['trend_state']='평가 불가'
            else:
                row.update(points.iloc[-1].to_dict())
                def flag(k):return str(row.get(k)).lower() in ('true','1','1.0')
                row['trend_state']='중기 훼손' if flag('below_ma50') else '단기 약화' if flag('below_ma20') else '추세 양호' if pd.notna(row.get('below_ma20')) and pd.notna(row.get('below_ma50')) else '평가 불가'
        rows.append(row)
    return pd.DataFrame(rows)



def tracking_path_figure(points):
    """Two aligned price-path panels; never infer missing observation dates.

    Returns one figure for the selected signal. Cost-adjusted close returns
    and raw high/low excursion metrics occupy separate y-axes/panels.
    """
    from plotly.subplots import make_subplots

    p = points.copy()
    required = {'evaluation_date', 'bars'}
    if p.empty or not required.issubset(p):
        return None
    p['bars'] = pd.to_numeric(p['bars'], errors='coerce')
    p['evaluation_date'] = pd.to_datetime(p['evaluation_date'], errors='coerce')
    p = p.dropna(subset=['evaluation_date', 'bars']).sort_values(
        ['bars', 'evaluation_date'], kind='stable'
    ).drop_duplicates('evaluation_date', keep='last')
    if p.empty:
        return None
    for k in ('hold_return_pct','peak_close_return_pct','mfe_pct','mae_pct'):
        p[k] = pd.to_numeric(p[k], errors='coerce') if k in p else np.nan
        p[k] = p[k].replace([np.inf, -np.inf], np.nan)
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.56, 0.44], vertical_spacing=0.14,
        subplot_titles=(
            '① 종가 기준 보유 수익 · 최고 수익 (비용 차감 후)',
            '② 누적 MFE · MAE (고가/저가, 비용 차감 전)',
        ),
    )
    dates = p['evaluation_date']
    bars = p['bars']
    valid_peak = p['peak_close_return_pct'].notna().any()
    valid_hold = p['hold_return_pct'].notna().any()
    if valid_peak:
        fig.add_trace(go.Scatter(
            x=dates, y=p['peak_close_return_pct'], name='최고 종가 수익',
            mode='lines', line=dict(color='#D97706', width=2, dash='dash'),
            connectgaps=False, customdata=bars,
            hovertemplate='%{x|%Y-%m-%d} · %{customdata:.0f}봉<br>최고 수익 %{y:+.2f}%<extra></extra>',
        ), row=1, col=1)
    if valid_hold:
        fig.add_trace(go.Scatter(
            x=dates, y=p['hold_return_pct'], name='보유 수익',
            mode='lines', line=dict(color='#2563EB', width=2.6),
            fill='tonexty' if valid_peak else None,
            fillcolor='rgba(217,119,6,0.12)' if valid_peak else None,
            connectgaps=False, customdata=bars,
            hovertemplate='%{x|%Y-%m-%d} · %{customdata:.0f}봉<br>보유 수익 %{y:+.2f}%<extra></extra>',
        ), row=1, col=1)
    for k, name, color in (
        ('mfe_pct', '누적 MFE', '#059669'),
        ('mae_pct', '누적 MAE', '#DC2626'),
    ):
        if p[k].notna().any():
            fig.add_trace(go.Scatter(
                x=dates, y=p[k], name=name, mode='lines',
                line=dict(color=color, width=2.3), connectgaps=False,
                customdata=bars,
                hovertemplate='%{x|%Y-%m-%d} · %{customdata:.0f}봉<br>'
                              + name + ' %{y:+.2f}%<extra></extra>',
            ), row=2, col=1)
    for row in (1,2):
        fig.add_hline(y=0, row=row, col=1, line_color='#64748B', line_dash='dot', line_width=1)
    # Only mark real, observed event dates within this selected time window.
    observed = set(dates.dt.strftime('%Y-%m-%d'))
    for k, name, color, symbol in (
        ('first_warning_date', '최초 추세 경고', '#F59E0B', 'diamond'),
        ('first_stop_date', '최초 고정 손절 터치', '#DC2626', 'x'),
    ):
        if k not in p or not valid_hold:
            continue
        actual = p[k].dropna().astype(str)
        first = actual.iloc[0] if not actual.empty else None
        if first not in observed:
            continue
        at = p.loc[p.evaluation_date.dt.strftime('%Y-%m-%d').eq(first)]
        if at.empty or not np.isfinite(at['hold_return_pct'].iloc[0]):
            continue
        fig.add_trace(go.Scatter(
            x=at.evaluation_date.iloc[:1], y=at.hold_return_pct.iloc[:1],
            name=name, mode='markers',
            marker=dict(color=color, size=11, symbol=symbol),
            hovertemplate=name+'<br>%{x|%Y-%m-%d}<br>%{y:+.2f}%<extra></extra>',
        ), row=1, col=1)
    milestones = p.loc[p.bars.isin([20,60,120]) & p.hold_return_pct.notna()]
    if not milestones.empty:
        fig.add_trace(go.Scatter(
            x=milestones.evaluation_date, y=milestones.hold_return_pct,
            name='20·60·120봉', mode='markers+text',
            text=[f'{int(v)}' for v in milestones.bars],
            textposition='top center',
            marker=dict(symbol='circle-open', color='#64748B', size=11, line=dict(width=1.4)),
            customdata=milestones.bars,
            hovertemplate='%{customdata}거래일 · %{x|%Y-%m-%d}<br>%{y:+.2f}%<extra></extra>',
        ), row=1, col=1)
    fig.update_layout(
        height=570, margin=dict(l=46, r=12, t=60, b=35),
        hovermode='x unified', legend=dict(orientation='h', y=1.13, x=0),
        meta=dict(kind='trend_selected_path', observed_dates=len(p),
                  has_mfe=bool(p.mfe_pct.notna().any()),
                  has_mae=bool(p.mae_pct.notna().any())),
    )
    fig.update_yaxes(title_text='수익률 (%)', row=1, col=1, zeroline=False)
    fig.update_yaxes(title_text='경로 변동 (%)', row=2, col=1, zeroline=False)
    fig.update_xaxes(title_text='실제 관측일', row=2, col=1, tickformat='%m/%d')
    return fig


def render_tracking(latest,daily,source,stock_dataframe,show_chart,chart_theme=None):
    def plot(fig,key):
        if chart_theme is not None:
            fig=chart_theme(fig,height=int(fig.layout.height or 350),legend=len(fig.data)>1)
            # Manual dark chart selection must remain readable on a light app page.
            fig.update_layout(paper_bgcolor=fig.layout.template.layout.paper_bgcolor or '#FFFFFF')
        if key == 'trend_return_path':
            # The global chart theme defaults to closest-point hover; restore
            # synchronized date inspection for the two aligned panels.
            fig.update_layout(hovermode='x unified', hoverdistance=30)
        if key == 'trend_status_bar':
            # Prevent the theme's default legend/margins from squeezing the
            # compact status distribution into a very small plotting domain.
            fig.update_layout(
                height=105, showlegend=False,
                margin=dict(l=3, r=3, t=3, b=3),
                xaxis=dict(visible=False), yaxis=dict(visible=False),
            )
        return show_chart(fig,key=key)
    st.subheader('📈 ② 중장기 추세 관찰 · 최대 120거래일')
    st.caption('단기 10일 사후 검증과 별도 자료입니다. 경고·관찰 종료는 실제 주문이나 매도 체결이 아닙니다.')
    if source.get('status')!='available':
        st.info(source.get('reason') or '추적 신호가 아직 없습니다. 다음 엔진 게시 실행에서 확인하세요.');return
    if latest.empty:
        _render_status_summary(latest, plot)
        st.info('현재 게시본의 추적 신호가 0건입니다. 수익률은 평가 대기입니다.');return
    if not {'market','status','bars','signal_id'}.issubset(latest) or not {'bars','signal_id','evaluation_date'}.issubset(daily):
        st.warning('추적 스키마 필드가 부족합니다.');return
    st.caption(f"run_id: {source.get('run_id')} · 계산 버전: {source.get('tracking_version')}")
    a,b,c,d=st.columns(4)
    market=a.selectbox('추적 시장',['전체','KR','US'],key='trend_market')
    horizon=b.selectbox('추적 기간 (거래일)',[20,60,120],index=2,key='trend_horizon')
    status=c.selectbox('추적 상태',['전체','진행','경고','진입 대기','종료','평가 불가','기타·미분류'],key='trend_status')
    since=d.selectbox('신호 발생 조회 범위',['전체','최근 30일','최근 90일'],key='trend_since')
    st.caption('조회 범위는 신호 발생일, 추적 기간은 진입 후 표시할 최대 관측 봉 수입니다. 실제 관찰은 최대120거래일까지 계속됩니다. 상태 필터·종료 건수는 최신 전체 관찰 상태이며 수익률·경로는 선택 기간에서 절단합니다.')
    selected=latest.copy()
    if market!='전체':selected=selected[selected.market==market]
    if since!='전체':
        days=30 if '30' in since else 90
        selected=selected[pd.to_datetime(selected.first_seen,errors='coerce')>=pd.Timestamp.now(tz='Asia/Seoul').tz_localize(None).normalize()-pd.Timedelta(days=days)]
    sectors=sorted(selected.sector.dropna().astype(str).unique()) if 'sector' in selected else []
    sector=st.selectbox('추적 섹터',['전체']+sectors,key='trend_sector')
    if sector!='전체':selected=selected[selected.sector==sector]
    st.caption('상태 요약 · 시장·신호 발생일·섹터 범위 기준, 상태 필터와 표시 기간 적용 전')
    _render_status_summary(selected, plot)
    st.caption('검증 불가는 손실이 아니며, 120일 도달은 관찰 종료입니다.')
    _render_invalid_details(selected,stock_dataframe)
    if status!='전체':
        choices={'진행':['추적 진행'],'경고':['추세 경고 발생'],'진입 대기':['진입 대기'],
                 '종료':['최대 관찰 기간 도달'],'평가 불가':[INVALID_STATUS]}
        selected=selected.loc[~selected.status.isin(STATUS_LABELS)] if status=='기타·미분류' else selected.loc[selected.status.isin(choices[status])]
    if selected.empty:st.info('선택 범위의 추적 신호가 없습니다.');return
    view=horizon_frame(selected,daily,horizon)
    good=view[view.status.isin(VALID_STATUSES)]
    st.markdown('##### 참고용 성과 · 유효한 관찰 상태만 집계')
    st.caption(f'아래 통계: 상태 필터 적용 후 {len(selected)}건 중 검증 가능한 상태 {len(good)}건. 지표별 결측은 각각 제외합니다.')
    statistics=metric_statistics(view)
    cols=st.columns(3)
    for col,key,label in zip(cols,['hold_return_pct','mfe_pct','mae_pct'],['평균 모의 보유 수익률','평균 MFE','평균 MAE']):
        stat=statistics[key]
        col.metric(label,f"{stat['mean']:+.2f}%" if stat['n'] else '평가 불가')
        col.caption(f"유효 n={stat['n']}")
    st.caption('평균 수익률은 비용 차감 후 종가 기준, MFE·MAE는 고가·저가 기준 비용 차감 전입니다. 항목별 유효 n을 확인하세요.')
    with st.expander('20·60·120거래일 도달 표본'):
        maturity=maturity_statistics(selected,daily)
        for col,period in zip(st.columns(3),(20,60,120)):
            stat=maturity[period]
            col.metric(f'{period}거래일 모의 보유 수익률',f"{stat['mean']:+.2f}%" if stat['n'] else '평가 대기')
            col.caption(f"유효 n={stat['n']}")
        st.caption('최신 전체 관찰에서 해당 거래일에 실제 도달한 유효 표본만 사용합니다. 표시 기간과 별개이며 20일 도달은 추적 종료가 아닙니다. 기존 공식 5·10·20일 성과를 대체하지 않습니다.')
    def below(key):
        values=good.get(key,pd.Series(dtype=str)).astype(str).str.lower()
        return f"{int(values.isin(['true','1','1.0']).sum())}건 / 유효n={int(values.isin(['true','false','1','0','1.0','0.0']).sum())}"
    st.caption(f'20일선 하회 {below("below_ma20")} · 50일선 하회 {below("below_ma50")} . MA 상태와 RS/섹터 Power 관측은 별개입니다.')
    if len(good)<20:st.warning('유효 표본20건 미만: 전략 우열·최적 청산 규칙 해석을 보류하세요.')
    with st.expander('📊 진행 중 신호 비교 · 수익률 상위 20건', expanded=False):
        active = good[good.status.isin(['추적 진행','추세 경고 발생'])].copy()
        if 'hold_return_pct' in active:
            active['hold_return_pct'] = pd.to_numeric(active.hold_return_pct, errors='coerce')
            top = active.dropna(subset=['hold_return_pct']).sort_values(
                'hold_return_pct', ascending=False
            ).head(20).iloc[::-1]
            if not top.empty:
                labels = top.apply(
                    lambda r:f"{r.get('name','')} ({r.get('ticker','')}) · {r.get('first_seen','')}", axis=1
                )
                colors = ['#16A34A' if v >= 0 else '#DC2626' for v in top.hold_return_pct]
                fig = go.Figure(go.Bar(
                    x=top.hold_return_pct, y=labels, orientation='h',
                    marker_color=colors,
                    customdata=top[['evaluation_date','bars']],
                    hovertemplate='%{y}<br>%{x:+.2f}% · %{customdata[1]}봉<br>기준일 %{customdata[0]}<extra></extra>',
                ))
                fig.update_layout(height=max(260, len(top)*27), xaxis_title='비용 차감 후 모의 수익률 (%)')
                plot(fig, key='trend_live_returns')
            else:
                st.caption('비교 가능한 진행 수익률이 없습니다.')
        else:
            st.caption('현재 수익률 컬럼이 없습니다.')
    if 'trend_state' in view:
        counts=view.trend_state.value_counts()
        st.caption('추세 상태: '+ ' · '.join(f'{name} {int(counts.get(name,0))}건' for name in ['추세 양호','단기 약화','중기 훼손','평가 불가']))
    st.markdown('##### 선택 신호 · 수익 유지·반납·MFE·MAE 추이')
    selected_id=st.selectbox('추적 신호 선택',list(view.signal_id),format_func=lambda sid:next(f"{r.get('name','')} · {r.get('market','')}:{r.get('ticker','')} · 신호 {r.get('first_seen','')}" for r in view.to_dict('records') if r['signal_id']==sid),key='trend_signal')
    points=daily[(daily.signal_id==selected_id)&(pd.to_numeric(daily.bars,errors='coerce')<=horizon)].sort_values('evaluation_date')
    record=view[view.signal_id==selected_id].iloc[0]
    st.caption(f"신호 {record.get('first_seen')} · 원 가격 기준일 {record.get('as_of')} · 진입 {record.get('entry_date')} / {record.get('entry_price')} · 원 손절 {record.get('stop_price')} · 신호 run_id {record.get('run_id')} · config {record.get('config_hash')}")
    if str(record.status)==INVALID_STATUS:st.warning(str(record.get('quality_reason','평가 근거 확인 불가')))
    elif str(record.status) in VALID_STATUSES and not points.empty:
        path_fig = tracking_path_figure(points)
        if path_fig is None:
            st.caption('선택 범위에 실제 관측일이 없습니다.')
        else:
            plot(path_fig, key='trend_return_path')
        st.caption(
            f"실제 확보 {int(record.get('observed_bars_total',0) or 0)}봉 · "
            f"표시 최대 {horizon}봉 · 손절/경고 이후도 계속 보유했다고 가정한 참고 관찰"
        )
        st.caption(
            '① 종가 기준 수익·최고 수익은 비용 차감 후, '
            '② MFE·MAE는 고가·저가 기준 비용 차감 전입니다. '
            '음영은 최고 종가 수익과 보유 수익의 차이이며 실제 체결 손익이 아닙니다.'
        )
    metrics=[k for k in ['market','sub_market','name','ticker','first_seen','evaluation_date','bars','observed_bars_total','status','trend_state',*METRICS,'first_stop_date','stop_scenario_status','first_warning_date','rs_score','rs_change_4w','rs_drawdown_points','sector_power','ma20_down','ma50_down','confirmed_below_ma20','confirmed_below_ma50','tracking_version','recorded_at','fixed_20d_consistency','quality_reason'] if k in view]
    with st.expander('종목별 관측 수치'):
        stock_dataframe(view[metrics],hide_index=True,width='stretch')
    with st.expander('ⓘ 반납률 계산·모의 손절 해석', expanded=False):
        st.caption('반납률=(최대 종가 순수익−현재 종가 순수익)/최대 종가 순수익×100. 최대 이익≤0이면 평가 불가, 손실 전환 시 100% 초과 가능. 실제 고점 대비 주가 하락률과 다릅니다. 고정 손절 결과는 첫 모의 청산 후 고정됩니다.')

