import json
from pathlib import Path
import pytest
import portfolio_view as view
from streamlit.testing.v1 import AppTest


class User(dict):
    is_logged_in=True


def test_oidc_backend_can_create_client_without_network():
    # st.login() only validates settings; the redirect backend needs HTTPX too.
    from authlib.integrations.starlette_client import OAuth
    oauth = OAuth()
    oauth.register('google', client_id='synthetic-client', client_secret='synthetic-secret',
                   server_metadata_url='https://accounts.google.com/.well-known/openid-configuration')
    assert oauth.create_client('google').name == 'google'


@pytest.mark.parametrize('user,expected', [(User(email='a@example.com',email_verified=True),True),
    (User(email='other@example.com',email_verified=True),False),
    (User(email='a@example.com',email_verified=False),False),
    (User(email='a@example.com'),False)])
def test_verified_allowlist(user,expected):
    assert view.allowed_user(user,{'a@example.com'}) is expected


def test_public_repository_never_reads_portfolio(monkeypatch):
    monkeypatch.delenv('PORTFOLIO_FILE',raising=False)
    monkeypatch.setenv('PORTFOLIO_REPO','example/private-data')
    monkeypatch.setenv('PORTFOLIO_TOKEN','synthetic')
    calls=[]
    class Response:
        def raise_for_status(self): pass
        def json(self): return {'private':False}
    monkeypatch.setattr(view.requests,'get',lambda url,**kwargs:(calls.append(url) or Response()))
    with pytest.raises(ValueError,match='Public'):
        view.read_snapshot()
    assert len(calls)==1


def test_unauthorized_screen_does_not_read_data(monkeypatch):
    monkeypatch.setenv('PORTFOLIO_ALLOWED_EMAILS','a@example.com')
    monkeypatch.setattr(view,'read_snapshot',lambda:pytest.fail('unauthorized I/O'))
    at=AppTest.from_string('import portfolio_view; portfolio_view.render()').run()
    assert not at.exception
    assert len(at.info)==1


def test_authorized_private_screen_and_simulation(monkeypatch):
    from datetime import datetime, timezone
    import streamlit as st
    monkeypatch.setenv('PORTFOLIO_ALLOWED_EMAILS','a@example.com')
    monkeypatch.setattr(st,'user',User(email='a@example.com',email_verified=True))
    sample={'schema_version':'1.0','generated_at':datetime.now(timezone.utc).isoformat(),
      'reconciliation':{'status':'not_checked'},
      'summary':{'complete':True,'equity_basis':'stocks_only','value_krw':100000,
                 'heat_krw':5000,'heat_pct':5},
      'positions':[{'account':'TEST','market':'KR','code':'123456','name':'가상기업','currency':'KRW',
        'qty':2,'avg_cost':90,'value':200,'price':100,'unrealized_pnl':20,'realized_pnl':0,
        'quote_as_of':'2026-11-02','quality':[],
        'risk':{'stop':95,'status':'ok','open_risk':10}}],'closed':[]}
    sample['positions'].append(dict(account='TEST',market='KR',code='654321',name='청산기업',
                                    currency='KRW',qty=0,realized_pnl=10,quality=['order_ambiguous']))
    monkeypatch.setattr(view,'read_snapshot',lambda:sample)
    at=AppTest.from_string('import portfolio_view; portfolio_view.render()').run()
    assert not at.exception and len(at.dataframe)>=2
    held_table = next(table.value for table in at.dataframe if '추세 상태' in table.value and '계좌' not in table.value)
    assert held_table['종목'].tolist() == ['가상기업 (123456)']
    assert at.selectbox(key='portfolio_sim_symbol').options == ['TEST / 가상기업 (123456)']
    assert any(m.label=='Heat' for m in at.metric)
    at.slider(key='portfolio_sim_gap').set_value(10).run()
    assert not at.exception
    assert any(m.value=='29.00' for m in at.metric)


def test_stale_private_allocations_never_show_actionable_amounts():
    data={'allocation':dict(status='ready',held_alloc_pct=25,total_alloc_pct=15,combined_risk_pct=2),
          'analytics':{'trades':[], 'cycles':[], 'signal_performance':[]}, 'reentry':[]}
    at=AppTest.from_string('import portfolio_view; portfolio_view.render_analysis('+repr(data)+', stale=True)').run()
    assert not at.exception
    assert len(at.warning)==1 and not at.metric


def test_private_analysis_displays_fifo_signal_performance_and_reentry():
    data={'allocation':dict(status='ready',held_alloc_pct=25,total_alloc_pct=15,combined_risk_pct=2,rows=[]),
          'analytics':{'trades':[dict(source_row=1,account='TEST',code='AAA',quality=[],entry_signal={'status':'matched'})],
                       'cycles':[dict(code='AAA',ma_pnl=10)],
                       'signal_performance':[dict(currency='USD',setup='VCP',regime='ATTACK',count=2,wins=1,ma_pnl=10)]},
          'reentry':[dict(code='AAA',status='waiting_new_setup')]}
    at=AppTest.from_string('import portfolio_view; portfolio_view.render_analysis('+repr(data)+')').run()
    assert not at.exception and len(at.dataframe)>=4
    assert any(m.label=='추가 배분' and m.value=='15.0%' for m in at.metric)


def test_preflight_reports_presence_without_exposing_values(monkeypatch):
    monkeypatch.setenv('PORTFOLIO_TOKEN','never-print-this-value')
    status=view.configuration_status()
    assert status['variables_present']['PORTFOLIO_TOKEN'] is True
    assert 'never-print-this-value' not in json.dumps(status)
    assert status['live_login_verified'] is False


@pytest.fixture
def cloud_secrets(monkeypatch):
    import streamlit as st
    for name in ('DATA_DIR','DATA_REPO','DATA_ROOT','DATA_BRANCH','GITHUB_TOKEN',
                 'PORTFOLIO_ALLOWED_EMAILS','PORTFOLIO_REPO','PORTFOLIO_TOKEN',
                 'PORTFOLIO_FILE','PORTFOLIO_BRANCH'):
        monkeypatch.delenv(name,raising=False)
    secrets={}
    monkeypatch.setattr(st,'secrets',secrets)
    return secrets


def test_cloud_array_allowlist_does_not_require_environment_promotion(cloud_secrets):
    import os
    cloud_secrets.update(PORTFOLIO_ALLOWED_EMAILS=[' A@Example.com '],PORTFOLIO_REPO='test/private')
    assert 'PORTFOLIO_ALLOWED_EMAILS' not in os.environ
    assert view.enabled() and view.allowed_emails()=={'a@example.com'}
    assert view.allowed_user(User(email='a@example.com',email_verified=True),view.allowed_emails())


def test_environment_overrides_secrets_and_missing_values_fall_back(cloud_secrets,monkeypatch):
    cloud_secrets['PORTFOLIO_REPO']='test/cloud'
    assert view.setting('PORTFOLIO_REPO')=='test/cloud'
    monkeypatch.setenv('PORTFOLIO_REPO','test/local')
    assert view.setting('PORTFOLIO_REPO')=='test/local'
    monkeypatch.setenv('PORTFOLIO_REPO',' ')
    assert view.setting('PORTFOLIO_REPO')=='test/cloud'


def test_partial_secrets_keep_menu_visible_and_block_data_reads(cloud_secrets,monkeypatch):
    cloud_secrets['PORTFOLIO_REPO']='test/private'
    monkeypatch.setattr(view,'read_snapshot',lambda:pytest.fail('missing allowlist read private data'))
    assert view.enabled()
    at=AppTest.from_string('import portfolio_view; portfolio_view.render()').run()
    assert not at.exception and len(at.warning)==1


def test_auth_table_alone_exposes_root_configuration_warning(cloud_secrets):
    cloud_secrets['auth']={'client_id':'test-client', 'PORTFOLIO_ALLOWED_EMAILS':['a@example.com']}
    assert view.enabled()
    assert not view.allowed_emails()  # app settings inside [auth] are not accepted


def test_cloud_private_reader_uses_explicit_branch_and_secrets_token(cloud_secrets,monkeypatch):
    cloud_secrets.update(PORTFOLIO_REPO='test/private',PORTFOLIO_TOKEN='synthetic-token',PORTFOLIO_BRANCH='feature/test')
    calls=[]
    class Response:
        def __init__(self,payload):self.payload=payload
        def raise_for_status(self):pass
        def json(self):return self.payload
    def get(url,**kwargs):
        calls.append((url,kwargs))
        return Response({'schema_version':'1.0','positions':[]}) if '/contents/' in url else Response({'private':True})
    monkeypatch.setattr(view.requests,'get',get)
    assert view.read_snapshot()['positions']==[]
    assert calls[1][1]['params']=={'ref':'feature/test'}
    assert calls[1][1]['headers']['Authorization']=='Bearer synthetic-token'


def test_real_toml_secrets_reader_handles_non_promoted_email_array(tmp_path,monkeypatch):
    import os
    import streamlit as st
    from streamlit.runtime.secrets import Secrets
    import streamlit.runtime.secrets as secrets_module
    for key in ('PORTFOLIO_ALLOWED_EMAILS','PORTFOLIO_REPO'):
        monkeypatch.delenv(key,raising=False)
    path=tmp_path/'test.toml'
    path.write_text('PORTFOLIO_ALLOWED_EMAILS = ["a@example.com"]\nPORTFOLIO_REPO = "test/private"\n')
    original=secrets_module.config.get_option
    monkeypatch.setattr(secrets_module.config,'get_option',lambda key: [str(path)] if key=='secrets.files' else original(key))
    store=Secrets()
    monkeypatch.setattr(Secrets,'_maybe_install_file_watchers',lambda self:None)
    monkeypatch.setattr(st,'secrets',store)
    try:
        assert view.enabled() and view.allowed_emails()=={'a@example.com'}
        assert 'PORTFOLIO_ALLOWED_EMAILS' not in os.environ
        assert os.environ['PORTFOLIO_REPO']=='test/private'
    finally:
        store._reset()


@pytest.mark.parametrize('previous_page', [None, '💼 내 포트폴리오'])
def test_private_menu_stays_hidden_with_old_secrets_and_sessions(cloud_secrets,monkeypatch,previous_page):
    cloud_secrets.update(PORTFOLIO_ALLOWED_EMAILS=['a@example.com'], PORTFOLIO_REPO='test/private')
    cloud_secrets['auth']={'client_id':'synthetic-client'}
    monkeypatch.setattr(view,'render',lambda:pytest.fail('hidden private page rendered'))
    monkeypatch.setattr(view,'read_snapshot',lambda:pytest.fail('hidden private page loaded account data'))
    monkeypatch.setattr(view.requests,'get',lambda *a,**k:pytest.fail('unconfigured source request'))
    at=AppTest.from_file(str(Path(__file__).resolve().parents[1]/'app.py'),default_timeout=30)
    if previous_page:
        at.session_state['main_page']=previous_page
    at.run()
    assert not at.exception
    assert at.radio(key='main_page').options == ['🧭 1. 국면·나침반', '🏭 2. 주도 섹터',
        '🎯 3. 전술·멀티차트', '🧪 4. 실전 검증', 'ℹ️ 5. 데이터', '📌 6. 관심종목·매크로']
    assert at.radio(key='main_page').value == '🧭 1. 국면·나침반'


def test_preflight_reads_nested_auth_without_printing_secrets(cloud_secrets):
    cloud_secrets.update(PORTFOLIO_ALLOWED_EMAILS=['a@example.com'],PORTFOLIO_REPO='test/private',
                         PORTFOLIO_TOKEN='do-not-disclose')
    cloud_secrets['auth']={key:'synthetic-private-value' for key in ('redirect_uri','cookie_secret','client_id','client_secret','server_metadata_url')}
    status=view.configuration_status()
    assert status['source_configured'] and status['allowlist_configured']
    assert all(status['oidc_fields_present'].values())
    assert 'do-not-disclose' not in json.dumps(status) and 'synthetic-private-value' not in json.dumps(status)


def test_partition_legacy_positions_keeps_closed_and_inventory_warnings_separate():
    from copy import deepcopy
    held = {'code': 'TEST', 'qty': 2, 'quality': []}
    flat = {'code': 'CLOSED', 'qty': 0, 'quality': ['order_ambiguous', 'quote_missing', 'technical_missing', 'stop_missing']}
    fractional = {'code': 'FRACTIONAL', 'qty': 0, 'quality': ['fractional_excluded']}
    negative = {'code': 'SHORTAGE', 'qty': -1, 'quality': ['negative_inventory']}
    data = {'positions': [held, flat, fractional, negative], 'closed': []}
    before = deepcopy(data)
    assert view.partition_positions(data) == ([held], [flat], [fractional, negative])
    assert data == before


@pytest.mark.parametrize('position,label', [
    ({'code': '123456', 'name': '가상기업'}, '가상기업 (123456)'),
    ({'code': 'TEST', 'name': 'Example Company'}, 'Example Company (TEST)'),
    ({'code': 'TEST'}, 'TEST'),
    ({'code': 'TEST', 'name': None}, 'TEST'),
    ({'code': 'TEST', 'name': 'TEST'}, 'TEST')])
def test_security_label_preserves_codes_and_supports_older_snapshots(position, label):
    assert view.security_label(position) == label


def test_closed_only_snapshot_renders_history_and_inventory_warning(monkeypatch):
    from datetime import datetime, timezone
    import streamlit as st
    monkeypatch.setenv('PORTFOLIO_ALLOWED_EMAILS', 'a@example.com')
    monkeypatch.setattr(st, 'user', User(email='a@example.com', email_verified=True))
    sample = {'generated_at': datetime.now(timezone.utc).isoformat(),
              'summary': {'complete': False, 'equity_basis': 'stocks_only',
                          'value_krw': None, 'heat_krw': None, 'heat_pct': None},
              'positions': [dict(account='TEST', market='US', code='FLAT', name='Flat Company',
                                 qty=0, currency='USD', realized_pnl=10, quality=['order_ambiguous']),
                            dict(account='TEST', market='US', code='FRACTIONAL', qty=0,
                                 currency='USD', realized_pnl=0, quality=['fractional_excluded'])]}
    monkeypatch.setattr(view, 'read_snapshot', lambda: sample)
    at = AppTest.from_string('import portfolio_view; portfolio_view.render()').run()
    assert not at.exception
    assert any('보유 포지션이 없습니다' in item.value for item in at.info)
    tables = [table.value for table in at.dataframe]
    assert any('Flat Company (FLAT)' in table.get('종목', []).values for table in tables if '종목' in table)
    assert any('fractional_excluded' in str(table.values) for table in tables)

