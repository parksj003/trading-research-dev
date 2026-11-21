from datetime import datetime,timezone
import copy
import pytest
from watchlist.manual_eod_provider import specification,result_of,bars_of,YahooDaily,Unavailable
from test_manual_eod_pipeline import row


def spec():return specification(row())
def doc():
    return dict(chart=dict(error=None,result=[dict(meta=dict(symbol='005930.KS',currency='KRW',instrumentType='EQUITY',exchangeName='KSC',exchangeTimezoneName='Asia/Seoul',dataGranularity='1d'),
        timestamp=[int(datetime(2026,11,11,tzinfo=timezone.utc).timestamp()),int(datetime(2026,11,12,tzinfo=timezone.utc).timestamp())],
        indicators=dict(quote=[dict(open=[100,102],high=[106,108],low=[99,101],close=[104,106],volume=[1000,None])]))]))


@pytest.mark.parametrize('field,value',[('symbol','OTHER'),('currency','USD'),('instrumentType','MUTUALFUND'),('exchangeName','NYQ'),('exchangeTimezoneName','America/New_York'),('dataGranularity','1m')])
def test_provider_identity_and_actual_daily_interval_are_strict(field,value):
    d=doc();d['chart']['result'][0]['meta'][field]=value
    with pytest.raises(Unavailable):result_of(d,'005930.KS',spec())


def test_intraday_is_excluded_and_missing_volume_is_not_zero_filled():
    result,_=result_of(doc(),'005930.KS',spec())
    pre=datetime(2026,11,12,5,tzinfo=timezone.utc)
    rows,excluded=bars_of(result,spec(),pre)
    assert [r['d'] for r in rows]==['2026-11-11'] and excluded==1
    rows,_=bars_of(result,spec(),datetime(2026,11,12,10,tzinfo=timezone.utc))
    assert rows[-1]['v'] is None and rows[-1]['o']==102 and rows[-1]['c']==106


def test_gaps_are_not_interpolated_and_bad_real_ohlc_is_rejected():
    d=doc();r=d['chart']['result'][0]
    r['indicators']['quote'][0]['close'][0]=None
    rows,excluded=bars_of(r,spec(),datetime(2026,11,12,10,tzinfo=timezone.utc))
    assert len(rows)==1 and rows[0]['d']=='2026-11-12' and excluded==1
    r['indicators']['quote'][0]['close'][0]=120
    with pytest.raises(Unavailable,match='OHLC'):bars_of(r,spec(),datetime(2026,11,12,10,tzinfo=timezone.utc))


def test_wrong_korean_product_can_only_resolve_to_verified_same_code_equity():
    class Response:
        status_code=200
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class Session:
        def __init__(self):self.calls=[]
        def get(self,url,**kw):
            self.calls.append(url);d=doc();meta=d['chart']['result'][0]['meta']
            if len(self.calls)==1:meta['instrumentType']='MUTUALFUND'
            else:meta.update(symbol='005930.KQ',exchangeName='KOE')
            return Response(d)
    session=Session();p=YahooDaily(session,pause=0)
    rows,identity,_=p.fetch(spec(),datetime(2026,11,12,10,tzinfo=timezone.utc))
    assert identity['provider_symbol']=='005930.KQ' and len(session.calls)==2 and rows[-1]['c']==106


def test_rate_limit_is_not_retried_hundreds_of_times():
    class Response:status_code=429
    class Session:
        def __init__(self):self.calls=0
        def get(self,*a,**k):self.calls+=1;return Response()
    session=Session();p=YahooDaily(session,pause=0)
    for _ in range(20):
        with pytest.raises(Unavailable):p.fetch(spec(),datetime(2026,11,12,10,tzinfo=timezone.utc))
    assert session.calls==1


def test_unsupported_venue_and_macro_never_get_proxy_prices():
    for item in [row(symbol='FSE:9407'),row(symbol='EURONEXT:CAP'),row(symbol='MCX:COPPER1!',asset_type='future'),row(symbol='FX_IDC:KRWUSD',asset_type='fx')]:
        with pytest.raises(Unavailable):specification(item)


@pytest.mark.parametrize('native,unit,lookup,venue,zone',[
 ('KRX:005930','KRW','005930.KS','KSC','Asia/Seoul'),
 ('NYSE:IBM','USD','IBM','NYQ','America/New_York'),
 ('NASDAQ:BRK.B','USD','BRK-B','NMS','America/New_York'),
 ('TSE:7203','JPY','7203.T','JPX','Asia/Tokyo'),
 ('SSE:600000','CNY','600000.SS','SHH','Asia/Shanghai'),
 ('SZSE:000001','CNY','000001.SZ','SHZ','Asia/Shanghai'),
 ('HKEX:700','HKD','0700.HK','HKG','Asia/Hong_Kong')])
def test_native_listing_mapping_exchange_currency_and_timezone(native,unit,lookup,venue,zone):
    s=specification(row(symbol=native,unit=unit));assert lookup in s['candidates']
    d=doc();m=d['chart']['result'][0]['meta'];m.update(symbol=lookup,currency=unit,exchangeName=venue,exchangeTimezoneName=zone)
    _,identity=result_of(d,lookup,s);assert identity['provider_symbol']==lookup
    m['currency']='CHF'
    with pytest.raises(Unavailable,match='CURRENCY'):result_of(d,lookup,s)


def test_unknown_catalog_currency_is_not_silently_assumed():
    with pytest.raises(Unavailable,match='CURRENCY'):specification(row(unit='unknown'))


def test_confirmed_etf_is_accepted_only_as_the_same_asset_type():
    s=specification(row(asset_type='etf'));d=doc();d['chart']['result'][0]['meta']['instrumentType']='ETF'
    assert result_of(d,'005930.KS',s)[1]['instrument_type']=='ETF'
    with pytest.raises(Unavailable):result_of(d,'005930.KS',spec())


def test_raw_china_daily_evidence_distinguishes_missing_session_from_null_price():
    from watchlist.manual_eod_provider import raw_daily_evidence
    src=specification(row(symbol='SZSE:002594',unit='CNY'))
    cutoff=datetime(2026,11,19,18,4,tzinfo=timezone.utc)
    d=doc()['chart']['result'][0]
    d['timestamp']=[int(datetime(2026,11,18,tzinfo=timezone.utc).timestamp())]
    for field in ('open','high','low','close','volume'):
        d['indicators']['quote'][0][field]=[100]
    observation=raw_daily_evidence(d,src,cutoff)
    assert observation['expected_session']=='2026-11-19'
    assert observation['source_last_session']=='2026-11-18'
    assert not observation['expected_session_present']
    d['timestamp'].append(int(datetime(2026,11,19,tzinfo=timezone.utc).timestamp()))
    for field in ('open','high','low','close','volume'):
        d['indicators']['quote'][0][field].append(None if field=='open' else 100)
    observation=raw_daily_evidence(d,src,cutoff)
    assert observation['source_last_session']=='2026-11-19'
    assert observation['expected_session_present']
    assert observation['expected_ohlc_null_fields']==['open']
    assert observation['expected_session_timestamp_count']==1
    rows,excluded=bars_of(d,src,cutoff)
    assert rows[-1]['d']=='2026-11-18' and excluded==1


def test_real_invalid_ohlc_exposes_magnitude_only_in_rejection_diagnostics():
    from watchlist.manual_eod_provider import InvalidPriceOHLC
    d=doc()['chart']['result'][0]
    d['indicators']['quote'][0]['high'][-1]=105.
    d['indicators']['quote'][0]['close'][-1]=106.
    with pytest.raises(InvalidPriceOHLC) as captured:
        bars_of(d,spec(),datetime(2026,11,12,10,tzinfo=timezone.utc))
    diagnostic=captured.value.diagnostic('005930.KS')
    assert diagnostic['date']=='2026-11-12'
    assert diagnostic['source_ohlc']==dict(o=102,h=105.,l=101,c=106.)
    assert diagnostic['violation_absolute']==1.
    assert diagnostic['violation_pct_of_close']==pytest.approx(100/106)

