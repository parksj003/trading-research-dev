"""Import a user's TradingView XLSX into the explicit personal-catalogue CSV.

No spreadsheet execution, network calls or symbol substitution. Source preserved.
"""
from __future__ import annotations
import argparse
from collections import Counter
import csv
import hashlib
import io
import json
from pathlib import Path,PurePosixPath
import sys
import xml.etree.ElementTree as E
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manual_catalog import FIELDS,csv_bytes,parse_csv,normalize_symbol,instrument_defaults

NS={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
SHEET_NAMES={'한국':'한국','글로벌미국':'글로벌/미국','일본':'일본','중국,홍콩':'중국/홍콩'}


def text(element):
    if element is None:return ''
    direct=element.find('x:t',NS)
    if direct is not None:return direct.text or ''
    return ''.join((r.find('x:t',NS).text or '') for r in element.findall('x:r',NS) if r.find('x:t',NS) is not None)


def workbook_rows(path):
    with zipfile.ZipFile(path) as archive:
        if sum(i.file_size for i in archive.infolist())>64_000_000:raise ValueError('워크북 압축 해제 크기 제한')
        if len(archive.namelist())>2000:raise ValueError('워크북 구조 제한')
        strings=[]
        if 'xl/sharedStrings.xml' in archive.namelist():
            strings=[text(v) for v in E.fromstring(archive.read('xl/sharedStrings.xml')).findall('x:si',NS)]
        rels={r.get('Id'):r.get('Target') for r in E.fromstring(archive.read('xl/_rels/workbook.xml.rels'))}
        workbook=E.fromstring(archive.read('xl/workbook.xml'));result=[]
        for sheet in workbook.findall('x:sheets/x:sheet',NS):
            target=rels[sheet.get('{'+REL+'}id')]
            file=target.lstrip('/') if target.startswith('/') else str(PurePosixPath('xl')/target)
            if '..' in PurePosixPath(file).parts or not file.startswith('xl/worksheets/'):
                raise ValueError('외부/비정상 워크시트 참조')
            root=E.fromstring(archive.read(file));rows=[]
            for row in root.findall('x:sheetData/x:row',NS):
                values={}
                for cell in row.findall('x:c',NS):
                    if cell.find('x:f',NS) is not None:raise ValueError(f"{sheet.get('name')}!{cell.get('r')}: 수식은 실행하거나 확정하지 않습니다.")
                    value=cell.find('x:v',NS);value=value.text if value is not None else ''
                    if cell.get('t')=='s':value=strings[int(value)] if value else ''
                    elif cell.get('t')=='inlineStr':value=text(cell.find('x:is',NS))
                    if value:values[''.join(c for c in cell.get('r','') if c.isalpha())]=value
                if values:rows.append((int(row.get('r')),values))
            result.append((sheet.get('name'),rows))
        return result


def parse_workbook(path):
    sheets=workbook_rows(path);entries=[];audit=[];groups=[];issues=[]
    masters=[(name,rows) for name,rows in sheets if rows and len(rows[0][1])==len(FIELDS) and set(rows[0][1].values())==set(FIELDS)]
    if len(masters)>1:raise ValueError('표준 관리 시트가 여러 개 있습니다. 하나로 정리하세요.')
    if masters:
        name,rows=masters[0];headers=rows[0][1]
        for number,cells in rows[1:]:
            if set(cells)-set(headers):raise ValueError(f'{name}!{number}: 표준 관리 시트에 추가 열이 있습니다.')
            entries.append({headers[col]:cells.get(col,'') for col in headers})
        entries=parse_csv(csv_bytes(entries))
        return entries,dict(source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),source_sheet=name,
            memberships=len(entries),unique_symbols=len({r['symbol'] for r in entries}),audit=[],issues=[],groups=[])
    for name,rows in sheets:
        if name not in SHEET_NAMES:
            if '가이드' in name:continue
            if rows:raise ValueError('알 수 없는 목록 시트: '+name)
            continue
        group=''
        for number,cells in rows:
            if number==1:continue
            if set(cells)=={'A'}:
                group=cells['A'].strip();groups.append(dict(sheet=name,row=number,sector=group));continue
            if set(cells)-{'A','B','C'}:raise ValueError(f'{name}!{number}: 추가 열은 먼저 검토해야 합니다.')
            a,b,c=(cells.get(k,'').strip() for k in ('A','B','C'))
            exchange,code,display=(b,c,a) if name=='글로벌미국' else (a,b,c)
            if not all((exchange,code,display,group)):raise ValueError(f'{name}!{number}: 거래소·심볼·이름·분류 누락')
            symbol=normalize_symbol(exchange,code);defaults=instrument_defaults(symbol,display);notes=[];flags=[]
            if symbol=='FX_IDC:KRWUSD':
                flags.append('FX_DIRECTION_REVIEW');notes.append('원문 KRWUSD: KRW 1당 USD. 통상 USDKRW와 역방향이므로 명칭·의도 확인. 임의로 반전하지 않음.')
            if defaults['asset_type']=='future':notes.append('연속선물 원문 심볼: 롤오버 영향을 포함하며 실제 거래 계약과 구별.')
            if symbol=='MCX:COPPER1!':
                flags.append('CONTRACT_UNIT_REVIEW');notes.append('MCX 거래소의 통화·계약 단위 확인. COMEX나 USD 구리로 대체하지 않음.')
            if exchange in ('SPREADEX','AMEX','FSE'):
                flags.append('PROVIDER_SYMBOL_REVIEW');notes.append('거래소·심볼·표시명 및 현재 공급자 지원 여부 확인. 원문 유지.')
            entry=dict(enabled=True,watch_region=SHEET_NAMES[name],watch_sector=group,watch_subsector='',
                symbol=symbol,name=display,**defaults,sort_order=len(entries)+1,note=' '.join(notes))
            entries.append(entry)
            record=dict(source_sheet=name,source_row=number,source_exchange=exchange,source_symbol=code,
                normalized_symbol=symbol,name=display,watch_sector=group,asset_type=defaults['asset_type'],
                review_flags=';'.join(flags),provider_validation='NOT_VERIFIED',normalization='numeric/format normalization only' if symbol!=exchange+':'+code else 'source preserved')
            audit.append(record)
            if flags:issues.append(record)
    entries=parse_csv(csv_bytes(entries))
    counts=dict(Counter(r['watch_region'] for r in entries));kinds=dict(Counter(r['asset_type'] for r in entries))
    return entries,dict(source_sha256=hashlib.sha256(Path(path).read_bytes()).hexdigest(),memberships=len(entries),
        unique_symbols=len({r['symbol'] for r in entries}),by_region=counts,by_asset_type=kinds,
        groups=groups,audit=audit,issues=issues,provider_symbols_live_verified=False)


def write_outputs(path,output):
    entries,report=parse_workbook(path);output=Path(output);output.mkdir(parents=True,exist_ok=True)
    targets=[output/'manual_watchlist.csv',output/'import_review.json',output/'symbol_review.csv']
    if any(p.exists() for p in targets):raise ValueError('기존 정리본을 덮어쓰지 않도록 새 출력 디렉터리를 지정하세요.')
    targets[0].write_bytes(csv_bytes(entries));targets[1].write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    fields=('source_sheet','source_row','source_exchange','source_symbol','normalized_symbol','name','watch_sector','asset_type','review_flags','provider_validation','normalization')
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerows(report['issues'])
    targets[2].write_bytes(stream.getvalue().encode('utf-8-sig'))
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='원문 심볼을 보존해 관심목록 XLSX를 정리합니다.')
    parser.add_argument('workbook');parser.add_argument('--output',required=True)
    args=parser.parse_args();report=write_outputs(args.workbook,args.output)
    print(json.dumps({k:v for k,v in report.items() if k not in ('audit','issues','groups')},ensure_ascii=False,indent=2))

