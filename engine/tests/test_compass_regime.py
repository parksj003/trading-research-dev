"""Price evidence, FTD snapshot semantics and model budget regressions."""
import numpy as np
import pandas as pd
import pytest

from watchlist import trend_watchlist as e


def bars():
    days = e.get_calendar("US").sessions_in_range("2024-01-02", "2025-01-02")[-240:]
    close = np.full(len(days), 100.)
    close[-14:] = 90
    frame = pd.DataFrame({"Open": close, "Close": close, "High": close + 1, "Low": close - 1,
                          "Volume": 100., "_session_confirmed": True, "_raw_ohlc_valid": True}, index=days)
    frame.iloc[-14, frame.columns.get_loc("Low")] = 88
    return frame


def candidate(market="US"):
    return pd.DataFrame([dict(market=market, sub_market="KOSPI" if market == "KR" else "SP500",
                              sector="S", ticker="A", position_size_pct=20., raw_stop_dist_pct=-.05)])


@pytest.mark.parametrize("market,regimes", [
    ("US", {"KOSPI": {"signal_light": "ATTACK"}}),
    ("KR", {"SP500": {"signal_light": "ATTACK"}}),
    ("US", {}), ("US", {"NASDAQ100": {"regime": "ATTACK"}}),
    ("US", {"SP500": {"regime": "UNKNOWN", "signal_light": "ATTACK"}}),
    ("US", {"SP500": e.calc_market_regime(pd.DataFrame())}),
])
def test_missing_local_evidence_blocks_new_weight(market, regimes):
    out, _ = e.allocate_portfolio_risk_budget(candidate(market), regimes, {})
    assert out.portfolio_alloc_pct.tolist() == [0.]
    assert "미확인" in out.portfolio_alloc_note.iloc[0]


def test_canonical_regime_controls_size_cap_and_guide():
    r = {"regime": "RISK_OFF", "signal_light": "🟢 ATTACK"}
    rows = [{"raw_stop_dist_pct": -.05}]
    e.sync_track_sizing_with_final_regime(rows, r, {})
    assert rows[0]["position_size_pct"] * .0515 <= .5
    _, m = e.allocate_portfolio_risk_budget(candidate(), {"SP500": r}, {})
    assert (m["exposure_cap_pct"], m["risk_cap_pct"]) == (25, 2.5)
    assert "0~25%" in e._compute_exposure_guide({"SP500": r})


def test_empty_benchmark_has_no_price_date_and_missing_volume_cannot_authorize_attack():
    assert e.calc_market_regime(pd.DataFrame())["as_of"] is None
    h = bars().iloc[:-14].drop(columns="Volume")
    r = e.calc_market_regime(h, breadth_info={"breadth_valid": True, "pct_above_50dma": 90})
    assert pd.isna(r["distribution_days_25d"]) and r["regime"] != "ATTACK"
    assert e.allocate_portfolio_risk_budget(candidate(), {"SP500": r}, {})[0].portfolio_alloc_pct.sum() == 0


def test_hysteresis_first_available_average_requires_confirmation():
    close = pd.Series([100.] * 199 + [98.])
    assert not e._hysteresis_risk_off(close)[0]
    assert e._hysteresis_risk_off(pd.concat([close, pd.Series([98.])], ignore_index=True))[0]


def test_public_model_floors_residual_weight_before_charging_full_precision_risk():
    frame = pd.concat([candidate(), candidate()], ignore_index=True)
    frame["sector"] = ["A", "B"]
    cfg = {"screen": {"max_portfolio_risk_pct": 1.137}}
    out, meta = e.allocate_portfolio_risk_budget(frame, {"SP500": {"signal_light": "ATTACK"}}, cfg)
    risk = (out.portfolio_alloc_pct * .0515).sum()
    assert out.portfolio_alloc_pct.tolist() == [20., 2.]
    assert risk <= meta["risk_cap_pct"] and meta["total_risk_pct"] == pytest.approx(risk)


def rally(offset):
    h = bars()
    i = len(h) - 14 + offset
    h.iloc[i:, h.columns.get_loc("Close")] = 92
    h.iloc[i:, h.columns.get_loc("High")] = 93
    h.iloc[i, h.columns.get_loc("Volume")] = 110
    return h.iloc[:i+1].copy()


@pytest.mark.parametrize("offset,expected", [(1, False), (2, False), (3, False), (4, True), (12, True), (13, False)])
def test_ftd_offsets_count_sessions_after_low(offset, expected):
    h = rally(offset)
    result = e.calc_market_regime(h, market="US")
    obs = result["ftd"]
    assert (obs["status"] == "detected") == expected
    assert obs == e.calc_market_regime(h.copy(), market="US")["ftd"]
    if expected:
        assert obs["candidate_date"] == h.index[-1].date().isoformat()
        assert result["regime"] == "RISK_OFF"


@pytest.mark.parametrize("defect", ["zero", "nan", "unconfirmed", "gap", "raw", "infinity", "low", "legacy"])
def test_incomplete_data_is_distinct_from_no_ftd(defect):
    h = rally(4)
    if defect == "gap":
        h = h.drop(h.index[-3])
    elif defect == "legacy":
        h = h.drop(columns="_raw_ohlc_valid")
    else:
        field, value = {"zero": ("Volume", 0.), "nan": ("Volume", np.nan), "infinity": ("Close", np.inf),
                        "raw": ("_raw_ohlc_valid", False), "low": ("Low", 999.),
                        "unconfirmed": ("_session_confirmed", False)}[defect]
        h.loc[h.index[-2], field] = value
    assert e.calc_market_regime(h, market="US")["ftd"]["status"] == "unverified"


def test_ftd_first_candidate_loses_its_low_without_changing_prior_snapshot():
    h = rally(4)
    assert e._detect_ftd(h)[0]
    later = bars().iloc[len(h):].copy()
    later.iloc[0, later.columns.get_loc("Low")] = 87
    assert not e._detect_ftd(pd.concat([h, later.iloc[:1]]))[0]
    # The original as-of calculation is independent of the subsequent drop.
    assert e._detect_ftd(h)[0]


def test_exact_eight_percent_correction_and_exact_gain_are_inclusive():
    frame = rally(4)
    frame.loc[:, ["High", "Low", "Close"]] = [100., 96., 98.]
    frame.loc[frame.index[-5], ["Low", "Close"]] = [92., 95.]
    frame.loc[frame.index[-2], ["High", "Close"]] = [100., 100.]
    frame.loc[frame.index[-1], ["High", "Close"]] = [102., 101.25]
    assert e._detect_ftd(frame)[0]
    frame.loc[frame.index[-1], "Volume"] = 100.
    assert not e._detect_ftd(frame)[0]
    frame.loc[frame.index[-1], ["Volume", "Close"]] = [110., 101.249]
    assert not e._detect_ftd(frame)[0]


@pytest.mark.parametrize("cutoff,quality,expected", [
    ("2024-11-28T22:00Z", "ok", "2024-11-27"),  # Thanksgiving
    ("2024-11-29T18:04Z", "ok", "2024-11-27"),  # early close plus 5 minutes
    ("2024-11-29T18:05Z", "benchmark_date_mismatch", "2024-11-29"),
    ("2024-12-01T18:00Z", "benchmark_date_mismatch", "2024-11-29"),
])
def test_price_date_uses_reviewed_sessions(cutoff, quality, expected):
    h = bars().loc[:"2024-11-27"]
    r = e.calc_market_regime(h, market="US", evaluated_at=pd.Timestamp(cutoff))
    assert (r["regime_quality"], r["expected_as_of"], r["as_of"]) == (quality, expected, "2024-11-27")
    if quality != "ok":
        assert e._exposure_rule({"SP500": r}, {})[0] == 0.


def test_missing_observed_session_is_not_counted_as_an_uneventful_day():
    h = bars().loc[:"2024-11-27"]
    r = e.calc_market_regime(h.drop(h.index[-5]), market="US", evaluated_at=pd.Timestamp("2024-11-28T22:00Z"))
    assert r["regime_quality"] == "distribution_session_gap"
    assert r["distribution_days_25d"] is None


def test_two_candidates_after_one_low_and_next_independent_rally():
    h = rally(4)
    more = bars().iloc[len(h):].copy()
    more.loc[:, ["High", "Low", "Close"]] = [94., 88.5, 92.]
    more.iloc[0, more.columns.get_loc("Low")] = 88.01  # below the first candidate's 99% line
    more.iloc[1, more.columns.get_loc("Close")] = 93.5
    more.iloc[1, more.columns.get_loc("Volume")] = 120
    assert not e._detect_ftd(pd.concat([h, more.iloc[:2]]))[0]
    later = rally(4)
    later.index = e.get_calendar("US").sessions_in_range("2025-01-02", "2026-04-01")[:len(later)]
    assert e._detect_ftd(pd.concat([h, more, later])) == (True, later.index[-1].strftime("%m/%d"))


def test_invalid_stop_cannot_consume_or_poison_the_next_allocation():
    frame = pd.concat([candidate(), candidate()], ignore_index=True)
    frame.loc[0, "raw_stop_dist_pct"] = np.nan
    out, m = e.allocate_portfolio_risk_budget(frame, {"SP500": {"regime": "ATTACK"}}, {})
    assert out.portfolio_alloc_pct.tolist() == [0., 20.]
    assert m["total_risk_pct"] == pytest.approx(1.03)


def test_trailing_swing_window_can_first_show_an_older_candidate_date():
    df = bars().iloc[:80].copy()
    df.loc[:, ["High", "Low", "Close", "Volume"]] = [101., 96., 100., 100.]
    df.loc[df.index[45], ["Low", "Close"]] = [80., 86.]
    df.loc[df.index[66], ["Low", "Close"]] = [85., 88.]
    df.loc[df.index[67:70], ["Low", "Close"]] = [88., 90.]
    df.loc[df.index[70:], ["Low", "Close"]] = [89., 92.]
    df.loc[df.index[70], "Volume"] = 130.
    assert not e._detect_ftd(df.iloc[:71])[0]
    assert e._detect_ftd(df.iloc[:76]) == (True, df.index[70].strftime("%m/%d"))


def test_breadth_collapse_requires_both_participation_and_distribution_pressure():
    h = bars().iloc[:-14].copy()
    # The benchmark remains above the hard stop, rotating within the band.
    for i in range(-24, 0, 2):
        h.loc[h.index[i], ["Close", "Volume"]] = [99.6, 200.]
    for pct, state in ((24.99, "RISK_OFF"), (25., "CAUTION")):
        r = e.calc_market_regime(h, breadth_info={"breadth_valid": True, "pct_above_50dma": pct})
        assert r["distribution_days_25d"] == 12
        assert r["regime"] == state

