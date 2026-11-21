"""Unified multi-chart / single-stock diagnostics - no market APIs."""
import pandas as pd
from streamlit.testing.v1 import AppTest

import app
from test_chart_coverage import grid_script


def test_compact_status_does_not_treat_missing_values_as_healthy():
    row = {"strict_minervini": None, "rs_score": 93, "rs_change_4w": None,
           "atr_from_50dma": None, "dist_days_25d": None}
    assert app.stock_grid_health_summary(row) == "정배열 – · RS – · 이격 – · 분배 –"
    row.update(strict_minervini=True, rs_change_4w=3, atr_from_50dma=4.1, dist_days_25d=5)
    assert "RS ✅" in app.stock_grid_health_summary(row)
    assert "이격 🚨" in app.stock_grid_health_summary(row)
    assert "분배 ⚠️" in app.stock_grid_health_summary(row)


def test_chart_grid_selects_one_stock_without_eagerly_rendering_detail_charts():
    at = AppTest.from_string(grid_script(count=3, available=3), default_timeout=30).run()
    assert not at.exception and len(at.get("plotly_chart")) == 3
    at.button(key="grid_detail_btn_US_T000_0").click().run()
    assert not at.exception
    assert any("Company 0" in x.value and "선택 종목 정밀 진단" in x.value for x in at.markdown)
    assert len(at.get("plotly_chart")) == 3  # no duplicated expanded candle by default
    at.radio(key="grid_detail_mode_US:T000").set_value("손익비 1R·2R·3R").run()
    assert not at.exception and len(at.get("plotly_chart")) == 4
    at.radio(key="grid_detail_mode_US:T000").set_value("차트 확대").run()
    assert not at.exception and len(at.get("plotly_chart")) == 4
    assert at.radio(key="grid_zoom_period_US:T000").value == 260
    at.button(key="grid_detail_btn_US_T001_1").click().run()
    assert not at.exception
    assert any("Company 1" in x.value and "선택 종목 정밀 진단" in x.value for x in at.markdown)
    assert not any("Company 0" in x.value and "선택 종목 정밀 진단" in x.value for x in at.markdown)


def test_grid_missing_chart_stock_retains_optional_analysis_path():
    at = AppTest.from_string(grid_script(count=2, available=0), default_timeout=30).run()
    assert not at.exception and len(at.get("plotly_chart")) == 0
    assert at.selectbox(key="grid_missing_pick").value == ""
    at.selectbox(key="grid_missing_pick").set_value("US:T000").run()
    assert not at.exception and len(at.get("plotly_chart")) == 0
    assert any("Company 0" in x.value and "선택 종목 정밀 진단" in x.value for x in at.markdown)
    at.radio(key="grid_detail_mode_US:T000").set_value("차트 확대").run()
    assert not at.exception and any("게시된 가격 차트가 없어" in x.value for x in at.info)


def test_market_and_ticker_identity_kept_separate_for_korean_zero_prefix():
    src = pd.DataFrame([{"market": "KR", "ticker": "005930", "name": "삼성전자"}])
    assert app.stock_grid_health_summary(src.iloc[0].to_dict()).count("–") == 4

