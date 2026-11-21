import ast
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
from rotation_markers import (rotation_marker_metrics,calculate_rotation_marker_sizes,
                             classify_sector_sample_size)
from test_chart_views import sectors,history


@pytest.mark.parametrize('passed,total,expected',[(3,4,75),(10,37,1000/37),(10,43,1000/43),(0,10,0),(0,0,None)])
def test_actual_count_rates(passed,total,expected):
    row=rotation_marker_metrics(pd.DataFrame({'passed_cnt':[passed],'univ_total':[total]})).iloc[0]
    assert pd.isna(row.pass_rate_pct) if expected is None else row.pass_rate_pct==pytest.approx(expected)


def test_existing_engine_percentage_is_used_when_consistent_and_only_display_is_repaired():
    d=pd.DataFrame({'passed_cnt':[10,10,10,10,0],'univ_total':[37,37,37,37,0],'pass_rate_pct':[27.,.27,np.nan,120.,50.]})
    before=d.copy(deep=True);r=rotation_marker_metrics(d)
    assert r.pass_rate_pct.iloc[0]==27.
    assert r.pass_rate_pct.iloc[1:4].tolist()==pytest.approx([1000/37]*3)
    assert r.rate_mismatch.iloc[1] and pd.isna(r.pass_rate_pct.iloc[4])
    pd.testing.assert_frame_equal(d,before)


def test_rate_size_boundaries_monotonic_equal_rates_and_unknown():
    d=pd.DataFrame({'passed_cnt':[0,1,25,100,1,10],'univ_total':[100,100,100,100,4,40]})
    size=calculate_rotation_marker_sizes(d)
    assert size.iloc[:4].is_monotonic_increasing
    assert size.iloc[[0,2,3]].tolist()==pytest.approx([12,27,42])
    assert size.iloc[2]==size.iloc[4]==size.iloc[5]
    bad=pd.DataFrame({'pass_rate_pct':[None,np.nan,-1,101,np.inf]})
    assert calculate_rotation_marker_sizes(bad).eq(12).all()
    assert calculate_rotation_marker_sizes(d).between(12,42).all()
    with pytest.raises(ValueError):calculate_rotation_marker_sizes(d,'other')


def test_count_mode_matches_legacy_formula_including_zero_and_missing():
    d=pd.DataFrame({'passed_cnt':[0,1,3,6,10,-1,np.nan]})
    expected=(d.passed_cnt.fillna(1).clip(lower=1)*4.5+12).clip(12,42)
    pd.testing.assert_series_equal(calculate_rotation_marker_sizes(d,'passed_count'),expected)


@pytest.mark.parametrize('n,status',[(4,'표본 매우 적음'),(5,'표본 주의'),(9,'표본 주의'),(10,'별도 표본 경고 없음'),(0,'표본 확인 불가'),(None,'표본 확인 불가'),(-1,'표본 확인 불가'),(1.5,'표본 확인 불가')])
def test_sample_size_status(n,status):assert classify_sector_sample_size(n)==status


def test_size_mode_does_not_change_coordinates_colors_paths_labels_or_outlines():
    d=sectors();tail=app.sector_rotation_history(history(),d,'KR','sector','2026-11-04',5)
    a=app.sector_rotation_quadrant(d,'sector',tail,size_mode='pass_rate')
    b=app.sector_rotation_quadrant(d,'sector',tail,size_mode='passed_count')
    for k in ['x','y','text','customdata']:
        assert np.array_equal(getattr(a.data[-1],k),getattr(b.data[-1],k))
    assert np.array_equal(a.data[-1].marker.color,b.data[-1].marker.color)
    assert a.data[-1].marker.colorscale==b.data[-1].marker.colorscale
    assert a.data[-1].marker.line==b.data[-1].marker.line
    # Single outlines are unchanged between size modes; arrow clearance follows radius.
    assert [t.to_json() for t in a.data[:-1]]==[t.to_json() for t in b.data[:-1]]
    for aa,bb in zip(a.layout.annotations,b.layout.annotations):
        da,db=aa.to_plotly_json(),bb.to_plotly_json()
        if aa.name=='rotation_direction':
            assert da.pop('standoff')==pytest.approx(a.data[-1].marker.size[0]/2+5)
            assert db.pop('standoff')==pytest.approx(b.data[-1].marker.size[0]/2+5)
        assert da==db
    assert a.layout.shapes==b.layout.shapes
    assert a.layout.meta['rotation_path_count']==b.layout.meta['rotation_path_count']


def test_hover_carries_actual_counts_rates_and_unknown_fields_without_zero_fill():
    d=pd.DataFrame({'sector':['a','b'],'median_rs':[70.,80.],'rs_delta_4w':[1.,2.],
                    'sector_power':[60.,70.],'passed_cnt':[3,0],'univ_total':[4,0],
                    'pct_above_50dma':[50.,np.nan],'actionable_cnt':[1,np.nan]})
    f=app.sector_rotation_quadrant(d,'sector');data=f.data[-1].customdata
    assert list(data[0][1:6])==['4개','3개','75%','50%','1개']
    assert data[0][7]=='표본 매우 적음'
    assert list(data[1][3:6])==['확인 불가']*3
    assert list(f.data[-1].marker.line.width)==[1.4,1.]


def test_ten_published_sectors_compare_sizes_and_preserve_engine_fields():
    fixture=json.loads(Path('tests/fixtures/rotation_sectors.json').read_text());d=pd.DataFrame(fixture['rows'])
    before=d.copy(deep=True);r=rotation_marker_metrics(d)
    assert len(d)==10 and not r.rate_mismatch.any()
    assert calculate_rotation_marker_sizes(d).tolist()==pytest.approx((12+30*np.sqrt(d.pass_rate_pct/100)).tolist())
    assert calculate_rotation_marker_sizes(d,'passed_count').tolist()==pytest.approx(((d.passed_cnt.clip(lower=1)*4.5+12).clip(12,42)).tolist())
    assert d.univ_total.lt(5).any() and d.passed_cnt.eq(0).any()
    pd.testing.assert_frame_equal(d,before)


def test_market_level_size_switches_are_unique_and_other_controls_unchanged():
    at=AppTest.from_string('''
import app
from test_chart_views import sectors
import pandas as pd
s=sectors();s['univ_total']=20;s['pass_rate_pct']=s['passed_cnt']/20*100
parts=[s.assign(market=m,level=l) for m in ['KR','US'] for l in ['macro','sector']]
b=app.Bundle(man={'d_str':'2026-11-04'},summ={},sectors=pd.concat(parts,ignore_index=True))
app.tab_sectors(b)
''',default_timeout=60).run()
    for market in ['KR','US']:
        at.radio(key='sec_mk').set_value(market).run()
        for level in ['macro','sector']:
            at.radio(key='sec_lvl').set_value(level).run()
            key=f'sec_size_{market}_{level}'
            assert at.radio(key=key).value=='pass_rate'
            assert any('원 크기: 통과 비율 (%)' in item.value for item in at.caption)
            at.radio(key=key).set_value('passed_count').run()
            assert not at.exception
            assert any('원 크기: 통과 종목 수' in item.value for item in at.caption)
            assert at.radio(key=f'sec_label_mode_{market}_{level}').value=='주요 라벨'
            assert at.radio(key=f'sec_path_cols_{market}_{level}').value==3
            assert at.toggle(key=f'sec_all_paths_{market}_{level}').value is False
            assert at.selectbox(key=f'sec_path_size_{market}_{level}').value==12
            assert list(map(str,at.radio(key=f'sec_tail_days_{market}_{level}').options))==['5','10','20']


def test_history_and_treemap_implementations_are_unchanged():
    expected={'sector_rotation_history': '747ac05aac9b79da5de9b68be6374555041287d7f24c471743852d4e969f617e', 'sector_treemap': 'dd865216ecf59ba3c19a151cbca5aba22e665cb5d79ea9afa4c8df6be9bd1e8e'}
    source=Path('app.py').read_text();lines=source.splitlines(keepends=True)
    for n in ast.parse(source).body:
        if isinstance(n,ast.FunctionDef) and n.name in expected:
            start=n.decorator_list[0].lineno if n.decorator_list else n.lineno
            assert hashlib.sha256(''.join(lines[start-1:n.end_lineno]).encode()).hexdigest()==expected[n.name]


def test_synthetic_high_power_low_rate_does_not_control_size():
    # No Power>=70 / rate<=30 example exists in this published snapshot.
    d=pd.DataFrame({'sector':['합성 고Power 저통과율','합성 저Power 고통과율'],
                    'median_rs':[80.,60.],'rs_delta_4w':[1.,-1.],
                    'sector_power':[95.,35.],'passed_cnt':[1,9],'univ_total':[10,10]})
    f=app.sector_rotation_quadrant(d,'sector')
    assert f.data[-1].marker.size[0]<f.data[-1].marker.size[1]
    assert list(f.data[-1].marker.color)==[95.,35.]


def test_eight_column_radiogroup_visible_without_expander():
    import inspect
    source=inspect.getsource(app.tab_sectors)
    assert 'list(range(1, 9))' in source
    assert 'sec_path_cols_' in source
    assert 'sec_all_paths_' in source
    assert 'with st.expander("⚙️ 표시 설정' not in source
    assert 'periods = st.radio(' in source
    assert 'size_mode = st.radio(' in source

