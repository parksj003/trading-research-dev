import json

import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest

import app


def sample():
    return pd.DataFrame(dict(bars=[20,10,3],ret_10d=[10.,-5.,99.],ex_10d=[5.,np.nan,99.],
        stop_aware_r_10d=[1.,-1.,-1.],r_10d=[99.,99.,99.],mae_10d_pct=[-2.,-12.,-99.],
        mfe_10d_pct=[12.,2.,99.],mae_pct=[-99.,-99.,-99.],mfe_pct=[99.,99.,99.],
        stop_hit_10d=[False,True,True],stop_hit=[True,True,True],
        calculation_version=['fixed_horizon_dates_v2']*3,entry_price=[100.]*3,stop_price=[90.]*3,
        path_ohlc_complete_10d=[True]*3,stop_price_basis=['matched']*3,calendar_complete_10d=[True]*3))


def test_metrics_use_mature_cohorts_and_each_indicators_own_denominator():
    g=sample();before=g.copy(deep=True);s=app.outcome_metrics(g)
    assert s['신호']==3 and s['10일 평가']==2 and s['승률(10일)']==50.
    assert s['평균수익']==2.5 and s['평균 R']==0. and s['평균 MAE']==-7.
    assert s['초과 평가']==1 and s['손절R 평가']==2 and s['손절 터치%']==50.
    pd.testing.assert_frame_equal(g,before)


def test_missing_ten_day_data_never_falls_back_to_unstopped_r_or_twenty_day_mae():
    g=sample().drop(columns=['stop_aware_r_10d','mae_10d_pct','stop_hit_10d']);s=app.outcome_metrics(g)
    assert pd.isna(s['평균 R']) and pd.isna(s['평균 MAE']) and pd.isna(s['손절 터치%'])


def test_unverified_adjustment_or_old_calculation_does_not_create_confirmed_stop_metrics():
    g=sample();g['stop_price_basis']='changed';s=app.outcome_metrics(g)
    assert s['10일 평가']==2 and s['손절R 평가']==0
    g['calculation_version']='legacy';s=app.outcome_metrics(g)
    assert s['초과 평가']==0


def test_validation_screen_discloses_legacy_simulation_and_draws_fixed_ten_day_paths():
    at=AppTest.from_string('''
import pandas as pd
import app
b=app.Bundle(man={},summ={})
b.outcomes=pd.DataFrame([dict(market='US',track='SP500',ticker='EX',name='Example',
 as_of='2026-10-01',first_seen='2026-10-01',setup_stage='VCP',sector='Example',status='종료',
 bars=20,ret_10d=10.,ex_10d=5.,r_10d=99.,stop_aware_r_10d=1.,entry_price=100.,stop_price=90.,
 mae_10d_pct=-2.,mfe_10d_pct=12.,mae_pct=-99.,mfe_pct=99.,stop_hit_10d=False,
 calculation_version='fixed_horizon_dates_v2',stop_price_basis='matched',path_ohlc_complete_10d=True,calendar_complete_10d=True)])
app.tab_validation(b)
''').run()
    assert not at.exception
    assert any('게시 이전 진입' in warning.value for warning in at.warning)
    specs=[json.loads(item.proto.spec) for item in at.get('plotly_chart')]
    assert not specs  # One eligible signal: show metrics, not an overconfident distribution.
    assert any('20건 미만' in c.value for c in at.caption)
    assert any('10거래일 신호별 사후 평가' in x.value for x in at.markdown)


def test_legacy_validation_fallback_is_never_shown_without_reference_only_warning():
    result=AppTest.from_string('''
import pandas as pd
import app
b=app.Bundle(man={},summ={},val=pd.DataFrame([{'sample_cnt':25,'avg_ex_10d':10.}]))
app.tab_validation(b)
''').run()
    assert not result.exception
    assert any('공식 전략 성과가 아닙니다' in item.value for item in result.warning)
    assert len(result.dataframe)==1


def test_ten_day_dashboard_uses_two_distributions_not_mixed_unit_cumulative_line():
    at=AppTest.from_string('''
import pandas as pd
import app
b=app.Bundle(man={},summ={})
b.outcomes=pd.DataFrame([dict(
 market='KR',track='KOSPI',ticker=str(i),name=f'Example{i}',
 as_of='2026-10-01',first_seen='2026-10-01',
 setup_stage='VCP',sector='Test',status='완료',bars=20,
 ret_10d=float(i-10),ex_10d=float(i-11),stop_aware_r_10d=0.5,
 entry_price=100.,stop_price=90.,mae_10d_pct=-float(1+i%5),
 mfe_10d_pct=float(i%7+1),stop_hit_10d=False,
 calculation_version='fixed_horizon_dates_v2',
 stop_price_basis='matched',path_ohlc_complete_10d=True,
 calendar_complete_10d=True,
 ) for i in range(24)])
app.tab_validation(b)
''',default_timeout=60).run()
    assert not at.exception
    specs=[json.loads(x.proto.spec) for x in at.get('plotly_chart')]
    assert any(x['layout'].get('xaxis',{}).get('title',{}).get('text')=='10일 MAE (%)'
               for x in specs)
    assert any(any(t.get('type')=='histogram' for t in x['data']) for x in specs)
    assert not any('cum_ex' in str(x) or 'cum_r' in str(x) for x in specs)
    assert at.radio(key='v_group_mode').value == '셋업'
    assert len(at.get('plotly_chart')) == 3  # Histogram, MAE/MFE, eligible-group comparison.
    at.radio(key='v_group_mode').set_value('신호 발생월').run()
    assert not at.exception
    assert any('코호트' in c.value for c in at.caption)


def test_ten_day_distribution_gates_unknown_mfe_and_mae():
    data=sample()
    data['path_ohlc_complete_10d']=[False,False,False]
    filtered=app.clean_validation_metrics(data)
    assert filtered['mfe_10d_pct'].isna().all()
    assert filtered['mae_10d_pct'].isna().all()
    assert app.outcome_metrics(filtered)['MAE 평가']==0



def test_review_hold_excludes_preserved_results():
    from app import clean_validation_metrics
    frame = pd.DataFrame([dict(bars=20, ret_10d=10., review_reason="price revision",
        calendar_complete_10d=True, calculation_version="fixed_horizon_dates_v2")])
    assert pd.isna(clean_validation_metrics(frame).iloc[0].ret_10d)
