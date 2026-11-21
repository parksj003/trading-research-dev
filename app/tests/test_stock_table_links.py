import ast
import json
import re
import hashlib
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from stock_table_links import tradingview_url,table_links,DISPLAY_SYMBOL


@pytest.mark.parametrize('market,ticker,symbol',[
 ('KR','005930','KRX:005930'),('KOSDAQ','086450','KRX:086450'),('KR',5930,'KRX:005930'),
 ('US','aapl','AAPL'),('SP500','BRK-B','BRK.B'),('NASDAQ100','NVDA','NVDA'),
])
def test_correct_symbol(market,ticker,symbol):
    url=tradingview_url(market,ticker)
    assert url=='https://kr.tradingview.com/chart/?symbol='+symbol
    assert re.search(DISPLAY_SYMBOL,url).group(1)==symbol.removeprefix('KRX:')


@pytest.mark.parametrize('market,ticker',[('XX','AAPL'),('US',None),('KR',pd.NA),('KR','ABC'),('US','javascript:alert(1)'),('US','AAPL&symbol=OTHER')])
def test_invalid_identity_never_becomes_a_link(market,ticker):
    assert tradingview_url(market,ticker) is None


def test_mixed_korean_labels_and_track_inference_preserve_inputs_and_other_formats():
    source=pd.DataFrame({'종목명':['삼성전자','Apple'],'티커':['005930','AAPL'],'소속 트랙':['KOSPI','NASDAQ100'],'RS':[90.,85.]})
    before=source.copy(deep=True);config={'RS':{'custom':'kept'}}
    out,cfg=table_links(source,column_config=config)
    assert out.shape==source.shape
    assert out['티커'].tolist()==[tradingview_url('KR','005930'),tradingview_url('US','AAPL')]
    assert cfg['RS']==config['RS'] and '티커' in cfg
    pd.testing.assert_frame_equal(source,before)
    assert '티커' not in config


def test_unknown_rows_keep_original_tickers_and_get_safe_compact_link_column():
    d=pd.DataFrame({'market':['US',None],'ticker':['AAPL','UNKNOWN']})
    out,cfg=table_links(d)
    assert out.ticker.tolist()==d.ticker.tolist()
    assert out['ticker 차트'].iloc[0]==tradingview_url('US','AAPL')
    assert pd.isna(out['ticker 차트'].iloc[1])


def test_sector_leader_links_use_explicit_market_and_empty_tables_are_unchanged():
    out,cfg=table_links(pd.DataFrame({'leader_ticker':['005930']}),market='KR')
    assert out.leader_ticker.iloc[0].endswith('KRX:005930')
    assert cfg['leader_ticker']['label']=='대장 티커'
    empty=pd.DataFrame(columns=['ticker'])
    pd.testing.assert_frame_equal(table_links(empty)[0],empty)
    numbers=pd.DataFrame({'RS':[90.]})
    pd.testing.assert_frame_equal(table_links(numbers)[0],numbers)


def test_native_linkcolumn_renders_ticker_without_an_extra_url_column():
    at=AppTest.from_string('''
import pandas as pd
from stock_table_links import stock_dataframe
stock_dataframe(pd.DataFrame({'market':['KR','US'],'ticker':['005930','AAPL'],'name':['삼성전자','Apple']}),hide_index=True)
''').run()
    assert not at.exception
    table=at.dataframe[0]
    assert list(table.value)==['market','ticker','name']
    config=json.loads(table.proto.columns)
    assert config['ticker']['type_config']['type']=='link'
    assert config['ticker']['type_config']['display_text']==DISPLAY_SYMBOL


def test_external_chart_links_remain_byte_for_byte_unchanged():
    # Keep the external link builder pinned. The intentionally redesigned
    # multi-chart renderer has behavior tests in test_chart_coverage.
    expected = {'ext_links': 'f536b33283b15cf76835bafcfe7c8750e3a228f0765384c004c5a05a740ad2e6'}
    source=Path('app.py').read_text()
    lines=source.splitlines(keepends=True)
    for node in ast.parse(source).body:
        if isinstance(node,ast.FunctionDef) and node.name in expected:
            start=node.decorator_list[0].lineno if node.decorator_list else node.lineno
            block=''.join(lines[start-1:node.end_lineno])
            assert hashlib.sha256(block.encode()).hexdigest()==expected[node.name]

