"""Independent identity, ordering, allocation-language and chart evidence checks."""
import json
import numpy as np
import pandas as pd
from streamlit.testing.v1 import AppTest
import app
from test_chart_views import stock_record, candle_payload


def rows():
    return pd.DataFrame([dict(stock_record(),ticker=t,actionable_now=True,
       in_buy_zone=True,wide_stop_actionable=False,is_leading_sector=True,
       selected=True,is_true_vcp=False,exit_signal='✅정상',tier='Tier A',
       dist_to_pivot_pct=d,stop_dist_pct=-.05) for t,d in [('Z',.02),('A',0.),('M',np.nan)]])


def test_tactical_ties_are_independent_of_input_order():
    f=rows()
    def run(x):return app.filter_by_tactical_mode(x,'ATTACK',False,False,False,0,[],'').ticker.tolist()
    assert run(f)==run(f.iloc[::-1])==['A','M','Z']


def test_zero_distance_is_first_and_nan_is_last_in_actual_grid():
    script='''
import app
from test_tactical_audit import rows
from test_chart_views import candle_payload
b=app.Bundle(man={'run_id':'audit-grid'},summ={},charts={f'US:{t}':candle_payload() for t in ['Z','A','M']})
app.render_multi_chart_grid(b,rows())
'''
    at=AppTest.from_string(script).run()
    at.selectbox(key='grid_sort').set_value('피벗 근접 순').run()
    assert not at.exception
    # Widget key is grid_detail_btn_US_TICKER_index.
    ids=[x.key.split('_')[4] for x in at.button if x.key.startswith('grid_detail_btn_')]
    assert ids==['A','Z','M']


def test_zero_allocation_is_not_labelled_available_or_recommended():
    script='''
import app
import pandas as pd
from test_tactical_audit import rows
from test_chart_views import candle_payload
f=rows().head(1)
b=app.Bundle(man={'run_id':'audit-zero'},summ={},charts={'US:Z':candle_payload()})
app.render_mode_specific_table(f,'ATTACK',pd.DataFrame(),'전체 (KR+US)')
app.render_multi_chart_grid(b,f)
'''
    at=AppTest.from_string(script).run()
    assert not at.exception
    words=' '.join(x.value for x in list(at.markdown)+list(at.caption))
    assert '배분 가능' not in words and '권장 5%' not in words
    assert '배분 0%' in words


def test_grid_inventory_exclusive_reasons_and_market_identity():
    f=pd.DataFrame([dict(stock_record(),market=m,ticker=t) for m,t in
                    [('KR','123'),('US','000123'),('KR','000123'),('US','BAD'),('US','EMPTY'),('US','NONE')]])
    good=candle_payload();bad=dict(good,pivot=500.)
    charts={'KR:000123':good,'US:000123':good,'US:BAD':bad,'US:EMPTY':{}}
    available,missing,reasons=app.tactical_chart_rows(f,charts)
    assert [x[4] for x in available]==['KR:000123','US:000123']
    assert reasons=={'duplicates':['KR:000123'],'US:BAD':'chart_line_mismatch','US:EMPTY':'invalid_chart_payload'}
    assert len(available)+len(missing)+len(reasons['duplicates'])==6


def test_table_groups_and_current_table_grid_order_match():
    f=rows();f.loc[0,['actionable_now','wide_stop_actionable']]=[False,False]
    f.loc[1,['actionable_now','wide_stop_actionable']]=[False,True]
    f.loc[0,'topdown_score']=100.;f.loc[1,'topdown_score']=50.
    ordered=app.filter_by_tactical_mode(f,'ATTACK',False,False,False,0,[],'')
    assert ordered.ticker.tolist()==['M','A','Z']
    grid,_,_=app.tactical_chart_rows(ordered,{f'US:{t}':candle_payload() for t in ['A','M','Z']})
    assert [r[4] for r in grid]==['US:M','US:A','US:Z']


def test_literal_search_accepts_brackets():
    assert app.filter_by_tactical_mode(rows(),'ATTACK',False,False,False,0,[],'[').empty


def test_empty_grid_clears_previous_page_and_detail():
    at=AppTest.from_string('''
import app, pandas as pd, streamlit as st
st.session_state['grid_page']=5
st.session_state['grid_detail_key']='US:OLD'
app.render_multi_chart_grid(app.Bundle(man={},summ={}),pd.DataFrame())
''').run()
    assert not at.exception
    assert at.session_state['grid_page']==1 and at.session_state['grid_detail_key'] is None


def test_plotly_ohlc_and_lines_are_the_same_snapshot():
    ch=candle_payload();ch.update(pivot=100.125,stop=94.321,snapshot_as_of=ch['d'][-1])
    fig=app.candle_chart(ch,'US')
    assert list(fig.data[0].close)==ch['c'][-130:]
    assert [(s.y0,s.y1) for s in fig.layout.shapes]==[
        (100.125*.975,100.125*1.05),(100.125,100.125),(94.321,94.321)]


def test_legacy_six_decimal_lines_are_accepted_without_false_exclusion():
    row=dict(stock_record(),pivot_price=78.08000183105469,stop_price=69.13318448638917)
    ch=dict(candle_payload(),pivot=78.080002,stop=69.133184)
    available,missing,_=app.tactical_chart_rows(pd.DataFrame([row]),{'US:TEST':ch})
    assert len(available)==1 and not missing


def test_missing_distribution_days_do_not_crash_open_diagnostics():
    at=AppTest.from_string('''
import pandas as pd, app
app.render_stock_sepa_checklist(pd.Series({'dist_days_25d':float('nan')}))
''').run()
    assert not at.exception
    assert any('분배일 자료 없음' in x.value for x in at.markdown)

