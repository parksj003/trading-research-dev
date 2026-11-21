from copy import deepcopy

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from manual_catalog import csv_bytes
from test_manual_catalog import row
import manual_watchlist_view as view
import manual_chart_data as data


def screen(tmp_path,rows):
    from test_manual_watchlist_view import app
    return app(tmp_path,rows)


def sample_rows():
    return [row(symbol='NASDAQ:SYNTH1',watch_subsector='장비'),
        row(symbol='NASDAQ:SYNTH2',watch_sector='자동차',watch_subsector='부품',sort_order=2),
        row(symbol='NASDAQ:SYNTH3',watch_region='일본',watch_sector='산업재',watch_subsector='로봇',sort_order=3)]


def test_selection_controls_use_compact_country_and_sector_panel_with_daily_defaults(tmp_path):
    at=screen(tmp_path,sample_rows()).run()
    assert not at.exception and not at.multiselect
    assert {r.key for r in at.radio}=={'manual_cols','manual_bars'}
    assert at.radio(key='manual_cols').value==2
    assert at.selectbox(key='manual_size').value==12
    assert at.radio(key='manual_bars').value==130
    at.selectbox(key='manual_region').set_value('한국').run()
    assert [b.label for b in at.button if b.key.startswith('manual_sector_btn_')]==['전체 섹터 · 2','반도체 · 1','자동차 · 1']
    at.button(key=view.sector_button_key('반도체')).click().run()
    assert len(at.get('plotly_chart'))==1 and not at.get('iframe')
    at.selectbox(key='manual_region').set_value('일본').run()
    assert not at.exception and [b.label for b in at.button if b.key.startswith('manual_sector_btn_')]==['전체 섹터 · 1','산업재 · 1']
    assert at.session_state['manual_sector'] is None
    assert len(at.get('plotly_chart'))==1


def test_columns_page_size_and_page_remain_valid_when_filter_changes(tmp_path):
    rows=[row(symbol=f'NASDAQ:SYNTH{i}',name=f'Synthetic {i}',sort_order=i) for i in range(17)]
    at=screen(tmp_path,rows).run()
    at.radio(key='manual_cols').set_value(4);at.selectbox(key='manual_size').set_value(8);at.run()
    assert len(at.get('plotly_chart'))==8 and at.number_input(key='manual_page').max==3
    at.number_input(key='manual_page').set_value(3).run()
    assert len(at.get('plotly_chart'))==1
    at.text_input(key='manual_query').input('Synthetic 0').run()
    assert not at.exception and at.number_input(key='manual_page').value==1
    assert at.radio(key='manual_cols').value==4 and len(at.get('plotly_chart'))==1


def test_yield_has_original_link_and_does_not_retry_known_restricted_embed(tmp_path,monkeypatch):
    monkeypatch.setattr(view,'load_chart_data',lambda r:pytest.fail('yield fetched unrelated prices'))
    at=screen(tmp_path,[row(symbol='TVC:US02Y',name='미국 2년 금리',asset_type='yield',unit='%',chart_style='line')]).run()
    at.selectbox(key='manual_scope').set_value('매크로').run()
    assert not at.exception and not at.get('iframe') and not at.get('plotly_chart')
    assert len(at.dataframe)==1
    table=at.dataframe[0].value
    assert table.iloc[0]['원본']==view.chart_url('TVC:US02Y')
    assert '미연결' in table.iloc[0]['사유']


def test_country_change_resets_page_even_if_old_page_number_would_still_fit(tmp_path):
    rows=[row(symbol=f'NASDAQ:SYNTH{i}',name=f'Synthetic {i}',sort_order=i) for i in range(16)]
    rows += [row(symbol=f'NASDAQ:JAPAN{i}',watch_region='일본',watch_sector='産業',sort_order=20+i) for i in range(16)]
    at=screen(tmp_path,rows).run();at.number_input(key='manual_page').set_value(2).run()
    at.selectbox(key='manual_region').set_value('일본').run()
    assert not at.exception and at.number_input(key='manual_page').value==1
    assert len(at.get('plotly_chart'))==12


@pytest.mark.parametrize('period,months',[('1개월',1),('3개월',3),('6개월',6),('1년',12),('2년',24)])
def test_display_period_is_sent_to_official_widget_without_changing_candle_interval(period,months):
    cfg=view.widget_config(row(symbol='FX:USDJPY',asset_type='fx'),interval='W',theme='light',ma=50,period=period)
    assert cfg['dateRanges']==[f'{months}m|1W']
    assert cfg['symbols'][0][1].endswith(f'|{months}M')
    with pytest.raises(ValueError):view.widget_config(row(),interval='D',theme='light',ma=0,period='</script>')


def bars():
    return dict(bars=[dict(date=d.date().isoformat(),open=100+i,high=103+i,low=99+i,close=102+i,volume=1000+i)
        for i,d in enumerate(pd.date_range('2024-11-12','2026-11-12',freq='B'))])


@pytest.mark.parametrize('interval',['D','W','M'])
def test_display_period_is_calendar_based_and_independent_of_candle_interval(interval):
    quotes=bars();before=deepcopy(quotes)
    short,count=data.price_figure(row(),quotes,interval=interval,theme='light',ma=20,period='1개월')
    long,_=data.price_figure(row(),quotes,interval=interval,theme='light',ma=20,period='1년')
    assert pd.Timestamp(short.layout.xaxis.range[0])==pd.Timestamp('2026-10-12')
    assert pd.Timestamp(long.layout.xaxis.range[0])==pd.Timestamp('2025-11-12')
    assert pd.Timestamp(short.layout.xaxis.range[1])==pd.Timestamp('2026-11-12')
    assert short.layout.uirevision!=long.layout.uirevision
    assert len(short.data[0].x)==count and quotes==before


def test_ma_uses_history_before_visible_window_and_period_change_does_not_fetch(monkeypatch):
    monkeypatch.setattr(data.requests,'get',lambda *a,**k:pytest.fail('display period made a network request'))
    quotes=bars();fig,_=data.price_figure(row(),quotes,interval='D',theme='light',ma=50,period='1개월')
    ma=next(trace for trace in fig.data if trace.name=='MA 50')
    first_visible=next(i for i,d in enumerate(ma.x) if pd.Timestamp(d)>=pd.Timestamp('2026-10-12'))
    expected=pd.Series([r['close'] for r in quotes['bars']]).rolling(50).mean().iloc[first_visible]
    assert ma.y[first_visible]==pytest.approx(expected)
    assert pd.notna(ma.y[first_visible])

