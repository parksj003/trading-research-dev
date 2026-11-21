"""Versioned manual-observation payload; no credentials, accounts or signal data."""
from __future__ import annotations
import gzip
import hashlib
import io
import json
import math
import re
from datetime import datetime

SCHEMA='manual-eod-v1'
MAX_BARS=650
MAX_SYMBOLS=5000
MAX_GZIP=12_000_000
MAX_JSON=64_000_000
STATES={'current','stale','retained','missing','unsupported','calendar_unverified'}
BAR_KEYS=('d','o','h','l','c','v')


def aware(value):
    if not isinstance(value,str):raise ValueError('timestamp required')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None:raise ValueError('timezone required')
    return parsed


def day(value):
    if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError('session date required')
    datetime.strptime(value,'%Y-%m-%d')
    return value


def canonical(doc):
    return json.dumps(doc,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def packed(doc):
    raw=canonical(doc)
    if len(raw)>MAX_JSON:raise ValueError('expanded snapshot size')
    return gzip.compress(raw,mtime=0)


def digest(raw):return hashlib.sha256(raw).hexdigest()


def no_duplicates(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate JSON key')
        result[key]=value
    return result


def json_doc(raw):
    def invalid(_):raise ValueError('nonfinite JSON')
    return json.loads(raw,parse_constant=invalid,object_pairs_hook=no_duplicates)


def finite(value,positive=False):
    return type(value) in (int,float) and math.isfinite(value) and (value>0 if positive else value>=0)



def validate_close_only(item,cutoff,generated=None):
    """Standalone verified daily closes: never synthesize OHLC candle fields."""
    partial=item.get('close_only')
    if partial is None:
        return
    if not isinstance(partial,dict) or set(partial)!={
        'd','c','source','source_symbol','adjustment','identity_evidence',
        'last_final_session','verified_at','history_coverage'}:
        raise ValueError('close-only payload contract')
    dates=partial['d'];prices=partial['c']
    if (not isinstance(dates,list) or not isinstance(prices,list)
            or not 2<=len(dates)<=MAX_BARS or len(dates)!=len(prices)
            or dates!=sorted(set(dates))):
        raise ValueError('close-only date order/count')
    for date in dates:
        day(date)
    if not all(finite(v,True) for v in prices):
        raise ValueError('close-only invalid real price')
    if partial['last_final_session']!=dates[-1]:
        raise ValueError('close-only last session mismatch')
    expected=item.get('expected_session')
    if expected is not None and dates[-1]>day(expected):
        raise ValueError('close-only unconfirmed session')
    if aware(partial['verified_at'])>(generated or cutoff):
        raise ValueError('close-only evidence in future')
    index=item.get('asset_type')=='index'
    expected_source=('Yahoo Finance verified index levels' if index
        else 'Yahoo Finance verified close-only')
    expected_basis=('yahoo:index-level-unadjusted' if index
        else 'yahoo:split-adjusted-OHLC;cash-dividend-unadjusted')
    if partial['source']!=expected_source or partial['adjustment']!=expected_basis:
        raise ValueError('close-only source/basis unreviewed')
    ident=partial['identity_evidence']
    if not isinstance(ident,dict) or any(
            not ident.get(field) for field in
            ('provider_symbol','provider_exchange','currency','instrument_type','timezone')):
        raise ValueError('close-only listing metadata missing')
    if (ident['provider_symbol']!=partial['source_symbol']
        or ident['currency']!=item.get('currency')
        or ident['instrument_type']!=('INDEX' if index else
            'EQUITY' if item.get('asset_type')=='stock' else 'ETF')
        or (index and (item.get('symbol')!='NASDAQ:NBI'
            or partial['source_symbol']!='^NBI'
            or ident['provider_exchange'] not in ('NIM','NAS','NMS')
            or ident['timezone'] not in ('America/New_York','US/Eastern')))):
        raise ValueError('close-only identity mismatch')
    cov=partial['history_coverage']
    if not isinstance(cov,dict) or (cov.get('first_session')!=dates[0]
            or cov.get('last_session')!=dates[-1]
            or cov.get('observed_sessions')!=len(dates)
            or type(cov.get('missing_between_first_last')) is not int
            or cov['missing_between_first_last']<0):
        raise ValueError('close-only history coverage mismatch')


def validate_entry(symbol,item,cutoff,generated=None):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,19}:[A-Z0-9][A-Z0-9.!/_-]{0,39}',symbol):raise ValueError('invalid native symbol')
    if item.get('symbol')!=symbol or item.get('status') not in STATES:raise ValueError('entry identity/status')
    validate_close_only(item,cutoff,generated)
    bars=item.get('bars') or {}
    if item.get('asset_type')=='index' and symbol=='NASDAQ:NBI':
        if bars:
            raise ValueError('NBI index must not masquerade as stock candles')
        partial=item.get('close_only')
        if item['status'] in ('current','stale','retained'):
            if not partial or item.get('last_final_session')!=partial['last_final_session']:
                raise ValueError('index final close missing')
            if (item['status']=='current'
                    and item.get('expected_session')!=partial['last_final_session']):
                raise ValueError('index current session mismatch')
            if aware(item['last_final_close_at'])>cutoff:
                raise ValueError('index unclosed session')
            if not all(item.get(k) for k in ('source_symbol','exchange','market',
                    'currency','exchange_timezone','source','adjustment',
                    'calendar_policy','identity_evidence','verified_at','last_success_at')):
                raise ValueError('index provenance missing')
            for k in ('verified_at','last_success_at'):
                if aware(item[k])>(generated or cutoff):
                    raise ValueError('future index evidence')
        elif item['status'] not in ('missing','unsupported','calendar_unverified'):
            raise ValueError('unreviewed index state')
        elif item.get('last_final_session') is not None:
            raise ValueError('index session without prices')
        return
    if not bars:
        if item['status'] not in ('missing','unsupported','calendar_unverified'):raise ValueError('price state without bars')
        if item.get('last_final_session') is not None:raise ValueError('last session without prices')
        return
    if item.get('asset_type') not in ('stock','etf'):raise ValueError('unreviewed asset price policy')
    if set(bars)!=set(BAR_KEYS):raise ValueError('OHLCV keys')
    dates=bars['d'];n=len(dates)
    if not 1<=n<=MAX_BARS or any(not isinstance(bars[k],list) or len(bars[k])!=n for k in BAR_KEYS):raise ValueError('OHLCV lengths')
    if dates!=sorted(set(dates)):raise ValueError('session order/duplicates')
    for date in dates:day(date)
    if item.get('last_final_session')!=dates[-1]:raise ValueError('last price date mismatch')
    expected=item.get('expected_session')
    if expected is not None and dates[-1]>day(expected):raise ValueError('unconfirmed session')
    if aware(item['last_final_close_at'])>cutoff:raise ValueError('unclosed last bar')
    for key in ('verified_at','last_success_at'):
        if aware(item[key])>(generated or cutoff):raise ValueError('future price evidence')
    if not all(item.get(k) for k in ('source_symbol','exchange','market','currency','exchange_timezone','source','adjustment','calendar_policy','identity_evidence')):
        raise ValueError('missing price provenance')
    for i in range(n):
        o,h,l,c=(bars[k][i] for k in ('o','h','l','c'))
        if not all(finite(x,True) for x in (o,h,l,c)) or h<max(o,c) or l>min(o,c) or h<l:raise ValueError('invalid real OHLC')
        v=bars['v'][i]
        if v is not None and not finite(v):raise ValueError('invalid volume')
    if item['status']=='current' and expected!=dates[-1]:raise ValueError('stale price labelled current')


def validate_snapshot(doc):
    if not isinstance(doc,dict) or doc.get('schema')!=SCHEMA:raise ValueError('snapshot schema')
    for key in ('run_id','core_run_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}',str(doc.get(key,''))):raise ValueError('run identity')
    if not re.fullmatch('[a-f0-9]{40}',str(doc.get('core_publication_commit',''))):raise ValueError('source commit')
    for field in ('catalog_sha256','core_manifest_sha256'):
        if not re.fullmatch('[a-f0-9]{64}',str(doc.get(field,''))):raise ValueError('source digest')
    cutoff=aware(doc['cutoff_at']);generated=aware(doc['generated_at'])
    if generated<cutoff or aware(doc['core_generated_at'])>cutoff:raise ValueError('publication chronology')
    entries=doc.get('instruments')
    if not isinstance(entries,dict) or len(entries)>MAX_SYMBOLS:raise ValueError('instrument limit')
    for symbol,item in entries.items():validate_entry(symbol,item,cutoff,generated)
    return doc


def descriptor(doc,raw):
    validate_snapshot(doc)
    if len(raw)>MAX_GZIP:raise ValueError('snapshot size')
    sha=digest(raw)
    return dict(schema=SCHEMA,run_id=doc['run_id'],core_run_id=doc['core_run_id'],generated_at=doc['generated_at'],
        catalog_sha256=doc['catalog_sha256'],snapshot=dict(file=f'manual_eod/objects/{sha}.json.gz',bytes=len(raw),sha256=sha))


def load_snapshot(pointer,raw):
    if not isinstance(pointer,dict) or pointer.get('schema')!=SCHEMA:raise ValueError('descriptor schema')
    info=pointer.get('snapshot') or {};sha=info.get('sha256','')
    if not re.fullmatch('[a-f0-9]{64}',str(sha)) or info.get('file')!=f'manual_eod/objects/{sha}.json.gz':raise ValueError('unsafe immutable snapshot path')
    if not isinstance(raw,bytes) or len(raw)>MAX_GZIP or type(info.get('bytes')) is not int or len(raw)!=info['bytes'] or digest(raw)!=sha:raise ValueError('snapshot integrity')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:plain=stream.read(MAX_JSON+1)
    if len(plain)>MAX_JSON:raise ValueError('expanded snapshot size')
    doc=validate_snapshot(json_doc(plain))
    for key in ('run_id','core_run_id','generated_at','catalog_sha256'):
        if doc.get(key)!=pointer.get(key):raise ValueError('descriptor/payload mismatch')
    return doc

