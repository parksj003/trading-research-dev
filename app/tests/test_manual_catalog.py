from copy import deepcopy
import csv
import io

import pytest


def module():
    import manual_catalog
    return manual_catalog


def row(**changes):
    item=dict(enabled=True,watch_region='한국',watch_sector='반도체',watch_subsector='',
              symbol='KRX:005930',name='삼성전자',asset_type='stock',unit='KRW',
              chart_style='candles',sort_order=1,note='')
    item.update(changes);return item


@pytest.mark.parametrize('exchange,value,expected',[
    ('KRX','5930.0','KRX:005930'),('KRX','660','KRX:000660'),
    ('SSE','1.0','SSE:000001'),('SZSE','32.0','SZSE:000032'),
    ('TSE','9433.0','TSE:9433'),('HKEX','700.0','HKEX:700'),
    ('NASDAQ','NVDA','NASDAQ:NVDA'),('NYSE','BRK.B','NYSE:BRK.B'),
    ('CME_MINI','RTY1!','CME_MINI:RTY1!')])
def test_original_tradingview_identity_is_preserved_with_exact_numeric_restoration(exchange,value,expected):
    assert module().normalize_symbol(exchange,value)==expected


@pytest.mark.parametrize('value',['KRX:12.5','NYSE:ABC</script>','NASDAQ:AAPL\nNYSE:IBM',
                                  'javascript:alert(1)','SSE:1234567'])
def test_ambiguous_or_executable_symbols_are_rejected(value):
    with pytest.raises(ValueError):module().normalize_full_symbol(value)


def test_csv_roundtrip_preserves_korean_zero_codes_and_personal_groups():
    m=module();items=[row(),row(watch_region='글로벌/미국',watch_sector='GLOBAL',symbol='FX:USDJPY',
        name='U.S. Dollar / Japanese Yen',asset_type='fx',unit='JPY / USD',chart_style='line',sort_order=2),
        row(watch_region='일본',watch_sector='지수',symbol='FX:USDJPY',name='USDJPY',asset_type='fx',
        unit='JPY / USD',chart_style='line',sort_order=3)]
    out=m.parse_csv(m.csv_bytes(items))
    assert len(out)==3 and len(m.unique_instruments(out))==2
    assert out[0]['symbol']=='KRX:005930' and out[0]['watch_subsector']==''


def test_exact_duplicate_membership_is_an_error_but_other_groups_are_allowed():
    m=module()
    with pytest.raises(ValueError):m.parse_csv(m.csv_bytes([row(),row()]))
    assert len(m.parse_csv(m.csv_bytes([row(),row(watch_sector='내 관심그룹')])) )==2


def test_disabled_entries_and_filtering_do_not_change_original_list():
    m=module();items=[row(),row(symbol='KRX:000660',name='SK하이닉스',enabled=False),
        row(symbol='FX:USDJPY',watch_region='일본',watch_sector='지수',asset_type='fx',chart_style='line')]
    before=deepcopy(items)
    assert len(m.filter_rows(items))==2
    assert m.filter_rows(items,regions=['일본'])[0]['symbol']=='FX:USDJPY'
    assert m.filter_rows(items,query='삼성')[0]['symbol']=='KRX:005930'
    assert m.filter_rows(items,asset_scope='macro')[0]['symbol']=='FX:USDJPY'
    assert items==before


def test_macro_typing_keeps_yield_fx_and_continuous_futures_distinct():
    m=module()
    assert m.instrument_defaults('TVC:US10Y','Treasury Yield')['asset_type']=='yield'
    assert m.instrument_defaults('FX_IDC:USDKRW','Dollar Won')['unit']=='KRW / USD'
    assert m.instrument_defaults('FX_IDC:KRWUSD','원/달러')['unit']=='USD / KRW'
    assert m.instrument_defaults('CBOE:VX1!','VIX Future')['asset_type']=='future'
    assert m.instrument_defaults('TVC:VIX','VIX')['asset_type']=='index'


def test_unknown_values_fail_before_any_charts_are_loaded():
    m=module()
    for changes in (dict(asset_type='magic'),dict(chart_style='script'),dict(enabled='perhaps'),dict(sort_order=-1)):
        with pytest.raises(ValueError):m.parse_csv(m.csv_bytes([row(**changes)]))


def test_managed_repository_file_passes_the_same_validation_as_user_edits():
    from pathlib import Path
    # No fixed count or hardcoded groups: normal additions/removals stay valid.
    module().parse_csv((Path(__file__).resolve().parents[1]/'watchlists/manual_watchlist.csv').read_bytes())

