import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from watchlist import analysis_report as research
from watchlist import trend_watchlist as engine


def manifest():
    return dict(run_id="test-current", d_str="2026-11-03", generated_at="2026-11-03T19:00:00+09:00",
                as_of={key: "2026-11-03" for key in ("KOSPI", "KOSDAQ", "SP500", "NASDAQ100")})


def frames():
    signals = pd.DataFrame([
        dict(first_seen="2026-10-01", market="US", ticker=ticker, regime="CAUTION",
             generated_at="2026-10-01T21:00:00+09:00", published_at="2026-10-01T21:05:00+09:00",
             rs_blue_dot=flag)
        for ticker, flag in (("EXA", True), ("EXB", False), ("EXC", None))])
    outcomes = pd.DataFrame([
        dict(first_seen="2026-10-01", as_of="2026-09-30", market="US", track="SP500",
             ticker=ticker, sector="Example", setup_stage="VCP", entry_date="2026-10-02",
             entry_price=100., stop_price=90., bars=bars, ret_5d=ret5, ret_10d=ret10,
             ret_20d=ret20, ex_5d=1., ex_10d=ex10, stop_aware_r_10d=stop_r,
             stop_aware_ret_10d=stop_ret, stop_hit_10d=hit, mfe_10d_pct=mfe,
             mae_10d_pct=mae, mae_pct=-99., r_10d=99.)
        for ticker,bars,ret5,ret10,ret20,ex10,stop_r,stop_ret,hit,mfe,mae in (
            ("EXA",20,2.,10.,12.,5.,1.,10.,False,12.,-2.),
            ("EXB",10,-1.,-5.,None,None,-1.,-10.,True,2.,-12.),
            ("EXC",3,None,None,None,None,-1.,-10.,True,1.,-10.))])
    outcomes["calculation_version"] = research.CALCULATION_VERSION
    outcomes["entry_price_source"] = "open"
    outcomes["stop_price_basis"] = "matched"
    outcomes["path_ohlc_complete_10d"] = True
    outcomes["entry_session_verified"] = True
    for n in (5,10,20):outcomes[f"calendar_complete_{n}d"]=True
    return signals, outcomes


def market10(result):
    return next(row for row in result["groups"] if row["dimension"]=="market" and row["horizon"]==10 and row["window"]=="all")


def test_maturity_denominators_stop_aware_r_and_exact_horizon_mae():
    s,o = frames(); before_s, before_o = s.copy(deep=True), o.copy(deep=True)
    result=research.validation_statistics(s,o,manifest()); row=market10(result)
    assert row["signals"]==3 and row["completed"]==2 and row["pending_or_excluded"]==1
    assert row["win_rate_pct"]==50 and row["mean_return_pct"]==2.5
    assert row["excess_n"]==1 and row["mean_excess_pp"]==5.
    assert row["stop_r_n"]==2 and row["mean_stop_r"]==0.
    assert row["paired_hold_n"]==2 and row["mean_paired_hold_return_pct"]==2.5 and row["mean_stop_minus_hold_pp"]==-2.5
    assert row["mean_mae_pct"]==-7. and row["mean_mfe_pct"]==7.
    assert row["stop_touch_pct"]==50. and row["evidence"]=="insufficient_sample"
    assert row["publication_checked_n"]==2
    twenty=next(r for r in result["groups"] if r["dimension"]=="market" and r["horizon"]==20 and r["window"]=="all")
    assert twenty["completed"]==1
    pd.testing.assert_frame_equal(s,before_s);pd.testing.assert_frame_equal(o,before_o)


def test_signal_tags_are_multi_label_known_values_only_and_do_not_invent_legacy_tags():
    s,o=frames();result=research.validation_statistics(s,o,manifest())
    rows=[r for r in result["groups"] if r["dimension"]=="signal" and r["window"]=="all" and r["horizon"]==10]
    assert {r["signal_name"] for r in rows}=={"rs_blue_dot"}
    assert {r["signal_value"] for r in rows}=={True,False}
    assert sum(r["signals"] for r in rows)==2  # Third record is unknown, not a negative control.
    assert all(r["entry_regime"]=="CAUTION" for r in result["groups"] if r["dimension"]=="regime")


@pytest.mark.parametrize("fault",["generation_only","lookahead","duplicate","missing_stop","unknown_stop","unmatched","future_entry","invalid_entry"])
def test_uncertain_data_is_not_promoted_to_valid_live_or_complete_evidence(fault):
    s,o=frames()
    if fault=="generation_only":s["published_at"]=None
    elif fault=="lookahead":s["generated_at"]="2026-10-02T23:00:00+09:00"
    elif fault=="duplicate":o=pd.concat([o,o.iloc[:1]])
    elif fault=="missing_stop":o["stop_price"]=None
    elif fault=="unknown_stop":o["stop_hit_10d"]="unknown"
    elif fault=="unmatched":s=s.iloc[1:]
    elif fault=="future_entry":o["entry_date"]="2026-12-01"
    elif fault=="invalid_entry":o["entry_date"]="NaT"
    result=research.validation_statistics(s,o,manifest());row=market10(result)
    if fault=="generation_only":assert row["publication_checked_n"]==0
    elif fault in ("lookahead","future_entry","invalid_entry"):assert row["completed"]==0 and row["mean_return_pct"] is None
    elif fault=="duplicate":assert row["completed"]==1 and result["quality"]["duplicate_outcomes"]==2
    elif fault=="missing_stop":assert row["stop_r_n"]==0 and row["mean_stop_r"] is None
    elif fault=="unknown_stop":assert row["stop_touch_n"]==0 and row["stop_touch_pct"] is None
    elif fault=="unmatched":assert row["completed"]==1 and result["quality"]["unmatched_signals"]==1


def test_horizon_values_cannot_override_immature_bars_and_long_mae_is_not_a_fallback():
    s,o=frames();o.loc[2,"ret_10d"]=50.;o.loc[2,"bars"]=3;o["mae_10d_pct"]=None
    row=market10(research.validation_statistics(s,o,manifest()))
    assert row["completed"]==2 and row["mean_mae_pct"] is None


def test_minimum_sample_is_descriptive_not_a_rule_change_and_windows_anchor_to_snapshot():
    s,o=frames();s=pd.concat([s.iloc[:1].assign(ticker=f"EX{i}") for i in range(20)])
    o=pd.concat([o.iloc[:1].assign(ticker=f"EX{i}") for i in range(20)])
    result=research.validation_statistics(s,o,manifest());row=market10(result)
    assert row["completed"]==20 and row["evidence"]=="simulation_only"
    assert result["policy"]["optimization_applied"] is False
    assert not [r for r in result["groups"] if r["window"]=="recent30"]  # Oct 1 precedes Nov 3 - 30 days.


def write_market(base, run="test-current", power=70., regime="CAUTION"):
    latest=base/"latest";latest.mkdir(parents=True,exist_ok=True)
    (latest/"summary.json").write_text(json.dumps({"regimes":{"SP500":{"regime":regime,"as_of":"2026-11-03"}}}))
    pd.DataFrame([dict(market="US",level="sector",macro_theme="Theme",sector="Example",sector_power=power,median_rs=80.,passed_cnt=2,univ_total=10)]).to_csv(latest/"sectors.csv",index=False)
    for name in ("watchlist.csv","candidates.csv"):pd.DataFrame([dict(market="US",ticker="EX")]).to_csv(latest/name,index=False)
    m=manifest();m["run_id"]=run;m["files"]={name:{"sha256":hashlib.sha256((latest/name).read_bytes()).hexdigest()} for name in research.MARKET_FILES}
    (latest/"manifest.json").write_text(json.dumps(m))
    history=base/"history";history.mkdir(exist_ok=True)
    for name,frame in zip(("signals.csv","signal_outcomes.csv"),frames()):frame.to_csv(history/name,index=False)
    return m


def read_report(path):
    return json.loads(path.read_text().split("````json\n",1)[1].rsplit("\n````",1)[0])


def test_atomic_supplement_preserves_contract_records_exact_baseline_and_excludes_private_fields(tmp_path):
    write_market(tmp_path,run="old",power=65.,regime="ATTACK");previous=research.read_previous_market(tmp_path)
    m=write_market(tmp_path);before={p.name:p.read_bytes() for p in (tmp_path/"latest").iterdir()}
    for name in ("signals.csv","signal_outcomes.csv"):
        p=tmp_path/"history"/name;df=pd.read_csv(p);df["account"]="PRIVATE_SENTINEL";df.to_csv(p,index=False)
    path=research.publish_analysis_report(tmp_path,previous);r=read_report(path)
    assert r["source"]["run_id"]==m["run_id"]
    assert r["market_changes"]["baseline"]["run_id"]=="old"
    assert r["market_changes"]["sectors"][0]["sector_power_change"]==5.
    assert r["market_changes"]["regimes"][0]["previous_regime"]=="ATTACK"
    assert "PRIVATE_SENTINEL" not in path.read_text()
    assert before=={p.name:p.read_bytes() for p in (tmp_path/"latest").iterdir()}
    assert not path.with_suffix(".tmp").exists()
    (tmp_path/"latest"/"sectors.csv").write_text("corrupt")
    old=path.read_bytes()
    with pytest.raises(ValueError,match="hash mismatch"):research.publish_analysis_report(tmp_path)
    assert path.read_bytes()==old


def test_history_failure_withholds_statistics_instead_of_republishing_them_as_current(tmp_path):
    write_market(tmp_path)
    r=read_report(research.publish_analysis_report(tmp_path,history_update_ok=False))
    assert r["source"]["history_update_ok"] is False
    assert r["validation"]["status"]=="history_update_incomplete" and r["validation"]["groups"]==[]


def test_macro_power_comparison_uses_the_existing_macro_value_instead_of_nan():
    before=pd.DataFrame([dict(market="KR",level="macro",macro_theme="Theme",sector="",macro_power=60.,sector_power=float("nan"))])
    now=before.copy();now["macro_power"]=65.
    result=research.market_changes(manifest(),{},now,{"source":{"run_id":"old"},"summary":{},"sectors":before})
    assert result["sectors"][0]["sector_power_change"]==5.


def test_new_signal_flags_do_not_backfill_old_events_or_change_cooldown(tmp_path):
    h=tmp_path/"history";h.mkdir()
    pd.DataFrame([dict(first_seen="2026-10-30",as_of="2026-10-29",market="US",ticker="OLD")]).to_csv(h/"signals.csv",index=False)
    action=pd.DataFrame([dict(market="US",sub_market="SP500",ticker=ticker,last_close=100.,
                             rs_blue_dot=True,is_true_vcp=False) for ticker in ("OLD","NEW")])
    n=engine.update_signal_ledger(tmp_path,action,"2026-11-03",generated_at="2026-11-03T19:00:00+09:00")
    assert n==1
    saved=pd.read_csv(h/"signals.csv").set_index("ticker")
    assert pd.isna(saved.loc["OLD","rs_blue_dot"])
    assert bool(saved.loc["NEW","rs_blue_dot"]) and not bool(saved.loc["NEW","is_true_vcp"])
    assert saved.loc["OLD","first_seen"]=="2026-10-30"


def test_supplement_failure_never_prevents_the_existing_market_publication(tmp_path, monkeypatch):
    cfg=copy.deepcopy(engine.DEFAULT_CONFIG)
    # Use the real cache-only builder so missing-price coverage stays consistent.
    monkeypatch.setattr(engine,"US_HISTORY_CACHE_DIR",tmp_path / "empty-prices")
    monkeypatch.setattr(engine,"update_signal_outcomes",lambda *a,**k:0)
    def fail(*a,**k):raise RuntimeError("synthetic supplemental failure")
    monkeypatch.setattr(engine,"publish_analysis_report",fail)
    row=dict(market="US",sub_market="SP500",ticker="EX",name="Example",as_of="2026-11-03",sector="Example",macro_theme="Theme",
             tier="A",setup_stage="VCP",is_actionable=True,in_buy_zone=True,is_breakout=False,is_true_vcp=True,
             strict_minervini=True,is_leading_sector=True,rs_score=90.,topdown_score=85.,sector_power=70.,
             last_close=100.,pivot_price=99.,dist_to_pivot_pct=.01,stop_price=94.,stop_dist_pct=.06,
             position_size_pct=15.,exit_signal="✅정상")
    track={"SP500":pd.DataFrame([row])}
    regimes={"SP500":{"regime":"CAUTION","signal_light":"🟡 CAUTION","as_of":"2026-11-03"}}
    out=engine.publish_artifacts("2026-11-03",track,track,regimes,{},pd.DataFrame(),pd.DataFrame(),cfg,base_dir=tmp_path)
    assert out is not None
    assert engine.validate_published_dir(out)==[]
    assert (out/"manifest.json").exists()


def workflow_fact(run_id='123'):
    return dict(id=int(run_id), status='completed', conclusion='success', head_branch='main',
                path='.github/workflows/watchlist.yml', created_at='2026-10-01T11:00:00Z', updated_at='2026-10-01T13:00:00Z')


def test_workflow_publication_reuses_positive_evidence_without_mutating_signal_history():
    s,_=frames();s['published_at']=None;s['workflow_run_id']='123';before=s.copy(deep=True);calls=[]
    def lookup(key):calls.append(key);return workflow_fact(key)
    resolved, cache=research.resolve_publications(s,lookup=lookup)
    assert calls==['123'] and resolved.published_at.notna().sum()==3
    assert cache['123'][0]['updated_at']=='2026-10-01T13:00:00+00:00'
    pd.testing.assert_frame_equal(s,before)
    repeated=[]
    def forbidden(*a):repeated.append(a);raise AssertionError('positive facts must be reused')
    again, cache2=research.resolve_publications(s,cached=cache,lookup=forbidden)
    assert again.published_at.tolist()==resolved.published_at.tolist() and cache2==cache and not repeated


@pytest.mark.parametrize('fault',['failed','in_progress','path','branch','naive','generated_outside','unknown_generation'])
def test_publication_lookup_never_guesses_for_invalid_workflows_or_timestamps(fault):
    s,_=frames();s['published_at']=None;s['workflow_run_id']='123';fact=workflow_fact()
    if fault=='failed':fact['conclusion']='failure'
    elif fault=='in_progress':fact['status']='in_progress'
    elif fault=='path':fact['path']='.github/workflows/trading_journal.yml'
    elif fault=='branch':fact['head_branch']='feature/portfolio-integration'
    elif fault=='naive':fact['updated_at']='2026-10-01T13:00:00'
    elif fault=='generated_outside':s['generated_at']='2026-10-01T14:00:00Z'
    elif fault=='unknown_generation':s['generated_at']=None
    resolved,_=research.resolve_publications(s,lookup=lambda key:fact)
    assert resolved.published_at.isna().all()


def test_workflow_lookup_is_bounded_and_missing_permissions_leave_unknown():
    s,_=frames();s['published_at']=None;s['workflow_run_id']=['1','2','3'];calls=[]
    def failed(key):calls.append(key);raise PermissionError('synthetic forbidden')
    resolved,cache=research.resolve_publications(s,lookup=failed,max_calls=2)
    assert calls==['3','2'] and not cache and resolved.published_at.isna().all()


def test_rerun_of_the_same_workflow_id_retains_the_earlier_publication_bound():
    s,_=frames();s['published_at']=None;s['workflow_run_id']='123'
    original,cache=research.resolve_publications(s.iloc[:1],lookup=lambda key:workflow_fact(key))
    new=s.iloc[:2].copy();new.loc[1,'generated_at']='2026-10-01T15:00:00Z'
    fact=workflow_fact();fact['updated_at']='2026-10-01T16:00:00Z';fact['run_attempt']=2
    resolved,cache=research.resolve_publications(new,cached=cache,lookup=lambda key:fact)
    assert resolved.published_at.tolist()==['2026-10-01T13:00:00+00:00','2026-10-01T16:00:00+00:00']
    assert len(cache['123'])==2


@pytest.mark.parametrize('field,value',[('published_at',None),('entry_price_source','close_fallback_or_halt'),('calculation_version','legacy')])
def test_primary_statistics_require_publication_open_and_the_corrected_calculation(field,value):
    s,o=frames()
    if field=='published_at':s[field]=value
    else:o[field]=value
    row=market10(research.validation_statistics(s,o,manifest()))
    assert row['completed']==0 and row['win_rate_pct'] is None


@pytest.mark.parametrize('basis',['changed','unverified'])
def test_uncertain_stop_adjustment_basis_withholds_stop_metrics_but_keeps_hold_returns(basis):
    s,o=frames();o['stop_price_basis']=basis
    row=market10(research.validation_statistics(s,o,manifest()))
    assert row['completed']==2 and row['stop_r_n']==0 and row['mean_stop_r'] is None


def test_optional_report_declares_unsupported_warning_and_reentry_analyses(tmp_path):
    write_market(tmp_path);r=read_report(research.publish_analysis_report(tmp_path))
    assert r['schema']=='watchlist-research-1.1'
    assert r['evaluation_coverage']['warnings'].startswith('unavailable')
    assert r['evaluation_coverage']['reentry'].startswith('unavailable')
    assert r['publication_evidence']=={}


def test_disabled_portfolio_and_sparse_checkout_dependency_are_preserved():
    root=Path(__file__).resolve().parents[1]
    text=(root/'tests/fixtures/workflows/trading_journal.yml').read_text()
    assert "PORTFOLIO_SNAPSHOT_ENABLED: 'false'" in text
    assert 'watchlist/analysis_report.py' in text
    watcher=(root/'tests/fixtures/workflows/watchlist.yml').read_text()
    assert '  schedule:' not in watcher and 'workflow_dispatch:' in watcher

