"""Status visibility must never upgrade unverified signals or invent returns."""
import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from trend_tracking_view import (INVALID_STATUS, VALID_STATUSES, invalid_reason_counts,
                                maturity_statistics, metric_statistics, tracking_status_counts)


def sample():
    states=['추적 진행']*5+['추세 경고 발생']*7+['진입 대기']*2+[INVALID_STATUS]*53
    rows=[];daily=[]
    for i,state in enumerate(states):
        valid=state in VALID_STATUSES
        rows.append(dict(signal_id=str(i),market='KR' if i%2==0 else 'US',ticker=f'{5000+i:06d}' if i%2==0 else f'T{i}',
            name=f'Synthetic {i}',sector='Tech',first_seen='2026-11-05',status=state,bars=2 if valid else 0,
            evaluation_date='2026-11-06' if valid else None,entry_date='2026-11-05' if valid else None,
            quality_reason=('게시 시각 근거 없음' if i<56 else '신호 종가 가격 기준 불일치/분할·조정 여부 미확인') if state==INVALID_STATUS else None,
            hold_return_pct=float(i) if valid else 999.,mfe_pct=None if i==0 else float(i+1),
            mae_pct=None if i in (0,1) else -float(i),first_warning_date=None,trend_state='추세 양호' if valid else '평가 불가'))
        if valid:
            for day in (1,2):
                daily.append(dict(signal_id=str(i),evaluation_date=f'2026-11-0{day+4}',bars=day,
                    hold_return_pct=float(i),first_stop_date=None,first_warning_date=None))
    return pd.DataFrame(rows),pd.DataFrame(daily)


@pytest.mark.parametrize('count',[0,1,67])
def test_status_and_reason_partitions_cover_all_signals_without_overlap(count):
    latest,_=sample();latest=latest.iloc[:count];before=latest.copy(deep=True)
    counts=tracking_status_counts(latest)
    assert sum(counts.values())==count
    reasons=invalid_reason_counts(latest)
    assert reasons['count'].sum()==counts[INVALID_STATUS]
    if count==67:
        assert [counts[s] for s in ('추적 진행','추세 경고 발생','진입 대기',INVALID_STATUS,'최대 관찰 기간 도달')]==[5,7,2,53,0]
        assert dict(zip(reasons.quality_reason,reasons['count']))=={'게시 시각 근거 없음':42,'신호 종가 가격 기준 불일치/분할·조정 여부 미확인':11}
        assert reasons.pct.sum()==pytest.approx(100.)
    pd.testing.assert_frame_equal(latest,before)


def test_unknown_missing_states_and_missing_reasons_are_visible():
    latest=pd.DataFrame({'status':['새로운 상태',None,INVALID_STATUS,INVALID_STATUS,INVALID_STATUS],
                         'quality_reason':[None,None,None,'','실제 새 원인 <문자열>']})
    counts=tracking_status_counts(latest)
    assert counts['기타·미분류']==2 and sum(counts.values())==5
    reasons=invalid_reason_counts(latest)
    assert dict(zip(reasons.quality_reason,reasons['count']))=={'사유 미기록':2,'실제 새 원인 <문자열>':1}


def test_each_metric_uses_own_finite_valid_denominator_not_all_tracking_signals():
    latest,_=sample();before=latest.copy(deep=True)
    stats=metric_statistics(latest)
    assert stats['hold_return_pct']=={'n':12,'mean':5.5}
    assert stats['mfe_pct']['n']==11 and stats['mae_pct']['n']==10
    latest.loc[0,'hold_return_pct']=np.inf
    latest.loc[1,'hold_return_pct']=np.nan
    assert metric_statistics(latest)['hold_return_pct']['n']==10
    assert metric_statistics(latest[latest.status.eq(INVALID_STATUS)])['hold_return_pct']=={'n':0,'mean':None}
    assert before.loc[14,'hold_return_pct']==999.  # Present but invalid does not enter means.


def test_maturity_uses_exact_recorded_days_and_valid_states_without_recalculating_returns():
    latest=pd.DataFrame([dict(signal_id=s,status=status,bars=bars,ret_120d=999.) for s,status,bars in
                         [('a','추적 진행',20),('b','추세 경고 발생',60),('c','최대 관찰 기간 도달',120),('d',INVALID_STATUS,120)]])
    daily=pd.DataFrame([dict(signal_id=s,evaluation_date=f'2026-{day//20+1:02d}-01',bars=day,hold_return_pct=value)
                        for s,day,value in [('a',20,1.),('a',19,999.),('b',20,2.),('b',60,6.),
                                            ('c',20,3.),('c',60,None),('c',120,12.),('d',120,999.)]])
    stats=maturity_statistics(latest,daily)
    assert stats[20]=={'n':3,'mean':2.}
    assert stats[60]=={'n':1,'mean':6.} and stats[120]=={'n':1,'mean':12.}
    a,b=sample()
    assert all(v=={'n':0,'mean':None} for v in maturity_statistics(a,b).values())


def dashboard():
    return AppTest.from_string('''
import app
from trend_tracking_view import render_tracking
from test_tracking_status_dashboard import sample
a,b=sample()
render_tracking(a,b,{'status':'available','run_id':'fixture'},app.stock_dataframe,app.show_plotly,app.clean_chart)
''',default_timeout=60).run()


def test_ui_shows_67_states_raw_causes_and_waiting_maturity():
    at=dashboard()
    assert not at.exception
    metrics={m.label:m.value for m in at.metric}
    assert metrics['전체 추적 신호 수']=='67'
    assert metrics['추적 진행·경고']=='12'
    assert metrics['데이터 부족·검증 불가']=='53'
    assert any('현재 정상 추적 중 5' in c.value and '추세 경고 발생 7' in c.value
               and '진입 대기 2' in c.value for c in at.caption)
    assert len([p for p in at.get('plotly_chart')
                if (p.key or '').startswith('trend_status_bar_')])==1
    assert metrics['평균 모의 보유 수익률']=='+5.50%'
    assert metrics['60거래일 모의 보유 수익률']==metrics['120거래일 모의 보유 수익률']=='평가 대기'
    assert list(at.dataframe[0].value['건수'])==[42,11]
    assert all(not e.proto.expanded for e in at.expander)


@pytest.mark.parametrize('market,count,valid',[('KR',34,6),('US',33,6)])
def test_market_status_and_reason_stock_filters_preserve_identity(market,count,valid):
    at=dashboard();at.selectbox(key='trend_market').set_value(market).run()
    assert not at.exception
    assert next(m.value for m in at.metric if m.label=='전체 추적 신호 수')==str(count)
    reasons=at.selectbox(key='trend_reason').options
    assert '게시 시각 근거 없음' in reasons
    at.selectbox(key='trend_reason').set_value('게시 시각 근거 없음').run()
    ticker='005014' if market=='KR' else 'T15'
    at.selectbox(key='trend_invalid_stock').set_value((market,ticker)).run()
    assert not at.exception
    detail=at.dataframe[1].value
    assert len(detail)==1 and detail.iloc[0]['시장']==market
    assert ticker in detail.iloc[0]['티커']  # Existing TradingView LinkColumn retained.
    assert detail.iloc[0]['경과 거래일(관측 봉)']=='확인 불가'
    at.selectbox(key='trend_status').set_value('평가 불가').run()
    assert not at.exception
    assert next(m.value for m in at.metric if m.label=='평균 모의 보유 수익률')=='평가 불가'
    # Scope summary remains visible even after drilling into invalid signals.
    assert next(m.value for m in at.metric if m.label=='전체 추적 신호 수')==str(count)


@pytest.mark.parametrize('period',[20,60,120])
def test_period_selection_does_not_change_current_status_counts(period):
    at=dashboard();at.selectbox(key='trend_horizon').set_value(period).run()
    assert not at.exception
    assert next(m.value for m in at.metric if m.label=='전체 추적 신호 수')=='67'


@pytest.mark.parametrize('available',[True,False])
def test_verified_zero_signals_vs_unavailable_are_distinct(available):
    at=AppTest.from_string(f'''
import pandas as pd
import app
from trend_tracking_view import render_tracking
render_tracking(pd.DataFrame(),pd.DataFrame(),{{'status':{'available' if available else 'unavailable'!r}}},app.stock_dataframe,app.show_plotly)
''').run()
    assert not at.exception
    assert (len(at.metric)==3) if available else (len(at.metric)==0)


def test_unknown_status_remains_visible_and_is_excluded_from_returns():
    at=AppTest.from_string('''
import pandas as pd
import app
from trend_tracking_view import render_tracking
x=pd.DataFrame([dict(signal_id='a',market='KR',ticker='005930',name='Example',first_seen='2026-11-05',bars=1,status='추가된 상태',hold_return_pct=999.)])
y=pd.DataFrame(columns=['signal_id','bars','evaluation_date'])
render_tracking(x,y,{'status':'available'},app.stock_dataframe,app.show_plotly)
''').run()
    assert not at.exception
    assert any('기타·미분류 1' in c.value for c in at.caption)
    assert next(m.value for m in at.metric if m.label=='평균 모의 보유 수익률')=='평가 불가'


def test_status_visual_bar_partitions_are_complete_and_unknown_excluded():
    at=dashboard()
    assert not at.exception
    charts=[p for p in at.get('plotly_chart') if (p.key or '').startswith('trend_status_bar_')]
    assert len(charts)==1
    import json
    spec=json.loads(charts[0].proto.spec)
    counts={t['name']:t['x'][0] if isinstance(t['x'],list) else t['x']
            for t in spec['data']}
    assert len(spec['data']) >= 3
    assert spec['layout']['barmode']=='stack'
    assert any('검증 불가 사유' in ex.label for ex in at.expander)

