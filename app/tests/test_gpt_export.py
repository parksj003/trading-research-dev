import copy
import json

import numpy as np
import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from gpt_export import ANALYSIS_PROMPT, build_analysis_export, parse_research_report


def inputs():
    manifest = dict(schema_version="1.0", run_id="20261103T230021Z-test", d_str="2026-11-04",
                    generated_at="2026-11-04T08:00:22+09:00",
                    as_of={key: "2026-11-03" for key in ("KOSPI", "KOSDAQ", "NASDAQ100", "SP500")})
    summary = dict(
        regimes={key: dict(regime="CAUTION", as_of="2026-11-03", pct_above_50dma=55.5)
                 for key in manifest["as_of"]},
        market_limits={key: dict(exposure_cap=50., risk_cap=5., alloc=10., risk=.5) for key in ("KR", "US")},
        health={key: dict(stale_cache=False, **{f"{key}_failed": 0, f"{key}_stale_tickers": 0})
                for key in ("kr", "us")},
        counts=dict(selected=30, actionable=4, passed=50),
    )
    records = [dict(market=market, sub_market="KOSPI" if market == "KR" else "NASDAQ100",
                    ticker=f"{i:06d}" if market == "KR" else f"EX{i}", name=f"예시기업{i}",
                    as_of="2026-11-03", overall_rank=str(26-i), rs_score=70.+i,
                    is_actionable=False, is_stale_price=False, stale_bars=0,
                    dist_to_pivot_pct=.05, stop_dist_pct=.08)
               for market in ("KR", "US") for i in range(25)]
    candidates = pd.DataFrame(records)
    selected = candidates.groupby("market", sort=False).head(2).drop(columns=["is_stale_price", "stale_bars"])
    sectors = pd.DataFrame([dict(market=market, level="sector", macro_theme="예시테마",
                                  sector=f"섹터{i}", sector_power=60., median_rs=70., rs_delta_4w=3.)
                            for market in ("KR", "US") for i in range(90)])
    return manifest, summary, selected, candidates, sectors


def payload(export):
    return json.loads(export.text.split("````json\n", 1)[1].rsplit("\n````", 1)[0])


def test_balanced_numeric_engine_ranks_all_sectors_all_selected_and_no_input_mutation():
    args = inputs()
    before = copy.deepcopy(args)
    export = build_analysis_export(*args)
    data = payload(export)
    assert export.counts == dict(watchlist=4, candidates_available=50, candidates_included=40, sectors=180)
    assert not export.warnings
    assert data["scope"]["candidates_truncated"] is True
    for market in ("KR", "US"):
        sample = [row for row in data["candidate_sample"] if row["market"] == market]
        assert len(sample) == 20
        assert [row["overall_rank"] for row in sample] == [str(i) for i in range(2, 22)]
    assert len(data["sectors"]) == 180  # Full sector coverage, independent of candidate limit.
    assert all(row["is_stale_price"] is False for row in data["actionable_watchlist"])
    assert data["actionable_watchlist"][0]["ticker"] == "000000"
    assert data["actionable_watchlist"][0]["stop_dist_pct"] == .08
    assert data["engine_counts_all_markets"]["selected"] == 30
    assert data["engine_counts_all_markets"]["actionable"] == 4
    assert "stop_dist_pct" in data["units"]["fraction_0_05_means_5_percent"]
    assert data["as_of_by_track"]["KOSPI"] == "2026-11-03"
    assert data["source"]["d_str"] == "2026-11-04"
    assert ANALYSIS_PROMPT in export.text
    assert export.filename == "watchlist_gpt_ALL_20261103T230021Z-test.md"
    assert args[:2] == before[:2]
    for actual, expected in zip(args[2:], before[2:]):
        pd.testing.assert_frame_equal(actual, expected)


@pytest.mark.parametrize("market,tracks", [("KR", {"KOSPI", "KOSDAQ"}), ("US", {"NASDAQ100", "SP500"})])
def test_market_filter_applies_to_every_payload_section(market, tracks):
    data = payload(build_analysis_export(*inputs(), market=market, candidate_limit=40))
    assert set(data["regimes"]) == set(data["as_of_by_track"]) == tracks
    assert set(data["engine_model_limits_not_actual_holdings"]) == set(data["health"]) == {market}
    for key in ("sectors", "actionable_watchlist", "candidate_sample"):
        assert {row["market"] for row in data[key]} == {market}
    assert not data["scope"]["candidates_truncated"]


def test_unknown_columns_nested_metadata_and_real_holdings_are_never_exported(monkeypatch):
    args = inputs()
    marker = "PRIVATE_VALUE_MUST_NOT_LEAVE_APP"
    monkeypatch.setenv("GITHUB_TOKEN", marker)
    args[0]["token"] = marker
    args[0]["as_of"]["private"] = marker
    args[1]["portfolio"] = dict(account=marker, equity=marker)
    args[1]["counts"]["account"] = marker
    args[1]["regimes"]["KOSPI"]["account"] = marker
    args[1]["regimes"]["KOSPI"]["benchmark"] = dict(token=marker)
    args[1]["health"]["swallowed"] = dict(secret=marker)
    args[1]["health"]["kr"]["account"] = marker
    args[1]["market_limits"]["KR"]["account"] = marker
    for frame in args[2:]:
        for key in ("account", "qty", "GITHUB_TOKEN", "nav", "actual_holdings", "cash", "realized_pnl"):
            frame[key] = marker
    export = build_analysis_export(*args)
    assert marker not in export.text
    assert "GITHUB_TOKEN" not in export.text
    assert payload(export)["regimes"]["KOSPI"]["benchmark"] is None


def test_freshness_matching_uses_exact_market_track_ticker_and_missing_stays_unknown():
    manifest, summary, _, _, sectors = inputs()
    candidates = pd.DataFrame([
        dict(market="US", sub_market="SP500", ticker="EX", is_stale_price=True, stale_bars=4),
        dict(market="US", sub_market="NASDAQ100", ticker="EX", is_stale_price=False, stale_bars=0),
    ])
    watch = candidates.drop(columns=["is_stale_price", "stale_bars"])
    watch = pd.concat([watch, pd.DataFrame([dict(market="US", sub_market="SP500", ticker="MISSING")])])
    rows = payload(build_analysis_export(manifest, summary, watch, candidates, sectors))["actionable_watchlist"]
    assert [row["is_stale_price"] for row in rows] == [True, False, None]
    assert [row["stale_bars"] for row in rows] == [4, 0, None]


def test_null_and_non_finite_values_serialize_as_unknown_without_altering_signals():
    args = inputs()
    args[3].loc[0, "rs_score"] = np.inf
    args[3].loc[1, "stop_dist_pct"] = np.nan
    args[3].loc[2, "name"] = pd.NA
    args[3].index = [0] * len(args[3])  # Repeated source indices must not multiply rows.
    data = payload(build_analysis_export(*args, candidate_limit=80))
    assert len(data["candidate_sample"]) == 50
    by_ticker = {row["ticker"]: row for row in data["candidate_sample"] if row["market"] == "KR"}
    assert by_ticker["000000"]["rs_score"] is None
    assert by_ticker["000001"]["stop_dist_pct"] is None
    assert by_ticker["000002"]["name"] is None
    assert by_ticker["000000"]["is_actionable"] is False


def test_incomplete_legacy_snapshot_keeps_explicit_warnings_and_unknown_dates():
    args = inputs()
    args[0].pop("run_id")
    args[0]["as_of"] = "2026-11-03"  # Old scalar contracts cannot imply all four track dates.
    args[1]["health"]["us"] = dict(stale_cache=True, us_failed=2, us_stale_tickers=1)
    export = build_analysis_export(*args)
    assert len(export.warnings) == 8
    assert set(payload(export)["as_of_by_track"].values()) == {None}
    assert any("오래된 캐시" in warning for warning in export.warnings)
    assert any("US 수집 실패: 2" in warning for warning in export.warnings)


@pytest.mark.parametrize("market,limit", [("XX", 20), ("ALL", 0), ("KR", 1000), ("US", True)])
def test_invalid_export_options_are_rejected(market, limit):
    with pytest.raises(ValueError):
        build_analysis_export(*inputs(), market=market, candidate_limit=limit)


def test_download_name_cannot_include_manifest_paths():
    args = inputs()
    args[0]["run_id"] = "../../escape/\nsecret"
    export = build_analysis_export(*args)
    assert "/" not in export.filename and "\n" not in export.filename


def test_export_screen_renders_download_and_scope_on_mobile_friendly_controls():
    at = AppTest.from_string('''
import pandas as pd
import app
b = app.Bundle(man={"run_id":"test", "d_str":"2026-11-04", "generated_at":"2026-11-04T08:00:00+09:00"}, summ={})
b.watch = pd.DataFrame([dict(market="KR", ticker="000001", name="예시기업")])
app.render_gpt_export(b)
''').run()
    assert not at.exception
    assert len(at.get("download_button")) == 1
    assert not at.get("download_button")[0].proto.disabled
    assert at.radio(key="gpt_market").value == "ALL"
    at.radio(key="gpt_market").set_value("US").run()
    assert not at.exception
    assert at.get("download_button")[0].proto.label == "📥 미국 ChatGPT 분석 파일 받기"
    assert at.get("download_button")[0].proto.disabled  # No US data, no misleading empty report.
    assert any("게시 적격 종목 데이터가 비어" in item.value for item in at.warning)


def research_fixture():
    args = inputs()
    hashes = {name: "a"*64 for name in ("summary.json", "watchlist.csv", "candidates.csv", "sectors.csv")}
    args[0]["files"] = {name: {"sha256": value} for name, value in hashes.items()}
    report = {
        "schema": "watchlist-research-1.1",
        "source": dict(run_id=args[0]["run_id"], market_file_sha256=hashes, history_update_ok=True),
        "validation": {"status": "available", "policy": {"min_completed_sample": 20, "optimization_applied": False},
            "quality": {"outcome_rows": 25},
            "groups": [dict(market=key, window="all", dimension="signal", horizon=10, signal_name="rs_blue_dot",
                            signal_value=True, signals=25, completed=3, pending_or_excluded=22,
                            evidence="insufficient_sample", mean_return_pct=1.5, publication_checked_n=0)
                       for key in ("KR", "US")]},
        "market_changes": {"baseline": {"run_id": "old"},
            "regimes": [dict(market=key, previous_regime="ATTACK", current_regime="CAUTION") for key in ("KR", "US")],
            "sectors": [dict(market=key, sector="Example", sector_power_change=-3.) for key in ("KR", "US")]}}
    return args, report


def as_markdown(report):
    return "````json\n"+json.dumps(report)+"\n````\n"


def test_research_statistics_are_version_checked_market_filtered_and_keep_denominators():
    args, report = research_fixture()
    export=build_analysis_export(*args, market="US", research_raw=as_markdown(report).encode())
    r=payload(export)["research"]
    assert r["status"]=="available"
    row=r["validation"]["groups"][0]
    assert row["signals"]==25 and row["completed"]==3 and row["pending_or_excluded"]==22
    assert row["signal_name"]=="rs_blue_dot" and row["publication_checked_n"]==0
    assert {row["market"] for row in r["validation"]["groups"]}=={"US"}
    assert {row["market"] for row in r["market_changes"]["sectors"]}=={"US"}
    assert r["market_changes"]["baseline"]["run_id"]=="old"
    assert "completed가 20 미만이면 판단 보류" in export.text


@pytest.mark.parametrize("fault", ["run_id", "hash", "schema", "malformed"])
def test_stale_or_corrupt_research_is_excluded_while_market_export_survives(fault):
    args, report=research_fixture()
    if fault=="run_id":report["source"]["run_id"]="old"
    elif fault=="hash":report["source"]["market_file_sha256"]["summary.json"]="b"*64
    elif fault=="schema":report["schema"]="unknown"
    raw=as_markdown(report) if fault!="malformed" else "unreadable"
    export=build_analysis_export(*args,research_raw=raw)
    assert payload(export)["research"]["status"]=="unavailable"
    assert export.counts["watchlist"]==4
    assert any("일치하지 않거나" in warning for warning in export.warnings)


def test_research_uses_allowlisted_nested_fields_and_rejects_incomplete_history():
    args, report=research_fixture();marker="PRIVATE_SENTINEL_RESEARCH"
    report["validation"]["account"]=marker
    report["validation"]["policy"]["nav"]=marker
    report["validation"]["quality"]["token"]=marker
    report["validation"]["groups"][0]["qty"]=marker
    report["market_changes"]["sectors"][0]["account"]=marker
    report["market_changes"]["baseline"]["token"]=marker
    export=build_analysis_export(*args,research_raw=as_markdown(report))
    assert marker not in export.text
    report["source"]["history_update_ok"]=False
    r=parse_research_report(as_markdown(report),args[0],("KR","US"))
    assert r["status"]=="history_update_incomplete" and r["validation"]["groups"]==[]


def test_research_coverage_versions_and_rs_context_are_preserved_without_inventing_evidence():
    args,r=research_fixture()
    r['evaluation_coverage']={'warnings':'unavailable: missing controls','reentry':'unavailable: missing cycles','token':'PRIVATE_COVERAGE'}
    r['validation']['quality'].update(legacy_calculation_rows=20,stop_basis_changed_rows=2)
    r['validation']['groups'][0].update(config_hash='fixed-test',engine_commit='fixed-revision',
        rs_change_n=3,mean_rs_change_4w=5.,payoff_ratio=None)
    export=build_analysis_export(*args,research_raw=as_markdown(r));data=payload(export)['research']
    assert data['evaluation_coverage']['warnings'].startswith('unavailable')
    assert data['validation']['quality_all_markets']['legacy_calculation_rows']==20
    assert data['validation']['groups'][0]['config_hash']=='fixed-test'
    assert data['validation']['groups'][0]['mean_rs_change_4w']==5.
    assert 'PRIVATE_COVERAGE' not in export.text
    r['schema']='watchlist-research-1.0'
    assert payload(build_analysis_export(*args,research_raw=as_markdown(r)))['research']['status']=='unavailable'


def test_close_delay_and_us_cap_fallback_are_carried_to_gpt_without_private_fields():
    args = inputs()
    args[1]['health']['kr'].update(kr_close_check_date='2026-11-04', kr_price_status='delayed_or_holiday',
                                 kr_price_delayed_tickers=399, kr_index_as_of={'account': 'private-marker'})
    args[1]['health']['us'].update(us_cap_requested=80, us_cap_fallback=10, us_cap_missing=2,
                                 us_fundamentals_missing=3)
    export = build_analysis_export(*args)
    data = payload(export)
    assert data['health']['KR']['kr_price_delayed_tickers'] == 399
    assert data['health']['US']['us_cap_fallback'] == 10
    assert any('2026-11-04' in warning and '미확보' in warning for warning in export.warnings)
    assert any('이전 정상 캐시 사용: 10종목' in warning for warning in export.warnings)
    assert any('분석 대상 제외' in warning for warning in export.warnings)
    assert 'private-marker' not in export.text


def test_metric_definitions_price_diagnostics_and_eligible_count_are_allowlisted_and_market_scoped():
    args,report=research_fixture()
    args[1]['metric_definitions']=dict(near_52w='종가 고점·저점 3% 근접',api_key='PRIVATE_AUDIT_MARKER',
                                       leading={'account':'PRIVATE_AUDIT_MARKER'})
    args[1]['price_observation']=dict(policy='기술적 적격과 최신성은 별개',tracks={
        'SP500':dict(benchmark_as_of='2026-11-03',clock_reference_date='2026-11-04',
                     session_unconfirmed=True,lagging_candidate_rows=1,account='PRIVATE_AUDIT_MARKER'),
        'KOSPI':dict(benchmark_as_of='2026-11-03',session_unconfirmed=True)})
    report['validation']['quality']['entry_eligible_rows']=1
    report['validation']['policy']['authority']='official simulated performance'
    export=build_analysis_export(*args,market='US',research_raw=as_markdown(report))
    data=payload(export)
    assert data['metric_definitions']['leading'] is None
    assert data['metric_definitions']['near_52w']=='종가 고점·저점 3% 근접'
    assert set(data['price_observation']['tracks'])=={'SP500'}
    assert data['research']['validation']['quality_all_markets']['entry_eligible_rows']==1
    assert any('기술적 적격은 최신성 보증이 아니며' in warning for warning in export.warnings)
    assert 'PRIVATE_AUDIT_MARKER' not in export.text

