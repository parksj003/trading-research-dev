"""Synthetic recorded paths: no provider requests or inferred trading dates."""
import json

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
from rotation_trails import PALETTES, effective_trail_mode, trail_styles


def fixture(count=8, records=20, market='KR', level='sector'):
    rows, old = [], []
    dates = pd.date_range('2026-09-01', periods=records, freq='2D')
    for i in range(count):
        row = dict(market=market, level=level, macro_theme=f'테마{i}', sector=f'섹터{i}',
                   median_rs=50.+i*5, rs_delta_4w=5.+i, sector_power=45.+i*5,
                   passed_cnt=10, univ_total=10, pass_rate_pct=100., pct_above_50dma=70., leader_name='테스트 기업')
        rows.append(row)
        for j, date in enumerate(dates):
            old.append(dict(row, date=date.strftime('%Y-%m-%d'),
                            median_rs=row['median_rs']-15*(1-j/(records-1)) if records>1 else row['median_rs'],
                            rs_delta_4w=row['rs_delta_4w']-20*(1-j/(records-1)) if records>1 else row['rs_delta_4w']))
    d=pd.DataFrame(rows)
    h=app.sector_rotation_history(pd.DataFrame(old),d,market,level,dates[-1].strftime('%Y-%m-%d'),20)
    return d,h


def traces(fig,kind):
    return [t for t in fig.data if t.meta and t.meta.get('kind')==kind]


@pytest.mark.parametrize('theme',['라이트','다크'])
@pytest.mark.parametrize('count',[1,2,5,8])
def test_visible_paths_distinct_styles_and_current_engine_values_unchanged(monkeypatch,theme,count):
    monkeypatch.setitem(app.st.session_state,'ui_theme_mode',theme)
    d,h=fixture(count);before=d.copy(deep=True);before_h=h.copy(deep=True)
    keys=[app.sector_key(r,'sector') for _,r in d.iterrows()]
    f=app.sector_rotation_quadrant(d,'sector',h,keys,trail_mode='sector')
    paths=traces(f,'rotation_path')
    assert len(paths)==count and f.layout.meta['rotation_path_count']==count
    assert len({p.line.color for p in paths})==count
    assert all(p.marker.symbol=='circle' and p.line.dash=='solid' for p in paths)
    assert all(p.line.width==1.4 and p.opacity==.4 for p in paths)
    assert not traces(f,'rotation_halo')
    assert len(f.data)<=3*count+2
    assert list(f.data[-1].x)==d.median_rs.tolist()
    assert list(f.data[-1].y)==d.rs_delta_4w.tolist()
    assert list(f.data[-1].marker.color)==d.sector_power.tolist()
    assert list(f.data[-1].marker.size)==[42]*count
    assert list(f.data[-1].marker.line.width)==[1.]*count
    assert not traces(f,'rotation_endpoint')
    for a in f.layout.annotations:
        if a.name=='rotation_direction': assert a.standoff==26 and a.arrowwidth==1.2
    pd.testing.assert_frame_equal(d,before);pd.testing.assert_frame_equal(h,before_h)


def test_color_identity_is_stable_under_rank_and_selection_order_changes():
    d,h=fixture();keys=d.apply(lambda r:app.sector_key(r,'sector'),axis=1).tolist()
    assert trail_styles(keys)==trail_styles(list(reversed(keys)))
    a=app.sector_rotation_quadrant(d,'sector',h,keys,trail_mode='sector')
    b=app.sector_rotation_quadrant(d.iloc[::-1],'sector',h,keys[::-1],trail_mode='sector')
    mapping=lambda f:{tuple(t.meta['identity']):(t.line.color,t.line.dash,t.marker.symbol[-1]) for t in traces(f,'rotation_path')}
    assert mapping(a)==mapping(b)
    assert trail_styles([keys[0]])[keys[0]]==trail_styles([keys[0]])[keys[0]]


@pytest.mark.parametrize('market',['KR','US'])
@pytest.mark.parametrize('level',['macro','sector'])
@pytest.mark.parametrize('records',[1,20])
@pytest.mark.parametrize('mode',['simple','sector','time'])
def test_markets_levels_and_record_limits(market,level,records,mode):
    d,h=fixture(2,records,market,level)
    f=app.sector_rotation_quadrant(d,level,h,trail_mode=mode)
    assert f.layout.meta['rotation_path_count']==(2 if records>1 else 0)
    assert list(f.data[-1].x)==d.median_rs.tolist()
    for p in traces(f,'rotation_path'):
        assert len(p.x)==records and p.customdata[-1][1]=='T0'


def test_time_mode_uses_real_order_and_coordinate_changes_without_splitting_lines():
    d,h=fixture(1,5)
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode='time')
    p=traces(f,'rotation_path')[0]
    assert [r[0] for r in p.customdata]==sorted(h.date.dt.strftime('%Y-%m-%d').tolist())
    assert [r[1] for r in p.customdata]==['T-4','T-3','T-2','T-1','T0']
    assert p.customdata[0][2:]==['확인 불가','확인 불가']
    assert p.customdata[1][2:]==['+3.75','+5.00']
    assert list(p.marker.color)==[0,1,2,3,4]
    assert len(traces(f,'rotation_path'))==1 and not traces(f,'rotation_halo')
    assert len(f.data)==3  # path, recent, current bubbles
    sector=app.sector_rotation_quadrant(d,'sector',h)
    assert f.data[-1].to_json()==sector.data[-1].to_json()
    assert list(p.x)==list(traces(sector,'rotation_path')[0].x)


def test_duplicate_dates_unsorted_rows_and_stationary_last_step_never_make_a_false_direction():
    d,h=fixture(1,5)
    h.loc[h.index[-2],['median_rs','rs_delta_4w']]=h.iloc[-1][['median_rs','rs_delta_4w']].to_numpy()
    h=pd.concat([h.iloc[::-1],h.iloc[[0]]],ignore_index=True)
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode='time')
    p=traces(f,'rotation_path')[0]
    assert len(p.x)==5 and [r[0] for r in p.customdata]==sorted({r[0] for r in p.customdata})
    assert p.customdata[-1][2:]==['+0.00','+0.00']
    assert list(traces(f,'rotation_recent')[0].x)==list(p.x[-3:])
    assert not any(a.name=='rotation_direction' for a in f.layout.annotations)
    assert not traces(f,'rotation_endpoint')


def test_tiny_last_change_has_endpoint_but_no_overlapping_arrow():
    d,h=fixture(1,3)
    h.loc[h.index[-2],['median_rs','rs_delta_4w']]=[d.iloc[0].median_rs-.001,d.iloc[0].rs_delta_4w-.001]
    f=app.sector_rotation_quadrant(d,'sector',h)
    assert traces(f,'rotation_recent') and not traces(f,'rotation_endpoint')
    assert not any(a.name=='rotation_direction' for a in f.layout.annotations)


def test_crossing_and_identical_trails_do_not_shift_coordinates_or_bubble_values():
    d,h=fixture(2,5)
    h.loc[h['_key'].map(lambda k:k==('테마1','섹터1')),['median_rs','rs_delta_4w']]=h[h['_key'].map(lambda k:k==('테마0','섹터0'))][['median_rs','rs_delta_4w']].to_numpy()
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode='sector')
    p=traces(f,'rotation_path')
    assert list(p[0].x)==list(p[1].x) and list(p[0].y)==list(p[1].y)
    assert p[0].line.color!=p[1].line.color and p[0].line.dash==p[1].line.dash=='solid'
    assert list(f.data[-1].x)==d.median_rs.tolist()


def test_time_mode_is_restricted_to_two_and_invalid_mode_fails_clearly():
    assert effective_trail_mode('time',2)=='time'
    d,h=fixture(5)
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode='time')
    assert f.layout.meta['trail_mode']=='sector'
    assert all(isinstance(p.marker.color,str) for p in traces(f,'rotation_path'))
    with pytest.raises(ValueError,match='궤적'):app.sector_rotation_quadrant(d,'sector',h,trail_mode='invalid')
    with pytest.raises(ValueError,match='8개'):trail_styles([(str(i),'') for i in range(9)])


def test_step_colors_are_separate_from_power_and_missing_paths_do_not_break():
    d,h=fixture(2)
    h=h[h['_key'].map(lambda k:k==('테마0','섹터0'))]
    a=app.sector_rotation_quadrant(d,'sector',h,trail_mode='time')
    b=app.sector_rotation_quadrant(d.assign(sector_power=[10,99]),'sector',h,trail_mode='time')
    assert len(traces(a,'rotation_path'))==1
    assert traces(a,'rotation_path')[0].to_json()==traces(b,'rotation_path')[0].to_json()
    assert list(a.data[-1].marker.color)!=list(b.data[-1].marker.color)


def test_palette_has_useful_contrast_in_both_themes():
    def luminance(color):
        channels=[int(color[i:i+2],16)/255 for i in (1,3,5)]
        linear=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in channels]
        return sum(v*w for v,w in zip(linear,[.2126,.7152,.0722]))
    for theme,bg in [('light','#F8FAFC'),('dark','#0F172A')]:
        for color in PALETTES[theme]:
            a,b=sorted([luminance(color),luminance(bg)])
            assert (b+.05)/(a+.05)>=3


def test_ui_separates_clean_overview_from_three_recorded_trails():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(5,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=60).run()
    assert not at.exception
    assert at.radio(key='sec_path_cols_KR_sector').value == 3
    assert at.toggle(key='sec_all_paths_KR_sector').value is False
    assert len(at.get('plotly_chart')) == 6  # Main overview, 3 minis, treemap and ranking
    original = [json.loads(p.proto.spec) for p in at.get('plotly_chart')]
    overview = original[0]
    assert len(overview['data']) == 1
    assert overview['data'][0]['meta']['kind'] == 'rotation_points'
    assert not any(t.get('meta',{}).get('kind') == 'rotation_path' for t in overview['data'])
    assert overview['layout']['meta']['rotation_path_count'] == 0
    assert len([a for a in overview['layout']['annotations'] if a.get('name')=='sector_label']) == 5
    for mini in original[1:4]:
        assert mini['layout']['meta']['rotation_mini_has_path'] is True
        assert any(t.get('meta',{}).get('kind') == 'rotation_mini_history' for t in mini['data'])
        assert mini['layout']['xaxis']['range'] == original[1]['layout']['xaxis']['range']
        assert mini['layout']['yaxis']['range'] == original[1]['layout']['yaxis']['range']
    old_other = original[-2:]

    at.multiselect(key='sec_focus_KR_sector').set_value([('테마0','섹터0')]).run()
    assert not at.exception
    changed = [json.loads(p.proto.spec) for p in at.get('plotly_chart')]
    assert len(changed) == 4  # Main, 1 mini, treemap, ranking
    assert len(changed[0]['data']) == 1
    assert changed[-2:] == old_other

    at.multiselect(key='sec_focus_KR_sector').set_value([]).run()
    assert not at.exception
    assert len(at.get('plotly_chart')) == 3
    cleared = json.loads(at.get('plotly_chart')[0].proto.spec)
    assert len(cleared['data']) == 1 and cleared['layout']['meta']['rotation_path_count'] == 0



def test_large_recent_move_has_arrow_clear_of_maximum_bubble_and_sparse_step_labels():
    d,h=fixture(1,3)
    f=app.sector_rotation_quadrant(d,'sector',h)
    arrows=[a for a in f.layout.annotations if a.name=='rotation_direction']
    assert len(arrows)==1 and arrows[0].standoff==26
    assert f.layout.meta['direction_count']==1
    d,h=fixture(2,20)
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode='time')
    assert all(sum(bool(text) for text in p.text)<=4 for p in traces(f,'rotation_path'))
    assert not traces(f,'rotation_endpoint')


def test_same_dates_share_time_color_even_when_one_sector_has_missing_observations():
    d,h=fixture(2,5)
    h=h.drop(h[(h['_key'].map(lambda k:k==('테마1','섹터1')))&(h.date.isin(h.date.unique()[1:3]))].index)
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode='time')
    a,b=traces(f,'rotation_path')
    colors=lambda p:{r[0]:color for r,color in zip(p.customdata,p.marker.color)}
    ca,cb=colors(a),colors(b)
    assert len(cb)<len(ca)
    assert all(ca[date]==color for date,color in cb.items())
    assert a.marker.cmin==b.marker.cmin and a.marker.cmax==b.marker.cmax
    assert b.customdata[-1][1]=='T0' and b.customdata[0][1]=='T-2'


@pytest.mark.parametrize('records',[1,2,3,5,10,20])
@pytest.mark.parametrize('mode',['simple','sector','time'])
def test_recent_three_actual_points(records,mode):
    d,h=fixture(1,records)
    f=app.sector_rotation_quadrant(d,'sector',h,trail_mode=mode)
    recent=traces(f,'rotation_recent')
    if records==1:
        assert not recent
    else:
        assert list(recent[0].x)==h.median_rs.tail(3).tolist()
        assert list(recent[0].y)==h.rs_delta_4w.tail(3).tolist()
        assert len(traces(f,'rotation_path')[0].x)==records


@pytest.mark.parametrize('theme',['라이트','다크'])
@pytest.mark.parametrize('count',[1,3,8])
def test_simple_view_visual_hierarchy_and_compact_labels(monkeypatch,theme,count):
    monkeypatch.setitem(app.st.session_state,'ui_theme_mode',theme)
    d,h=fixture(12,20)
    keys=[app.sector_key(r,'sector') for _,r in d.head(count).iterrows()]
    f=app.sector_rotation_quadrant(d,'sector',h,keys,compact_labels=True)
    paths=traces(f,'rotation_path')
    assert len(paths)==count and len(f.data[-1].x)==12
    assert len({p.line.color for p in paths})==1
    assert all(p.line.dash=='solid' and p.line.width==1.4 and p.opacity==.4 for p in paths)
    assert all(p.line.width==2.2 and p.opacity==1 and len(p.x)==3 for p in traces(f,'rotation_recent'))
    assert not traces(f,'rotation_halo') and not traces(f,'rotation_endpoint')
    expected=set(keys)|{app.sector_key(r,'sector') for _,r in d.nlargest(5,'sector_power').iterrows()}
    labels=[a for a in f.layout.annotations if a.name=='sector_label']
    assert len(labels)==len(expected)
    assert len(f.data[-1].text)==12  # Hidden names stay in hover.
    default=app.sector_rotation_quadrant(d,'sector',h)
    assert default.layout.meta['rotation_path_count']==3


def test_ui_defaults_and_all_period_choices():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(8,20)
app.tab_sectors(app.Bundle(man={'d_str':'2026-10-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=60).run()
    assert not at.exception
    assert len(at.multiselect(key='sec_focus_KR_sector').value) == 3
    assert at.radio(key='sec_tail_days_KR_sector').value == 5
    assert list(map(str, at.radio(key='sec_tail_days_KR_sector').options)) == ['5','10','20']
    assert at.radio(key='sec_size_KR_sector').value == 'pass_rate'
    assert at.radio(key='sec_label_mode_KR_sector').value == '주요 라벨'
    assert len(at.get('plotly_chart')) == 6
    for n in [5,10,20]:
        at.radio(key='sec_tail_days_KR_sector').set_value(n).run()
        assert not at.exception
        charts = [json.loads(c.proto.spec) for c in at.get('plotly_chart')]
        assert len(charts[0]['data']) == 1  # No trails in overview regardless of period.
        for mini in charts[1:4]:
            trails=[t for t in mini['data'] if t.get('meta',{}).get('kind')=='rotation_mini_history']
            assert len(trails)==1 and len(trails[0]['customdata'])==n
        assert charts[-2]['data'][0]['type'] == 'treemap'


def test_rotation_grid_column_switch_and_all_paths_shows_every_sector():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(10,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=90).run()
    assert not at.exception
    assert at.radio(key='sec_path_cols_KR_sector').value == 3
    assert len(at.get('plotly_chart')) == 6   # 3 initial selections + overview/tree/rank
    all_options = [(f'테마{i}', f'섹터{i}') for i in range(10)]
    at.multiselect(key='sec_focus_KR_sector').set_value(all_options).run()
    assert not at.exception
    assert len(at.get('plotly_chart')) == len(all_options)+3
    at.radio(key='sec_path_cols_KR_sector').set_value(4).run()
    assert not at.exception
    assert len(at.get('plotly_chart')) == len(all_options)+3
    all_specs = [json.loads(chart.proto.spec) for chart in at.get('plotly_chart')]
    assert len(all_specs[0]['data']) == 1
    assert all(spec['layout']['yaxis']['scaleanchor'] == 'x' and
               spec['layout']['yaxis']['scaleratio'] == 1 for spec in all_specs[1:-2])
    assert all(spec['layout']['meta']['rotation_mini_has_path'] is True for spec in all_specs[1:-2])
    at.multiselect(key='sec_focus_KR_sector').set_value([]).run()
    assert len(at.get('plotly_chart')) == 3
    at.toggle(key='sec_all_paths_KR_sector').set_value(True).run()
    assert not at.exception and len(at.get('plotly_chart')) == len(all_options)+3
    assert not any(x.key == 'sec_focus_KR_sector' for x in at.multiselect)
    at.radio(key='sec_path_cols_KR_sector').set_value(1).run()
    assert not at.exception
    assert len(at.get('plotly_chart')) == len(all_options)+3


def test_grid_eight_columns_and_visible_path_settings():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(10,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=90).run()
    assert not at.exception
    assert at.radio(key='sec_path_cols_KR_sector').value == 3
    assert at.radio(key='sec_tail_days_KR_sector').value == 5
    assert at.radio(key='sec_size_KR_sector').value == 'pass_rate'
    assert at.toggle(key='sec_all_paths_KR_sector').value is False
    assert len(at.get('plotly_chart')) == 6
    at.radio(key='sec_path_cols_KR_sector').set_value(8).run()
    assert not at.exception
    at.toggle(key='sec_all_paths_KR_sector').set_value(True).run()
    assert not at.exception
    charts = [json.loads(x.proto.spec) for x in at.get('plotly_chart')]
    assert len(charts) == 13
    assert not any(x.key == 'sec_focus_KR_sector' for x in at.multiselect)
    assert all(c['layout']['meta']['rotation_mini_columns'] == 8 for c in charts[1:-2])
    assert all(c['layout']['xaxis']['constrain'] == 'range' for c in charts[1:-2])
    assert all(c['layout']['yaxis']['scaleratio'] == 1 for c in charts[1:-2])
    at.toggle(key='sec_all_paths_KR_sector').set_value(False).run()
    assert not at.exception
    assert at.multiselect(key='sec_focus_KR_sector').value


def test_rotation_grid_pages_only_render_current_page_with_stable_comparison():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(18,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=90).run()
    assert not at.exception
    assert at.selectbox(key='sec_path_size_KR_sector').value==12
    assert len(at.get('plotly_chart'))==6  # 3 manually selected + overview/tree/rank
    at.toggle(key='sec_all_paths_KR_sector').set_value(True).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==15  # 12 mini cards, not 18.
    page1=[json.loads(c.proto.spec) for c in at.get('plotly_chart')]
    assert all(p['layout']['yaxis']['scaleanchor']=='x' for p in page1[1:-2])
    first_range=page1[1]['layout']['xaxis']['range']
    assert at.number_input(key='sec_path_page_KR_sector').value==1
    at.number_input(key='sec_path_page_KR_sector').set_value(2).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==9  # 6 mini cards on second page
    page2=[json.loads(c.proto.spec) for c in at.get('plotly_chart')]
    assert page2[1]['layout']['xaxis']['range']==first_range
    assert page2[0]['layout']['meta']['rotation_path_count']==0
    assert at.dataframe[0].value.shape[0]==18
    assert any('18개 중 13–18개 표시' in c.value for c in at.caption)
    at.selectbox(key='sec_path_size_KR_sector').set_value('전체').run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==21
    assert not any(x.key.startswith('sec_path_page_') for x in at.number_input)
    at.selectbox(key='sec_path_size_KR_sector').set_value(6).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==9
    assert at.number_input(key='sec_path_page_KR_sector').value==1
    at.radio(key='sec_path_cols_KR_sector').set_value(4).run()
    assert not at.exception and len(at.get('plotly_chart'))==9
    at.toggle(key='sec_all_paths_KR_sector').set_value(False).run()
    assert not at.exception and len(at.get('plotly_chart'))==6


def test_rotation_page_limit_bigger_than_selected_set_does_not_hide_any_card():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(10,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=90).run()
    assert not at.exception
    at.selectbox(key='sec_path_size_KR_sector').set_value(6).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==6
    assert not any(x.key.startswith('sec_path_page_') for x in at.number_input)
    options=[(f'테마{i}', f'섹터{i}') for i in range(10)]
    at.multiselect(key='sec_focus_KR_sector').set_value(options).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==9
    assert at.number_input(key='sec_path_page_KR_sector').value==1


def test_sector_map_full_labels_restore_all_names_and_number_mode():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(12,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=90).run()
    assert not at.exception
    assert at.radio(key='sec_label_mode_KR_sector').value=='주요 라벨'
    default=json.loads(at.get('plotly_chart')[0].proto.spec)
    assert len([x for x in default['layout']['annotations'] if x.get('name')=='sector_label'])==5
    chart_count=len(at.get('plotly_chart'))
    at.radio(key='sec_label_mode_KR_sector').set_value('전체 라벨').run()
    assert not at.exception
    entire=json.loads(at.get('plotly_chart')[0].proto.spec)
    labels=[x for x in entire['layout']['annotations'] if x.get('name')=='sector_label']
    assert len(labels)==12
    assert len(at.get('plotly_chart'))==chart_count  # Paths are unaffected by labels.
    assert any('전체 섹터 이름 표시' in c.value for c in at.caption)
    at.radio(key='sec_label_mode_KR_sector').set_value('번호 표시').run()
    assert not at.exception
    numbered=json.loads(at.get('plotly_chart')[0].proto.spec)
    assert len([x for x in numbered['layout']['annotations'] if x.get('name')=='sector_label'])==12
    assert any('번호' in frame.value.columns for frame in at.dataframe)
    at.radio(key='sec_label_mode_KR_sector').set_value('주요 라벨').run()
    assert not at.exception
    compact=json.loads(at.get('plotly_chart')[0].proto.spec)
    assert len([x for x in compact['layout']['annotations'] if x.get('name')=='sector_label'])==5


def test_page_stepper_stays_within_bounds_when_options_reduce():
    at=AppTest.from_string('''
import app
from test_rotation_trails import fixture
d,h=fixture(18,5)
app.tab_sectors(app.Bundle(man={'d_str':'2026-09-09'},summ={},sectors=d,
                           h_sector=h.drop(columns='_key').assign(market='KR',level='sector')))
''',default_timeout=90).run()
    assert not at.exception
    at.toggle(key='sec_all_paths_KR_sector').set_value(True).run()
    assert not at.exception
    pager=at.number_input(key='sec_path_page_KR_sector')
    assert pager.min==1 and pager.max==2 and pager.step==1
    pager.set_value(2).run()
    assert not at.exception
    assert any('18개 중 13–18개 표시' in c.value for c in at.caption)
    at.selectbox(key='sec_path_size_KR_sector').set_value(6).run()
    assert not at.exception
    assert at.number_input(key='sec_path_page_KR_sector').value==2
    at.number_input(key='sec_path_page_KR_sector').set_value(3).run()
    assert not at.exception
    assert len(at.get('plotly_chart'))==9
    at.selectbox(key='sec_path_size_KR_sector').set_value('전체').run()
    assert not at.exception
    assert not any(x.key=='sec_path_page_KR_sector' for x in at.number_input)
    at.selectbox(key='sec_path_size_KR_sector').set_value(6).run()
    assert not at.exception
    assert at.number_input(key='sec_path_page_KR_sector').value==1
    at.toggle(key='sec_all_paths_KR_sector').set_value(False).run()
    assert not at.exception and len(at.get('plotly_chart'))==6

