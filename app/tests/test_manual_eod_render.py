from copy import deepcopy
import json

import pandas as pd
import pytest

from manual_catalog import parse_csv,filter_rows,unique_instruments
from manual_chart_data import eod_chart,cached_eod_json,EOD_HEIGHT
from manual_eod_fixture import snapshot_for
from test_manual_catalog import row
from test_manual_watchlist_view import app


def test_ma_all_three_use_full_close_history_before_visible_window(monkeypatch):
    import requests
    monkeypatch.setattr(requests,'get',lambda *a,**k:pytest.fail('renderer queried prices'))
    doc,_=snapshot_for([row()]);entry=doc['instruments'][row()['symbol']];before=deepcopy(entry)
    fig,missing=eod_chart(entry,20)
    assert not missing and fig.data[0].type=='candlestick' and len(fig.data[0].x)==20
    for length,color in [(20,'#2563EB'),(50,'#D97706'),(200,'#7C3AED')]:
        line=next(t for t in fig.data if t.name==f'{length}일')
        assert line.line.color==color
        assert line.y[0]==pytest.approx(pd.Series(entry['bars']['c']).rolling(length).mean().iloc[-20])
    assert entry==before and not fig.layout.shapes


@pytest.mark.parametrize('length,missing',[(1,[20,50,200]),(20,[50,200]),(50,[200]),(200,[])])
def test_short_history_and_missing_volume_are_not_fabricated(length,missing):
    doc,_=snapshot_for([row()],length=length);entry=next(iter(doc['instruments'].values()))
    entry['bars']['v']=[None]*length
    fig,actual=eod_chart(entry)
    assert actual==missing and all(t.type!='bar' for t in fig.data)
    assert len(fig.data[0].x)==min(length,130)
    assert list(fig.data[0].open)==entry['bars']['o'][-130:]


@pytest.mark.parametrize('cols',[1,2,3,4])
@pytest.mark.parametrize('theme',['light','dark'])
def test_column_height_and_theme_are_explicit_and_consistent(cols,theme):
    doc,_=snapshot_for([row()]);fig,_=eod_chart(next(iter(doc['instruments'].values())),columns=cols,theme=theme)
    assert fig.layout.height==EOD_HEIGHT[cols]
    assert fig.layout.font.color==('#CBD5E1' if theme=='dark' else '#334155')
    assert fig.layout.xaxis.type=='category'
    assert fig.layout.margin.t==14


def test_missing_dates_remain_gaps_and_cache_uses_content_hash():
    from manual_eod_contract import canonical,digest
    doc,_=snapshot_for([row()]);entry=next(iter(doc['instruments'].values()))
    for values in entry['bars'].values():values.pop(-3)
    cached_eod_json.clear()
    args=(doc['run_id'],entry['symbol'])
    raw,_=cached_eod_json(*args,digest(canonical(entry)),20,2,'light',True,entry)
    fig=json.loads(raw)
    assert fig['data'][0]['x']==entry['bars']['d'][-20:]
    changed=deepcopy(entry);changed['bars']['c'][-1]-=.5
    raw2,_=cached_eod_json(*args,digest(canonical(changed)),20,2,'light',True,changed)
    assert raw!=raw2


def test_full_catalog_memberships_unique_order_and_defaults_are_preserved():
    # Preserve scale/order/dedup coverage using synthetic memberships only.
    from manual_catalog import csv_bytes
    regions=['한국','글로벌/미국','일본','중국/홍콩']
    synthetic=[row(symbol=f'NYSE:SYNTH{i}', name=f'Synthetic {i}',
        watch_region=regions[min(i//106,3)],sort_order=i) for i in range(424)]
    synthetic.append(dict(synthetic[-1],watch_sector='Synthetic duplicate membership',sort_order=424))
    rows=parse_csv(csv_bytes(synthetic))
    assert len(rows)==425 and len(unique_instruments(filter_rows(rows)))==424
    assert list(dict.fromkeys(r['watch_region'] for r in rows))==['한국','글로벌/미국','일본','중국/홍콩']
    doc,_=snapshot_for(rows)
    assert list(doc['instruments'])==[r['symbol'] for r in unique_instruments(filter_rows(rows))]


def test_page_period_and_columns_render_only_requested_cards(tmp_path,monkeypatch):
    import requests
    monkeypatch.setattr(requests,'get',lambda *a,**k:pytest.fail('screen queried prices'))
    rows=[row(symbol=f'NYSE:SYNTH{i}',name=('긴 이름 '*20 if i%2 else '짧음')+str(i),sort_order=i) for i in range(65)]
    at=app(tmp_path,rows).run()
    assert not at.exception and len(at.get('plotly_chart'))==12
    for cols in (1,2,3,4):
        at.radio(key='manual_cols').set_value(cols).run()
        assert not at.exception and len(at.get('plotly_chart'))==12
    at.selectbox(key='manual_size').set_value(60).run()
    assert len(at.get('plotly_chart'))==60
    at.number_input(key='manual_page').set_value(2).run()
    assert len(at.get('plotly_chart'))==5
    at.radio(key='manual_bars').set_value(20).run()
    assert not at.exception and len(at.get('plotly_chart'))==5 and at.number_input(key='manual_page').value==2


def test_one_bad_card_does_not_remove_other_charts(tmp_path):
    from streamlit.testing.v1 import AppTest
    rows=[row(symbol='NYSE:SYNTH1'),row(symbol='NYSE:SYNTH2',sort_order=2)]
    from manual_catalog import csv_bytes
    path=tmp_path/'list.csv';path.write_bytes(csv_bytes(rows));doc,_=snapshot_for(rows)
    doc['instruments']['NYSE:SYNTH1']['bars'].pop('c')
    at=AppTest.from_string('from manual_watchlist_view import render_manual_watchlist\nrender_manual_watchlist('+repr(str(path))+',snapshot='+repr(doc)+',source_state={"status":"ready"})').run()
    assert not at.exception and len(at.warning)==1 and len(at.get('plotly_chart'))==1

