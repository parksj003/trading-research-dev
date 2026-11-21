"""Compact market compass: one-glance comparison, honest missing values, lazy charts."""
import pandas as pd
from streamlit.testing.v1 import AppTest

import app


def regimes():
    return {
        "KOSPI": dict(signal_light="🟢ATTACK", benchmark="KOSPI", as_of="2026-11-10",
                      benchmark_close=3020., benchmark_sma50=2950., benchmark_sma200=2800.,
                      pct_above_50dma=62.4, pct_above_200dma=58.1, distribution_days_25d=2,
                      nh_nl_label="신고 3 / 신저 1", tier_a_total=15, breadth_valid=True),
        "KOSDAQ": dict(signal_light="🔴RISK_OFF", benchmark="KOSDAQ", as_of="2026-11-10",
                       benchmark_close=870., benchmark_sma50=900., benchmark_sma200=920.,
                       pct_above_50dma=21.5, pct_above_200dma=24.6, distribution_days_25d=6,
                       nh_nl_label="신고 0 / 신저 9", tier_a_total=1, breadth_valid=True),
        "NASDAQ100": dict(signal_light="🟡CAUTION", benchmark="NDX", as_of="2026-11-09",
                          benchmark_close=21000., benchmark_sma50=20800., benchmark_sma200=19900.,
                          pct_above_50dma=43., pct_above_200dma=49.0, distribution_days_25d=3,
                          nh_nl_label="신고 1 / 신저 2", tier_a_total=4, breadth_valid=True),
        "SP500": dict(signal_light="", benchmark="S&P500 ex-NDX", as_of="2026-11-09",
                      benchmark_close=6100., benchmark_sma50=6050., benchmark_sma200=6000.,
                      pct_above_50dma=88., pct_above_200dma=72., distribution_days_25d=None,
                      breadth_valid=False, breadth_status="검증되지 않은 표본"),
    }


def test_frame_preserves_four_track_order_and_distinct_price_dates():
    f = app.regime_comparison_frame(regimes())
    assert f["지수"].tolist() == ["KOSPI", "KOSDAQ", "NASDAQ100", "SP500 ex-NDX"]
    assert f["가격 기준일"].tolist() == ["2026-11-10", "2026-11-10", "2026-11-09", "2026-11-09"]
    assert f["레짐"].tolist() == ["🟢 공략", "🔴 방어", "🟡 관찰", "⚪ 미확인"]
    assert f.loc[0, "50일/200일↑"] == "62.4% / 58.1%"
    assert f.loc[1, "50일/200일↑"] == "21.5% / 24.6%"
    assert f.loc[1, "분배(25일)"] == "6일"
    assert f.loc[3, "50일/200일↑"] == "산출불가"
    assert f.loc[3, "분배(25일)"] == "—"
    assert f.loc[0, "200일 이격"] == "+7.9%"


def test_missing_signal_and_partial_breadth_are_not_converted_to_caution_or_zero():
    regs = {"KOSPI": dict(signal_light=None, breadth_valid=True, pct_above_50dma=52,
                           pct_above_200dma=None, distribution_days_25d=None,
                           benchmark_close=100, benchmark_sma200=0, as_of=None)}
    row = app.regime_comparison_frame(regs).iloc[0]
    assert row["레짐"] == "⚪ 미확인"
    assert row["50일/200일↑"] == "52% / —"
    assert row["분배(25일)"] == "—"
    assert row["200일 이격"] == "—"
    assert row["가격 기준일"] == "미확인"


def compact_script():
    return """
import pandas as pd
import app
from test_compact_compass import regimes
regs=regimes()
hist=pd.DataFrame([
    dict(track="KOSPI",as_of="2026-11-09",signal_light="🟡CAUTION",
         benchmark_close=3000.,benchmark_sma50=2950.,benchmark_sma200=2780.),
    dict(track="KOSPI",as_of="2026-11-10",signal_light="🟢ATTACK",
         benchmark_close=3020.,benchmark_sma50=2950.,benchmark_sma200=2800.),
])
b=app.Bundle(man={},summ={"regimes":regs},h_regime=hist)
app.render_regime_cards_and_breadth(regs,{})
app.render_market_snapshot_and_history(b)
"""


def test_compact_compass_shows_three_up_comparisons_and_selected_history_when_expanded():
    at = AppTest.from_string(compact_script(), default_timeout=30).run()
    assert not at.exception
    assert len(at.dataframe) == 1 and len(at.dataframe[0].value) == 4
    assert list(at.dataframe[0].value.columns) == [
        "지수", "레짐", "50일/200일↑", "분배(25일)", "200일 이격", "가격 기준일"
    ]
    assert at.session_state["mk_focus_trk"] == "KOSPI"
    assert len(at.get("plotly_chart")) == 0
    assert len(at.get("progress")) == 0  # Internal health data is displayed once in the matrix.
    assert any("S&P500 ex-NDX" in c.value for c in at.caption)

    at.button(key="mk_detail_button").click().run()
    assert not at.exception and len(at.get("plotly_chart")) == 4
    assert not any(x.key == "mk_chart_mode" for x in at.radio)
    assert any("실제 게시된 날짜" in c.value for c in at.caption)

    at.button(key="mk_pick_SP500").click().run()
    assert not at.exception and at.session_state["mk_focus_trk"] == "SP500"
    assert len(at.get("plotly_chart")) == 3  # Still compare all indices; SP500 has no history.
    assert any("검증되지 않은 표본" in w.value for w in at.warning)
    assert any("기록이 2개 날짜 미만" in x.value for x in at.info)

    at.button(key="mk_pick_KOSPI").click().run()
    assert not at.exception and len(at.get("plotly_chart")) == 4
    assert at.session_state["mk_focus_trk"] == "KOSPI"

    at.button(key="mk_detail_button").click().run()
    assert not at.exception and len(at.get("plotly_chart")) == 0


def test_history_with_insufficient_dates_shows_message_not_invented_points():
    script = """
import app
b=app.Bundle(man={},summ={"regimes":{"KOSPI":{"signal_light":"ATTACK","as_of":"2026-11-10"}}})
app.render_regime_cards_and_breadth(b.summ["regimes"])
app.render_market_snapshot_and_history(b)
"""
    at = AppTest.from_string(script).run()
    assert not at.exception
    at.button(key="mk_detail_button").click().run()
    assert not at.exception
    assert len(at.get("plotly_chart")) == 3  # Three comparisons remain visible without history.
    assert any("2개 날짜 미만" in x.value for x in at.info)


def test_full_table_is_always_visible_and_index_buttons_keep_focus():
    at = AppTest.from_string(compact_script(), default_timeout=30).run()
    assert not at.exception
    assert list(at.dataframe[0].value.columns) == [
        "지수", "레짐", "50일/200일↑", "분배(25일)", "200일 이격", "가격 기준일"
    ]
    assert "mk_table_full" not in at.session_state
    assert "2026-11-09" in at.dataframe[0].value["가격 기준일"].tolist()
    at.button(key="mk_pick_NASDAQ100").click().run()
    assert not at.exception and at.session_state["mk_focus_trk"] == "NASDAQ100"
    assert any("NDX · 벤치마크 가격 요약" in x.value for x in at.markdown)


def test_invalid_breadth_outside_percent_range_does_not_appear_in_table_or_plot():
    data = {"KOSPI": dict(signal_light="ATTACK", as_of="NaT",
                          breadth_valid=True, pct_above_50dma=147.0,
                          pct_above_200dma=-4.5, distribution_days_25d=2)}
    record = app.regime_comparison_frame(data).iloc[0]
    assert record["50일/200일↑"] == "미확인"
    assert record["가격 기준일"] == "미확인"
    assert pd.isna(app.valid_breadth_pct(data["KOSPI"], "pct_above_50dma"))
    assert pd.isna(app.valid_breadth_pct(data["KOSPI"], "pct_above_200dma"))
    assert app.valid_breadth_pct({"pct_above_50dma": 0}, "pct_above_50dma") == 0.0
    assert app.valid_breadth_pct({"pct_above_50dma": 100}, "pct_above_50dma") == 100.0
    script = """
import app
import streamlit as st
regs={'KOSPI':{'signal_light':'ATTACK','pct_above_50dma':147.,'pct_above_200dma':-4.5,'breadth_valid':True}}
st.session_state['mk_detail_open']=True
original=app.show_plotly
app.show_plotly=lambda fig,key: st.session_state.update(_breadth_y=list(fig.data[0].y)) if key == 'plt_breadth_bar' else None
try:
 app.render_market_snapshot_and_history(app.Bundle(man={},summ={'regimes':regs}))
finally:
 app.show_plotly=original
"""
    at = AppTest.from_string(script).run()
    assert not at.exception
    assert pd.isna(at.session_state["_breadth_y"][0])
    assert len(at.get("plotly_chart")) == 0  # Test replaces plotting with a capture callback.


def test_compass_holdings_editors_are_collapsible_but_values_survive_navigation():
    from test_view_state import SCRIPT
    at = AppTest.from_string(SCRIPT, default_timeout=30).run()
    assert not at.exception
    assert any("보유 비중 입력·한도 안내" in x.label for x in at.expander)
    assert not at.toggle(key="_held_confirmed").value
    assert at.slider(key="_held_kr_pct").disabled
    assert any("보유 비중 미반영" in x.value for x in at.caption)
    at.toggle(key="_held_confirmed").set_value(True).run()
    at.slider(key="_held_kr_pct").set_value(50).run()
    assert not at.exception
    assert any("현재 보유 비중 반영" in x.value and "신규 잔여 한도" in x.value for x in at.caption)
    at.radio(key="main_page").set_value("🎯 3. 전술·멀티차트").run()
    at.radio(key="main_page").set_value("🧭 1. 국면·나침반").run()
    assert not at.exception
    assert at.slider(key="_held_kr_pct").value == 50
    assert at.toggle(key="_held_confirmed").value


def test_main_compass_shows_portfolio_exposure_risk_and_sector_visuals_by_default():
    from test_view_state import SCRIPT
    at = AppTest.from_string(SCRIPT, default_timeout=30).run()
    assert not at.exception
    assert len(at.get("plotly_chart")) >= 2  # Exposure and stop-risk remain visible.
    assert not any(t.key == "mk_portfolio_charts" for t in at.toggle)
    assert at.button(key="mk_detail_button")
    assert len(at.dataframe) >= 1


def test_three_comparisons_have_distinct_metrics_and_short_labels():
    script = """
import app
import streamlit as st
from test_compact_compass import regimes
regs=regimes()
st.session_state['mk_detail_open']=True
captured={}
original=app.show_plotly
app.show_plotly=lambda fig,key: captured.update({key: fig})
try:
 app.render_market_snapshot_and_history(app.Bundle(man={},summ={'regimes':regs}))
 st.session_state['chart_keys']=list(captured)
 st.session_state['breadth_names']=list(captured['plt_breadth_bar'].data[0].x)
 st.session_state['breadth_values']=list(captured['plt_breadth_bar'].data[0].y)
 st.session_state['distance_values']=list(captured['plt_idx_dist'].data[0].y)
 st.session_state['distribution_values']=list(captured['plt_idx_ddays'].data[0].y)
finally:
 app.show_plotly=original
"""
    at=AppTest.from_string(script).run()
    assert not at.exception
    assert at.session_state['chart_keys'] == ['plt_breadth_bar', 'plt_idx_dist', 'plt_idx_ddays']
    assert at.session_state['breadth_names'] == ['KOSPI', 'KOSDAQ', 'NDX100', 'SP ex-NDX']
    assert pd.isna(at.session_state['breadth_values'][3])
    assert at.session_state['distance_values'][1] < 0
    assert pd.isna(at.session_state['distribution_values'][3])


def test_compass_unified_healthy_selector_and_price_values_without_repeated_kpis():
    at=AppTest.from_string(compact_script(),default_timeout=30).run()
    assert not at.exception
    assert len(at.dataframe)==1
    assert len(at.get('progress'))==0
    assert not at.metric  # No extra three large metric cards duplicating the matrix.
    assert any('지수 선택 · 시장 내부 체력 · 핵심 가격' in x.value for x in at.markdown)
    assert any('KOSPI · 선택 지수 가격 요약' in x.value for x in at.markdown)
    assert sum('지수 종가' in c.value for c in at.caption)==1
    assert sum('50일 이동평균' in c.value for c in at.caption)==1
    assert sum('200일 이동평균' in c.value for c in at.caption)==1
    assert sum('Tier A 후보' in c.value for c in at.caption)==1
    assert '62.4% / 58.1%' in at.dataframe[0].value['50일/200일↑'].values
    at.button(key='mk_pick_KOSDAQ').click().run()
    assert not at.exception and at.session_state['mk_focus_trk']=='KOSDAQ'
    assert any('KOSDAQ · 선택 지수 가격 요약' in x.value for x in at.markdown)
    assert any('870.00' in x.value for x in at.markdown)
    assert list(at.dataframe[0].value['지수']) == ['KOSPI','KOSDAQ','NASDAQ100','SP500 ex-NDX']


def test_compass_selected_missing_values_remain_missing_in_compact_summary():
    at=AppTest.from_string(compact_script(),default_timeout=30).run()
    at.button(key='mk_pick_SP500').click().run()
    assert not at.exception and at.session_state['mk_focus_trk']=='SP500'
    assert at.dataframe[0].value.loc[3,'50일/200일↑']=='산출불가'
    assert any('Breadth 산출불가' in x.value for x in at.warning)
    assert any('레짐 신호가 미확인' in x.value for x in at.warning)
    assert any('벤치마크 미확인 · 벤치마크 가격 요약' in x.value for x in at.markdown)


def test_canonical_state_wins_and_legacy_binary_state_reads_contemporary_light():
    records = {"SP500": {"regime": "RISK_OFF", "signal_light": "ATTACK"}}
    assert app.regime_comparison_frame(records).iloc[3]["레짐"] == "🔴 방어"
    assert app.detect_default_mode(records, "SP500") == "RISK_OFF"
    records["SP500"]["regime"] = "RISK_ON"
    assert app.regime_comparison_frame(records).iloc[3]["레짐"] == "🟢 공략"
    records["SP500"]["regime"] = "UNKNOWN"
    assert app.regime_comparison_frame(records).iloc[3]["레짐"] == "⚪ 미확인"


def test_old_snapshot_ftd_is_only_a_publication_note_and_new_snapshot_names_price_basis():
    script = """
import app
from test_compact_compass import regimes
r=regimes()
r['SP500'].update(benchmark='S&P 500 (SPY)',regime='RISK_OFF',signal_light='🔴 RISK_OFF · 🌱FTD확인(11/18)')
r['NASDAQ100'].update(benchmark='NASDAQ 100 (QQQ)',ftd={'version':'swing-ftd-v1.1','status':'not_detected'})
app.render_regime_cards_and_breadth(r)
"""
    at = AppTest.from_string(script).run()
    at.button(key="mk_pick_SP500").click().run()
    assert any("SPY ETF 종가" == x.value for x in at.caption)
    assert any("기존 게시" in x.value and "11/18" in x.value for x in at.caption)
    at.button(key="mk_pick_NASDAQ100").click().run()
    assert any("QQQ ETF 종가" == x.value for x in at.caption)
    assert any("FTD" in x.value and "미감지" in x.value for x in at.caption)


def test_missing_country_cap_does_not_inherit_global_cap(monkeypatch):
    monkeypatch.setattr(app.st, 'session_state', {})
    b = app.Bundle(man={}, summ={'market_limits': {'US': {'exposure_cap': 50., 'alloc': 9., 'risk': .5}},
                                 'portfolio': {'exposure_cap_pct': 75.}})
    result = app.compute_remaining_portfolio(b)
    assert pd.isna(result['kr_cap'])
    assert pd.isna(result['adj_kr_alloc'])
    assert result['adj_us_alloc'] == 9.


def test_confirmed_holdings_summary_uses_same_rounded_rows_and_stop_risk_as_table(monkeypatch):
    monkeypatch.setattr(app.st, "session_state", {"held_confirmed": True, "held_kr_pct": 90})
    watch = pd.DataFrame({"market": ["KR"] * 3, "portfolio_alloc_pct": [10.] * 3,
                          "portfolio_risk_contrib_pct": [.5, 1., 1.5]})
    b = app.Bundle(man={}, watch=watch, summ={"portfolio": {"exposure_cap_pct": 100.}, "market_limits": {
        "KR": {"exposure_cap": 100., "alloc": 30., "risk": 3.},
        "US": {"exposure_cap": 50., "alloc": 0., "risk": 0.}}})
    rem = app.compute_remaining_portfolio(b)
    shown = app.allocation_view(watch, rem)
    assert shown.portfolio_alloc_pct.tolist() == [3.3] * 3
    assert rem["adj_total_alloc"] == round(shown.portfolio_alloc_pct.sum(), 1) == 9.9
    assert rem["adj_total_risk"] == .99


def test_ftd_evidence_checks_candidate_date_and_does_not_promote_risk_off():
    from streamlit.testing.v1 import AppTest
    script = """
import app
r={'regime':'RISK_OFF','as_of':'2026-11-19','ftd':{'status':'detected','candidate_date':'2026-11-18','version':'swing-ftd-v1.1'}}
app.render_regime_cards_and_breadth({'KOSPI':r})
"""
    at = AppTest.from_string(script).run()
    assert any("2026-11-18" in c.value and "방어 레짐" in c.value for c in at.caption)
    row = at.dataframe[0].value
    assert row.iloc[0]["레짐"] == "🔴 방어" and row.iloc[1]["레짐"] == "⚪ 미확인"
    for bad_date in ("2026-11-20", "unknown", None):
        r = {"ftd": {"status": "detected", "candidate_date": bad_date, "version": "swing-ftd-v1.1"}, "as_of": "2026-11-19"}
        assert "검증 불가" in app.compass_ftd_text(r)

