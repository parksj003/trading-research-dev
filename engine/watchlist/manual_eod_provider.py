"""Explicit instrument identity and rate-limited Yahoo daily acquisition.

No ETF proxies, FX inversion, futures replacement, account data or UI calls.
"""
from __future__ import annotations
from datetime import datetime,timezone
from urllib.parse import quote
import math
import time
from zoneinfo import ZoneInfo
import requests
from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException as CurlRequestsError
from .manual_catalog import normalize_full_symbol
from .manual_eod_sessions import MARKETS,confirmed,latest_session
from .manual_macro_review import review_instrument
from .manual_eod_source_diagnostics import europe_source_day_evidence

# Yahoo venue codes must match the source listing, not a market-group label.
VENUES={
 'KRX':('KR','KRW',('KSC','KOE'),('Asia/Seoul',)),
 'NYSE':('US','USD',('NYQ',),('America/New_York','US/Eastern')),
 'NASDAQ':('US','USD',('NMS','NGM','NCM','NAS'),('America/New_York','US/Eastern')),
 'AMEX':('US','USD',('ASE',),('America/New_York','US/Eastern')),
 'TSE':('JP','JPY',('JPX',),('Asia/Tokyo',)),
 'SSE':('CN','CNY',('SHH',),('Asia/Shanghai',)),
 'SZSE':('CN','CNY',('SHZ',),('Asia/Shanghai',)),
 'HKEX':('HK','HKD',('HKG',),('Asia/Hong_Kong',)),
 'XETR':('DE','EUR',('GER',),('Europe/Berlin',)),
 'SIX':('CH','CHF',('EBS',),('Europe/Zurich',)),
 'BME':('ES','EUR',('MCE',),('Europe/Madrid',)),
}
BASIS='yahoo:split-adjusted-OHLC;cash-dividend-unadjusted'
INDEX_BASIS='yahoo:index-level-unadjusted'
# Explicitly reviewed index identity, not an unrestricted index-ticker alias.
NBI_NATIVE='NASDAQ:NBI'
NBI_PROVIDER='^NBI'


class Unavailable(ValueError):pass
class WrongAsset(Unavailable):pass
class RateLimited(Unavailable):pass


class InvalidPriceOHLC(Unavailable):
    """Strict rejection, with bounded original-source evidence in job artifacts.

    Expose the real offending OHLC only in diagnostic request logs, NEVER as
    adjusted/corrected price bars. Do not silently round or quarantine values.
    """
    def __init__(self, session, rule, values=None):
        super().__init__('INVALID_REAL_OHLC')
        self.session=session
        self.rule=rule
        self.values=values

    def diagnostic(self, candidate):
        detail=dict(date=self.session,rule=self.rule,candidate=candidate)
        if self.values is not None:
            o,h,l,c=self.values
            discrepancy=(max(o,c)-h if self.rule=='HIGH_BELOW_OPEN_OR_CLOSE'
                         else l-min(o,c) if self.rule=='LOW_ABOVE_OPEN_OR_CLOSE'
                         else l-h)
            detail.update(source_ohlc=dict(o=o,h=h,l=l,c=c),
                violation_absolute=round(discrepancy,10),
                violation_pct_of_close=round(100*discrepancy/c,8))
        return detail

def raw_daily_evidence(result,spec,cutoff):
    """Read-only last-source-bar diagnosis before finality/null filtering.

    A provider can return HTTP 200 but omit the expected session altogether,
    or contain the date with null OHLC. Distinguish the two without emitting a
    synthetic value or relaxing the exchange calendar.
    """
    try:
        times=result['timestamp']
        quote=result['indicators']['quote'][0]
        latest=latest_session(spec['market'],cutoff)
        dates=[datetime.fromtimestamp(t,timezone.utc)
               .astimezone(ZoneInfo(spec['exchange_timezone'])).date().isoformat()
               for t in times]
        positions=[i for i,day in enumerate(dates) if day==latest]
        null_fields=[]
        if positions:
            ix=positions[-1]
            null_fields=[key for key in ('open','high','low','close')
                         if len(quote.get(key,[]))<=ix or quote[key][ix] is None]
        return dict(expected_session=latest,source_last_session=max(dates) if dates else None,
            source_rows=len(times),expected_session_present=bool(positions),
            expected_ohlc_null_fields=null_fields,
            expected_session_timestamp_count=len(positions),
            source_last_session_is_expected=bool(dates) and max(dates)==latest)
    except (KeyError,TypeError,ValueError,OverflowError,OSError,IndexError):
        return dict(audit='SOURCE_DAILY_EVIDENCE_UNAVAILABLE')



def specification(row):
    symbol=normalize_full_symbol(row['symbol']);venue,code=symbol.split(':')
    if row['asset_type']=='index' and symbol==NBI_NATIVE:
        if row.get('unit')!='지수 포인트' or row.get('chart_style')!='line':
            raise Unavailable('INDEX_DEFINITION_UNIT_UNVERIFIED')
        return dict(symbol=symbol,exchange=venue,market='US',currency='USD',asset_type='index',
            exchange_timezone=MARKETS['US'][1],candidates=(NBI_PROVIDER,),
            provider_exchanges=('NIM','NAS','NMS'),provider_timezones=('America/New_York','US/Eastern'))
    if row['asset_type'] not in ('stock','etf'):raise Unavailable(review_instrument(row)['reason'])
    if venue not in VENUES:raise Unavailable('LISTING_VENUE_NOT_VERIFIED')
    market,currency,exchanges,zones=VENUES[venue]
    if row.get('unit')!=currency:raise Unavailable('CATALOG_CURRENCY_MISMATCH')
    if venue=='KRX':candidates=(code+'.KS',code+'.KQ')
    elif venue=='TSE' and code.isdigit():candidates=(code+'.T',)
    elif venue=='SSE' and code.isdigit():candidates=(code+'.SS',)
    elif venue=='SZSE' and code.isdigit():candidates=(code+'.SZ',)
    elif venue=='HKEX' and code.isdigit():candidates=(code.zfill(4)+'.HK',)
    elif venue in ('NYSE','NASDAQ','AMEX'):candidates=(code.replace('.','-'),)
    elif venue=='XETR':candidates=(code+'.DE',)
    elif venue=='SIX':candidates=(code+'.SW',)
    elif venue=='BME':candidates=(code+'.MC',)
    else:raise Unavailable('SYMBOL_MAPPING_NOT_VERIFIED')
    return dict(symbol=symbol,exchange=venue,market=market,currency=currency,asset_type=row['asset_type'],
        exchange_timezone=MARKETS[market][1],candidates=candidates,provider_exchanges=exchanges,provider_timezones=zones)


def result_of(doc,symbol,spec):
    try:
        if doc['chart'].get('error'):raise Unavailable('PROVIDER_ERROR')
        result=doc['chart']['result'][0];meta=result['meta']
        if meta.get('symbol')!=symbol:raise Unavailable('PROVIDER_SYMBOL_MISMATCH')
        kind={'stock':'EQUITY','etf':'ETF','index':'INDEX'}[spec['asset_type']]
        if meta.get('instrumentType')!=kind:raise WrongAsset('PROVIDER_ASSET_TYPE_MISMATCH')
        if meta.get('currency')!=spec['currency']:raise Unavailable('PROVIDER_CURRENCY_MISMATCH')
        if meta.get('exchangeName') not in spec['provider_exchanges']:raise Unavailable('PROVIDER_EXCHANGE_MISMATCH')
        if meta.get('exchangeTimezoneName') not in spec['provider_timezones']:raise Unavailable('PROVIDER_TIMEZONE_MISMATCH')
        if meta.get('dataGranularity')!='1d':raise Unavailable('PROVIDER_NOT_DAILY')
        return result,dict(provider_symbol=symbol,provider_exchange=meta['exchangeName'],currency=meta['currency'],
            instrument_type=kind,timezone=meta['exchangeTimezoneName'])
    except (KeyError,IndexError,TypeError) as exc:raise Unavailable('PROVIDER_STRUCTURE_INVALID') from exc


def bars_of(result,spec,cutoff):
    try:
        timestamps=result['timestamp'];quote=result['indicators']['quote'][0]
        if len(set(timestamps))!=len(timestamps):raise Unavailable('DUPLICATE_PRICE_TIMESTAMP')
        if any(len(quote[k])!=len(timestamps) for k in ('open','high','low','close')):raise Unavailable('PRICE_ARRAY_LENGTH')
        out=[];excluded=0
        for i,timestamp in enumerate(timestamps):
            stamp=datetime.fromtimestamp(timestamp,timezone.utc)
            if stamp>cutoff:excluded+=1;continue
            date=stamp.astimezone(ZoneInfo(spec['exchange_timezone'])).date().isoformat()
            try:is_final=confirmed(spec['market'],date,cutoff)
            except ValueError:raise Unavailable('BAR_OUTSIDE_REVIEWED_SESSION')
            if not is_final:excluded+=1;continue
            values=[quote[k][i] for k in ('open','high','low','close')]
            if any(v is None for v in values):excluded+=1;continue
            if not all(type(v) in (int,float) and math.isfinite(v) and v>0 for v in values):raise Unavailable('INVALID_OHLC_VALUE')
            o,h,l,c=values
            if h<max(o,c):
                raise InvalidPriceOHLC(date,'HIGH_BELOW_OPEN_OR_CLOSE',values)
            if l>min(o,c):
                raise InvalidPriceOHLC(date,'LOW_ABOVE_OPEN_OR_CLOSE',values)
            if h<l:
                raise InvalidPriceOHLC(date,'HIGH_BELOW_LOW',values)
            volume=quote.get('volume',[]);v=volume[i] if i<len(volume) else None
            if v is not None and (type(v) not in (int,float) or not math.isfinite(v) or v<0):v=None
            out.append(dict(d=date,o=o,h=h,l=l,c=c,v=v))
        out.sort(key=lambda r:r['d'])
        if len({r['d'] for r in out})!=len(out):raise Unavailable('DUPLICATE_PRICE_SESSION')
        if not out:raise Unavailable('NO_FINAL_DAILY_BARS')
        return out,excluded
    except Unavailable:raise
    except (KeyError,IndexError,TypeError,ValueError,OverflowError,OSError) as exc:raise Unavailable('PRICE_STRUCTURE_INVALID') from exc



def close_rows_of(result,spec,cutoff):
    """Verified *close-only* market sessions, independent from valid OHLC bars.

    Identity must already have passed result_of(). A missing or invalid close
    is not imputed from Open/High/Low, and closed sessions are not synthesized.
    """
    try:
        timestamps=result['timestamp']
        quote=result['indicators']['quote'][0]
        closes=quote['close']
        if len(set(timestamps))!=len(timestamps):
            raise Unavailable('DUPLICATE_PRICE_TIMESTAMP')
        if len(closes)!=len(timestamps):
            raise Unavailable('PRICE_ARRAY_LENGTH')
        rows=[];excluded=0
        for i,timestamp in enumerate(timestamps):
            stamp=datetime.fromtimestamp(timestamp,timezone.utc)
            if stamp>cutoff:
                excluded+=1;continue
            date=stamp.astimezone(ZoneInfo(spec['exchange_timezone'])).date().isoformat()
            try:
                final=confirmed(spec['market'],date,cutoff)
            except ValueError as exc:
                raise Unavailable('BAR_OUTSIDE_REVIEWED_SESSION') from exc
            if not final:
                excluded+=1;continue
            close=closes[i]
            if type(close) not in (int,float) or not math.isfinite(close) or close<=0:
                excluded+=1;continue
            rows.append(dict(d=date,c=close))
        rows.sort(key=lambda r:r['d'])
        if len({r['d'] for r in rows})!=len(rows):
            raise Unavailable('DUPLICATE_PRICE_SESSION')
        if len(rows)<2:
            raise Unavailable('NO_VALID_FINAL_CLOSE_SERIES')
        return rows[-650:],excluded
    except Unavailable:
        raise
    except (KeyError,IndexError,TypeError,ValueError,OverflowError,OSError) as exc:
        raise Unavailable('CLOSE_PRICE_STRUCTURE_INVALID') from exc


# Same Yahoo chart endpoint used by Collector. A secondary host is only tried for
# transient server failures; throttling/access denials must not be worked around.
YAHOO_CHART_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
YAHOO_USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


class YahooDaily:
    def __init__(self, session=None, pause=.35, clock=time.monotonic,
                 sleeper=time.sleep, max_seconds=900, max_calls=64):
        # Use the same curl_cffi transport as yfinance; standard requests
        # receives HTTP 429 on the first EOD probe in the hosted runner.
        # No IP rotation, proxies or retries after a provider 429.
        self.session = session if session is not None else curl_requests.Session(impersonate='chrome')
        self.pause, self.clock, self.sleep = pause, clock, sleeper
        self.next_at = 0
        self.calls = 0
        self.deadline = clock() + max_seconds
        self.max_calls = max_calls
        # A Korean 429 must not silently skip all American stocks. Preserve
        # one bounded independent probe per market; any 429 blocks that market
        # without retrying the other Yahoo hostname.
        self.market_circuits = {}
        self.connection_failures_by_market = {}
        self.request_log = []

    def fetch(self, spec, cutoff, *, start=None, metadata_only=False, close_only=False):
        market = spec['market']
        if self.clock() >= self.deadline:
            raise Unavailable('COLLECTION_TIME_BUDGET_EXCEEDED')
        if market in self.market_circuits:
            raise RateLimited(self.market_circuits[market])
        for symbol in spec['candidates']:
            params = {'interval': '1d'}
            if start is not None:
                params.update(
                    period1=int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()),
                    period2=int(cutoff.timestamp()) + 86400,
                )
            else:
                params['range'] = '5d' if metadata_only else '2y'
            for host_index, host in enumerate(YAHOO_CHART_HOSTS):
                if self.clock() >= self.deadline:
                    raise Unavailable('COLLECTION_TIME_BUDGET_EXCEEDED')
                # Per-run, across all exchanges: do not let 142 international
                # cold starts cause unbounded Yahoo traffic from one runner.
                if self.calls >= self.max_calls:
                    raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
                delay = self.next_at - self.clock()
                if delay > 0:
                    self.sleep(delay)
                request = dict(market=market, symbol=spec['symbol'],
                               operation='identity' if metadata_only else 'price',
                               host=host, status=None)
                self.request_log.append(request)
                self.calls += 1
                try:
                    response = self.session.get(
                        'https://' + host + '/v8/finance/chart/' + quote(symbol, safe=''),
                        params=params, headers={'User-Agent': YAHOO_USER_AGENT},
                        timeout=(5, 20), allow_redirects=False,
                    )
                except (requests.RequestException, CurlRequestsError) as exc:
                    failures = self.connection_failures_by_market.get(market, 0) + 1
                    self.connection_failures_by_market[market] = failures
                    if failures >= 3:
                        self.market_circuits[market] = 'PROVIDER_CONNECTION_CIRCUIT_OPEN'
                    raise Unavailable('PROVIDER_CONNECTION_FAILED') from exc
                request['status'] = response.status_code
                self.next_at = self.clock() + self.pause
                if response.status_code == 429:
                    self.market_circuits[market] = 'PROVIDER_RATE_LIMIT_CIRCUIT_OPEN'
                    raise RateLimited('PROVIDER_RATE_LIMITED')
                if response.status_code in (401, 403):
                    self.market_circuits[market] = 'PROVIDER_ACCESS_CIRCUIT_OPEN'
                    raise Unavailable('PROVIDER_ACCESS_DENIED')
                if response.status_code == 404:
                    break  # A missing listing is not retried on another host.
                if response.status_code in (500, 502, 503, 504) and host_index == 0:
                    continue
                if response.status_code != 200:
                    raise Unavailable(f'PROVIDER_HTTP_{response.status_code}')
                try:
                    result, identity = result_of(response.json(), symbol, spec)
                    if not metadata_only:
                        request['source_daily_evidence']=raw_daily_evidence(result,spec,cutoff)
                    if market in ('DE','CH','ES'):
                        request['europe_source_days']=europe_source_day_evidence(result,spec,cutoff)
                except WrongAsset:
                    if spec['exchange'] == 'KRX':
                        break  # Try the other KRX-listed symbol, not another host.
                    raise
                if metadata_only:
                    return [], identity, 0
                if close_only:
                    rows, excluded = close_rows_of(result,spec,cutoff)
                    request['close_only_valid_sessions']=len(rows)
                    request['close_only_last_final_session']=rows[-1]['d']
                    return rows, identity, excluded
                try:
                    rows, excluded = bars_of(result, spec, cutoff)
                except InvalidPriceOHLC as exc:
                    request['price_integrity_failure']=exc.diagnostic(symbol)
                    raise
                return rows, identity, excluded
        raise Unavailable('PROVIDER_SYMBOL_NOT_FOUND')

