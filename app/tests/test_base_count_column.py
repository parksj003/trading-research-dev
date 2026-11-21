import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
import base_count_view as view
from test_base_count_view import frame,payload,bundle


def attach(doc=None,rows=None):
    raw,manifest=bundle(doc)
    observations,source=view.load_base_counts(raw,manifest)
    return view.attach_base_counts(frame() if rows is None else rows,observations,source)


def test_verified_number_and_state_share_one_column_without_changing_inputs():
    doc=payload();row=doc['observations'][0]
    row.update(current_base_ordinal=2,completed_base_count=1,status='BASE_FORMING',quality='ESTIMATED')
    before=frame().copy(deep=True)
    result=attach(doc)
    assert result.bc_display.tolist()==['2차 · 형성 중 (추정)']
    assert result.bc_current_base_ordinal.tolist()==[2]
    pd.testing.assert_frame_equal(result[before.columns],before)


@pytest.mark.parametrize('status,label',[('absent','자료 미게시'),('disabled','비활성'),('unavailable','자료 확인 불가')])
def test_all_current_rows_have_an_unknown_column_when_observations_are_unavailable(status,label):
    rows=pd.concat([frame(),frame().assign(ticker='ANOTHER')],ignore_index=True)
    before=rows.copy(deep=True)
    result=view.attach_base_counts(rows,pd.DataFrame(),{'status':status})
    assert result.bc_display.tolist()==['미확인 · '+label]*2
    assert result.bc_current_base_ordinal.isna().all() and result.bc_completed_base_count.isna().all()
    pd.testing.assert_frame_equal(rows,before)
    pd.testing.assert_frame_equal(result[before.columns],before)


def test_unknown_history_never_becomes_first_base_and_missing_instrument_is_distinct():
    doc=payload();doc['observations'][0].update(quality='HISTORY_INSUFFICIENT',current_base_ordinal=None,completed_base_count=None)
    rows=pd.concat([frame(),frame().assign(ticker='ANOTHER')],ignore_index=True)
    result=attach(doc,rows)
    assert result.bc_display.tolist()==['미확인 · 이력 부족','미확인 · 관측 자료 없음']
    assert result.bc_current_base_ordinal.isna().all()


@pytest.mark.parametrize('mode',['ATTACK','CAUTION','RISK_OFF'])
def test_each_tactical_table_has_one_base_column_for_every_displayed_stock(mode):
    code='''
import app,pandas as pd,streamlit as st
raw=pd.DataFrame([dict(market='US',sub_market='SP500',ticker='EX',name='Example',sector='Example',
 tier='Tier A',setup_stage='VCP',rs_score=85.,topdown_score=80.,exit_signal='✅정상',
 last_close=100.,pivot_price=100.,stop_dist_pct=-.05,position_size_pct=20.,portfolio_alloc_pct=0.,
 actionable_now=True,wide_stop_actionable=False,is_true_vcp=True,selected=True)])
captured=[];original=app.stock_dataframe
app.stock_dataframe=lambda table,**kw:captured.append(table)
try:
 app.render_mode_specific_table(raw,MODE,pd.DataFrame(),'SP500')
 st.session_state['labels']=list(captured[0].columns)
 st.session_state['bases']=captured[0]['베이스 순번'].tolist()
 st.session_state['rows']=len(captured[0])
finally:
 app.stock_dataframe=original
'''.replace('MODE',repr(mode))
    at=AppTest.from_string(code).run()
    assert not at.exception
    assert at.session_state['labels'].count('베이스 순번')==1
    assert '베이스 상태' not in at.session_state['labels'] and '베이스 관측 품질' not in at.session_state['labels']
    assert at.session_state['bases']==['미확인 · 자료 미게시'] and at.session_state['rows']==1


def test_old_payload_still_renders_verified_number_in_the_same_column():
    result=attach()
    assert result.bc_label.tolist()==['Base 1']
    assert result.bc_display.tolist()==['1차 · 돌파 완료']

