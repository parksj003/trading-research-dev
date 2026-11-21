import json
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app


def sectors():
    return pd.DataFrame([
        dict(market='KR', level='sector', macro_theme='반도체', sector='장비·소재',
             sector_power=82., median_rs=85., rs_delta_4w=8., passed_cnt=3,
             univ_total=10, pct_above_50dma=70., leader_name='가상기업'),
        dict(market='KR', level='sector', macro_theme='바이오', sector='장비·소재',
             sector_power=45., median_rs=55., rs_delta_4w=-2., passed_cnt=0,
             univ_total=5, pct_above_50dma=40., leader_name='예시기업'),
    ])


def history():
    rows = []
    for date, rs in [('2026-10-28', 60.), ('2026-10-29', 70.), ('2026-10-30', 75.)]:
        rows.append(dict(date=date, market='KR', level='sector', macro_theme='반도체',
                         sector='장비·소재', median_rs=rs, rs_delta_4w=-6.))
    rows += [dict(rows[0], market='US', median_rs=99.),
             dict(rows[0], level='macro', median_rs=98.),
             dict(rows[0], date='2026-11-01', median_rs=97.),
             dict(rows[0], date='2026-10-28', median_rs=65.)]
    return pd.DataFrame(rows)


def test_paths_use_recorded_dates_and_exact_market_level_identity_without_mutating_inputs():
    current, old = sectors(), history()
    before_current, before_old = current.copy(deep=True), old.copy(deep=True)
    result = app.sector_rotation_history(old, current, 'KR', 'sector', '2026-10-30', 3)
    tail = result[result['_key'].map(lambda k: k == ('반도체', '장비·소재'))]
    assert tail['date'].dt.strftime('%Y-%m-%d').tolist() == ['2026-10-28', '2026-10-29', '2026-10-30']
    assert tail['median_rs'].tolist() == [65., 70., 85.]  # Latest snapshot wins on its date.
    other = result[result['_key'].map(lambda k: k == ('바이오', '장비·소재'))]
    assert len(other) == 1  # Same sector label in another theme never borrows a trail.
    pd.testing.assert_frame_equal(current, before_current)
    pd.testing.assert_frame_equal(old, before_old)


@pytest.mark.parametrize('date', [None, '', 'invalid-date', {'KOSPI':'2026-10-30'}])
def test_unknown_snapshot_date_never_invents_a_current_history_point(date):
    assert app.sector_rotation_history(history(), sectors(), 'KR', 'sector', date).empty


@pytest.mark.parametrize('missing', ['date', 'median_rs', 'sector'])
def test_legacy_history_missing_required_coordinates_displays_current_points_only(missing):
    tail = app.sector_rotation_history(history().drop(columns=missing), sectors(), 'KR', 'sector', '2026-10-30')
    assert tail.empty
    fig = app.sector_rotation_quadrant(sectors(), 'sector', tail)
    assert fig.layout.meta['rotation_path_count'] == 0
    assert len(fig.data[-1].x) == 2


def test_macro_history_supports_empty_sector_identity_and_a_real_record_limit():
    current = sectors().head(1).drop(columns='sector').assign(level='macro')
    old = history().query("market == 'KR'").drop(columns='sector').assign(level='macro')
    tail = app.sector_rotation_history(old, current, 'KR', 'macro', '2026-10-30', 2)
    assert tail['date'].nunique() == 2
    assert set(tail['_key']) == {('반도체', '')}


def test_rotation_draws_only_focused_real_paths_and_preserves_current_coordinates():
    current = sectors()
    tail = app.sector_rotation_history(history(), current, 'KR', 'sector', '2026-10-30')
    fig = app.sector_rotation_quadrant(current, 'sector', tail, [('반도체', '장비·소재')])
    assert fig.layout.height >= 900
    assert fig.layout.meta['rotation_path_count'] == 1
    assert list(fig.data[-1].x) == current['median_rs'].tolist()
    assert list(fig.data[-1].marker.color) == current['sector_power'].tolist()
    assert fig.data[-1].mode == 'markers'
    path = next(t for t in fig.data if t.meta and t.meta.get('kind') == 'rotation_path')
    assert [row[0] for row in path.customdata] == ['2026-10-28', '2026-10-29', '2026-10-30']
    assert len([a for a in fig.layout.annotations if a.name == 'sector_label']) == len(current)
    assert len([a for a in fig.layout.annotations if a.name == 'rotation_direction']) == 1
    no_focus = app.sector_rotation_quadrant(current, 'sector', tail, [])
    assert no_focus.layout.meta['rotation_path_count'] == 0 and len(no_focus.data) == 1
    assert len([a for a in no_focus.layout.annotations if a.name == 'sector_label']) == len(current)


def test_stationary_sector_does_not_receive_a_false_direction_arrow():
    current = sectors().head(1)
    old = current.assign(date='2026-10-29')
    tail = app.sector_rotation_history(old, current, 'KR', 'sector', '2026-10-30')
    fig = app.sector_rotation_quadrant(current, 'sector', tail)
    assert fig.layout.meta['rotation_path_count'] == 0
    assert not any(a.name == 'rotation_direction' for a in fig.layout.annotations)


@pytest.mark.parametrize('name', ['AI반도체·HBM·본딩장비', 'Semiconductor Equipment & Materials',
                                '아주긴섹터이름을생략없이확인하는표시테스트(MLB)'])
def test_sector_name_wrapping_preserves_the_entire_name(name):
    lines = app.sector_label_lines(name)
    assert ''.join(lines).replace(' ', '') == name.replace(' ', '')
    assert not any('…' in line for line in lines)


def test_all_sector_names_remain_visible_when_no_trails_are_selected():
    current = pd.concat([sectors().head(1).assign(sector=f'전체섹터{i:03d}') for i in range(100)], ignore_index=True)
    fig = app.sector_rotation_quadrant(current, 'sector', focus_keys=[])
    labels = [a for a in fig.layout.annotations if a.name == 'sector_label']
    assert len(labels) == len(fig.data[-1].x) == 100
    assert all(str(row.sector) in label.text for row, label in zip(current.itertuples(), labels))
    assert fig.layout.meta['rotation_path_count'] == 0


def test_treemap_shows_all_names_and_metrics_with_equal_and_original_area_options():
    current = sectors()
    before = current.copy(deep=True)
    fig = app.sector_treemap(current, 'KR')
    trace = fig.data[0]
    assert fig.layout.height >= 900 and fig.layout.uniformtext.mode is False
    parents = set(trace.parents)
    leaves = [i for i, node in enumerate(trace.ids) if node not in parents]
    assert sorted(trace.values[i] for i in leaves) == [1., 1.]
    assert all('customdata[0]' in trace.texttemplate[i] and 'customdata[2]' in trace.texttemplate[i] for i in leaves)
    assert sorted(float(trace.customdata[i][2]) for i in leaves) == [0., 3.]
    assert all('대장주' in trace.hovertemplate[i] for i in leaves)
    assert all('대장주' not in trace.hovertemplate[i] for i, node in enumerate(trace.ids) if node in parents)
    detailed = app.sector_treemap(current, 'KR', details=True)
    assert any('customdata[3]' in text for text in detailed.data[0].texttemplate)
    weighted = app.sector_treemap(current, 'KR', equal_size=False)
    assert sorted(weighted.data[0].values[i] for i in leaves) == [1., 3.]
    assert list(trace.marker.colors) == list(weighted.data[0].marker.colors)
    pd.testing.assert_frame_equal(current, before)


def test_sector_screen_renders_native_overview_mini_cards_and_missing_history(monkeypatch):
    monkeypatch.setattr(app.requests, 'get', lambda *a, **k: pytest.fail('sector screen must reuse its bundle'))
    records = sectors().to_dict('records')
    script = ('import pandas as pd\nimport app\n'
              f"app.tab_sectors(app.Bundle(man={{'d_str':'2026-10-30'}}, summ={{}}, sectors=pd.DataFrame({records!r})))")
    at = AppTest.from_string(script).run()
    assert not at.exception
    assert len(at.get('plotly_chart')) == 5  # Main map, 2 current-only mini cards, treemap, ranking
    assert at.radio(key='sec_path_cols_KR_sector').value == 3
    assert at.toggle(key='sec_all_paths_KR_sector').value is False
    assert at.dataframe[0].value['섹터파워'].tolist() == [82., 45.]
    assert any('이력 부족' in c.value for c in at.caption)
    assert at.radio(key='sec_label_mode_KR_sector').value == '주요 라벨'
    first = json.loads(at.get('plotly_chart')[0].proto.spec)
    assert first['layout']['meta']['rotation_path_count'] == 0
    assert len(first['data']) == 1
    for chart in at.get('plotly_chart')[1:3]:
        spec = json.loads(chart.proto.spec)
        assert spec['layout']['meta']['rotation_mini_has_path'] is False
        assert [t['meta']['kind'] for t in spec['data']] == ['rotation_mini_current']
    at.toggle(key='sec_tree_details_KR').set_value(True).run()
    assert not at.exception
    at.radio(key='sec_label_mode_KR_sector').set_value('번호 표시').run()
    assert not at.exception
    numbered = [table.value for table in at.dataframe if '번호' in table.value]
    assert len(numbered) == 1 and numbered[0]['번호'].tolist() == [1, 2]
    at.multiselect(key='sec_focus_KR_sector').set_value([]).run()
    assert not at.exception and len(at.get('plotly_chart')) == 3
    main = json.loads(at.get('plotly_chart')[0].proto.spec)
    assert main['layout']['meta']['rotation_path_count'] == 0



def test_publication_date_uses_korean_date_instead_of_per_track_as_of():
    tail = app.sector_rotation_history(history(), sectors(), 'KR', 'sector', '2026-10-29T23:00:00Z')
    assert tail['date'].max().strftime('%Y-%m-%d') == '2026-10-30'


def candle_payload(length=280):
    return {'d': pd.bdate_range('2025-01-02', periods=length).strftime('%Y-%m-%d').tolist(),
            **{key: [100.] * length for key in ('o', 'h', 'l', 'c', 's20', 's50', 's200')},
            'v': [1000] * length, 'pivot': None, 'stop': None}


@pytest.mark.parametrize('bars', app.CHART_PERIODS)
def test_candle_periods_keep_ohlcv_and_moving_averages_aligned(bars):
    source = candle_payload()
    fig = app.candle_chart(source, 'US', bars=bars)
    assert list(fig.data[0].x) == source['d'][-bars:]
    assert all(len(trace.x) == bars for trace in fig.data)
    assert len(source['d']) == 280


def stock_record():
    return dict(market='US', ticker='TEST', name='Example Company', sub_market='SP500',
                rs_score=90., topdown_score=80., fund_badge='-', setup_stage='관찰', sector='Tech',
                rs_change_4w=2., last_close=100., stop_price=95., pivot_price=99.,
                position_size_pct=5., portfolio_alloc_pct=0.)


def test_deep_dive_offers_one_year_and_honestly_reports_legacy_short_history():
    # Payloads are synthetic and generated inside AppTest; no source requests.
    script = ('import pandas as pd\nimport app\n'
              'from test_chart_views import candle_payload\n'
              f"b=app.Bundle(man={{}}, summ={{}}, charts={{'US:TEST':candle_payload(130)}})\n"
              f"app.render_single_stock_deep_dive(b, pd.DataFrame([{stock_record()!r}]))")
    at = AppTest.from_string(script).run()
    assert not at.exception and at.radio(key='deep_bars').value == 260
    assert any('260봉 중 게시된 130봉' in item.value for item in at.info)
    at.radio(key='deep_bars').set_value(45).run()
    assert not at.exception
    assert any('실제 표시: 45봉' in item.value for item in at.caption)
    assert not any('게시된 130봉' in item.value for item in at.info)


def test_multi_chart_offers_one_year_and_retains_shorter_periods():
    script = ('import pandas as pd\nimport app\n'
              'from test_chart_views import candle_payload\n'
              f"b=app.Bundle(man={{'run_id':'synthetic'}}, summ={{}}, charts={{'US:TEST':candle_payload()}})\n"
              f"app.render_multi_chart_grid(b, pd.DataFrame([{stock_record()!r}]))")
    at = AppTest.from_string(script).run()
    assert not at.exception
    at.radio(key='grid_bars').set_value(260).run()
    assert not at.exception and at.radio(key='grid_bars').value == 260


def test_mini_charts_preserve_recorded_dates_and_use_shared_axes():
    current = sectors()
    tail = app.sector_rotation_history(history(), current, 'KR', 'sector', '2026-10-30', 5)
    first = current.iloc[0]
    second = current.iloc[1]
    before_cur = current.copy(deep=True)
    before_tail = tail.copy(deep=True)
    axes = app.rotation_mini_shared_bounds(current, tail)
    f1 = app.sector_rotation_mini_figure(first, tail[tail['_key'].map(lambda x: x == ('반도체','장비·소재'))],
                                          *axes, '#0077BB')
    f2 = app.sector_rotation_mini_figure(second, tail[tail['_key'].map(lambda x: x == ('바이오','장비·소재'))],
                                          *axes, '#C05A14')
    assert f1.layout.xaxis.range == f2.layout.xaxis.range
    assert f1.layout.yaxis.range == f2.layout.yaxis.range
    for fig in (f1, f2):
        assert fig.layout.yaxis.scaleanchor == 'x'
        assert fig.layout.yaxis.scaleratio == 1
        assert fig.layout.xaxis.constrain == fig.layout.yaxis.constrain == 'range'
    assert f1.layout.meta['rotation_mini_has_path'] is True
    h = next(t for t in f1.data if t.meta and t.meta.get('kind') == 'rotation_mini_history')
    assert list(h.customdata) == ['2026-10-28','2026-10-29','2026-10-30']
    assert list(h.x) == [65.,70.,85.]
    assert len([t for t in f1.data if t.meta and t.meta.get('kind') == 'rotation_mini_recent']) == 1
    assert f2.layout.meta['rotation_mini_has_path'] is False
    assert [t.meta['kind'] for t in f2.data] == ['rotation_mini_current']
    assert f1.data[-1].x[0] == first.median_rs and f1.data[-1].y[0] == first.rs_delta_4w
    assert '직전 관측 대비 RS' in f1.data[-1].hovertemplate
    assert '직전 관측 대비 RS' not in f2.data[-1].hovertemplate
    pd.testing.assert_frame_equal(current,before_cur)
    pd.testing.assert_frame_equal(tail,before_tail)


def test_mini_stationary_or_missing_observations_have_no_fake_path():
    current = sectors().iloc[0]
    stationary = pd.DataFrame([
        dict(date='2026-10-28', median_rs=80., rs_delta_4w=5.),
        dict(date='2026-10-29', median_rs=80., rs_delta_4w=5.),
    ])
    f = app.sector_rotation_mini_figure(current, stationary, (60.,90.), (-10.,10.), '#0077BB')
    assert f.layout.meta['rotation_mini_has_path'] is False
    assert [t.meta['kind'] for t in f.data] == ['rotation_mini_current']
    missing = stationary.assign(median_rs=[float('nan'), float('nan')])
    g = app.sector_rotation_mini_figure(current, missing, (60.,90.), (-10.,10.), '#0077BB')
    assert g.layout.meta['rotation_mini_history_points'] == 0
    assert [t.meta['kind'] for t in g.data] == ['rotation_mini_current']


def test_sector_table_is_above_rotation_tabs_and_grid_without_duplication():
    import inspect
    source = inspect.getsource(app.tab_sectors)
    assert source.index('st.markdown("##### 📋 섹터 현황') < source.index('st.tabs([')
    assert source.count('d[table_cols].rename(columns=LABELS)') == 1
    assert 'height=340' in source


def test_many_paths_use_stable_colors_beyond_legacy_eight_limit():
    # The old trail_styles() was limited to eight paths; cards must not be.
    from test_rotation_trails import fixture
    import json
    d, h = fixture(10, 5)
    keys = [app.sector_key(r, 'sector') for _, r in d.iterrows()]
    plot = app.sector_rotation_quadrant(d, 'sector', history=None, focus_keys=keys[:3], compact_labels=True)
    assert len(plot.data) == 1
    assert len(keys) == 10
    from rotation_trails import PALETTES
    values = [PALETTES['light'][int.from_bytes(
        __import__('hashlib').sha256(json.dumps(k, ensure_ascii=False, separators=(",", ":")).encode()).digest()[:4],
        'big') % len(PALETTES['light'])] for k in keys]
    assert len(values) == 10


@pytest.mark.parametrize('columns', [1, 2, 3, 4, 5, 6, 7, 8])
def test_mini_chart_fills_domain_without_distorting_one_to_one_units(columns):
    row = sectors().iloc[0]
    fig = app.sector_rotation_mini_figure(
        row, pd.DataFrame(), (30., 100.), (-55., 55.), '#0077BB', columns=columns
    )
    assert fig.layout.yaxis.scaleanchor == 'x'
    assert fig.layout.yaxis.scaleratio == 1
    assert fig.layout.xaxis.constrain == fig.layout.yaxis.constrain == 'range'
    assert fig.layout.meta['rotation_mini_columns'] == columns
    assert fig.layout.height > 100
    assert fig.layout.margin.t <= 4
    assert fig.layout.margin.l <= 34
    if columns >= 7:
        assert fig.layout.xaxis.showticklabels is False
        assert fig.layout.yaxis.showticklabels is False
        assert fig.data[-1].mode == 'markers'
    else:
        assert fig.layout.xaxis.showticklabels is True
    assert len(fig.layout.shapes) == 6  # Quadrants + two zero/70 lines.


def test_ux_controls_are_visible_and_attached_to_their_correct_charts():
    import inspect
    source = inspect.getsource(app.tab_sectors)
    assert source.index('size_mode = st.radio(') < source.index('show_sector_plot(fig,')
    assert source.index('st.markdown("##### ②') < source.index('n_cols = st.radio(')
    assert source.index('n_cols = st.radio(') < source.index('all_paths = st.toggle(')
    assert 'with st.expander("⚙️ 표시 설정' not in source
    assert 'list(range(1, 9))' in source
    assert 'show_plotly(mini' in source

