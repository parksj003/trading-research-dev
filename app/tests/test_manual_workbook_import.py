from pathlib import Path
from xml.sax.saxutils import escape
import zipfile

import pytest

from manual_catalog import FIELDS, csv_bytes, parse_csv
from scripts.import_manual_workbook import parse_workbook, write_outputs


def workbook(path,sheets,formula=False):
    ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    rel='http://schemas.openxmlformats.org/officeDocument/2006/relationships'
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('xl/workbook.xml',f'<workbook xmlns="{ns}" xmlns:r="{rel}"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(sheets,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(sheets)+1))+'</Relationships>')
        for i,(_,rows) in enumerate(sheets,1):
            content=''
            for number,cells in enumerate(rows,1):
                content+=f'<row r="{number}">'
                for col,value in cells.items():
                    content+=f'<c r="{col}{number}" t="inlineStr">'+('<f>1+1</f>' if formula else '')+f'<is><t>{escape(str(value))}</t></is></c>'
                content+='</row>'
            z.writestr(f'xl/worksheets/sheet{i}.xml',f'<worksheet xmlns="{ns}"><sheetData>{content}</sheetData></worksheet>')
    return path


def test_both_column_layouts_preserve_order_and_restore_zeroes(tmp_path):
    path=workbook(tmp_path/'source.xlsx',[
        ('한국',[{'A':'header'},{'A':'반도체'},{'A':'KRX','B':'5930.0','C':'삼성전자'}]),
        ('글로벌미국',[{'A':'header'},{'A':'GLOBAL'},{'A':'Dollar Yen','B':'FX','C':'USDJPY'}]),
        ('일본',[{'A':'header'},{'A':'지수'},{'A':'FX','B':'USDJPY','C':'Dollar Yen'}])])
    before=path.read_bytes();rows,report=parse_workbook(path)
    assert [r['symbol'] for r in rows]==['KRX:005930','FX:USDJPY','FX:USDJPY']
    assert [r['watch_region'] for r in rows]==['한국','글로벌/미국','일본']
    assert all(r['watch_subsector']=='' for r in rows)
    assert report['unique_symbols']==2 and path.read_bytes()==before
    assert report['provider_symbols_live_verified'] is False
    output=tmp_path/'output';write_outputs(path,output)
    assert parse_csv((output/'manual_watchlist.csv').read_bytes())==rows
    with pytest.raises(ValueError,match='덮어쓰지'):write_outputs(path,output)


def test_canonical_workbook_roundtrips_without_classification_inference(tmp_path):
    from test_manual_catalog import row
    item=row(watch_region='직접 만든 국가',watch_sector='새 섹터',watch_subsector='새 서브섹터',enabled=False)
    values={str(__import__('string').ascii_uppercase[i]):('false' if v is False else v) for i,v in enumerate(item.values())}
    path=workbook(tmp_path/'master.xlsx',[('관리',[dict(zip('ABCDEFGHIJK',FIELDS)),values])])
    rows,report=parse_workbook(path)
    assert rows==parse_csv(csv_bytes([item])) and report['source_sheet']=='관리'


@pytest.mark.parametrize('case',['formula','unknown_sheet','missing','extra'])
def test_ambiguous_workbook_fails_instead_of_silently_dropping_rows(tmp_path,case):
    name='한국' if case!='unknown_sheet' else '알 수 없는 시트'
    cells={'A':'KRX','B':'5930','C':'Synthetic'}
    if case=='missing':cells.pop('B')
    if case=='extra':cells['D']='extra'
    path=workbook(tmp_path/'invalid.xlsx',[(name,[{'A':'header'},{'A':'반도체'},cells])],formula=case=='formula')
    with pytest.raises(ValueError):parse_workbook(path)


@pytest.mark.parametrize('case',['duplicate_header','extra_value'])
def test_management_workbook_never_silently_discards_extra_columns(tmp_path,case):
    from test_manual_catalog import row
    headers=dict(zip('ABCDEFGHIJK',FIELDS))
    values=dict(zip('ABCDEFGHIJK',[str(v).lower() if type(v) is bool else str(v) for v in row().values()]))
    if case=='duplicate_header':headers['L']='symbol'
    else:values['L']='unexpected value'
    path=workbook(tmp_path/'invalid-master.xlsx',[('관리',[headers,values])])
    with pytest.raises(ValueError):parse_workbook(path)

