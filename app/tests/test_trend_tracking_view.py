import gzip
import hashlib
import json

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from trend_tracking_view import load_tracking,horizon_frame,tracking_path_figure


def data():
    latest=pd.DataFrame([dict(signal_id='a',evaluation_date='2026-11-05',first_seen='2026-01-01',market='KR',ticker='005930',name='Example',sector='Tech',status='추적 진행',bars=60,hold_return_pct=40.,peak_close_return_pct=100.,mfe_pct=102.,mae_pct=-5.,profit_giveback_pct=60.,trend_state='추세 양호',first_warning_date=None)])
    daily=pd.DataFrame([dict(signal_id='a',evaluation_date=date,bars=i,hold_return_pct=i,peak_close_return_pct=i,mfe_pct=i+1,mae_pct=-5.,profit_giveback_pct=0.,below_ma20=False,below_ma50=False,first_stop_date=None,first_warning_date=None,stop_scenario_return_pct=None) for i,date in enumerate(pd.bdate_range('2026-08-14',periods=60).strftime('%Y-%m-%d'),1)])
    files={'trend_tracking.csv':latest.to_csv(index=False).encode(),'trend_tracking_daily.csv.gz':gzip.compress(daily.to_csv(index=False).encode(),mtime=0)}
    m={'schema':'trend-tracking-1.0','run_id':'now','tracking_version':'trend-observation-1.0','files':{n:{'bytes':len(v),'sha256':hashlib.sha256(v).hexdigest()} for n,v in files.items()}}
    files['trend_tracking_manifest.json']=json.dumps(m).encode()
    return latest,daily,files


def test_verified_load_and_ticker_zeros():
    _,_,files=data();a,b,meta=load_tracking('now',lambda rel:files.get(rel.split('/')[-1]))
    assert meta['status']=='available' and a.iloc[0].ticker=='005930' and len(b)==60


@pytest.mark.parametrize('failure',['run','hash','missing','large'])
def test_invalid_artifacts_remain_unavailable(failure):
    _,_,files=data()
    if failure=='hash':files['trend_tracking.csv']+=b'bad'
    if failure=='missing':files.pop('trend_tracking_daily.csv.gz')
    if failure=='large':
        m=json.loads(files['trend_tracking_manifest.json']);m['files']['trend_tracking.csv']['bytes']=99_000_000;files['trend_tracking_manifest.json']=json.dumps(m).encode()
    a,b,m=load_tracking('wrong' if failure=='run' else 'now',lambda rel:files.get(rel.split('/')[-1]))
    assert a.empty and b.empty and m['status']=='unavailable'


def test_horizon_caps_metrics_without_future_stop_or_price_leakage():
    a,b,_=data();a['first_stop_date']='2026-11-01';a['stop_scenario_return_pct']=-10.
    view=horizon_frame(a,b,20).iloc[0]
    assert view.bars==20 and view.hold_return_pct==20 and pd.isna(view.first_stop_date)
    assert pd.isna(view.stop_scenario_return_pct) and view.observed_bars_total==60


def test_view_filters_and_empty_data_do_not_break_validation():
    at=AppTest.from_string('''
import app
from trend_tracking_view import render_tracking
from test_trend_tracking_view import data
a,b,_=data()
render_tracking(a,b,{'status':'available','run_id':'now'},app.stock_dataframe,app.show_plotly)
''',default_timeout=60).run()
    assert not at.exception
    assert at.selectbox(key='trend_horizon').value==120
    at.selectbox(key='trend_horizon').set_value(20).run()
    assert not at.exception and len(at.get('plotly_chart'))==3
    at.selectbox(key='trend_status').set_value('평가 불가').run()
    assert not at.exception


def test_waiting_invalid_signals_are_not_zero_returns():
    at=AppTest.from_string('''
import pandas as pd
import app
from trend_tracking_view import render_tracking
x=pd.DataFrame([dict(signal_id='a',evaluation_date=None,market='US',status='진입 대기',bars=0,ticker='TEST',first_seen='2026-01-01',quality_reason='대기')])
y=pd.DataFrame(columns=['signal_id','bars','evaluation_date'])
render_tracking(x,y,{'status':'available'},app.stock_dataframe,app.show_plotly)
''',default_timeout=60).run()
    assert not at.exception
    assert sum(m.value=='평가 불가' for m in at.metric)==3


@pytest.mark.parametrize('theme',['라이트','다크'])
def test_charts_use_existing_theme_without_changing_numeric_data(theme):
    at=AppTest.from_string(f'''
import app
from trend_tracking_view import render_tracking
from test_trend_tracking_view import data
a,b,_=data()
app.st.session_state['ui_theme_mode']={theme!r}
render_tracking(a,b,{{'status':'available','run_id':'now'}},app.stock_dataframe,app.show_plotly,app.clean_chart)
''',default_timeout=60).run()
    assert not at.exception
    specs=[json.loads(p.proto.spec) for p in at.get('plotly_chart')]
    assert len(specs)==3
    assert all(s['layout']['font']['color']==('#F8FAFC' if theme=='다크' else '#0F172A') for s in specs)
    assert all(s['layout']['paper_bgcolor']!='rgba(0,0,0,0)' for s in specs)


def test_selected_signal_has_two_aligned_observed_date_panels_and_no_synthetic_curve():
    a,daily,_=data()
    fig=tracking_path_figure(daily)
    assert fig is not None
    assert fig.layout.meta['kind']=='trend_selected_path'
    assert fig.layout.meta['observed_dates']==60
    assert fig.layout.meta['has_mfe'] and fig.layout.meta['has_mae']
    assert fig.layout.xaxis.matches=='x2' or fig.layout.xaxis2.matches=='x'
    names={t.name for t in fig.data}
    assert {'보유 수익','최고 종가 수익','누적 MFE','누적 MAE','20·60·120봉'} <= names
    hold=next(t for t in fig.data if t.name=='보유 수익')
    assert hold.fill=='tonexty' and hold.connectgaps is False
    assert len(hold.x)==60 and len(hold.y)==60
    marks=next(t for t in fig.data if t.name=='20·60·120봉')
    assert list(marks.customdata)==[20,60]
    assert fig.layout.yaxis.title.text=='수익률 (%)'
    assert fig.layout.yaxis2.title.text=='경로 변동 (%)'
    assert fig.layout.hovermode=='x unified'
    assert fig.layout.height>=500


def test_selected_observation_chart_does_not_interpolate_missing_or_future_events():
    _,daily,_=data()
    p=daily.iloc[:4].copy()
    p['mfe_pct']=[None,3.,4.,5.]
    p['mae_pct']=[-2.,None,-4.,-4.]
    p['hold_return_pct']=[0.,None,2.,3.]
    p['first_warning_date']='2026-10-01'  # Not an observed date
    p['first_stop_date']='2026-08-14'     # First observed date, valid
    before=p.copy(deep=True)
    fig=tracking_path_figure(p)
    assert len(fig.data)<7
    assert '최초 추세 경고' not in {t.name for t in fig.data}
    assert '최초 고정 손절 터치' in {t.name for t in fig.data}
    hold=next(t for t in fig.data if t.name=='보유 수익')
    assert pd.isna(hold.y[1]) and hold.connectgaps is False
    assert fig.layout.meta['observed_dates']==4
    pd.testing.assert_frame_equal(p,before)


def test_selected_tracking_chart_omits_excursion_when_source_columns_absent():
    _,daily,_=data()
    p=daily.iloc[:2].drop(columns=['mfe_pct','mae_pct','peak_close_return_pct'])
    fig=tracking_path_figure(p)
    assert fig.layout.meta['has_mfe'] is False
    assert fig.layout.meta['has_mae'] is False
    assert {t.name for t in fig.data if t.name} == {'보유 수익'}

