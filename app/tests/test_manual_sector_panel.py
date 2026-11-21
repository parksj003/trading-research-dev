"""Layout/state regressions using synthetic EOD data, never provider requests."""
from copy import deepcopy
import json

import pytest

import manual_watchlist_view as view
from manual_catalog import filter_rows
from test_manual_catalog import row
from test_manual_watchlist_view import app


def memberships():
    return [row(symbol='NASDAQ:ALPHA',name='Alpha',watch_sector='반도체'),
            row(symbol='NASDAQ:BETA',name='Beta',watch_sector='반도체',sort_order=2),
            row(symbol='NASDAQ:ALPHA',name='Alpha',watch_sector='자동차',sort_order=3),
            row(symbol='NASDAQ:GAMMA',name='Gamma',watch_sector='자동차',sort_order=4),
            row(symbol='NASDAQ:JAPAN',watch_region='일본',watch_sector='산업재',sort_order=5),
            row(symbol='TVC:US02Y',watch_sector='금리',asset_type='yield',unit='%',chart_style='line',sort_order=6)]


def sector_button(at,sector):
    return at.button(key=view.sector_button_key(sector))


@pytest.fixture(autouse=True)
def no_price_fetch(monkeypatch):
    import requests
    monkeypatch.setattr(requests,'get',lambda *a,**k:pytest.fail('layout fetched external prices'))
    monkeypatch.setattr(view,'load_chart_data',lambda *a,**k:pytest.fail('layout fetched live prices'))


def test_sector_counts_are_unique_memberships_and_preserve_catalog_order():
    data=memberships();before=deepcopy(data)
    regional=filter_rows(data,regions=('한국',),asset_scope='stocks')
    assert view.sector_counts(regional)==[(None,3),('반도체',2),('자동차',2)]
    assert data==before


def test_sector_labels_truncate_only_display_and_keep_distinct_identity():
    name='아주 긴 섹터 이름과 [분류] '*8
    label=view.sector_label(name,123)
    assert '…' in label and label.endswith(' · 123') and len(label)<40
    assert view.sector_button_key(name)!=view.sector_button_key(name+'2')
    assert view.sector_button_key(None)!=view.sector_button_key('전체 섹터')


def test_panel_default_ratio_all_and_single_sector_counts(tmp_path):
    at=app(tmp_path,memberships()).run()
    assert not at.exception and at.selectbox(key='manual_layout').value=='20:80'
    assert at.selectbox(key='manual_layout').options==['10:90','20:80','30:70','직접 조절']
    assert at.selectbox(key='manual_pane_height_mode').value=='900px'
    assert at.checkbox(key='manual_panel_open').value is True
    assert sector_button(at,None).label=='전체 섹터 · 4'
    at.selectbox(key='manual_region').set_value('한국').run()
    assert sector_button(at,None).label=='전체 섹터 · 3'
    assert sector_button(at,'반도체').label=='반도체 · 2'
    sector_button(at,'반도체').click().run()
    assert at.session_state['manual_sector']=='반도체' and len(at.get('plotly_chart'))==2
    assert sector_button(at,'반도체').proto.type=='primary'
    sector_button(at,None).click().run()
    assert len(at.get('plotly_chart'))==3 and at.session_state['manual_sector'] is None


def test_sector_search_and_symbol_search_have_independent_state(tmp_path):
    at=app(tmp_path,memberships()).run()
    sector_button(at,'반도체').click().run()
    at.text_input(key='manual_query').input('Beta').run()
    assert len(at.get('plotly_chart'))==1
    at.text_input(key='manual_sector_query').input('자동차').run()
    assert at.session_state['manual_sector']=='반도체' and len(at.get('plotly_chart'))==1
    assert sector_button(at,'반도체') and sector_button(at,'자동차')
    sector_button(at,'자동차').click().run()
    assert not at.get('plotly_chart') and at.text_input(key='manual_query').value=='Beta'
    at.text_input(key='manual_query').input('Alpha').run()
    assert len(at.get('plotly_chart'))==1


def test_region_scope_and_sector_change_reset_only_page_and_invalid_sector(tmp_path):
    at=app(tmp_path,memberships()).run()
    sector_button(at,'반도체').click().run()
    at.radio(key='manual_bars').set_value(45).run()
    at.selectbox(key='manual_region').set_value('일본').run()
    assert at.session_state['manual_sector'] is None and len(at.get('plotly_chart'))==1
    assert at.radio(key='manual_bars').value==45 and at.session_state['manual_page']==1
    at.selectbox(key='manual_region').set_value(None).run()
    at.selectbox(key='manual_scope').set_value('매크로').run()
    assert at.session_state['manual_sector'] is None and not at.get('plotly_chart')
    assert sector_button(at,'금리').label=='금리 · 1'


@pytest.mark.parametrize('layout',['10:90','30:70','직접 조절'])
def test_ratio_changes_and_reset_preserve_selection_page_and_options(tmp_path,layout):
    rows=[row(symbol=f'NASDAQ:SYNTH{i}',name=f'Synthetic {i}',sort_order=i) for i in range(25)]
    at=app(tmp_path,rows).run()
    sector_button(at,'반도체').click().run()
    at.radio(key='manual_cols').set_value(3).run()
    at.radio(key='manual_bars').set_value(80).run()
    at.selectbox(key='manual_size').set_value(8).run()
    at.number_input(key='manual_page').set_value(2).run()
    before=[c.proto.spec for c in at.get('plotly_chart')]
    at.selectbox(key='manual_layout').set_value(layout).run()
    if layout=='직접 조절':
        width=at.slider(key='manual_custom_width')
        assert width.min==1 and width.max==50 and width.step==1
        width.set_value(50).run()
    at.selectbox(key='manual_pane_height_mode').set_value('1600px').run()
    assert not at.exception and at.session_state['manual_sector']=='반도체'
    assert at.number_input(key='manual_page').value==2
    assert at.radio(key='manual_cols').value==3 and at.radio(key='manual_bars').value==80
    assert [c.proto.spec for c in at.get('plotly_chart')]==before
    at.button(key='manual_layout_reset').click().run()
    assert at.selectbox(key='manual_layout').value=='20:80' and at.number_input(key='manual_page').value==2
    assert at.selectbox(key='manual_pane_height_mode').value=='900px'
    assert at.session_state['manual_custom_width']==20
    assert at.session_state['manual_sector']=='반도체'
    assert at.radio(key='manual_cols').value==3 and at.radio(key='manual_bars').value==80
    assert [c.proto.spec for c in at.get('plotly_chart')]==before


def test_hide_show_panel_retains_filters_across_widget_cleanup(tmp_path):
    at=app(tmp_path,memberships()).run()
    at.selectbox(key='manual_region').set_value('한국').run()
    sector_button(at,'자동차').click().run()
    at.text_input(key='manual_sector_query').input('자동').run()
    at.checkbox(key='manual_panel_open').uncheck().run()
    assert not at.exception and not any(x.key=='manual_region' for x in at.selectbox)
    assert len(at.get('plotly_chart'))==2
    # Additional rerun while hidden exercises real widget-state cleanup.
    at.radio(key='manual_bars').set_value(20).run()
    at.checkbox(key='manual_panel_open').check().run()
    assert at.selectbox(key='manual_region').value=='한국'
    assert at.session_state['manual_sector']=='자동차'
    assert at.text_input(key='manual_sector_query').value=='자동' and len(at.get('plotly_chart'))==2


def test_sector_change_resets_page_even_when_old_page_would_fit(tmp_path):
    rows=[row(symbol=f'NASDAQ:SYNTH{i}',watch_sector='반도체' if i<20 else '자동차',sort_order=i) for i in range(40)]
    at=app(tmp_path,rows).run();at.number_input(key='manual_page').set_value(2).run()
    sector_button(at,'자동차').click().run()
    assert at.number_input(key='manual_page').value==1 and len(at.get('plotly_chart'))==12
    assert 'SYNTH20' in at.get('plotly_chart')[0].proto.id


@pytest.mark.parametrize('mode,height',[('600px',600),('900px',900),('1200px',1200),('1600px',1600),('직접 입력',2000)])
def test_many_sectors_have_two_equal_scroll_panes_and_full_label_tooltip(tmp_path,mode,height):
    rows=[row(symbol=f'NASDAQ:SYNTH{i}',watch_sector='매우 긴 이름의 섹터 '+str(i)*10,sort_order=i) for i in range(45)]
    at=app(tmp_path,rows).run()
    at.selectbox(key='manual_pane_height_mode').set_value(mode).run()
    if mode=='직접 입력':
        custom=at.number_input(key='manual_pane_height_custom')
        assert custom.min==320
        custom.set_value(height).run()
    assert not at.exception
    containers=[x for x in at.get('flex_container') if x.proto.height_config.WhichOneof('height_spec')=='pixel_height']
    # Both panes scroll independently; the old nested 360px sector scroller is gone.
    assert len(containers)==2
    assert all(x.proto.height_config.pixel_height==height for x in containers)
    assert len(containers[0].get('button'))==46 and not containers[0].get('plotly_chart')
    assert len(containers[1].get('plotly_chart'))==12
    button=sector_button(at,rows[0]['watch_sector'])
    assert '…' in button.label and rows[0]['watch_sector'] in button.proto.help


def test_layout_search_and_returning_page_reuse_chart_cache_without_price_calls(tmp_path,monkeypatch):
    import manual_chart_data as data
    calls=[];original=data.eod_chart
    def count(entry,*args,**kwargs):
        calls.append(entry['symbol']);return original(entry,*args,**kwargs)
    monkeypatch.setattr(data,'eod_chart',count);data.cached_eod_json.clear()
    rows=[row(symbol=f'NYSE:CACHE{i}',name=f'Cache {i}',sort_order=i) for i in range(18)]
    at=app(tmp_path,rows).run()
    assert len(calls)==12
    at.selectbox(key='manual_layout').set_value('30:70').run()
    at.selectbox(key='manual_pane_height_mode').set_value('1200px').run()
    at.checkbox(key='manual_panel_open').uncheck().run()
    assert len(calls)==12 and len(at.get('plotly_chart'))==12
    at.number_input(key='manual_page').set_value(2).run()
    assert len(calls)==18 and len(at.get('plotly_chart'))==6
    at.number_input(key='manual_page').set_value(1).run()
    assert len(calls)==18 and len(at.get('plotly_chart'))==12
    at.text_input(key='manual_query').input('Cache 0').run()
    assert len(calls)==18 and len(at.get('plotly_chart'))==1
    assert json.loads(at.get('plotly_chart')[0].proto.config)['responsive'] is True


def test_session_defaults_do_not_duplicate_widget_defaults_or_warn(tmp_path,monkeypatch,caplog):
    from streamlit.elements.lib import policies
    monkeypatch.setattr(policies,'_shown_default_value_warning',False)
    at=app(tmp_path,memberships()).run()
    assert not at.exception
    assert at.checkbox(key='manual_panel_open').value and at.checkbox(key='manual_volume').value
    assert at.radio(key='manual_cols').value==2 and at.radio(key='manual_bars').value==130
    assert at.selectbox(key='manual_size').value==12
    assert not any('created with a default value' in record.message for record in caplog.records)

