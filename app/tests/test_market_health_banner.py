"""Visible warnings are independent of the legacy four-day age threshold."""
import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize('health,expected', [
    ({'kr': {'kr_close_check_date': '2026-11-04', 'kr_price_status': 'delayed_or_holiday',
             'kr_price_message': 'KR 2026-11-04 마감 일봉 일부 미확보. 기존 자료 유지(휴장/공급 지연 확인 필요).'}},
     '2026-11-04 마감 일봉 일부 미확보'),
    ({'us': {'us_cap_fallback': 10, 'us_cap_missing': 2}}, '이전 정상 캐시 사용 10종목'),
    ({'kr': {'kr_close_check_date': '2026-11-04', 'kr_price_status': 'delayed_or_holiday'}}, '휴장/공급 지연'),
])
def test_banner_shows_current_close_delay_and_cache_fallback(health, expected):
    script = ('import app\n'
              f"b=app.Bundle(man={{'as_of':{{'KOSPI':'2026-11-03'}}}}, summ={{'health':{health!r}}})\n"
              'app.render_banner(b)')
    result = AppTest.from_string(script).run()
    assert not result.exception
    assert any(expected in item.value for item in result.warning)


@pytest.mark.parametrize('health', [{}, {'kr': {'kr_price_status': 'not_due'}},
                                    {'kr': {'kr_price_status': 'current'}}])
def test_old_bundles_morning_and_current_bundles_do_not_show_false_delay(health):
    script = ('import app\n'
              f"app.render_banner(app.Bundle(man={{}}, summ={{'health':{health!r}}}))")
    result = AppTest.from_string(script).run()
    assert not result.exception
    assert not any('미확보' in item.value or '캐시 사용' in item.value for item in result.warning)


def test_one_session_lagged_technical_candidates_receive_separate_price_warning():
    script = '''
import app
b=app.Bundle(man={},summ={'price_observation':{'tracks':{'SP500':{
 'benchmark_as_of':'2026-11-03','clock_reference_date':'2026-11-04',
 'session_unconfirmed':True,'lagging_candidate_rows':1,'lagging_actionable_rows':1}}}})
app.render_banner(b)
'''
    result=AppTest.from_string(script).run()
    assert not result.exception
    assert any('기술적 적격은 최신성 보증이 아닙니다' in item.value for item in result.warning)
    assert any('뒤처진 후보 1개' in item.value for item in result.warning)


def test_invalid_breadth_is_missing_not_zero_and_old_near_extreme_label_is_explained():
    # Missing Breadth remains invalid in the table and in all three opened charts.
    script = '''
import app
import streamlit as st
regs={'SP500':{'as_of':'2026-11-03','signal_light':'CAUTION',
  'breadth_valid':False,'breadth_status':'표본부족','pct_above_50dma':None,'pct_above_200dma':None,
  'nh_nl_label':'신고 8 / 신저 2','tier_a_total':4}}
original=app.show_plotly
def capture(fig,key):
 if key=='plt_breadth_bar': st.session_state['breadth_y']=list(fig.data[0].y)
app.show_plotly=capture
try:
 app.render_regime_cards_and_breadth(regs)
 st.session_state['mk_detail_open']=True
 app.render_market_snapshot_and_history(app.Bundle(man={},summ={'regimes':regs}))
finally: app.show_plotly=original
'''
    result = AppTest.from_string(script).run()
    assert not result.exception
    assert any('Breadth 산출불가' in item.value for item in result.warning)
    assert any('ex-NDX' in item.value for item in result.caption)
    assert any('고점 3% 근접 8' in item.value for item in result.caption)
    assert any('돌파 건수가 아닙니다' in item.value for item in result.caption)
    table = result.dataframe[0].value.set_index("지수")
    assert table.loc["SP500 ex-NDX", "50일/200일↑"] == "산출불가"
    assert table.loc[["KOSPI", "KOSDAQ", "NASDAQ100"], "50일/200일↑"].tolist() == ["미확인"] * 3
    assert result.session_state['breadth_y'][0] != result.session_state['breadth_y'][0]  # NaN, not zero.

