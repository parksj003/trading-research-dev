from pathlib import Path
import json
import re

import pytest
from streamlit.testing.v1 import AppTest

from test_manual_catalog import row
from manual_catalog import csv_bytes


@pytest.fixture(autouse=True)
def offline_prices(monkeypatch):
    import manual_watchlist_view as view
    monkeypatch.setattr(view,'load_chart_data',lambda *a,**k:pytest.fail('EOD screen fetched live prices'))


def test_widget_uses_native_symbol_validated_controls_and_official_source_only():
    import manual_watchlist_view as v
    config=v.widget_config(row(), interval='W', theme='dark', ma=50)
    assert config['symbols']==[['KRX:005930','KRX:005930|6M']]
    assert config['dateRanges']==['6m|1W']
    assert config['showMA'] is True and config['maLength']==50
    assert config['showVolume'] is True
    assert not v.widget_config(row(asset_type='yield'), interval='D', theme='light', ma=0)['showVolume']
    markup=v.widget_html(row(name='</script><script>alert(1)</script>'),interval='D',theme='light',ma=0)
    assert 'alert(1)' not in markup
    assert 'https://s3.tradingview.com/external-embedding/embed-widget-symbol-overview.js' in markup
    assert 'TradingView' in markup
    with pytest.raises(ValueError):v.widget_html(row(symbol='NYSE:A</script>'),interval='D',theme='light',ma=0)
    with pytest.raises(ValueError):v.widget_config(row(), interval='1S', theme='dark', ma=50)


def app(tmp_path, rows):
    file=tmp_path/'list.csv';file.write_bytes(csv_bytes(rows))
    return AppTest.from_string('from manual_watchlist_view import render_manual_watchlist\nfrom manual_catalog import parse_csv\nfrom manual_eod_fixture import snapshot_for\nfrom pathlib import Path\np='+repr(str(file))+'\ntry:\n doc,_=snapshot_for(parse_csv(Path(p).read_bytes()))\nexcept ValueError:\n doc={}\nrender_manual_watchlist(p,theme="light",snapshot=doc,source_state={"status":"ready"})',default_timeout=30)


def test_filters_dedup_and_pagination_limit_widgets_without_mutating_file(tmp_path):
    rows=[row(symbol=f'NASDAQ:SYNTH{i}',name=f'Synthetic {i}',sort_order=i) for i in range(15)]
    rows.append(row(symbol='NASDAQ:SYNTH0',watch_region='일본',watch_sector='別',sort_order=16))
    at=app(tmp_path,rows);before=(tmp_path/'list.csv').read_bytes();at.run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==12 and not at.get('iframe')
    assert any('15개 고유 심볼' in x.value for x in at.caption)
    at.number_input(key='manual_page').set_value(2).run()
    assert not at.exception and len(at.get('plotly_chart'))==3
    at.text_input(key='manual_query').input('Synthetic 1').run()
    assert not at.exception and at.number_input(key='manual_page').value==1
    assert len(at.get('plotly_chart'))==6
    at.selectbox(key='manual_region').set_value('일본').run()
    assert not at.exception and not at.get('plotly_chart')
    assert before==(tmp_path/'list.csv').read_bytes()


def test_bad_csv_and_missing_file_fail_without_empty_success(tmp_path):
    at=app(tmp_path,[row(),row()]).run()
    assert not at.exception and len(at.error)==1 and not at.get('iframe')
    at=AppTest.from_string('from manual_watchlist_view import render_manual_watchlist\nrender_manual_watchlist('+repr(str(tmp_path/'missing.csv'))+')').run()
    assert not at.exception and at.error and not at.get('iframe')


def test_unconfigured_manual_page_warns_without_source_or_private_account_access(monkeypatch):
    import requests
    import portfolio_view
    import manual_snapshot_source
    monkeypatch.setattr(portfolio_view,'setting',lambda name,default='':default)
    monkeypatch.setattr(requests.sessions.Session,'request',lambda *a,**k:pytest.fail('manual page fetched backend source'))
    monkeypatch.setattr(manual_snapshot_source,'load_published_eod',lambda *a,**k:pytest.fail('unconfigured page loaded EOD source'))
    monkeypatch.setattr(portfolio_view,'read_snapshot',lambda *a,**k:pytest.fail('manual page read private account'))
    monkeypatch.setattr(portfolio_view,'render',lambda *a,**k:pytest.fail('manual page rendered private account'))
    at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=40)
    at.session_state['main_page']='📌 6. 관심종목·매크로'
    at.run()
    assert not at.exception and not at.error
    assert at.radio(key='main_page').value=='📌 6. 관심종목·매크로'
    assert not at.get('plotly_chart') and not at.get('iframe')
    assert any('EOD 데이터 소스가 설정되지 않았습니다.' in item.value for item in at.warning)


@pytest.mark.parametrize('old_state',['1 / 54','1',None,99,0])
def test_stale_or_formatted_page_state_is_reset_before_filter_rerender(tmp_path,old_state):
    at=app(tmp_path,[row()]);at.session_state['manual_page']=old_state
    at.run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==1
    assert at.session_state['manual_page']==1

