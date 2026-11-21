import pandas as pd
from streamlit.testing.v1 import AppTest

import app


SCRIPT = '''
import app,pandas as pd,streamlit as st
regime=st.session_state.get('fixture_regime','RISK_OFF')
row=dict(market='KR',sub_market='KOSPI',ticker='123456',name='Synthetic',sector='Example',
 macro_theme='Example',rs_score=80.,rs_change_4w=2.,setup_stage='VCP',tier='Tier A',
 last_close=100.,pivot_price=100.,base_pivot_price=100.,stop_price=95.,stop_dist_pct=-.05,
 position_size_pct=20.,portfolio_alloc_pct=20.,selected=True,is_actionable=True,in_buy_zone=True,
 is_leading_sector=True,strict_minervini=True,exit_signal='✅정상',dist_to_pivot_pct=0.,
 atr_from_50dma=1.,dist_days_25d=0,range_10d_pct=.05,vol_dry_up_ratio=.5,vol_ratio_50d=1.5)
b=app.Bundle(man={'run_id':'fixture','schema_version':'1.0'},summ={
 'regimes':{'KOSPI':{'regime':regime,'signal_light':regime}},
 'portfolio':{'exposure_cap_pct':100.,'risk_cap_pct':5.},
 'market_limits':{'KR':{'exposure_cap':100.,'alloc':20.,'risk':1.},'US':{'exposure_cap':0.,'alloc':0.,'risk':0.}},
 'counts':{'actionable':1,'selected':1}},cand=pd.DataFrame([row]),watch=pd.DataFrame([row]))
original=(app.get_source,app.fetch_manifest,app.load_bundle)
app.get_source=lambda:{'mode':'local','dir':'synthetic'}
app.fetch_manifest=lambda *a:b.man
app.load_bundle=lambda *a:b
try:
 app.main()
finally:
 app.get_source,app.fetch_manifest,app.load_bundle=original
'''


def test_held_inputs_survive_real_main_navigation_and_are_session_local():
    at=AppTest.from_string(SCRIPT,default_timeout=30).run()
    at.toggle(key='_held_confirmed').set_value(True).run()
    at.slider(key='_held_kr_pct').set_value(50).run()
    at.radio(key='main_page').set_value('🎯 3. 전술·멀티차트').run()
    assert not at.exception and at.session_state['held_kr_pct']==50
    at.radio(key='main_page').set_value('🧭 1. 국면·나침반').run()
    assert not at.exception
    assert at.toggle(key='_held_confirmed').value and at.slider(key='_held_kr_pct').value==50
    fresh=AppTest.from_string(SCRIPT,default_timeout=30).run()
    assert not fresh.exception and not fresh.toggle(key='_held_confirmed').value
    assert fresh.slider(key='_held_kr_pct').value==0


def test_default_manual_mode_and_track_survive_navigation_and_recompute_on_regime_change():
    at=AppTest.from_string(SCRIPT,default_timeout=30).run()
    at.radio(key='main_page').set_value('🎯 3. 전술·멀티차트').run()
    at.selectbox(key='pb_track').set_value('KOSPI').run()
    assert at.radio(key='pb_mode').value=='RISK_OFF'
    at.radio(key='main_page').set_value('🧭 1. 국면·나침반').run()
    at.radio(key='main_page').set_value('🎯 3. 전술·멀티차트').run()
    assert at.selectbox(key='pb_track').value=='KOSPI' and at.radio(key='pb_mode').value=='RISK_OFF'
    at.radio(key='pb_mode').set_value('CAUTION').run()
    at.radio(key='main_page').set_value('🧭 1. 국면·나침반').run()
    at.radio(key='main_page').set_value('🎯 3. 전술·멀티차트').run()
    assert at.radio(key='pb_mode').value=='CAUTION'
    at.session_state['fixture_regime']='ATTACK'
    at.run()
    assert not at.exception and at.radio(key='pb_mode').value=='ATTACK'


def test_allocation_view_is_idempotent_and_preserves_published_inputs_and_eligibility():
    frame=pd.DataFrame([dict(market='KR',portfolio_alloc_pct=12.34,actionable_now=True)])
    before=frame.copy(deep=True)
    info={'scale_kr':.5,'scale_us':0.,'held_confirmed':True}
    view=app.allocation_view(frame,info)
    assert view.portfolio_alloc_pct.iloc[0]==6.1 and view.actionable_now.iloc[0]
    pd.testing.assert_frame_equal(frame,before)
    pd.testing.assert_frame_equal(view,app.allocation_view(view,info))


def test_existing_widget_values_are_detached_during_upgrade():
    legacy='''
import streamlit as st
version=st.radio('version',['old','new'],key='version')
if version=='old':
 st.toggle('legacy held',key='held_confirmed')
 st.slider('legacy KR',0,100,step=5,key='held_kr_pct')
else:
'''
    at=AppTest.from_string(legacy+'\n'.join(' '+line for line in SCRIPT.splitlines()),default_timeout=30).run()
    at.toggle(key='held_confirmed').set_value(True).run()
    at.slider(key='held_kr_pct').set_value(50).run()
    at.radio(key='version').set_value('new').run()
    at.radio(key='main_page').set_value('🎯 3. 전술·멀티차트').run()
    assert not at.exception
    assert at.session_state['_held_inputs']['held_confirmed'] and at.session_state['_held_inputs']['held_kr_pct']==50


def test_real_playbook_unified_diagnostics_uses_adjusted_allocation():
    at=AppTest.from_string(SCRIPT,default_timeout=30).run()
    at.toggle(key='_held_confirmed').set_value(True).run()
    at.slider(key='_held_kr_pct').set_value(100).run()
    at.radio(key='main_page').set_value('🎯 3. 전술·멀티차트').run()
    # Chart-less candidates still have access to the integrated SEPA/allocation
    # diagnostic. The obsolete third sub-view no longer exists.
    assert len(at.radio(key='pb_sub_view').options)==2
    at.selectbox(key='grid_missing_pick').set_value('KR:123456').run()
    assert not at.exception
    assert any('선택 종목 정밀 진단' in m.value for m in at.markdown)
    assert any('게시 적격 · 배분 0.0%' in m.value for m in at.markdown)


def test_zero_remaining_budget_is_shared_by_table_deep_dive_and_pie():
    script=SCRIPT[:SCRIPT.index('original=')]+'''
st.session_state.update(held_confirmed=True,held_kr_pct=100.,held_us_pct=0.)
f=app._cached_prepare_candidates.__wrapped__('view-fixture',b.cand,b.watch,pd.DataFrame())
f=app.allocation_view(f,app.compute_remaining_portfolio(b))
tables=[];figures={}
original=(app.stock_dataframe,app.show_plotly)
app.stock_dataframe=lambda frame,**kw:tables.append(frame)
app.show_plotly=lambda fig,key:figures.update({key:fig})
try:
 app.render_mode_specific_table(f,'ATTACK',pd.DataFrame(),'KOSPI',app.compute_remaining_portfolio(b))
 app.render_single_stock_deep_dive(b,f)
 app.render_portfolio_visuals(b,app.compute_remaining_portfolio(b))
 st.session_state['table_alloc']=float(tables[0][app.LABELS['portfolio_alloc_pct']].iloc[0])
 st.session_state['pie_absent']='plt_sec_pie' not in figures
 st.session_state['published_alloc']=float(b.watch.portfolio_alloc_pct.iloc[0])
finally:
 app.stock_dataframe,app.show_plotly=original
'''
    at=AppTest.from_string(script,default_timeout=30).run()
    assert not at.exception
    assert at.session_state['table_alloc']==0. and at.session_state['pie_absent']
    assert at.session_state['published_alloc']==20.
    metric=next(m for m in at.metric if m.label=='권장비중 (포트배분)')
    assert metric.delta=='포트 0.0%'

