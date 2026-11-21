import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app


def row(**changes):
    return dict(dict(actionable_now=False, tier='Tier A', stop_dist_pct=-.05,
                     exit_signal='✅정상', earnings_tag='', setup_stage='VCP'), **changes)


@pytest.mark.parametrize('tag,blackout', [('D-0',True),('D-1',True),('D-2',True),('D-3',True),
    ('D-4',False),('D-10',False),('D-14',False),('D-30',False),('',False),(None,False),(np.nan,False),
    ('실적 D-14 · 진입보류',True)])
def test_exact_earnings_day_parsing(tag, blackout):
    result=app.build_exclusion_reason(pd.DataFrame([row(earnings_tag=tag)])).iloc[0]
    assert ('실적블랙아웃' in result) is blackout
    assert 'nan' not in result.lower()


def test_unknown_values_do_not_invent_tier_or_quota_and_do_not_mutate():
    frame=pd.DataFrame([row(tier=np.nan,exit_signal=np.nan,stop_dist_pct=None,setup_stage=None),row()])
    before=frame.copy(deep=True)
    results=app.build_exclusion_reason(frame)
    assert '미확인' in results.iloc[0] and 'nan' not in results.iloc[0].lower()
    assert results.iloc[1]=='상세 제외 사유 미확인'
    assert not results.str.contains('섹터쿼터').any()
    pd.testing.assert_frame_equal(frame,before)


def test_selected_and_actionable_are_distinct_display_facts():
    result=app.build_exclusion_reason(pd.DataFrame([row(selected=False),row(actionable_now=True),row(actionable_now='False')]))
    assert result.iloc[0]=='Top20 미선정(세부 사유 미확인)'
    assert result.iloc[1]=='✅ 적격'
    assert result.iloc[2]=='상세 제외 사유 미확인'


def test_deep_dive_receives_the_same_reference_reason():
    at=AppTest.from_string('''
import app,pandas as pd,streamlit as st
raw=pd.DataFrame([dict(market='US',sub_market='SP500',ticker='EX',name='Example',tier='Tier A',
 stop_dist_pct=-.05,exit_signal='✅정상',earnings_tag='D-14',setup_stage='VCP',rs_score=80.,
 last_close=100.,pivot_price=100.,stop_price=95.,position_size_pct=20.,is_actionable=False)])
frame=app._cached_prepare_candidates.__wrapped__('reason-fixture',raw,pd.DataFrame(),pd.DataFrame())
st.session_state['reason']=frame.exclusion_reason.iloc[0]
app.render_single_stock_deep_dive(app.Bundle(man={},summ={}),frame)
''').run()
    assert not at.exception
    assert at.session_state['reason']=='상세 제외 사유 미확인'
    assert any(at.session_state['reason'] in element.value for element in at.markdown)

