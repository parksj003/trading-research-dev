from types import SimpleNamespace

import pytest

import app
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize('held_kr,held_us', [(60.,0.), (0.,60.), (60.,50.), (33.33,33.33)])
def test_existing_holdings_and_new_allocation_respect_global_cap(monkeypatch, held_kr, held_us):
    monkeypatch.setattr(app.st, 'session_state', {'held_confirmed':True, 'held_kr_pct':held_kr, 'held_us_pct':held_us})
    bundle = SimpleNamespace(summ={'portfolio':{'exposure_cap_pct':100.}, 'market_limits':{
        'KR':{'exposure_cap':100.,'alloc':50.,'risk':1.},
        'US':{'exposure_cap':100.,'alloc':50.,'risk':1.}}})
    result = app.compute_remaining_portfolio(bundle)
    assert result['adj_total_alloc'] <= max(0.,100.-held_kr-held_us) + 1e-9
    assert result['adj_kr_alloc'] <= max(0.,100.-held_kr)
    assert result['adj_us_alloc'] <= max(0.,100.-held_us)
    assert result['adj_total_risk'] <= 2.


def test_unconfirmed_holdings_preserve_original_proposals(monkeypatch):
    monkeypatch.setattr(app.st, 'session_state', {'held_confirmed':False,'held_kr_pct':90.})
    b=SimpleNamespace(summ={'portfolio':{'exposure_cap_pct':100.}, 'market_limits':{
        'KR':{'exposure_cap':100.,'alloc':50.,'risk':1.},'US':{'exposure_cap':100.,'alloc':50.,'risk':1.}}})
    r=app.compute_remaining_portfolio(b)
    assert r['adj_total_alloc']==100. and r['scale_kr']==r['scale_us']==1.


def test_displayed_stock_allocations_also_respect_remaining_cap():
    at = AppTest.from_string('''
import app
import pandas as pd
import streamlit as st
st.session_state.update(held_confirmed=True,held_kr_pct=60.,held_us_pct=0.)
b=app.Bundle(man={},summ={'portfolio':{'exposure_cap_pct':100.},'market_limits':{
 'KR':{'exposure_cap':100.,'alloc':50.,'risk':1.},'US':{'exposure_cap':100.,'alloc':50.,'risk':1.}}})
f=pd.DataFrame([dict(market='KR',ticker=str(i),name='Synthetic',portfolio_alloc_pct=50./6,
 actionable_now=True,wide_stop_actionable=False) for i in range(6)]+
 [dict(market='US',ticker='US',name='Synthetic',portfolio_alloc_pct=50.,actionable_now=True,wide_stop_actionable=False)])
captured=[]
original=app.stock_dataframe
app.stock_dataframe=lambda frame,**kw:captured.append(frame)
try:
 app.render_mode_specific_table(f,'ATTACK',pd.DataFrame(),'전체 (KR+US)',app.compute_remaining_portfolio(b))
finally:
 app.stock_dataframe=original
st.session_state['display_total']=float(captured[0][app.LABELS['portfolio_alloc_pct']].sum())
''').run()
    assert not at.exception
    assert at.session_state['display_total'] <= 40.

