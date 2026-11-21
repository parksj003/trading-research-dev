"""Verified close-only price chart is visibly limited, never a candle proxy."""
from copy import deepcopy

from manual_chart_data import EOD_PERIODS,eod_chart
from manual_eod_fixture import snapshot_for
from test_manual_catalog import row


def _entry(*,full=True,close_day='2026-11-12',n=520):
    doc,_=snapshot_for([row()],length=300)
    entry=doc['instruments'][row()['symbol']]
    entry['symbol']='KRX:005930'
    last=entry['last_final_session']
    # Mock already-verified standalone source; full history remains independent.
    dates=entry['bars']['d']
    ext=['2026-11-12'] if close_day=='2026-11-12' else []
    date_series=(dates+ext)[-n:]
    partial=dict(d=date_series,c=[150.+i for i in range(len(date_series))],
        source='Yahoo Finance verified close-only',adjustment='yahoo:split-adjusted-OHLC;cash-dividend-unadjusted',
        last_final_session=date_series[-1])
    entry['close_only']=partial
    if not full:
        entry['bars']={};entry['status']='missing'
        entry['last_final_session']=None
    else:
        entry['last_final_session']=last
    return entry


def test_partial_series_is_line_not_fake_candles_and_never_has_volume():
    entry=_entry()
    before=deepcopy(entry)
    fig,_=eod_chart(entry,bars=260,volume=True)
    assert fig.layout.meta['close_only'] is True
    assert fig.data[0].type=='scatter'
    assert fig.data[0].name=='가격'
    assert not any(t.type in ('candlestick','bar') for t in fig.data)
    assert fig.layout.xaxis.type=='date'
    assert fig.data[0].x[-1]=='2026-11-12'
    assert entry==before


def test_latest_complete_candles_have_priority_over_old_close_only():
    entry=_entry(close_day='2026-11-11')
    fig,_=eod_chart(entry)
    assert fig.layout.meta['close_only'] is False
    assert fig.data[0].type=='candlestick'
    assert fig.layout.xaxis.type=='category'


def test_only_close_available_still_renders_and_honors_520_period():
    entry=_entry(full=False)
    fig,missing=eod_chart(entry,bars=520)
    assert 520 in EOD_PERIODS
    assert fig.layout.meta['close_only'] is True
    assert len(fig.data[0].x)==len(entry['close_only']['d'])
    assert len(fig.data[0].x)<=520
    assert fig.layout.xaxis.type=='date'


def test_missing_close_cannot_be_silently_replaced_by_old_candle_close():
    entry=_entry()
    entry['close_only']=None
    fig,_=eod_chart(entry)
    assert fig.layout.meta['close_only'] is False
    assert fig.data[0].type=='candlestick'


def test_app_displays_close_only_card_even_without_any_ohlc(tmp_path):
    from streamlit.testing.v1 import AppTest
    from manual_catalog import csv_bytes
    source_row=row()
    path=tmp_path/'list.csv'
    path.write_bytes(csv_bytes([source_row]))
    doc,_=snapshot_for([source_row],length=300)
    entry=doc['instruments'][source_row['symbol']]
    close=entry['bars']['c']
    dates=entry['bars']['d']
    entry['bars']={}
    entry['status']='missing'
    entry['last_final_session']=None
    entry['close_only']=dict(d=dates,c=close,source='Yahoo Finance verified close-only',
        adjustment='yahoo:split-adjusted-OHLC;cash-dividend-unadjusted',
        source_symbol='005930.KS',last_final_session=dates[-1])
    script=('from manual_watchlist_view import render_manual_watchlist\n'
        'render_manual_watchlist('+repr(str(path))+',snapshot='+repr(doc)
        +',source_state={"status":"ready"})')
    at=AppTest.from_string(script).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==1
    assert any('종가 전용' in x.value for x in at.get('caption'))

