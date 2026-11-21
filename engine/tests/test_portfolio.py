from datetime import datetime, timezone
import pandas as pd
import pytest
import portfolio_engine as engine
import collector_github_actions as collector

NOW = '2026-11-02T12:00:00+00:00'


@pytest.mark.parametrize('column,value', [('account', None), ('account', '  '), ('code', None)])
def test_missing_identity_is_rejected_instead_of_dropping_holdings(column, value):
    df = base()
    df.loc[0, column] = value
    with pytest.raises(ValueError, match='식별자 누락'):
        snapshot(df)


def ledger(rows):
    return pd.DataFrame([dict(account=a, code=c, currency=cur, date=d, tx_type=side, qty=q,
                              total_amt=q*p, unit_price=p, name=c) for a,c,cur,d,side,q,p in rows])


def base():
    return ledger([('CMA','AMD','USD','2026-10-01','매수',10,100),
                   ('CMA','AMD','USD','2026-10-02','매수',10,120),
                   ('CMA','AMD','USD','2026-10-03','매도',5,130),
                   ('ISA','AMD','USD','2026-10-01','매수',2,90)])


def snapshot(df, **kwargs):
    args = dict(generated_at=NOW, ledger_sha='synthetic',
                quotes={('US','AMD'): dict(price=140,as_of='2026-10-30')},
                diagnostics={('US','AMD'): {'status': 'normal'}},fx=1400,fx_as_of='2026-11-02')
    args.update(kwargs)
    return engine.build_snapshot(df, **args)


def test_moving_average_separates_accounts_and_preserves_input():
    df=base(); before=df.copy(deep=True)
    out=snapshot(df)
    a,b=out['positions']
    assert (a['qty'],a['avg_cost'],a['realized_pnl'],a['unrealized_pnl']) == (15,110,100,450)
    assert (b['qty'],b['avg_cost']) == (2,90)
    pd.testing.assert_frame_equal(df,before)
    assert out['summary']['heat_pct'] is not None
    assert out['summary']['exposure_pct'] is None


def test_fractional_row_is_excluded_whole_and_marks_downstream_shortage():
    df=ledger([('CMA','AMD','USD','2026-10-01','매수',1.37,100),
               ('CMA','AMD','USD','2026-10-02','매도',1,120)])
    out=snapshot(df); p=out['positions'][0]
    assert p['qty']==-1 and p['avg_cost'] is None and p['realized_pnl'] is None
    assert {'fractional_excluded','insufficient_inventory'} <= set(p['quality'])
    assert out['summary']['heat_pct'] is None


def test_flat_same_day_trades_are_closed_and_do_not_require_quotes_or_stops():
    flat = ledger([('CMA', '123456', 'KRW', '2026-10-02', '매수', 2, 100),
                   ('CMA', '123456', 'KRW', '2026-10-02', '매도', 2, 110)])
    out = snapshot(pd.concat([base(), flat], ignore_index=True))
    assert len(out['positions']) == 2
    assert out['closed'][0]['qty'] == 0
    assert out['closed'][0]['realized_pnl'] == 20
    assert out['closed'][0]['quality'] == ['order_ambiguous']
    assert out['summary']['complete'] and out['summary']['heat_pct'] is not None


@pytest.mark.parametrize('rows,expected_flags', [
    ([('CMA', 'AMD', 'USD', '2026-10-01', '매수', 0.5, 100)], {'fractional_excluded'}),
    ([('CMA', 'AMD', 'USD', '2026-10-01', '매도', 1, 100),
      ('CMA', 'AMD', 'USD', '2026-10-02', '매수', 1, 100)], {'insufficient_inventory', 'cost_unknown'})])
def test_zero_calculated_inventory_with_uncertainty_stays_flagged(rows, expected_flags):
    out = snapshot(ledger(rows))
    assert not out['closed'] and out['positions'][0]['qty'] == 0
    assert expected_flags <= set(out['positions'][0]['quality'])
    assert not out['summary']['complete'] and out['summary']['heat_pct'] is None


def test_security_names_reuse_metadata_then_latest_meaningful_ledger_name():
    df = base()
    df.loc[0, 'name'] = 'Example Semiconductor'
    out = snapshot(df)
    assert out['positions'][0]['name'] == 'Example Semiconductor'
    assert out['positions'][1]['name'] == 'AMD'
    enriched = snapshot(df, names={('US', 'AMD'): 'Broker Company Name'})
    assert all(p['name'] == 'Broker Company Name' for p in enriched['positions'])
    kr = ledger([('CMA', '123456', 'KRW', '2026-10-01', '매수', 1, 100)])
    kr.loc[0, 'name'] = '가상기업'
    assert snapshot(kr)['positions'][0]['name'] == '가상기업'
    assert engine.security_name('TEST', None, float('nan'), '', 'None', 'TEST') == 'TEST'


@pytest.mark.parametrize('quotes,fx,fx_date', [({},1400,'2026-11-02'),
    ({('US','AMD'):dict(price=140,as_of='2026-10-20')},1400,'2026-11-02'),
    ({('US','AMD'):dict(price=140,as_of='2026-10-30')},None,None),
    ({('US','AMD'):dict(price=140,as_of='2026-11-05')},1400,'2026-11-02')])
def test_missing_or_stale_inputs_do_not_report_safe_heat(quotes,fx,fx_date):
    out=snapshot(base(),quotes=quotes,fx=fx,fx_as_of=fx_date)
    assert not out['summary']['complete'] and out['summary']['heat_pct'] is None


def test_breached_stop_remains_breach_even_with_zero_remaining_risk():
    out=snapshot(base(),levels={('AMD','USD'):{'stop':150}})
    assert out['positions'][0]['risk']['status']=='breach'
    assert out['positions'][0]['risk']['open_risk']==0


def test_missing_stop_blocks_aggregate_heat():
    out=snapshot(base(),limits={'default_stop_pct':0,'stop_near_pct':3})
    assert out['summary']['heat_pct'] is None
    assert 'stop_missing' in out['positions'][0]['quality']


def test_legacy_api_preserves_cycles_and_daily_aggregation():
    df=base()
    result=collector.compute_positions(df)
    assert 'cycles' in result.attrs
    assert result.loc[2,'sell_pnl']==100
    assert collector.compute_realized(df)[('2026-10-03','AMD','USD')]['pnl']==100


def test_public_repo_refused_before_any_input_fetch():
    class Public:
        private=False
        def get_branch(self,*args):
            pytest.fail('public repo must not be read for portfolio')
    with pytest.raises(ValueError,match='비공개'):
        collector.save_portfolio_snapshot(Public(),datetime.now(timezone.utc))


def test_dry_run_does_not_save_snapshot(monkeypatch):
    monkeypatch.setattr(collector,'DATA_TOKEN','synthetic')
    monkeypatch.setattr(collector,'ensure_limits',lambda repo:None)
    monkeypatch.setattr(collector,'get_data_repo',lambda:object())
    monkeypatch.setattr(collector,'run_manual',lambda args,now:None)
    monkeypatch.setattr(collector,'portfolio_enabled',lambda:True)
    monkeypatch.setattr(collector,'save_portfolio_snapshot',lambda *a,**k:pytest.fail('dry run wrote'))
    monkeypatch.setattr(collector,'RUN_WARNINGS',[])
    assert collector.run(collector.build_parser().parse_args(['--mode','manual']))==0


def test_snapshot_writes_only_private_derivative_and_pins_ledger(monkeypatch):
    from types import SimpleNamespace
    df=base(); text=df.to_csv(index=False)
    reads=[]; writes=[]
    class Private:
        private=True
        default_branch='main'
        def get_branch(self,name):
            return SimpleNamespace(commit=SimpleNamespace(sha='pinned'))
    def read(repo,path,ref=None):
        reads.append((path,ref))
        if path==collector.LEDGER_PATH:return text,'blob'
        return None,None
    hc={'tech':{},'levels':{},'errs':[], 'ledger':df,'rows':[], 'recon':[]}
    monkeypatch.setattr(collector,'read_repo_text',read)
    monkeypatch.setattr(collector,'fetch_yf_history',lambda *a,**k:([],[],[],0))
    monkeypatch.setattr(collector,'HOLDINGS_OK',set())
    monkeypatch.setattr(collector,'upsert_file',lambda repo,path,message,content:writes.append((path,content)))
    collector.save_portfolio_snapshot(Private(),datetime.fromisoformat(NOW),context=hc)
    assert len(writes)==1 and writes[0][0]=='private/portfolio/latest.json'
    import json
    result=json.loads(writes[0][1])
    assert result['ledger_blob_sha']=='blob' and result['reconciliation']['status']=='not_checked'
    assert result['summary']['heat_pct'] is None
    assert len(result['positions'])==2 # absent broker/market data never drops ledger holdings
    assert reads[0]==(collector.LEDGER_PATH,'pinned')


def test_same_day_order_is_explicit_and_invalid_amount_rejected():
    df=base();df.loc[2,'date']='2026-10-02'
    assert 'order_ambiguous' in snapshot(df)['positions'][0]['quality']
    df['total_amt'] = df['total_amt'].astype(object)
    df.loc[0,'total_amt']='broken'
    with pytest.raises(ValueError,match='수치'):
        snapshot(df)


def test_fifo_is_supplementary_and_allocates_recorded_costs():
    df = base()
    df['fee'] = [10, 20, 5, 0]
    out = snapshot(df)
    sale = next(t for t in out['analytics']['trades'] if t['side'] == 'sell')
    assert sale['ma_realized'] == 100
    assert sale['fifo_realized'] == 150
    assert sale['fifo_recorded_costs_net'] == 140  # five buy fees + five sale fees
    assert out['positions'][0]['avg_cost'] == 110  # unchanged moving-average basis


def test_fifo_shortage_and_fractional_impact_never_invent_cost():
    df = ledger([('CMA','AMD','USD','2026-10-01','매수',0.5,100),
                 ('CMA','AMD','USD','2026-10-02','매도',1,120)])
    sale = snapshot(df)['analytics']['trades'][0]
    assert sale['fifo_realized'] is None
    assert {'fractional_excluded', 'cost_unknown'} <= set(sale['quality'])


def test_signals_require_prior_verified_publication_not_just_as_of():
    events = [dict(market='US', ticker='AMD', as_of='2026-09-30', published_at='2026-10-01T21:00:00Z', setup_stage='late'),
              dict(market='US', ticker='AMD', as_of='2026-09-29', published_at='2026-09-30T22:00:00Z', setup_stage='prior'),
              dict(market='US', ticker='AMD', as_of='2026-09-30', setup_stage='undated')]
    row = dict(market='US', code='AMD', date='2026-10-01', time='00:00:00')
    match = engine.match_entry_signal(row, events)
    assert match['setup_stage'] == 'prior'
    assert match['time_basis'] == 'before_trading_date'
    row['time'] = '18:00:00'
    assert engine.match_entry_signal(row, events)['setup_stage'] == 'late'
    assert engine.match_entry_signal(row, [events[-1]])['status'] == 'no_prior_published_signal'


def test_closed_cycle_links_entry_signal_and_reentry_needs_new_setup():
    df = ledger([('CMA','AMD','USD','2026-10-01','매수',2,100),
                 ('CMA','AMD','USD','2026-10-20','매도',2,120)])
    events = [dict(market='US', ticker='AMD', as_of='2026-09-30',
                   published_at='2026-09-30T22:00:00Z', setup_stage='VCP', regime='ATTACK')]
    out = snapshot(df, signal_events=events)
    assert out['analytics']['signal_performance'][0]['ma_pnl'] == 40
    assert out['analytics']['cycles'][0]['entry_signal']['setup_stage'] == 'VCP'
    d = {('US', 'AMD'):dict(as_of='2026-10-30', is_actionable=True, setup_started_at='2026-10-01')}
    regimes = {'SP500': {'regime':'ATTACK', 'as_of':'2026-10-30'}}
    assert engine.reentry_watch(out, d, regimes)[0]['status'] == 'waiting_new_setup'
    d[('US','AMD')]['setup_started_at'] = '2026-10-21'
    assert engine.reentry_watch(out, d, regimes)[0]['status'] == 'new_setup_watch'
    assert engine.reentry_watch(out, d, {'SP500':{'regime':'RISK_OFF','as_of':'2026-10-30'}})[0]['status'] == 'defensive_watch'
    d[('US','AMD')]['as_of'] = '2026-10-10'
    assert engine.reentry_watch(out, d, regimes)[0]['status'] == 'data_unknown'


def test_budget_requires_nav_date_reconciliation_and_classification():
    d = {('US','AMD'): dict(status='normal',sector='Technology',macro_theme='AI')}
    out = snapshot(base(), diagnostics=d, equity_krw=10000000, equity_as_of='2026-11-02',
                   reconciliation={'status':'checked','mismatch_count':0})
    held = engine.held_budget(out)
    assert held['complete']
    assert held['positions'][0]['exposure_pct'] == pytest.approx(29.4)
    out['reconciliation']['mismatch_count'] = 1
    assert 'reconciliation_unconfirmed' in engine.held_budget(out)['reasons']
    out['summary']['equity_as_of'] = '2026-10-01'
    assert 'nav_stale' in engine.held_budget(out)['reasons']


def actions():
    return pd.DataFrame([dict(market='US',sub_market='SP500',ticker='AMD',sector='Tech',macro_theme='AI',
                             position_size_pct=25.,raw_stop_dist_pct=-.05)])


def test_existing_allocator_deducts_held_sector_theme_and_symbol_budgets():
    from watchlist import trend_watchlist as w
    cfg = w.load_config()
    regimes = {'SP500':dict(signal_light='ATTACK')}
    rows = [dict(market='US',code='AMD',sector='Tech',macro_theme='AI',exposure_pct=20.,risk_pct=1.)]
    before = actions().copy(deep=True)
    out, meta = w.allocate_portfolio_risk_budget(actions(),regimes,cfg,held={'complete':True,'positions':rows})
    assert out.iloc[0]['portfolio_alloc_pct'] == 5  # per-symbol cap includes all held accounts
    assert meta['combined_alloc_pct'] == 25
    assert meta['total_alloc_pct'] == 5
    assert meta['combined_risk_pct'] > meta['total_risk_pct']
    pd.testing.assert_frame_equal(actions(), before)
    rows[0]['code'] = 'OTHER'
    rows[0]['exposure_pct'] = 34
    out, _ = w.allocate_portfolio_risk_budget(actions(),regimes,cfg,held={'complete':True,'positions':rows})
    assert out.iloc[0]['portfolio_alloc_pct'] == 0  # 1% sector remainder below minimum allocation


def test_budget_counts_other_markets_even_without_new_candidates():
    from watchlist import trend_watchlist as w
    held = {'complete':True,'positions':[dict(market='KR',code='123456',sector='Other',macro_theme='Other',exposure_pct=98.,risk_pct=4.99)]}
    regs = {'SP500':{'signal_light':'ATTACK'},'KOSPI':{'signal_light':'ATTACK'}}
    out, meta = w.allocate_portfolio_risk_budget(actions(),regs,w.load_config(),held=held)
    assert out.iloc[0]['portfolio_alloc_pct'] == 0
    assert meta['combined_risk_pct'] == 4.99


@pytest.mark.parametrize('held', [dict(complete=False,positions=[]),
    dict(complete=True,positions=[dict(market='US',code='A',sector='T',macro_theme='T',exposure_pct=float('nan'),risk_pct=0)])])
def test_allocation_blocks_incomplete_or_nonfinite_input(held):
    from watchlist import trend_watchlist as w
    out, meta = w.allocate_portfolio_risk_budget(actions(),{'SP500':{'signal_light':'ATTACK'}},w.load_config(),held=held)
    assert meta['status']=='blocked' and out['portfolio_alloc_pct'].sum()==0


def test_signal_publication_timestamp_saved_without_backdating_old_rows(tmp_path):
    from watchlist import trend_watchlist as w
    old = dict(first_seen='2026-09-01',market='US',ticker='OLD')
    (tmp_path/'history').mkdir()
    pd.DataFrame([old]).to_csv(tmp_path/'history/signals.csv',index=False)
    w.update_signal_ledger(tmp_path,actions(),'2026-11-02',published_at=NOW,run_id='synthetic',
                           regimes={'SP500':{'regime':'ATTACK'}})
    saved=pd.read_csv(tmp_path/'history/signals.csv').set_index('ticker')
    assert saved.loc['AMD','published_at']==NOW
    assert saved.loc['AMD','regime']=='ATTACK'
    assert pd.isna(saved.loc['OLD','published_at'])


def test_private_rounding_never_rounds_up_past_remaining_risk():
    from watchlist import trend_watchlist as w
    held={'complete':True,'positions':[dict(market='US',code='OTHER',sector='Other',macro_theme='Other',exposure_pct=10.,risk_pct=4.89)]}
    out,meta=w.allocate_portfolio_risk_budget(actions(),{'SP500':{'signal_light':'ATTACK'}},w.load_config(),held=held)
    assert out.iloc[0]['portfolio_alloc_pct']==2.1
    assert meta['combined_risk_pct']<=5


def test_held_market_limit_breach_blocks_new_other_market_allocations():
    from watchlist import trend_watchlist as w
    held={'complete':True,'positions':[dict(market='KR',code='123456',sector='Other',macro_theme='Other',exposure_pct=30.,risk_pct=1.)]}
    regimes={'KOSPI':{'signal_light':'RISK_OFF'},'SP500':{'signal_light':'ATTACK'}}
    out,meta=w.allocate_portfolio_risk_budget(actions(),regimes,w.load_config(),held=held)
    assert meta['status']=='blocked' and meta['reasons']==['held_limits_exceeded']
    assert out['portfolio_alloc_pct'].sum()==0


def test_trend_state_distinguishes_unknown_early_and_broken():
    out=snapshot(base(),diagnostics={('US','AMD'):dict(advanced_status='computed',exit_signal='✅정상')})
    p=out['positions'][0]
    assert p['trend_state']=='normal_trend'
    p['diagnostic']['exit_signal']='⚠️10일선2일이탈'
    assert engine.trend_state(p)=='early_weakening'
    p['diagnostic']['exit_signal']='🚨50일선이탈'
    assert engine.trend_state(p)=='trend_broken'
    p['quality'].append('quote_stale')
    assert engine.trend_state(p)=='data_unknown'


def test_refresh_mode_dry_run_never_initializes_or_saves_source_files(monkeypatch):
    monkeypatch.setattr(collector,'DATA_TOKEN','synthetic')
    monkeypatch.setattr(collector,'get_data_repo',lambda:object())
    monkeypatch.setattr(collector,'ensure_limits',lambda *a:pytest.fail('dry run wrote limits'))
    monkeypatch.setattr(collector,'load_limits',lambda *a:None)
    monkeypatch.setattr(collector,'RUN_WARNINGS',[])
    calls=[]
    def save(repo,now,*,apply):
        calls.append(apply)
        return {'positions':[], 'allocation':{'status':'blocked'}}
    monkeypatch.setattr(collector,'save_portfolio_snapshot',save)
    assert collector.run(collector.build_parser().parse_args(['--mode','portfolio']))==0
    assert calls==[False]


@pytest.mark.parametrize('mode', ['auto', 'hourly_check'])
def test_disabled_automatic_snapshots_preserve_trade_sync_and_legacy_risk_alerts(monkeypatch, mode):
    events = []
    monkeypatch.setenv('PORTFOLIO_SNAPSHOT_ENABLED', 'false')
    monkeypatch.setattr(collector, 'DATA_TOKEN', 'synthetic')
    monkeypatch.setattr(collector, 'RUN_WARNINGS', [])
    monkeypatch.setattr(collector, 'reset_run_caches', lambda: None)
    monkeypatch.setattr(collector, 'get_data_repo', lambda: object())
    monkeypatch.setattr(collector, 'ensure_limits', lambda *a: None)
    monkeypatch.setattr(collector, 'ensure_market_journal', lambda *a, **k: None)
    monkeypatch.setattr(collector, 'check_and_update_trades', lambda now, market, *a: events.append('sync_' + market))
    monkeypatch.setattr(collector, 'risk_check', lambda *a, **k: events.append('legacy_risk'))
    monkeypatch.setattr(collector, 'save_portfolio_snapshot', lambda *a, **k: pytest.fail('automatic snapshot must stay off'))
    assert collector.run(collector.build_parser().parse_args(['--mode', mode])) == 0
    assert events == ['sync_KR', 'sync_US', 'legacy_risk']


def test_market_reader_rejects_modified_artifact_before_use(monkeypatch):
    import hashlib, json
    expected=json.dumps({'regimes':{'SP500':{'regime':'ATTACK'}}})
    manifest={'schema_version':'1.0','files':{'summary.json':{'sha256':hashlib.sha256(expected.encode()).hexdigest()}}}
    def read(repo,path,ref=None):
        assert ref=='fixed'
        return (json.dumps(manifest),'x') if path.endswith('manifest.json') else ('tampered','x')
    monkeypatch.setattr(collector,'read_repo_text',read)
    with pytest.raises(ValueError,match='무결성'):
        collector.load_portfolio_market(object(),'fixed')


@pytest.mark.parametrize('broker_name,expected_name', [('AMD', 'Example Semiconductor'),
                                                      ('Broker Company Name', 'Broker Company Name')])
def test_full_private_snapshot_uses_held_allocation_and_preserves_source_on_dry_run(monkeypatch, broker_name, expected_name):
    import hashlib,json
    from types import SimpleNamespace
    from watchlist import trend_watchlist as w
    df=base()
    payloads={collector.LEDGER_PATH:df.to_csv(index=False)}
    watch=actions().rename(columns={'raw_stop_dist_pct':'stop_dist_pct'})
    watch['ticker']='NEW'
    candidates=pd.DataFrame([dict(market='US',ticker='AMD',name='Example Semiconductor',as_of='2026-10-30',sector='Tech',macro_theme='AI',rs_score=90)])
    summary={'regimes':{'SP500':dict(regime='ATTACK',signal_light='ATTACK',as_of='2026-10-30')}}
    latest={'summary.json':json.dumps(summary),'candidates.csv':candidates.to_csv(index=False),'watchlist.csv':watch.to_csv(index=False)}
    manifest={'schema_version':'1.0','generated_at':NOW,'run_id':'fixed-run','config_hash':w.get_config_version_hash(w.load_config()),
              'files':{name:{'sha256':hashlib.sha256(raw.encode()).hexdigest()} for name,raw in latest.items()}}
    payloads.update({'watchlist/published/latest/'+name:raw for name,raw in latest.items()})
    payloads['watchlist/published/latest/manifest.json']=json.dumps(manifest)
    def read(repo,path,ref=None):return payloads.get(path),'source-blob'
    class Repo:
        private=True
        default_branch='main'
        def get_branch(self,*args):return SimpleNamespace(commit=SimpleNamespace(sha='fixed'))
    hc={'tech':{('US','AMD'):dict(status='normal',vs50='above',dist=0,rs=[1,2],trail=None,as_of='2026-10-30',close=140)},
        'levels':{('AMD','USD'):{'stop':135}},'errs':[],'ledger':df,'rows':[dict(account='CMA',market='US',currency='USD',code='AMD',qty=15,name=broker_name),
                                              dict(account='ISA',market='US',currency='USD',code='AMD',qty=2,name='AMD')]}
    monkeypatch.setattr(collector,'read_repo_text',read)
    monkeypatch.setattr(collector,'fetch_yf_history',lambda *a,**k:(['2026-11-02'],[1400],[],0))
    # Supply sufficient synthetic OHLCV to exercise the backend feature path.
    bars=[dict(date=d.strftime('%Y%m%d'),open=140,high=141,low=139,close=140,vol=10000)
          for d in pd.bdate_range(end='2026-10-30',periods=260)]
    monkeypatch.setattr(collector,'fetch_yf_candles',lambda *a:bars)
    monkeypatch.setattr(w,'compute_features',lambda *a,**k:dict(as_of='2026-10-30',exit_signal='✅정상',is_stale_price=False,is_breakout=False,base_len_days=30))
    monkeypatch.setattr(collector,'HOLDINGS_OK',{('CMA','US'),('ISA','US')})
    monkeypatch.setattr(collector,'LIMITS',{**collector.LIMITS,'equity_krw':10000000})
    monkeypatch.setenv('PORTFOLIO_EQUITY_AS_OF','2026-11-02')
    monkeypatch.setattr(collector,'upsert_file',lambda *a:pytest.fail('dry run wrote'))
    snapshot=collector.save_portfolio_snapshot(Repo(),datetime.fromisoformat(NOW),context=hc,apply=False)
    assert snapshot['allocation']['status']=='ready', snapshot['allocation'].get('reasons')
    assert all(p['name'] == expected_name for p in snapshot['positions'])
    assert snapshot['allocation']['held_alloc_pct']==pytest.approx(33.32)
    assert snapshot['allocation']['rows'][0]['ticker']=='NEW'
    assert snapshot['allocation']['rows'][0]['portfolio_alloc_pct']<=1.6 # existing sector exposure is 33.32%, cap 35%
    assert snapshot['reconciliation']['status']=='checked' and snapshot['reconciliation']['mismatch_count']==0
    assert snapshot['market_source']['commit']=='fixed'
    assert snapshot['analytics']['trades']


@pytest.mark.parametrize('status,conclusion,expect', [('completed','success',True),('in_progress',None,False),('completed','failure',False)])
def test_workflow_completion_is_conservative_publication_evidence(monkeypatch,status,conclusion,expect):
    import hashlib,json
    from types import SimpleNamespace
    files={'summary.json':'{}','candidates.csv':'market,ticker\n','watchlist.csv':'market,ticker\n'}
    manifest={'schema_version':'1.0','files':{n:{'sha256':hashlib.sha256(raw.encode()).hexdigest()} for n,raw in files.items()}}
    event=dict(market='US',ticker='AMD',as_of='2026-10-01',generated_at='2026-10-01T10:00:00Z',workflow_run_id='123')
    def read(repo,path,ref=None):
        if path.endswith('manifest.json'):return json.dumps(manifest),'x'
        if path.endswith('signals.csv'):return pd.DataFrame([event]).to_csv(index=False),'x'
        return files.get(path.rsplit('/',1)[-1]),'x'
    class Repo:
        def get_workflow_run(self,run_id):
            assert run_id==123
            return SimpleNamespace(status=status,conclusion=conclusion,path='.github/workflows/watchlist.yml',
                                   created_at='2026-10-01T09:00:00Z',updated_at='2026-10-01T10:30:00Z')
    monkeypatch.setattr(collector,'read_repo_text',read)
    result=collector.load_portfolio_market(Repo(),'fixed')['events'][0]
    assert bool(result.get('published_at')) is expect
    if expect:assert result['published_at']=='2026-10-01T10:30:00+00:00'


def test_generation_timestamp_alone_never_establishes_publication(tmp_path,monkeypatch):
    from watchlist import trend_watchlist as w
    monkeypatch.setenv('GITHUB_RUN_ID','123')
    w.update_signal_ledger(tmp_path,actions(),'2026-11-02',generated_at=NOW,run_id='engine-run')
    row=pd.read_csv(tmp_path/'history/signals.csv').iloc[0]
    assert row['generated_at']==NOW and pd.isna(row['published_at'])
    assert row['workflow_run_id']==123


def test_full_liquidation_fifo_and_ma_gross_pnl_reconcile():
    df=base().iloc[:3].copy()
    df['fee']=[10,20,5]
    df=pd.concat([df,ledger([('CMA','AMD','USD','2026-10-20','매도',15,140)]).assign(fee=5)],ignore_index=True)
    out=snapshot(df)
    cycle=out['analytics']['cycles'][0]
    assert cycle['ma_pnl']==cycle['fifo_pnl']==550
    assert sum(t['fifo_recorded_costs_net'] for t in out['analytics']['trades'] if t['side']=='sell')==510


def test_undated_nav_does_not_become_account_exposure():
    out=snapshot(base(),equity_krw=10000000)
    assert out['summary']['equity_basis']=='stocks_only'
    assert out['summary']['exposure_pct'] is None
    assert out['summary']['nav_status']=='unconfirmed'

