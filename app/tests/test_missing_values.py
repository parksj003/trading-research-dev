from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app


@pytest.mark.parametrize('value', [0., None, np.nan, np.inf, 'invalid'])
def test_caps_preserve_zero_and_do_not_allow_unknown_limits(monkeypatch, value):
    monkeypatch.setattr(app.st, 'session_state', {'held_confirmed': True, 'held_kr_pct': 0., 'held_us_pct': 0.})
    b = SimpleNamespace(summ={'portfolio': {'exposure_cap_pct': 100.}, 'market_limits': {
        'KR': {'exposure_cap': value, 'alloc': 20., 'risk': 1.},
        'US': {'exposure_cap': 100., 'alloc': 0., 'risk': 0.}}})
    result = app.compute_remaining_portfolio(b)
    if value == 0:
        assert result['kr_cap'] == result['scale_kr'] == result['adj_kr_alloc'] == 0.
    else:
        assert pd.isna(result['kr_cap']) and pd.isna(result['adj_kr_alloc'])
        assert pd.isna(result['adj_total_alloc'])


def test_zero_risk_budget_and_missing_snapshot_are_not_replaced_by_safe_defaults():
    at = AppTest.from_string('''
import app
import streamlit as st
captured={}
original=app.show_plotly
app.show_plotly=lambda fig,key: captured.update({key:fig})
try:
 b=app.Bundle(man={},summ={'portfolio':{'exposure_cap_pct':0.,'risk_cap_pct':0.}})
 app.render_portfolio_visuals(b,app.compute_remaining_portfolio(b))
 st.session_state['mk_detail_open']=True
 app.render_market_snapshot_and_history(app.Bundle(man={},summ={'regimes':{
  'KOSPI':{},'KOSDAQ':{'benchmark_close':100.,'benchmark_sma50':100.,'benchmark_sma200':100.,'distribution_days_25d':0}}}))
 st.session_state['risk_text']=list(captured['plt_cap_risk'].data[1].text)
 st.session_state['distribution_text']=list(captured['plt_idx_ddays'].data[0].text)
 st.session_state['distribution_colors']=list(captured['plt_idx_ddays'].data[0].marker.color)
 st.session_state['distance_text']=list(captured['plt_idx_dist'].data[0].text)
finally:
 app.show_plotly=original
''').run()
    assert not at.exception
    assert any('한도 0.0%' in x for x in at.session_state['risk_text'])
    assert at.session_state['distribution_text'] == ['자료 없음', '0일']
    assert at.session_state['distribution_colors'] == [app.PALETTE['slate'], app.PALETTE['green']]
    assert at.session_state['distance_text'] == ['자료 없음', '+0.0%']
    assert any('분배일 자료 없음: KOSPI' in item.value for item in at.caption)
    assert any('이격 자료 없음: KOSPI' in item.value for item in at.caption)


def test_missing_global_limit_does_not_expand_confirmed_holdings_budget(monkeypatch):
    monkeypatch.setattr(app.st, 'session_state', {'held_confirmed': True})
    b=SimpleNamespace(summ={'portfolio': {'exposure_cap_pct': None}, 'market_limits': {
        'KR': {'exposure_cap': 100., 'alloc': 20.}, 'US': {'exposure_cap':100., 'alloc':20.}}})
    assert pd.isna(app.compute_remaining_portfolio(b)['adj_total_alloc'])

