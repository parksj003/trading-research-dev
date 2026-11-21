"""Optional observation UI contract. Synthetic data; no stock APIs."""
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import app
import base_count_view as view


def payload():
    return json.loads((Path(__file__).parent/'fixtures/base_counts_valid.json').read_text())


def bundle(doc=None):
    doc=payload() if doc is None else doc
    raw=json.dumps(doc,ensure_ascii=False).encode()
    man=dict(run_id=doc['run_id'],files={view.FILE_NAME:dict(sha256=hashlib.sha256(raw).hexdigest(),bytes=len(raw))})
    return raw,man


def frame():
    row=payload()['observations'][0]
    return pd.DataFrame([dict(market=row['market'],ticker=row['ticker'],as_of=row['as_of'],
                              rank=1,rs_score=95.,topdown_score=91.,is_actionable=True,
                              stop_price=190.,position_size_pct=4.)])


def attached():
    raw,man=bundle();observations,source=view.load_base_counts(raw,man)
    return view.attach_base_counts(frame(),observations,source)


def test_valid_engine_fixture_loads_exact_counts_and_date():
    raw,man=bundle();rows,source=view.load_base_counts(raw,man)
    assert source['status']=='enabled' and len(rows)==1
    assert rows.iloc[0].completed_base_count==1
    assert rows.iloc[0].status=='BREAKOUT_CONFIRMED'


def test_legacy_manifest_does_not_request_or_invent_observations():
    rows,source=view.load_base_counts(None,{'files':{}})
    assert source=={'status':'absent'} and rows.empty
    result=view.attach_base_counts(frame(),rows,source)
    pd.testing.assert_frame_equal(result[frame().columns],frame())
    assert result.bc_display.tolist()==['미확인 · 자료 미게시']
    assert result.bc_current_base_ordinal.isna().all()


@pytest.mark.parametrize('problem',['hash','size','missing','schema','run','duplicate','negative','bool_count','unknown_count','future_geometry','invalid_date','naive_time'])
def test_invalid_optional_payload_is_hidden_without_affecting_legacy_candidates(problem):
    doc=payload()
    if problem=='schema':doc['schema']='different'
    if problem=='run':doc['run_id']='wrong'
    if problem=='duplicate':doc['observations']*=2
    row=doc['observations'][0]
    if problem=='negative':row['completed_base_count']=-1
    if problem=='bool_count':row['completed_base_count']=True
    if problem=='unknown_count':row['quality']='HISTORY_INSUFFICIENT'
    if problem=='future_geometry':row['base_start_date']='2099-01-01'
    if problem=='invalid_date':row['as_of']='bad'
    if problem=='naive_time':row['recorded_at']='2026-11-09'
    raw,man=bundle(doc)
    if problem=='run':man['run_id']='expected'
    if problem=='hash':raw+=b' '
    if problem=='size':man['files'][view.FILE_NAME]['bytes']+=1
    if problem=='missing':raw=None
    rows,source=view.load_base_counts(raw,man)
    assert rows.empty and source['status']=='unavailable'
    result=view.attach_base_counts(frame(),rows,source)
    assert result.bc_current_base_ordinal.isna().all()
    pd.testing.assert_frame_equal(result[frame().columns],frame())


def test_exact_identity_date_join_never_borrows_another_market_or_stale_count():
    raw,man=bundle();rows,source=view.load_base_counts(raw,man)
    f=pd.concat([frame(),frame().assign(market='KR'),frame().assign(as_of='2024-01-01')],ignore_index=True)
    result=view.attach_base_counts(f,rows,source)
    assert result.bc_current_base_ordinal.tolist()[0]==1
    assert result.bc_current_base_ordinal.iloc[1:].isna().all()
    assert result.bc_status.tolist()==['BREAKOUT_CONFIRMED','UNKNOWN','UNKNOWN']
    pd.testing.assert_frame_equal(result[f.columns],f)


def test_duplicate_candidate_tracks_do_not_duplicate_rows_or_change_order_or_signals():
    raw,man=bundle();rows,source=view.load_base_counts(raw,man)
    f=pd.concat([frame().assign(sub_market='SP500'),frame().assign(sub_market='NASDAQ100')],ignore_index=True)
    result=view.attach_base_counts(f,rows,source)
    assert len(result)==len(f) and list(result.sub_market)==list(f.sub_market)
    pd.testing.assert_frame_equal(result[f.columns],f)


def test_history_insufficient_remains_unknown_not_zero():
    doc=payload();row=doc['observations'][0]
    row.update(quality='HISTORY_INSUFFICIENT',current_base_ordinal=None,completed_base_count=None)
    raw,man=bundle(doc);rows,source=view.load_base_counts(raw,man)
    result=view.attach_base_counts(frame(),rows,source)
    assert result.bc_completed_base_count.isna().all() and result.bc_label.iloc[0]=='순번 미확인'


def test_default_filter_is_identity_and_selected_filters_only_change_view():
    f=attached();before=f.copy(deep=True)
    pd.testing.assert_frame_equal(view.filter_base_counts(f),before)
    assert len(view.filter_base_counts(f,ordinals=['1']))==1
    assert view.filter_base_counts(f,ordinals=['2']).empty
    assert view.filter_base_counts(f,statuses=['BASE_FAILED']).empty
    assert len(view.filter_base_counts(f,qualities=['CONFIRMED']))==1
    pd.testing.assert_frame_equal(f,before)


def test_disabled_publication_does_not_expose_old_counts():
    doc=payload();doc['status']='disabled';raw,man=bundle(doc)
    rows,source=view.load_base_counts(raw,man);f=view.attach_base_counts(frame(),rows,source)
    assert f.bc_current_base_ordinal.isna().all() and f.bc_status.iloc[0]=='UNKNOWN'


def chart():
    row=payload()['observations'][0]
    dates=pd.bdate_range(row['base_start_date'],row['as_of']).strftime('%Y-%m-%d').tolist()
    n=len(dates)
    return dict(d=dates,o=[200]*n,h=[205]*n,l=[195]*n,c=[204]*n,v=[1000]*n,pivot=201.,stop=190.)


def test_overlay_preserves_price_volume_existing_pivot_stop_and_bounds():
    ch=chart();row=attached().iloc[0];fig=app.candle_chart(ch,'US')
    before=[t.to_plotly_json() for t in fig.data];shapes=len(fig.layout.shapes)
    view.add_base_overlay(fig,ch,row,130)
    assert [t.to_plotly_json() for t in fig.data]==before
    assert len(fig.layout.shapes)==shapes+2
    rect=fig.layout.shapes[-2]
    assert ch['d'][0]<=rect.x0<=rect.x1<=row.bc_as_of
    assert fig.layout.shapes[0].y0==201*.975
    assert fig.layout.shapes[2].y0==190


def test_missing_legacy_or_unknown_geometry_does_not_add_overlays():
    ch=chart();fig=app.candle_chart(ch,'US');before=fig.to_json()
    view.add_base_overlay(fig,ch,{},130)
    assert fig.to_json()==before
    row=attached().iloc[0].to_dict();row['bc_base_start_date']='2099-01-01'
    view.add_base_overlay(fig,ch,row,130)
    assert fig.to_json()==before


def test_streamlit_filter_panel_and_observation_metrics_render_without_errors():
    # Fixtures are loaded locally inside AppTest; no engine or network import.
    script='''
import json,hashlib
from pathlib import Path
import pandas as pd
import streamlit as st
from base_count_view import load_base_counts,attach_base_counts,observation_filters,render_base_panel
p=Path('tests/fixtures/base_counts_valid.json')
doc=json.loads(p.read_text());raw=json.dumps(doc).encode()
man={'run_id':doc['run_id'],'files':{'base_counts.json':{'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}}}
rows,source=load_base_counts(raw,man)
f=attach_base_counts(pd.DataFrame([{'market':'US','ticker':'SYNTH','as_of':rows.iloc[0].as_of}]),rows,source)
f=observation_filters(f,source)
st.write('display_count',len(f))
if not f.empty:render_base_panel(f.iloc[0])
'''
    at=AppTest.from_string(script).run()
    assert not at.exception and len(at.metric)==3
    assert at.metric[0].value=='Base 1' and at.metric[1].value=='1'
    at.multiselect(key='bc_ordinals').set_value(['2']).run()
    assert not at.exception and len(at.metric)==0


@pytest.mark.parametrize('advertised',[True,False])
def test_bundle_optional_fetch_failure_is_nonfatal_and_legacy_does_not_fetch_it(monkeypatch,advertised):
    raw,man=bundle();requested=[]
    if not advertised:man['files']={}
    def fetch(src_key,run_id,rel,src):
        requested.append(rel)
        if rel=='latest/base_counts.json':raise RuntimeError('optional request failure')
        if rel=='latest/summary.json':return b'{"regimes":{"SP500":{"regime":"RISK_ON"}}}'
        if rel=='latest/candidates.csv':return b'market,ticker,as_of\nUS,SYNTH,2024-04-30\n'
        return None
    app.load_bundle_cached.clear()
    monkeypatch.setattr(app,'fetch_file',fetch)
    b,errors=app.load_bundle_cached('fixture-'+str(advertised),'run-1',{'mode':'local','dir':'unused'},man)
    assert len(b.cand)==1
    assert b.base_source['status']==('unavailable' if advertised else 'absent')
    assert ('latest/base_counts.json' in requested)==advertised
    app.load_bundle_cached.clear()


def test_empty_view_filters_are_safe_with_previous_widget_selections():
    assert view.filter_base_counts(pd.DataFrame(),ordinals=['2'],statuses=['BASE_CONFIRMED']).empty

