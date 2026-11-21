"""Read-only public prices for precisely mapped manual-list instruments.

No strategy, account, signal history or engine bundle dependency. Futures and
unmapped yields are never silently replaced with related ETFs/contracts.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from urllib.parse import quote
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import streamlit as st

from manual_catalog import normalize_full_symbol

DISPLAY_PERIODS={'1개월':1,'3개월':3,'6개월':6,'1년':12,'2년':24}

INDEX_MAP={'KRX:KOSPI':'^KS11','KRX:KOSPI200':'^KS200','KRX:KOSDAQ':'^KQ11',
    'TVC:DJI':'^DJI','NASDAQ:IXIC':'^IXIC','SP:SPX':'^GSPC','TVC:VIX':'^VIX',
    'TVC:SOX':'^SOX','TVC:NI225':'^N225','TVC:UKX':'^FTSE','XETR:DAX':'^GDAXI',
    'TVC:SX5E':'^STOXX50E','TVC:SXXP':'^STOXX','SPREADEX:CAC':'^FCHI',
    'TVC:HSI':'^HSI','SSE:000001':'000001.SS','SZSE:399001':'399001.SZ','SSE:000300':'000300.SS'}
# Confirmed restricted in a real widget. Do not render known broken embeds.
LINK_ONLY={'TVC:KR03Y','TVC:KR01Y','TVC:US02Y','TVC:US10Y','TVC:US30Y',
    'TVC:DE02Y','TVC:DE10Y','TSE:TOPIX','NYMEX:CL1!','CME_MINI:RTY1!',
    'ICEEUR:BRN1!','MCX:COPPER1!'}


def source_spec(row):
    symbol=normalize_full_symbol(row['symbol']);exchange,code=symbol.split(':')
    candidates=()
    if row['asset_type']=='index' and symbol in INDEX_MAP:candidates=(INDEX_MAP[symbol],)
    elif row['asset_type'] in ('stock','etf') and code.isdigit():
        if exchange=='KRX':candidates=(code+'.KS',code+'.KQ')
        elif exchange=='TSE':candidates=(code+'.T',)
        elif exchange=='HKEX':candidates=(code.zfill(4)+'.HK',)
    if candidates:return dict(provider='yahoo',candidates=candidates)
    if symbol in LINK_ONLY:return dict(provider='link',candidates=())
    return dict(provider='tradingview',candidates=())


class PriceUnavailable(ValueError):
    pass


class AssetTypeMismatch(PriceUnavailable):
    pass


def decode_yahoo(doc, symbol, asset_type, unit, *, now=None):
    try:
        chart=doc['chart']
        if chart.get('error'):raise PriceUnavailable('공급자가 이 심볼의 가격을 제공하지 않습니다.')
        result=chart['result'][0];meta=result['meta']
        if meta['symbol']!=symbol:raise PriceUnavailable('응답 심볼이 요청과 다릅니다.')
        expected={'index':('INDEX',),'stock':('EQUITY',),'etf':('ETF',)}[asset_type]
        if meta.get('instrumentType') not in expected:raise AssetTypeMismatch('공급자 자산 유형이 목록과 다릅니다.')
        if asset_type in ('stock','etf') and unit in ('KRW','JPY','HKD') and meta.get('currency')!=unit:
            raise PriceUnavailable('공급자 통화가 목록과 다릅니다.')
        zone=ZoneInfo(meta['exchangeTimezoneName'])
        timestamps=result['timestamp'];prices=result['indicators']['quote'][0]
        if len(set(timestamps))!=len(timestamps):raise PriceUnavailable('가격 시각이 중복되었습니다.')
        if any(len(prices[k])!=len(timestamps) for k in ('open','high','low','close')):
            raise PriceUnavailable('가격 배열의 길이가 맞지 않습니다.')
        now=datetime.now(timezone.utc) if now is None else now
        bars=[];skipped=0
        for i,timestamp in enumerate(timestamps):
            when=datetime.fromtimestamp(timestamp,timezone.utc)
            if when.timestamp()>now.timestamp()+300:raise PriceUnavailable('미래 시각의 가격 응답입니다.')
            values=[prices[k][i] for k in ('open','high','low','close')]
            if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in values):skipped+=1;continue
            o,h,l,c=values
            if l>min(o,c) or h<max(o,c) or l>h:raise PriceUnavailable('고가·저가·시가·종가가 맞지 않습니다.')
            volume=prices.get('volume',[])
            volume=volume[i] if i<len(volume) else None
            if type(volume) not in (int,float) or not math.isfinite(volume) or volume<0:volume=None
            bars.append(dict(date=when.astimezone(zone).date().isoformat(),open=o,high=h,low=l,close=c,volume=volume))
        bars.sort(key=lambda b:b['date'])
        if len({b['date'] for b in bars})!=len(bars):raise PriceUnavailable('같은 거래일의 가격이 중복되었습니다.')
        if len(bars)<2:raise PriceUnavailable('추세를 표시할 가격 이력이 부족합니다.')
        return dict(provider='Yahoo Finance',provider_symbol=symbol,currency=meta.get('currency',''),
            timezone=str(zone),bars=bars,skipped=skipped,retrieved_at=now.isoformat())
    except PriceUnavailable:raise
    except (KeyError,IndexError,TypeError,ValueError,OverflowError,OSError) as exc:
        raise PriceUnavailable('가격 응답 형식을 확인할 수 없습니다.') from exc


@st.cache_data(ttl=3600,max_entries=512,show_spinner=False)
def fetch_yahoo(candidates,asset_type,unit):
    korean_pair=(len(candidates)==2 and candidates[0].endswith('.KS') and
        candidates[1]==candidates[0][:-3]+'.KQ' and candidates[0][:-3].isdigit())
    for symbol in candidates:
        try:
            response=requests.get('https://query1.finance.yahoo.com/v8/finance/chart/'+quote(symbol,safe=''),
                params={'range':'2y','interval':'1d'},headers={'User-Agent':'Mozilla/5.0'},timeout=(5,20))
            if response.status_code==404:continue
            if response.status_code!=200:raise PriceUnavailable(f'가격 공급자 HTTP {response.status_code}.')
            return decode_yahoo(response.json(),symbol,asset_type,unit)
        except AssetTypeMismatch:
            # Yahoo can return a MUTUALFUND placeholder on the wrong Korean
            # venue. Reject that product, then require the same code to pass
            # all identity/currency/OHLC checks on the other venue.
            if korean_pair:continue
            raise
        except requests.RequestException as exc:raise PriceUnavailable('가격 공급자 연결에 실패했습니다. 잠시 후 새로고침하세요.') from exc
    raise PriceUnavailable('대응되는 종목의 공개 가격 이력을 찾지 못했습니다.')


def load_chart_data(row):
    spec=source_spec(row)
    if spec['provider']!='yahoo':return None
    return fetch_yahoo(spec['candidates'],row['asset_type'],row['unit'])


def price_frame(data,interval):
    if interval not in ('D','W','M'):raise ValueError('지원하지 않는 주기')
    frame=pd.DataFrame(data['bars']);frame['date']=pd.to_datetime(frame['date']);frame=frame.set_index('date')
    if interval!='D':
        frame['last_session']=frame.index
        frame=frame.resample('W-FRI' if interval=='W' else 'ME').agg(
            {'open':'first','high':'max','low':'min','close':'last','volume':lambda v:v.sum(min_count=1),'last_session':'last'})
        frame=frame.dropna(subset=['close']).set_index('last_session')
    return frame


def price_figure(row,data,*,interval,theme,ma,period='6개월'):
    if period not in DISPLAY_PERIODS:raise ValueError('지원하지 않는 표시 기간')
    frame=price_frame(data,interval)
    volume=row['asset_type'] in ('stock','etf') and frame['volume'].notna().any()
    fig=make_subplots(rows=2 if volume else 1,cols=1,shared_xaxes=True,
        row_heights=[.8,.2] if volume else [1],vertical_spacing=.035)
    if row['chart_style']=='candles':
        fig.add_trace(go.Candlestick(x=frame.index,open=frame['open'],high=frame['high'],low=frame['low'],close=frame['close'],name=row['symbol']),row=1,col=1)
    else:fig.add_trace(go.Scatter(x=frame.index,y=frame['close'],mode='lines',name=row['symbol']),row=1,col=1)
    if ma and len(frame)>=ma:
        fig.add_trace(go.Scatter(x=frame.index,y=frame['close'].rolling(ma,min_periods=ma).mean(),mode='lines',name=f'MA {ma}',line=dict(width=1.5)),row=1,col=1)
    if volume:fig.add_trace(go.Bar(x=frame.index,y=frame['volume'],name='거래량',marker_color='#94a3b8'),row=2,col=1)
    fig.update_layout(height=400,margin=dict(l=8,r=8,t=20,b=8),template='plotly_dark' if theme=='dark' else 'plotly_white',
        showlegend=False,xaxis_rangeslider_visible=False,uirevision=row['symbol']+interval+period)
    fig.update_yaxes(title_text=row['unit'],row=1,col=1)
    # Compute indicators from all available bars, then change the visible range.
    # A month means a calendar month regardless of the selected candle interval.
    end=frame.index[-1]
    fig.update_xaxes(range=[end-pd.DateOffset(months=DISPLAY_PERIODS[period]),end])
    return fig,len(frame)


# EOD rendering entrypoints. The legacy network helpers above are retained only
# for compatibility; the observation screen never invokes them.
from ohlcv_chart import MA_COLORS,add_price_traces
from manual_eod_contract import digest,canonical

EOD_PERIODS=(20,45,80,130,260,520)
EOD_HEIGHT={1:520,2:330,3:300,4:280}


def eod_chart(entry,bars=130,columns=2,theme='light',volume=True):
    if bars not in EOD_PERIODS or columns not in EOD_HEIGHT or theme not in ('light','dark'):raise ValueError('invalid EOD chart settings')
    full=entry.get('bars') or {}
    alternate=entry.get('close_only') or {}
    # A standalone close-only series is visible only when it contains a
    # genuinely newer final session (or there are no valid candles at all).
    # Never combine close-only price basis with another OHLC/adjusted series.
    close_mode=bool(alternate and (not full or
        alternate['last_final_session']>entry.get('last_final_session','')))
    prices=alternate if close_mode else full
    closes=pd.Series(prices['c'],dtype=float)
    chart={key:list(value) for key,value in prices.items()};missing=[]
    for length in (20,50,200):
        if len(closes)<length:missing.append(length);continue
        chart['s'+str(length)]=[None if pd.isna(v) else float(v) for v in closes.rolling(length,min_periods=length).mean()]
    has_volume=not close_mode and volume and any(v is not None for v in prices['v'])
    fig=make_subplots(rows=2 if has_volume else 1,cols=1,shared_xaxes=True,
        row_heights=[.76,.24] if has_volume else [1],vertical_spacing=.03)
    add_price_traces(fig,chart,bars,columns>=2,'#E11D48','#10B981',
        kind='line' if close_mode else 'candles')
    if has_volume:
        colors=['#E11D48' if c>=o else '#10B981' for o,c in zip(prices['o'][-bars:],prices['c'][-bars:])]
        fig.add_trace(go.Bar(x=prices['d'][-bars:],y=prices['v'][-bars:],name='거래량',marker_color=colors,opacity=.65),row=2,col=1)
    dark=theme=='dark';text='#CBD5E1' if dark else '#334155';plot='#0F172A' if dark else '#FFFFFF';grid='#243244' if dark else '#E2E8F0'
    fig.update_layout(template='plotly_dark' if dark else 'plotly_white',height=EOD_HEIGHT[columns],
        margin=dict(l=10,r=14,t=14,b=14),showlegend=False,paper_bgcolor='rgba(0,0,0,0)',plot_bgcolor=plot,
        font=dict(family='Pretendard, -apple-system, BlinkMacSystemFont, Malgun Gothic, sans-serif',size=11,color=text),
        hoverlabel=dict(font=dict(color=text),bgcolor=plot),
        uirevision=(entry['symbol']+str(prices.get('last_final_session',entry.get('last_final_session')))
            +str(bars)+str(close_mode)),
        meta=dict(manual_eod=True,close_only=close_mode,
            price_basis=alternate.get('adjustment') if close_mode else entry.get('adjustment'),
            missing_ma=missing,visible_bars=min(bars,len(closes))))
    fig.update_xaxes(type='date' if close_mode else 'category',nticks=5 if columns>=2 else 8,
        rangeslider_visible=False,showgrid=True,gridcolor=grid,zeroline=False,
        tickfont=dict(color=text))
    fig.update_yaxes(showgrid=True,gridcolor=grid,zeroline=False,tickfont=dict(color=text))
    if has_volume:fig.update_yaxes(showticklabels=columns==1,row=2,col=1)
    return fig,missing


@st.cache_data(ttl=3600,max_entries=512,show_spinner=False)
def cached_eod_json(snapshot_id,symbol,price_hash,bars,columns,theme,volume,_entry):
    del snapshot_id,symbol,price_hash
    fig,missing=eod_chart(_entry,bars,columns,theme,volume)
    return fig.to_json(),missing

