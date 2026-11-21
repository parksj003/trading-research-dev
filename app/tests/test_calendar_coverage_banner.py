import pytest
from streamlit.testing.v1 import AppTest


@pytest.mark.parametrize('message', ['', '2026년까지 검토됨: 이후 확정 가격 미확보'])
def test_calendar_coverage_warning_is_visible_without_a_stale_date(message):
    code='''
import app
b=app.Bundle(man={},summ={'health':{'kr':{'kr_price_status':'calendar_unreviewed','kr_price_message':MESSAGE}}})
app.render_banner(b)
'''.replace('MESSAGE',repr(message))
    at=AppTest.from_string(code).run()
    assert not at.exception
    assert any((message or '일정 검토') in item.value for item in at.warning)

