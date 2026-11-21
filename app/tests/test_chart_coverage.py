"""Chart availability is independent of tactical eligibility and page size."""
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
from test_chart_views import candle_payload, stock_record


@pytest.mark.parametrize('coverage', [None, [], 'invalid', {'missing': []}, {'missing': {'US:TEST': ['invalid']}}])
def test_legacy_or_invalid_diagnostics_do_not_invent_a_priority_reason(coverage):
    b = app.Bundle(man={}, summ={'chart_coverage': coverage})
    result = app.chart_missing_details(b, [('US', 'TEST', 'Example')])
    assert result.reason.tolist() == ['미제공 사유 정보 없음']


def test_missing_details_only_include_the_current_filter_and_keep_inputs():
    missing = {'US:TEST': 'cache_missing', 'KR:005930': 'limit_reached', 'US:OTHER': 'outside_chart_scope'}
    b = app.Bundle(man={}, summ={'chart_coverage': {'missing': missing}})
    rows = [('US', 'TEST', 'Example'), ('KR', '005930', '삼성전자')]
    details = app.chart_missing_details(b, rows)
    assert details.reason.tolist() == ['저장된 가격 이력 없음', '차트 생성 개수 제한']
    assert details.ticker.tolist() == ['TEST', '005930'] and len(missing) == 3 and len(rows) == 2


def grid_script(count=38, available=15, diagnostics=True, last_only=False):
    return f'''
import pandas as pd
import app
from unittest.mock import patch
from test_chart_views import candle_payload, stock_record
records=[dict(stock_record(), ticker=f'T{{i:03}}', name=f'Company {{i}}',
              topdown_score=1000.-i, selected=i<17, is_leading_sector=True, is_true_vcp=True) for i in range({count})]
b=app.Bundle(man={{'run_id':'coverage-fixture-{count}-{available}'}}, summ={{}}, cand=pd.DataFrame(records),
             charts={{f'US:T{{i:03}}':candle_payload(260) for i in range({available})}})
if {last_only!r}:
    b.charts={{f'US:T{{{count}-1:03}}':candle_payload(260)}}
if {diagnostics!r}:
    b.summ['chart_coverage']={{'missing':{{f'US:T{{i:03}}': 'limit_reached' if i<17 else 'outside_chart_scope' for i in range({available},{count})}}}}
f=app.filter_by_tactical_mode(app.prepare_candidates(b),'CAUTION',True,False,False,0,[],'')
with patch('app.requests.get',side_effect=AssertionError('chart view must not fetch prices')):
    app.render_multi_chart_grid(b,f)
'''


def test_partial_coverage_reports_real_reasons_and_paginates_without_changing_links():
    at = AppTest.from_string(grid_script(), default_timeout=20).run()
    assert not at.exception and len(at.get('plotly_chart')) == 12
    assert at.selectbox(key='grid_psz').value == 12 and at.radio(key='grid_cols').value == 2
    assert any('15개' in c.value and '미제공 23종목' in c.value for c in at.caption)
    assert any('범위 밖 21종목' in c.value and '개수 제한 2종목' in c.value for c in at.caption)
    assert not any('후순위' in c.value for c in at.caption)
    assert any('[📈TV]' in m.value and 'symbol=T000' in m.value for m in at.markdown)
    at.number_input(key='grid_page').set_value(2).run()
    assert not at.exception and len(at.get('plotly_chart')) == 3


def test_all_thirty_eight_candidates_have_charts_and_page_size_sixty_means_sixty_per_page():
    at = AppTest.from_string(grid_script(38, 38), default_timeout=20).run()
    assert not at.exception and len(at.get('plotly_chart')) == 12
    assert any('38개' in c.value for c in at.caption)
    assert not any('미제공' in e.label for e in at.expander)
    at.number_input(key='grid_page').set_value(4).run()
    assert not at.exception and len(at.get('plotly_chart')) == 2
    at.selectbox(key='grid_psz').set_value(60).run()
    assert not at.exception and len(at.get('plotly_chart')) == 38
    assert '60개씩' in list(at.selectbox(key='grid_psz').options)


def test_candidates_after_five_hundred_are_counted_without_rendering_all_candidates():
    # Chart only the final candidate: an implicit head(500) would hide it entirely.
    script = grid_script(503, 0, last_only=True)
    at = AppTest.from_string(script, default_timeout=20).run()
    assert not at.exception and len(at.get('plotly_chart')) == 1
    assert any('미제공 502종목' in c.value for c in at.caption)


def test_zero_charts_and_legacy_publication_still_explain_missing_data():
    at = AppTest.from_string(grid_script(2, 0, diagnostics=False), default_timeout=20).run()
    assert not at.exception and len(at.get('plotly_chart')) == 0
    assert any('미제공 사유 정보 없음 2종목' in c.value for c in at.caption)

