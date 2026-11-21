"""Personal chart catalogue. No prices, providers, credentials or strategy imports."""
from __future__ import annotations

import csv
from decimal import Decimal, InvalidOperation
import io
import re

FIELDS=('enabled','watch_region','watch_sector','watch_subsector','symbol','name',
        'asset_type','unit','chart_style','sort_order','note')
ASSET_LABELS={'stock':'주식','etf':'ETF','index':'지수','fx':'환율','yield':'국채금리',
              'future':'선물','commodity':'원자재 현물','crypto':'가상자산','economic':'경제지표'}
INDEX_SYMBOLS=frozenset(('NASDAQ:NBI','KRX:KOSPI','KRX:KOSPI200','KRX:KOSDAQ','TVC:DJI','NASDAQ:IXIC',
    'SP:SPX','TVC:DXY','TVC:VIX','TVC:SOX','DJ:REIT','TVC:SX5E','TVC:SXXP','SPREADEX:CAC',
    'TVC:UKX','XETR:DAX','TVC:NI225','TSE:TOPIX','SSE:000001','SSE:000300',
    'SZSE:399001','TVC:HSI','HSI:HSTECH'))
STOCK_CURRENCIES={'KRX':'KRW','TSE':'JPY','HKEX':'HKD','SSE':'CNY','SZSE':'CNY',
    'NYSE':'USD','NASDAQ':'USD','AMEX':'USD','NYSEARCA':'USD','XETR':'EUR','EURONEXT':'EUR',
    'BME':'EUR','SIX':'CHF'}


def normalize_symbol(exchange,value):
    exchange=str(exchange).strip().upper();value=str(value).strip().upper()
    if not re.fullmatch(r'[A-Z][A-Z0-9_]{0,19}',exchange):raise ValueError('거래소 코드가 올바르지 않습니다.')
    if re.fullmatch(r'\d+(?:\.\d+)?',value):
        try:
            number=Decimal(value)
            if number!=number.to_integral_value():raise ValueError('소수 부분이 있는 종목 코드는 확정할 수 없습니다.')
            value=str(int(number))
        except InvalidOperation as exc:raise ValueError('숫자 종목 코드 오류') from exc
        if exchange in ('KRX','SSE','SZSE'):
            if len(value)>6:raise ValueError('6자리를 초과하는 종목 코드입니다.')
            value=value.zfill(6)
    if not re.fullmatch(r'[A-Z0-9][A-Z0-9.!/_-]{0,39}',value):raise ValueError('지원하지 않는 심볼 형식입니다.')
    return exchange+':'+value


def normalize_full_symbol(value):
    value=str(value)
    if '\n' in value or '\r' in value or value.count(':')!=1:raise ValueError('거래소:심볼 형식이 필요합니다.')
    exchange,symbol=value.split(':')
    return normalize_symbol(exchange,symbol)


def instrument_defaults(symbol,name=''):
    symbol=normalize_full_symbol(symbol);exchange,code=symbol.split(':')
    if symbol in INDEX_SYMBOLS:kind='index'
    elif exchange=='TVC' and re.fullmatch(r'[A-Z]{2}\d{2}Y',code):kind='yield'
    elif exchange in ('FX','FX_IDC') and re.fullmatch('[A-Z]{6}',code):kind='fx'
    elif code.endswith('!'):kind='future'
    elif exchange in ('BITSTAMP','COINBASE','BINANCE','KRAKEN','CRYPTO'):kind='crypto'
    elif symbol in ('TVC:GOLD','TVC:SILVER','TVC:PLATINUM'):kind='commodity'
    elif exchange=='ECONOMICS':kind='economic'
    elif re.search(r'\bETF\b|\bUCITS\b',str(name),re.I):kind='etf'
    else:kind='stock'
    if kind=='yield':unit='%'
    elif kind=='fx':unit=code[3:]+' / '+code[:3]
    elif kind=='index':unit='지수 포인트'
    elif kind=='future':unit='거래소 계약 단위'
    elif kind=='crypto' and code.endswith('USD'):unit='USD'
    elif kind=='stock' or kind=='etf':unit=STOCK_CURRENCIES.get(exchange,'공급자 표시 단위')
    else:unit='공급자 표시 단위'
    return dict(asset_type=kind,unit=unit,chart_style='line' if kind in ('index','fx','yield','economic') else 'candles')


def csv_bytes(rows):
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=FIELDS,extrasaction='raise')
    writer.writeheader()
    for row in rows:
        item={key:row.get(key,'') for key in FIELDS}
        if type(item['enabled']) is bool:item['enabled']='true' if item['enabled'] else 'false'
        writer.writerow(item)
    return stream.getvalue().encode('utf-8-sig')


def parse_csv(raw):
    if not isinstance(raw,bytes) or len(raw)>2_000_000:raise ValueError('목록 파일은 2MB 이하의 UTF-8 CSV여야 합니다.')
    try:reader=csv.DictReader(io.StringIO(raw.decode('utf-8-sig'),newline=''))
    except UnicodeDecodeError as exc:raise ValueError('UTF-8 CSV 인코딩을 확인하세요.') from exc
    required={'enabled','watch_region','watch_sector','symbol','name','asset_type'}
    headers=reader.fieldnames or []
    if not required.issubset(headers) or set(headers)-set(FIELDS) or len(headers)!=len(set(headers)):
        raise ValueError('관리 CSV의 열 이름을 확인하세요: '+','.join(FIELDS))
    out=[];memberships=set();definitions={}
    for line,row in enumerate(reader,2):
        if line>5001:raise ValueError('목록은 5,000행까지 지원합니다.')
        if not any(row.values()):continue
        if None in row or any(v is None for v in row.values()):raise ValueError(f'{line}행의 열 개수가 맞지 않습니다.')
        item={key:str(row.get(key,'')).strip() for key in FIELDS}
        try:
            truth=item['enabled'].casefold()
            if truth not in ('true','false','1','0'):raise ValueError('enabled는 true 또는 false여야 합니다.')
            item['enabled']=truth in ('true','1')
            if not item['watch_region'] or not item['watch_sector']:raise ValueError('국가/그룹과 섹터를 입력하세요.')
            item['symbol']=normalize_full_symbol(item['symbol']);item['name']=item['name'] or item['symbol']
            if item['asset_type'] not in ASSET_LABELS:raise ValueError('asset_type을 확인하세요.')
            defaults=instrument_defaults(item['symbol'],item['name'])
            item['unit']=item['unit'] or defaults['unit']
            item['chart_style']=item['chart_style'] or ('line' if item['asset_type'] in ('fx','yield','index','economic') else 'candles')
            if item['chart_style'] not in ('line','candles'):raise ValueError('chart_style은 line 또는 candles여야 합니다.')
            item['sort_order']=int(item['sort_order']) if item['sort_order'] else line-1
            if item['sort_order']<0:raise ValueError('sort_order는 0 이상이어야 합니다.')
            if any(len(str(value))>2000 for value in item.values()):raise ValueError('너무 긴 셀이 있습니다.')
            key=tuple(item[k] for k in ('watch_region','watch_sector','watch_subsector','symbol'))
            if key in memberships:raise ValueError('같은 분류 안에 동일 심볼이 중복됐습니다.')
            definition=(item['asset_type'],item['unit'])
            if item['symbol'] in definitions and definitions[item['symbol']]!=definition:
                raise ValueError('동일 심볼의 자산 유형·단위가 서로 다릅니다.')
            memberships.add(key);definitions[item['symbol']]=definition
        except (ValueError,TypeError) as exc:raise ValueError(f'{line}행: {exc}') from exc
        out.append(item)
    return sorted(out,key=lambda row:row['sort_order'])


def filter_rows(rows,*,regions=(),sectors=(),subsectors=(),asset_scope='all',query=''):
    result=[];query=query.strip().casefold()
    for row in rows:
        if not row['enabled']:continue
        if regions and row['watch_region'] not in regions:continue
        if sectors and row['watch_sector'] not in sectors:continue
        if subsectors and row['watch_subsector'] not in subsectors:continue
        stock=row['asset_type'] in ('stock','etf')
        if asset_scope=='stocks' and not stock:continue
        if asset_scope=='macro' and stock:continue
        if query and query not in ' '.join(str(row.get(k,'')) for k in ('name','symbol','watch_region','watch_sector','watch_subsector','note')).casefold():continue
        result.append(dict(row))
    return result


def unique_instruments(rows):
    lookup={}
    for row in rows:
        symbol=row['symbol'];membership=' / '.join(str(row[k]) for k in ('watch_region','watch_sector','watch_subsector') if row[k])
        if symbol not in lookup:
            lookup[symbol]={**row,'memberships':[membership]}
        elif membership not in lookup[symbol]['memberships']:
            lookup[symbol]['memberships'].append(membership)
    return list(lookup.values())

