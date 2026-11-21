"""TradingView links at the table presentation boundary; source frames stay raw."""
import re

import pandas as pd
import streamlit as st

_MARKETS = {'KR':'KR','KOSPI':'KR','KOSDAQ':'KR','US':'US','NASDAQ100':'US','SP500':'US'}
DISPLAY_SYMBOL = r'symbol=(?:KRX:)?([^&]+)'


def tradingview_url(market, ticker):
    market = _MARKETS.get(str(market).strip().upper())
    if ticker is None or pd.isna(ticker):
        return None
    symbol = str(ticker).strip().upper()
    if market == 'KR':
        if not re.fullmatch(r'\d{1,6}',symbol):
            return None
        symbol = 'KRX:' + symbol.zfill(6)
    elif market == 'US':
        if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,14}',symbol):
            return None
        symbol = symbol.replace('-', '.')
    else:
        return None
    return 'https://kr.tradingview.com/chart/?symbol=' + symbol


def table_links(frame, *, market=None, column_config=None):
    """Replace displayed tickers only. Unknown identities get a separate safe link.

    Native LinkColumn retains Streamlit's table controls, without HTML rendering.
    """
    config = dict(column_config or {})
    if not isinstance(frame,pd.DataFrame):
        return frame,config
    out = frame.copy()
    for col in ('ticker','티커','leader_ticker','대장 티커'):
        if col not in out:
            continue
        links=[]
        for row in out.to_dict('records'):
            m = market
            if m is None:
                m = next((row.get(k) for k in ('market','시장','sub_market','소속 트랙','트랙')
                          if str(row.get(k)).upper() in _MARKETS),None)
            links.append(tradingview_url(m,row[col]))
        if not any(links):
            continue
        target = col if all(links) else col+' 차트'
        out[target]=links
        config[target]=st.column_config.LinkColumn(
            '대장 티커' if col in ('leader_ticker','대장 티커') and target==col else ('티커' if target==col else '차트'),
            display_text=DISPLAY_SYMBOL if target==col else '↗',
            help='티커를 클릭하면 TradingView 차트가 열립니다. 한국은 KRX, 미국은 TradingView 심볼 검색을 사용합니다.',
        )
    return out,config


def stock_dataframe(frame, *, market=None, column_config=None, **kwargs):
    display,config=table_links(frame,market=market,column_config=column_config)
    return st.dataframe(display,column_config=config,**kwargs)

