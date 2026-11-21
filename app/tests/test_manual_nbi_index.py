"""NBI is an explicitly modeled index, not an equity or placeholder candle."""
from pathlib import Path
import copy
import pytest

from manual_catalog import parse_csv,filter_rows,instrument_defaults
from manual_chart_data import eod_chart
from manual_eod_contract import validate_snapshot

CATALOG=Path(__file__).resolve().parents[1]/'watchlists'/'manual_watchlist.csv'

def test_catalog_removes_x_and_keeps_biotech_index():
    rows=parse_csv(CATALOG.read_bytes())
    assert not any(r['symbol']=='AMEX:X' for r in rows)
    nbi=[r for r in rows if r['symbol']=='NASDAQ:NBI']
    assert len(nbi)==1
    assert nbi[0]['asset_type']=='index'
    assert nbi[0]['unit']=='지수 포인트'
    assert nbi[0]['chart_style']=='line'
    assert instrument_defaults('NASDAQ:NBI')['asset_type']=='index'
    assert nbi[0] in filter_rows(rows,asset_scope='macro')
    assert nbi[0] not in filter_rows(rows,asset_scope='stocks')

def test_verified_nbi_index_is_line_only():
    item=dict(symbol='NASDAQ:NBI',asset_type='index',bars={},
        last_final_session='2026-11-18',status='current',
        close_only=dict(d=['2026-11-17','2026-11-18'],c=[6000.,6010.],
            last_final_session='2026-11-18',adjustment='yahoo:index-level-unadjusted'))
    fig,missing=eod_chart(item,bars=20,columns=2,volume=True)
    assert fig.layout.meta['close_only'] is True
    assert fig.layout.meta['price_basis']=='yahoo:index-level-unadjusted'
    assert len(fig.data)>=1
    assert all(trace.type!='candlestick' for trace in fig.data)
    assert all(trace.type!='bar' for trace in fig.data)
    assert 20 in missing

