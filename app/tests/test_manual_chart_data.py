from copy import deepcopy
from datetime import datetime,timezone

import pytest

import manual_chart_data as m
from test_manual_catalog import row


def document(symbol='005930.KS'):
    return {'chart':{'error':None,'result':[{'meta':{'symbol':symbol,'currency':'KRW','instrumentType':'EQUITY','exchangeTimezoneName':'Asia/Seoul'},'timestamp':[1704153600,1704240000,1704326400],
        'indicators':{'quote':[{'open':[100,101,102],'high':[103,104,105],'low':[99,100,101],'close':[102,103,104],'volume':[1000,1200,1100]}],
                      'adjclose':[{'adjclose':[1,2,3]}]}}]}}


def test_exact_mapping_does_not_replace_unrelated_contracts_or_fx_direction():
    assert m.source_spec(row())['candidates']==('005930.KS','005930.KQ')
    assert m.source_spec(row(symbol='TSE:7203'))['candidates']==('7203.T',)
    assert m.source_spec(row(symbol='HKEX:700'))['candidates']==('0700.HK',)
    assert m.source_spec(row(symbol='FX_IDC:KRWUSD',asset_type='fx'))['provider']=='tradingview'
    assert m.source_spec(row(symbol='TVC:KR03Y',asset_type='yield'))['provider']=='link'
    assert m.source_spec(row(symbol='NYSE:X'))['provider']=='tradingview'
    assert m.source_spec(row(symbol='NYMEX:CL1!',asset_type='future'))['provider']=='link'
    assert m.source_spec(row(symbol='TVC:US02Y',asset_type='yield'))['provider']=='link'
    assert m.source_spec(row(symbol='CBOE:VX1!',asset_type='future'))['provider']=='tradingview'


def test_quotes_keep_original_ohlc_session_dates_and_no_dividend_adjustment():
    doc=document();before=deepcopy(doc)
    data=m.decode_yahoo(doc,'005930.KS','stock','KRW')
    assert data['bars'][0]['close']==102 and data['bars'][0]['date']=='2024-01-02'
    assert data['timezone']=='Asia/Seoul' and doc==before
    week=m.price_frame(data,'W');month=m.price_frame(data,'M')
    assert week.index[-1].strftime('%Y-%m-%d')=='2024-01-04'
    assert week.iloc[-1]['open']==100 and week.iloc[-1]['close']==104
    assert week.iloc[-1]['high']==105 and week.iloc[-1]['volume']==3300
    assert month.index[-1]==week.index[-1]
    fig,bars=m.price_figure(row(),data,interval='W',theme='dark',ma=20)
    assert bars==1 and len(fig.data)==2  # price + volume, insufficient MA


@pytest.mark.parametrize('problem',['symbol','currency','type','duplicate','bad_ohlc','short','future','length','nan'])
def test_bad_or_wrong_identity_prices_fail_instead_of_silently_charting(problem):
    doc=document();result=doc['chart']['result'][0]
    if problem=='symbol':result['meta']['symbol']='wrong'
    if problem=='currency':result['meta']['currency']='USD'
    if problem=='type':result['meta']['instrumentType']='INDEX'
    if problem=='duplicate':result['timestamp'][1]=result['timestamp'][0]
    if problem=='bad_ohlc':result['indicators']['quote'][0]['high'][0]=10
    if problem=='short':result['indicators']['quote'][0]['close']=[None,None,104]
    if problem=='future':result['timestamp'][0]=9999999999
    if problem=='length':result['indicators']['quote'][0]['open']=[]
    if problem=='nan':result['indicators']['quote'][0]['close']=[float('nan')]*3
    with pytest.raises(m.PriceUnavailable):m.decode_yahoo(doc,'005930.KS','stock','KRW')


def test_null_bar_is_omitted_without_forward_fill():
    doc=document();doc['chart']['result'][0]['indicators']['quote'][0]['close'][1]=None
    data=m.decode_yahoo(doc,'005930.KS','stock','KRW')
    assert len(data['bars'])==2 and data['skipped']==1
    assert data['bars'][-1]['close']==104


def test_only_not_found_can_try_other_korean_venue(monkeypatch):
    calls=[]
    class Response:
        def __init__(self,status,data):self.status_code=status;self.data=data
        def json(self):return self.data
    def get(url,**kwargs):
        calls.append(url)
        return Response(404,{}) if len(calls)==1 else Response(200,document('035900.KQ'))
    m.fetch_yahoo.clear();monkeypatch.setattr(m.requests,'get',get)
    data=m.fetch_yahoo(('035900.KS','035900.KQ'),'stock','KRW')
    assert data['provider_symbol']=='035900.KQ' and len(calls)==2
    m.fetch_yahoo.clear();calls.clear()
    monkeypatch.setattr(m.requests,'get',lambda url,**k:(calls.append(url) or Response(429,{})))
    with pytest.raises(m.PriceUnavailable,match='429'):m.fetch_yahoo(('035900.KS','035900.KQ'),'stock','KRW')
    assert len(calls)==1
    m.fetch_yahoo.clear()


def test_korean_venue_resolution_discards_non_equity_instead_of_plotting_wrong_product(monkeypatch):
    calls=[]
    class Response:
        status_code=200
        def json(self):
            doc=document('399720.KS' if len(calls)==1 else '399720.KQ')
            if len(calls)==1:doc['chart']['result'][0]['meta']['instrumentType']='MUTUALFUND'
            return doc
    def get(url,**kwargs):calls.append(url);return Response()
    m.fetch_yahoo.clear();monkeypatch.setattr(m.requests,'get',get)
    data=m.fetch_yahoo(('399720.KS','399720.KQ'),'stock','KRW')
    assert data['provider_symbol']=='399720.KQ' and len(calls)==2
    m.fetch_yahoo.clear()


def test_bad_ohlc_never_tries_another_venue_to_hide_the_error(monkeypatch):
    calls=[]
    class Response:
        status_code=200
        def json(self):
            doc=document();doc['chart']['result'][0]['indicators']['quote'][0]['high'][0]=90
            return doc
    monkeypatch.setattr(m.requests,'get',lambda url,**k:(calls.append(url) or Response()))
    m.fetch_yahoo.clear()
    with pytest.raises(m.PriceUnavailable,match='고가'):m.fetch_yahoo(('005930.KS','005930.KQ'),'stock','KRW')
    assert len(calls)==1
    m.fetch_yahoo.clear()

