from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from manual_eod_contract import validate_snapshot
from manual_watchlist_view import eod_reason
from manual_eod_fixture import snapshot_for
from test_manual_catalog import row
from manual_catalog import csv_bytes


@pytest.mark.parametrize('reason',[
    'PROVIDER_RATE_LIMITED','PROVIDER_RATE_LIMIT_CIRCUIT_OPEN','PROVIDER_ACCESS_DENIED',
    'PROVIDER_ACCESS_CIRCUIT_OPEN','PROVIDER_CONNECTION_FAILED','PROVIDER_CONNECTION_CIRCUIT_OPEN',
    'CACHE_MISSING','CACHE_IDENTITY_UNVERIFIED','CACHE_ADJUSTMENT_MISMATCH','CACHE_BEHIND_FINAL_SESSION'])
def test_failure_reason_does_not_claim_retained_prices(reason):
    label=eod_reason(reason)
    assert label and '이전' not in label and '확정 가격 확인 실패' not in label


def test_direct_limit_and_circuit_skip_have_different_labels():
    assert '직접 조회 실패' in eod_reason('PROVIDER_RATE_LIMITED')
    assert '미시도' in eod_reason('PROVIDER_RATE_LIMIT_CIRCUIT_OPEN')


def test_older_provider_prices_explain_why_previous_publication_is_retained():
    assert eod_reason('PROVIDER_BEHIND_PREVIOUS_SNAPSHOT')=='공급자 가격이 기존 게시본보다 오래됨 · 이전 자료 유지'


def test_missing_prices_show_cache_diagnosis_and_never_claim_retention(tmp_path):
    item=row();doc,_=snapshot_for([item]);entry=doc['instruments'][item['symbol']]
    entry.update(bars={},status='missing',last_final_session=None,reason='PROVIDER_RATE_LIMIT_CIRCUIT_OPEN',
        cache_diagnostic={'reason':'CACHE_IDENTITY_UNVERIFIED'},listing_identity={'untrusted':'unused by UI'})
    validate_snapshot(doc)
    path=tmp_path/'catalog.csv';path.write_bytes(csv_bytes([item]))
    at=AppTest.from_string('from manual_watchlist_view import render_manual_watchlist\nrender_manual_watchlist('+repr(str(path))+',snapshot='+repr(doc)+',source_state={"status":"ready"})').run()
    assert not at.exception and not at.get('plotly_chart')
    table=next(t.value for t in at.dataframe if '캐시진단' in t.value.columns)
    assert '미시도' in table.iloc[0]['사유'] and '검증 대기' in table.iloc[0]['캐시진단']
    assert '이전 자료 유지' not in table.to_string()


def test_v1_contract_accepts_optional_diagnostics_without_reinterpreting_prices():
    doc,_=snapshot_for([row()]);before=deepcopy(doc)
    doc['collection_metrics']={'http_requests':0}
    doc['instruments'][row()['symbol']]['cache_diagnostic']={'classification':'reusable'}
    validate_snapshot(doc)
    assert doc['instruments'][row()['symbol']]['bars']==before['instruments'][row()['symbol']]['bars']


@pytest.mark.parametrize('reason,fragment',[
    ('MACRO_INDEX_DEFINITION_SESSION_UNVERIFIED','지수 정의'),
    ('MACRO_YIELD_DEFINITION_UNIT_UNVERIFIED','금리 정의'),
    ('MACRO_FX_QUOTE_SESSION_UNVERIFIED','환율 표시 방향'),
    ('MACRO_FUTURES_CONTRACT_ROLL_UNVERIFIED','만기 교체'),
    ('MACRO_SPOT_SOURCE_UNVERIFIED','현물 원본'),
    ('MACRO_CRYPTO_VENUE_UNVERIFIED','지정 거래소'),
])
def test_macro_review_explains_validation_gap_without_claiming_prices(reason,fragment):
    label=eod_reason(reason)
    assert fragment in label and '검증 대기' in label and '이전 자료 유지' not in label


def test_macro_candidate_metadata_is_not_rendered_as_a_price_chart(tmp_path,monkeypatch):
    import requests
    monkeypatch.setattr(requests.sessions.Session,'request',lambda *a,**k:pytest.fail('macro UI fetched prices'))
    item=row(symbol='TVC:US10Y',asset_type='yield',unit='%',chart_style='line')
    doc,_=snapshot_for([item]);entry=doc['instruments'][item['symbol']]
    entry.update(reason='MACRO_YIELD_DEFINITION_UNIT_UNVERIFIED',source_review={
        'collection_enabled':False,'candidates':[{'provider':'Yahoo Finance','symbol':'^TNX'}]})
    validate_snapshot(doc)
    path=tmp_path/'catalog.csv';path.write_bytes(csv_bytes([item]))
    at=AppTest.from_string('from manual_watchlist_view import render_manual_watchlist\nrender_manual_watchlist('+repr(str(path))+',snapshot='+repr(doc)+',source_state={"status":"ready"})').run()
    at.selectbox(key='manual_scope').set_value('매크로').run()
    assert not at.exception and not at.get('plotly_chart')
    assert '금리 정의' in at.dataframe[0].value.iloc[0]['사유']

