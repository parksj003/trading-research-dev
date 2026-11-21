#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Top-Down Trend Leader Watchlist (v22.0 - 누적 이력(후보·신호 결과)·HTML 정제·게시 계층·KR 병렬수집)
핵심 업그레이드 내역:
1. 주섹터(대테마) & 서브섹터(밸류체인) 강도 시각화 및 로테이션 판별 엔진 추가
2. 탑다운(Top-Down) 리포트 렌더러 재편: 시장 레짐 -> 테마 강도 -> 주도주 액션
3. 한국/미국 시장 밸류체인 사전 세분화 (원전/건설 분리, 반도체 공정별 분리 등)
4. 미국 장중 미완성 봉 제거 버그 및 4주 RS 변화량 정합성 버그 수정
5. 억지 클리핑 제거 및 정배열/VCP 셋업 기반 포지션 사이징/리스크 점검 고도화
"""
from __future__ import annotations

import argparse
import gzip
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
import hashlib
import os
import html
import io
import json
import logging
from pathlib import Path
import re
import shutil
import sys
import time
import uuid
from typing import Any, Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
if __package__:
    from .session_calendar import get_calendar, calendar_policy, KR_CLOSED_DATES
    from .analysis_report import CALCULATION_VERSION, SIGNAL_FLAGS, publish_analysis_report, read_previous_market, tag_raw_price_quality, metric_definitions, price_observation
else:
    from session_calendar import get_calendar, calendar_policy, KR_CLOSED_DATES
    from analysis_report import CALCULATION_VERSION, SIGNAL_FLAGS, publish_analysis_report, read_previous_market, tag_raw_price_quality, metric_definitions, price_observation
try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "settings.yml"
REPORT_DIR = ROOT / "reports"
STATE_DIR = ROOT / "data"
UNIVERSE_CACHE_DIR = STATE_DIR / "universe_cache"
KR_HISTORY_CACHE_DIR = STATE_DIR / "kr_history_cache"
US_HISTORY_CACHE_DIR = STATE_DIR / "us_history_cache"
LOG = logging.getLogger("trend_watchlist")

KST = timezone(timedelta(hours=9))
IPO_RS_PENALTY = 8.0  # 상장 252거래일 미만 종목 RS 백분위 감점(점)
STOCK_DIST_EXIT_DAYS = 5  # 종목 분배일 청산 경고 임계(25일, 랜덤워크 우연확률 약 7%)
# EST_OFFSET = timedelta(hours=-5) # 제거: 서머타임 자동 적용을 위해 ZoneInfo 사용

def log_progress(percent: int, stage: str, detail: str = "") -> None:
    LOG.info("PROGRESS|%d|%s|%s", max(0, min(100, int(percent))), stage, detail)

def retry_call(func: Callable, *args, retries: int = 3, delay: float = 1.2, **kwargs) -> Any:
    last_exc = None
    for attempt in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(delay * (attempt + 1))
    raise last_exc

SWALLOWED: Counter = Counter()
_SWALLOW_LOG_LIMIT = 3
_SWALLOW_WARN_FUNCS = {
    "fetch_kr_fundamentals", "get_kr_universe", "get_us_universe", "bulk_sync_kr_recent_ohlcv",
    "fetch_kr_history", "fetch_us_histories", "_dl_chunks", "get_us_market_caps", "_fetch_single_us_cap",
    "load_sector_overrides", "load_historical_tracking", "run_kr_tracks", "run_us_tracks",
}

def _swallow(where: str, exc: BaseException, ctx: Any = None) -> None:
    """삼켜진 예외를 기록: 함수별 집계 + 처음 N건은 WARNING(데이터 핵심 경로) / 나머지는 DEBUG."""
    key = f"{where}:{type(exc).__name__}"
    SWALLOWED[key] += 1
    cnt = SWALLOWED[key]
    msg = "[예외삼킴] %s%s -> %s: %s (누적 %d건)"
    args = (where, f" [{ctx}]" if ctx else "", type(exc).__name__, str(exc)[:160], cnt)
    if where in _SWALLOW_WARN_FUNCS and cnt <= _SWALLOW_LOG_LIMIT:
        LOG.warning(msg, *args)
    else:
        LOG.debug(msg, *args, exc_info=(cnt <= _SWALLOW_LOG_LIMIT))

def swallowed_summary(top: int = 8) -> str:
    if not SWALLOWED:
        return ""
    items = ", ".join(f"{k}×{v}" for k, v in SWALLOWED.most_common(top))
    return f"내부 예외 {sum(SWALLOWED.values())}건 (상위: {items})"

DEFAULT_CONFIG: dict[str, Any] = {
    "market": {
        "kospi_min_market_cap_krw": 1_000_000_000_000,
        "kosdaq_min_market_cap_krw": 400_000_000_000,
        "sp500_min_market_cap_usd": 10_000_000_000,
        "nasdaq_min_market_cap_usd": 15_000_000_000,
        "us_min_market_cap_usd": 10_000_000_000,
        "kospi_min_avg_daily_value_krw": 5_000_000_000,
        "kosdaq_min_avg_daily_value_krw": 3_000_000_000,
        "sp500_min_avg_daily_value_usd": 30_000_000,
        "nasdaq_min_avg_daily_value_usd": 50_000_000,
        "min_avg_daily_value_krw": 3_000_000_000,
        "min_avg_daily_value_usd": 30_000_000,
        "history_years": 2,
        "max_candidates_per_track": 20,
        "max_per_sector": 4,
        "kr_min_price": 3000.0,
        "us_min_price": 10.0,
        "exclude_nasdaq_from_sp500": True,
        "trim_incomplete_intraday_bar": True,
    },
    "screen": {
        "min_history_days": 200,
        "near_high_pct": 0.28,
        "min_rs_score": 70,
        "min_price_vs_200dma": 0.90,
        "min_rs_63d": -0.08,
        "min_rs_126d": -0.12,
        "rs_mode": "STANDARD",
        "weights": {"rs_63d": 0.25, "rs_126d": 0.35, "rs_252d": 0.40},
        "fast_weights": {"rs_21d": 0.35, "rs_63d": 0.45, "rs_126d": 0.20},
        "risk_per_trade_pct": 0.01,
        "caution_risk_per_trade_pct": 0.0075,
        "risk_off_risk_per_trade_pct": 0.005,
        "max_position_size_pct": 25.0,
        "max_acceptable_stop_pct": 0.08,
        "wide_stop_max_pct": 0.12,
        "slippage_pct": 0.0015,
        "round_trip_cost_pct": 0.003,
        "max_portfolio_risk_pct": 5.0,
        "risk_off_max_portfolio_risk_pct": 2.5,
        "max_sector_alloc_pct": 35.0,
        "max_global_theme_alloc_pct": 45.0,
    },
    "market_regime": {
        "kospi_benchmark": "1001",
        "kosdaq_benchmark": "2001",
        "sp500_benchmark": "SPY",
        "nasdaq_benchmark": "QQQ",
        "risk_off_below_200dma": True,
        "risk_off_band_pct": 0.01,
        "risk_off_confirm_days": 2,
        "risk_off_hard_pct": 0.05,
        "dist_down_sigma_mult": 0.75,
        "dist_attack_max": 2,
        "dist_collapse_min": 6,
    },
    "universe": {
        "us_sources": ["sp500", "nasdaq100"],
        "kr_markets": ["KOSPI", "KOSDAQ"],
        "sector_overrides_csv": "sector_overrides.csv",
        "use_kr_etf_pdf": True,
        "use_pykrx_bulk_daily_sync": True,
    },
    "publish": {
        "enabled": True,
        "dir": "published",
        "chart_bars": 260,
        "chart_scope": "all_candidates",  # 차트 게시 범위만 확대. 선정·전술·리스크 계산과 독립.
        "chart_max": 60,
        "keep_daily": 60,
        "signal_cooldown_days": 14,
        "candidates_hist_days": 400,
    },
    "reports": {
        "keep_daily_files": 30,
        "minimal_output": True,
        "save_debug_csv": False,
        "enable_png_chart": False,
        "enable_html_viewer": False,
        "enable_forward_validation": True,
        "check_us_earnings": True,
    },
    "runtime": {
        "request_pause_seconds": 0.08,
        "log_level": "INFO",
        "history_cache_ttl_hours": 12,
        "universe_cache_ttl_hours": 24,
        "refresh_cache": False,
        "kr_fetch_workers": 4,
    },
}

# =====================================================================
# 1. 밸류체인 사전 (대테마 -> 세부섹터) 및 정밀 분류기 (세분화 됨)
# =====================================================================

KR_THEME_ETFS: list[tuple[str, str, str]] = [
    ("471990", "AI·반도체", "AI반도체·HBM·본딩장비"),
    ("487240", "AI·전력·인프라", "AI전력·변압기·차단기"),
    ("466920", "조선·방산·우주", "조선·해양·엔진기자재"),
    ("449450", "조선·방산·우주", "K-방산·우주항공"),
    ("463250", "조선·방산·우주", "K-방산·우주항공"),
    ("434730", "원전·SMR", "원전·SMR·원전기자재"),
    ("305720", "로봇·2차전지·모빌리티", "2차전지·셀·핵심소재"),
    ("445290", "로봇·2차전지·모빌리티", "휴머노이드·협동로봇"),
    ("244580", "바이오·헬스케어", "신약·플랫폼(ADC·비만)"),
    ("228790", "K-소비재·콘텐츠", "K-뷰티·화장품"),
    ("091170", "금융·밸류업", "은행·금융지주"),
    ("102970", "금융·밸류업", "증권·보험·밸류업"),
]

_KR_GROUPS: list[tuple[str, str, str]] = [
    ("AI·반도체", "종합반도체(IDM·HBM)", "005930,000660,402340"),
    ("AI·반도체", "AI반도체·HBM·본딩장비", "042700,039030,089030,357780,240810"),
    ("AI·반도체", "전공정장비·소부장", "036930,031980,403870,140860,074600,005290"),
    ("AI·반도체", "반도체 후공정·테스트", "058470,095340,131970,067310,131290,000990,108320"),
    ("AI·반도체", "유리기판·차세대기판(MLB)", "007660,009150,353200,222800,007810,011070"),
    ("AI·전력·인프라", "AI전력·변압기·차단기", "267260,298040,010120,103590,033100,062040"),
    ("AI·전력·인프라", "전선·케이블·구리", "006260,001440,229640,000500,010130"),
    ("AI·전력·인프라", "AI소프트웨어·클라우드·통신", "035420,035720,012510,018260,012750,017670,030200,032640"),
    ("조선·방산·우주", "K-방산·우주항공", "012450,079550,064350,047810,272210,103140"),
    ("조선·방산·우주", "조선·해양·엔진기자재", "329180,009540,042660,010140,010620,097230,071970,082740,443060,033500,017960"),
    ("원전·SMR", "원전·SMR·원전기자재", "034020,052690,028050,047040"),
    ("건설·인프라기계", "건설·플랜트·인프라기계", "000720,375500,241560,267270"),
    ("바이오·헬스케어", "위탁생산(CDMO)·시밀러", "207940,068270"),
    ("바이오·헬스케어", "신약·플랫폼(ADC·비만)", "196170,141080,298380,000100,128940,326030,006280,086450,096530"),
    ("바이오·헬스케어", "미용의료기기·톡신", "214450,145020,214150"),
    ("로봇·2차전지·모빌리티", "휴머노이드·협동로봇", "277810,454910,108490,058610"),
    ("로봇·2차전지·모빌리티", "2차전지·셀·핵심소재", "373220,006400,051910,003670,247540,086520,066970"),
    ("로봇·2차전지·모빌리티", "완성차·자율주행·전장", "005380,000270,012330,089860"),
    ("에너지·해운·산업소재", "정유·에너지·LNG", "010950,096770,018670,015760,036460"),
    ("에너지·해운·산업소재", "해운·물류·항공", "011200,028670,003490,086280"),
    ("K-소비재·콘텐츠", "K-뷰티·화장품", "278470,090430,051900,192820,161890"),
    ("K-소비재·콘텐츠", "K-푸드·음식료수출", "003230,097950,004370,033780,280360,026960"),
    ("K-소비재·콘텐츠", "유통·렌탈·엔터·게임", "021240,282330,007070,139480,030000,259960,352820"),
    ("금융·밸류업", "은행·금융지주", "105560,055550,086790,316140,024110,138930,139130,175330"),
    ("금융·밸류업", "증권·보험·밸류업", "138040,000810,005830,001450,088350,032830,071050,005940,016360,039490,006800"),
]
KR_VALUE_CHAIN_MAP: dict[str, tuple[str, str]] = {
    tkr.strip(): (m_th, sec) for m_th, sec, tkrs in _KR_GROUPS for tkr in tkrs.split(",") if tkr.strip()
}

_US_GROUPS: list[tuple[str, str, str]] = [
    ("AI·반도체", "AI칩·가속기·설계", "NVDA,AVGO,AMD,ARM,MRVL,QCOM"),
    ("AI·반도체", "AI메모리·HBM·스토리지", "MU,WDC,STX"),
    ("AI·반도체", "반도체 전공정·장비·EDA", "KLAC,LRCX,AMAT,ASML,SNPS,CDNS"),
    ("AI·반도체", "아날로그·전력반도체", "TXN,ADI,NXPI,MPWR,ON"),
    ("AI·데이터센터 인프라", "광통신·네트워킹장비", "COHR,LITE,CIEN,ANET,CSCO"),
    ("AI·데이터센터 인프라", "AI서버·스토리지", "DELL,SMCI,HPE,NTAP"),
    ("AI·데이터센터 인프라", "전력기기·냉각·리츠", "GEV,ETN,PWR,TT,CARR,EME,EQIX,DLR"),
    ("AI·데이터센터 인프라", "AI전력·원전·독립발전", "CEG,VST,NRG,NEE"),
    ("AI·소프트웨어·빅테크", "빅테크·AI클라우드", "MSFT,GOOGL,GOOG,AMZN,META,AAPL"),
    ("AI·소프트웨어·빅테크", "AI에이전트·데이터SW", "PLTR,NOW,CRM,ORCL,APP,ADBE,INTU"),
    ("AI·소프트웨어·빅테크", "사이버보안·네트워크보안", "CRWD,PANW,FTNT,ZS"),
    ("바이오·헬스케어", "비만·대사질환·신약", "LLY,VRTX,REGN,AMGN"),
    ("바이오·헬스케어", "수술로봇·첨단의료기기", "ISRG,BSX,SYK,GEHC"),
    ("방산·우주·기계", "우주항공·차세대방산", "RKLB,ASTS,AXON,LMT,RTX"),
    ("방산·우주·기계", "인프라·산업기계", "GE,HWM,CAT,URI,DE"),
    ("모빌리티·소비재·플랫폼", "자율주행·플랫폼·소비재", "TSLA,NFLX,SPOT,UBER,BKNG,COST,WMT"),
    ("금융·디지털자산", "핀테크·디지털자산", "COIN,HOOD,SQ,SOFI,V,MA,PYPL"),
]
US_VALUE_CHAIN_MAP: dict[str, tuple[str, str]] = {
    tkr.strip(): (m_th, sec) for m_th, sec, tkrs in _US_GROUPS for tkr in tkrs.split(",") if tkr.strip()
}

def map_us_sub_industry(gics_sector: str, sub_industry: str) -> tuple[str, str]:
    s = f"{gics_sector} {sub_industry}".lower()
    if "semiconductor equipment" in s or "semiconductor materials" in s:
        return ("AI·반도체", "반도체 전공정·장비·EDA")
    if "semiconductor" in s:
        return ("AI·반도체", "AI칩·가속기·설계")
    if "systems software" in s:
        return ("AI·소프트웨어·빅테크", "사이버보안·네트워크보안")
    if "application software" in s:
        return ("AI·소프트웨어·빅테크", "AI에이전트·데이터SW")
    if "communications equipment" in s:
        return ("AI·데이터센터 인프라", "광통신·네트워킹장비")
    if "computer hardware" in s or "technology hardware" in s:
        return ("AI·데이터센터 인프라", "AI서버·스토리지")
    if "electric utilities" in s or "independent power" in s or "multi-utilities" in s:
        return ("AI·데이터센터 인프라", "AI전력·원전·독립발전")
    if "electrical components" in s or "heavy electrical" in s or "building products" in s:
        return ("AI·데이터센터 인프라", "전력기기·냉각·리츠")
    if "aerospace" in s or "defense" in s:
        return ("방산·우주·기계", "우주항공·차세대방산")
    if "machinery" in s or "engineering" in s:
        return ("방산·우주·기계", "인프라·산업기계")
    if "biotechnology" in s or "pharmaceuticals" in s:
        return ("바이오·헬스케어", "비만·대사질환·신약")
    if "health care" in s or "life sciences" in s:
        return ("바이오·헬스케어", "수술로봇·첨단의료기기")
    if any(k in s for k in ("crypto", "digital asset")):
        return ("금융·디지털자산", "핀테크·디지털자산")
    if any(k in s for k in ("bank", "financial", "insurance", "asset management")):
        return ("금융·디지털자산", "은행·투자·금융서비스")
    if any(k in s for k in ("oil", "gas", "energy")):
        return ("에너지·원자재", "석유·천연가스·LNG")
    return (gics_sector or "기타·일반산업", sub_industry or gics_sector or "일반산업")

def map_kr_upjong_fallback(raw_sector: str, stock_name: str = "") -> tuple[str, str]:
    s, nm = str(raw_sector).strip(), str(stock_name).strip()
    if any(k in nm for k in ("바이오", "제약", "약품", "테라퓨틱스", "파마", "셀트리온", "씨젠", "올릭스", "보로노이", "알테오젠", "리가켐")):
        return ("바이오·헬스케어", "신약·플랫폼(ADC·비만)")
    if any(k in nm for k in ("반도체", "세미콘", "마이크론", "테크윙", "원익IPS", "이수페타시스", "심텍", "해성디에스", "한미반도체", "이오테크닉스")):
        return ("AI·반도체", "AI반도체·HBM·본딩장비")
    if any(k in nm for k in ("HD현대일렉트릭", "일진전기", "제룡전기", "효성중공업", "LS ELECTRIC", "대한전선")):
        return ("AI·전력·인프라", "AI전력·변압기·차단기")
    if any(k in nm for k in ("중공업", "조선", "미포", "한화오션", "한화엔진", "STX엔진", "HD현대마린")):
        return ("조선·방산·우주", "조선·해양·엔진기자재")
    if any(k in nm for k in ("에어로스페이스", "LIG넥스원", "현대로템", "한국항공우주", "한화시스템", "풍산")):
        return ("조선·방산·우주", "K-방산·우주항공")
    if any(k in nm for k in ("두산에너빌리티", "한전기술", "한전KPS")):
        return ("원전·SMR", "원전·SMR·원전기자재")
    if any(k in nm for k in ("현대건설", "삼성E&A", "대우건설", "HD현대인프라코어", "두산밥캣")):
        return ("건설·인프라기계", "건설·플랜트·인프라기계")
    if any(k in nm for k in ("금융지주", "KB금융", "신한지주", "하나금융", "우리금융", "기업은행", "BNK", "JB금융", "DGB")):
        return ("금융·밸류업", "은행·금융지주")
    if any(k in nm for k in ("증권", "화재", "해상", "생명", "손해보험")):
        return ("금융·밸류업", "증권·보험·밸류업")
    if any(k in nm for k in ("화장품", "코스맥스", "한국콜마", "아모레", "클리오", "브이티", "실리콘투", "에이피알")):
        return ("K-소비재·콘텐츠", "K-뷰티·화장품")
    if any(k in nm for k in ("삼양식품", "농심", "오리온", "CJ제일제당", "풀무원", "대상", "빙그레")):
        return ("K-소비재·콘텐츠", "K-푸드·음식료수출")
    if any(k in nm for k in ("로보틱스", "로보티즈", "레인보우로보", "두산로보틱스", "에스피지")):
        return ("로봇·2차전지·모빌리티", "휴머노이드·협동로봇")
    if any(k in nm for k in ("에코프로", "엘앤에프", "포스코퓨처엠", "LG에너지솔루션", "삼성SDI", "피엔티")):
        return ("로봇·2차전지·모빌리티", "2차전지·셀·핵심소재")

    if "의약" in s or "의료" in s:
        return ("바이오·헬스케어", "신약·플랫폼(ADC·비만)")
    # Fallback 로직 완화 (무조건 기판으로 보내는 현상 방지)
    if "전기전자" in s or "전기·전자" in s:
        return ("IT·기기부품", "디스플레이·기기부품")
    if "기계" in s:
        return ("건설·인프라기계", "건설·플랜트·인프라기계")
    if "건설" in s:
        return ("건설·인프라기계", "건설·플랜트·인프라기계")
    if "운수장비" in s or "운송장비" in s:
        return ("로봇·2차전지·모빌리티", "완성차·자율주행·전장")
    if "은행" in s:
        return ("금융·밸류업", "은행·금융지주")
    if "증권" in s or "보험" in s or "금융" in s:
        return ("금융·밸류업", "증권·보험·밸류업")
    if "음식료" in s or "유통" in s or "섬유" in s:
        return ("K-소비재·콘텐츠", "유통·렌탈·엔터·게임")
    if "화학" in s or "철강" in s or "비금속" in s or "운수창고" in s:
        return ("에너지·해운·산업소재", s if s else "산업소재")
    return ("기타·미분류", s if s else "미분류")

def fetch_kr_etf_theme_map(date_str: str, cfg: dict) -> dict[str, dict[str, Any]]:
    if not cfg.get("universe", {}).get("use_kr_etf_pdf", True):
        return {}
    UNIVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    etf_hash = hashlib.md5(json.dumps(KR_THEME_ETFS).encode("utf-8")).hexdigest()[:8]
    cache_file = UNIVERSE_CACHE_DIR / f"kr_etf_theme_map_v15_{etf_hash}.pkl"
    if is_cache_fresh(cache_file, 168.0, bool(cfg["runtime"].get("refresh_cache", False))):
        try:
            cached = pd.read_pickle(cache_file)
            if isinstance(cached, dict) and cached:
                return cached
        except Exception as _exc:
            _swallow("fetch_kr_etf_theme_map", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    try:
        from pykrx import stock
    except ImportError:
        return {}
    etf_map: dict[str, dict[str, Any]] = {}
    reviewed_etfs = []
    for etf_code, macro_theme, sub_sector in KR_THEME_ETFS:
        try:
            pdf = retry_call(stock.get_etf_portfolio_deposit_file, etf_code, date_str, retries=2, delay=0.4)
            if pdf is None or pdf.empty:
                continue
            pdf = pdf.reset_index()
            t_col = "티커" if "티커" in pdf.columns else pdf.columns[0]
            w_col = next((c for c in pdf.columns if "비중" in str(c) or "금액" in str(c)), None)
            for _, row in pdf.iterrows():
                tkr = str(row[t_col]).strip().zfill(6)
                if not tkr.isdigit():
                    continue
                w = float(pd.to_numeric(row[w_col], errors="coerce")) if w_col else 1.0
                w = 1.0 if np.isnan(w) else w
                prev = etf_map.get(tkr)
                member = {"code": etf_code, "macro_theme": macro_theme, "sector": sub_sector, "weight": w}
                if prev is None:
                    etf_map[tkr] = {"macro_theme": macro_theme, "sector": sub_sector, "max_weight": w, "themes": [sub_sector], "memberships": [member]}
                else:
                    prev["memberships"].append(member)
                    if sub_sector not in prev["themes"]:
                        prev["themes"].append(sub_sector)
                    if w > prev["max_weight"]:
                        prev.update({"macro_theme": macro_theme, "sector": sub_sector, "max_weight": w})
            reviewed_etfs.append(etf_code)
        except Exception as _exc:
            _swallow("fetch_kr_etf_theme_map", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    if etf_map:
        etf_map["__review_metadata__"] = {"retrieved_at": datetime.now(timezone.utc).isoformat(),
            "source_as_of": date_str, "complete": len(reviewed_etfs) == len(KR_THEME_ETFS)}
        try:
            pd.to_pickle(etf_map, cache_file)
        except Exception as _exc:
            _swallow("fetch_kr_etf_theme_map", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    return etf_map

def classify_theme_and_sector(
    market: str,
    ticker: str,
    raw_sector: str,
    sub_industry: str,
    overrides: dict[str, tuple[str, str]],
    etf_map: dict[str, dict[str, Any]] | None = None,
    stock_name: str = "",
) -> tuple[str, str, str]:
    mkt = "KR" if market.upper().startswith("KR") or market.upper() in ("KOSPI", "KOSDAQ") else "US"
    tkr = ticker.zfill(6) if mkt == "KR" else ticker.upper()
    key = f"{mkt}:{tkr}"
    if key in overrides:
        return overrides[key][0], overrides[key][1], overrides[key][1]
    if mkt == "KR":
        etf_info = (etf_map or {}).get(tkr)
        etf_tags = ", ".join(etf_info["themes"]) if etf_info and etf_info.get("themes") else ""
        if tkr in KR_VALUE_CHAIN_MAP:
            m_th, s_sec = KR_VALUE_CHAIN_MAP[tkr]
            return m_th, s_sec, f"{s_sec}" + (f" ({etf_tags})" if etf_tags and etf_tags != s_sec else "")
        if etf_info:
            return etf_info["macro_theme"], etf_info["sector"], etf_tags
        m_th, s_sec = map_kr_upjong_fallback(raw_sector, stock_name=stock_name)
        return m_th, s_sec, s_sec
    if tkr in US_VALUE_CHAIN_MAP:
        m_th, s_sec = US_VALUE_CHAIN_MAP[tkr]
        return m_th, s_sec, s_sec
    m_th, s_sec = map_us_sub_industry(raw_sector, sub_industry)
    return m_th, s_sec, s_sec


def _sector_review_support():
    if __package__:
        from . import sector_review
    else:
        import sector_review
    return sector_review


def _configure_sector_review(cfg: dict, manual: dict, now: str) -> dict:
    """Only explicit maintained decisions augment the existing classification map."""
    import inspect
    support = _sector_review_support()
    base = ROOT / str(cfg.get("publish", {}).get("dir", "published"))
    decisions_path = ROOT / cfg.get("sector_review", {}).get("decisions_csv", "sector_review_decisions.csv")
    active, decisions = support.load_decisions(decisions_path, base, now=now)
    static_keys = {"KR:" + key for key in KR_VALUE_CHAIN_MAP} | {"US:" + key for key in US_VALUE_CHAIN_MAP}
    retained = support.retained_classifications(base, static_keys=static_keys)
    cfg["_sector_review_capture"] = bool(cfg.get("sector_review", {}).get("enabled", True))
    cfg["_sector_review_configured"] = True
    cfg["_sector_review_decisions"] = decisions
    cfg["_sector_review_manual_keys"] = set(manual)
    cfg["_sector_review_approved_keys"] = set(active)
    cfg["_sector_review_retained_keys"] = set(retained)
    cfg["_sector_review_policy_hash"] = support.digest(dict(
        configuration=get_config_version_hash(cfg, manual),
        rules=inspect.getsource(map_us_sub_industry) + inspect.getsource(map_kr_upjong_fallback)))
    return {**retained, **active, **manual}  # Direct user overrides remain authoritative.


def _capture_sector_sources(frame, cfg, overrides, *, raw_sectors=None, sub_industries=None,
                            source_urls=None, source_as_of="", etf_map=None):
    """Retain raw evidence at acquisition time; cache reads never re-attest it."""
    if not cfg.get("_sector_review_capture"):
        return frame
    raw_sectors, sub_industries = raw_sectors or {}, sub_industries or {}
    source_urls, etf_map = source_urls or {}, etf_map or {}
    now = datetime.now(timezone.utc).isoformat()
    etf_meta = etf_map.get("__review_metadata__", {})
    support = _sector_review_support()
    etf_complete = bool(etf_meta.get("complete"))
    if etf_complete:
        age = support.stamp(now) - support.stamp(etf_meta["retrieved_at"])
        etf_complete = timedelta(0) <= age < timedelta(days=support.INTERVAL_DAYS)
    observations = []
    for row in frame.to_dict("records"):
        market, ticker = str(row["market"]), str(row["ticker"])
        key = market + ":" + ticker
        raw = str(raw_sectors.get(ticker, row.get("raw_sector", "")) or "").strip()
        sub = str(sub_industries.get(ticker, "") or "").strip()
        info = etf_map.get(ticker, {})
        memberships = info.get("memberships", []) if etf_complete else []
        if market == "KR":
            external = ((info["macro_theme"], info["sector"]) if etf_complete and info
                        else map_kr_upjong_fallback(raw, str(row.get("name", ""))))
            urls = ["https://data.krx.co.kr/"]
            origin = ("static_map" if ticker in KR_VALUE_CHAIN_MAP else "etf" if info else "fallback")
            valid = raw not in ("", "KOSPI", "KOSDAQ", "nan") or bool(memberships)
        else:
            external = map_us_sub_industry(raw, sub)
            urls = source_urls.get(ticker, [])
            origin = "static_map" if ticker in US_VALUE_CHAIN_MAP else "fallback"
            valid = bool(raw or sub)
        if key in cfg.get("_sector_review_manual_keys", ()):
            origin = "manual_override"
        elif key in cfg.get("_sector_review_approved_keys", ()):
            origin = "reviewed_override"
        elif key in cfg.get("_sector_review_retained_keys", ()):
            origin = "retained_classification"
        observations.append(dict(market=market, ticker=ticker, name=str(row.get("name", "")),
            current_macro_theme=str(row.get("macro_theme", "")), current_sector=str(row.get("sector", "")),
            origin=origin, raw_sector=raw, raw_sub_industry=sub,
            external_macro_theme=external[0], external_sector=external[1],
            provider="KRX" if market == "KR" else "|".join(sorted(urls)), source_urls=sorted(set(urls)),
            etfs=memberships, etf_primary=[info.get("macro_theme"), info.get("sector")] if etf_complete and info else None,
            etf_source={"provider": "pykrx.stock.get_etf_portfolio_deposit_file",
                        "retrieved_at": etf_meta.get("retrieved_at"), "source_as_of": etf_meta.get("source_as_of")} if market == "KR" else None,
            etf_evidence_complete=etf_complete if market == "KR" else None,
            retrieved_at=now, source_as_of=source_as_of, source_valid=valid,
            policy_hash=cfg.get("_sector_review_policy_hash", "")))
    result = frame.copy()
    result["_sector_review_observation"] = observations
    return result


def _sector_review_input(universe, cfg):
    return dict(observations=universe.get("_sector_review_observation", pd.Series(dtype=object)).tolist(),
                stale=bool(universe.get("is_stale_cache", pd.Series(False, index=universe.index)).any()),
                policy_hash=cfg.get("_sector_review_policy_hash"))


def _protect_sector_assignments(universe, cfg, overrides):
    """Apply explicit/retained classifications to cache fallback views, never files."""
    if not cfg.get("_sector_review_configured") or universe.empty:
        return universe
    out = universe.copy()
    now = datetime.now(timezone.utc).isoformat()
    for index, row in out.iterrows():
        key = str(row.get("market", "")) + ":" + str(row["ticker"])
        pair = overrides.get(key)
        if pair is None:
            continue
        if (row.get("macro_theme"), row.get("sector")) != pair:
            out.loc[index, ["macro_theme", "sector"]] = pair
            if "theme_tags" in out:
                out.loc[index, "theme_tags"] = pair[1]
        evidence = row.get("_sector_review_observation")
        if isinstance(evidence, dict):
            evidence = dict(evidence)
            evidence.update(current_macro_theme=pair[0], current_sector=pair[1], classification_observed_at=now)
            evidence["origin"] = ("manual_override" if key in cfg.get("_sector_review_manual_keys", ()) else
                                  "reviewed_override" if key in cfg.get("_sector_review_approved_keys", ()) else
                                  "retained_classification")
            out.at[index, "_sector_review_observation"] = evidence
    return out

# =====================================================================
# 2. 데이터 위생, 장중 미완성 봉 제거 및 설정 로더
# =====================================================================

def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists() or yaml is None:
        return DEFAULT_CONFIG
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        user_cfg = yaml.safe_load(f) or {}
    merged = deep_merge(DEFAULT_CONFIG, user_cfg)
    mkt_u = user_cfg.get("market", {})
    if "kospi_min_market_cap_krw" in mkt_u and mkt_u["kospi_min_market_cap_krw"] == 400_000_000_000:
        merged["market"]["kospi_min_market_cap_krw"] = 1_000_000_000_000
    if "kosdaq_min_market_cap_krw" in mkt_u and mkt_u["kosdaq_min_market_cap_krw"] == 200_000_000_000:
        merged["market"]["kosdaq_min_market_cap_krw"] = 400_000_000_000
    return merged

def deep_merge(base: dict, override: dict) -> dict:
    res = dict(base)
    for k, v in override.items():
        res[k] = deep_merge(res[k], v) if isinstance(v, dict) and isinstance(res.get(k), dict) else v
    return res

def setup_logging(cfg: dict) -> None:
    lvl = str(cfg["runtime"].get("log_level", "INFO")).upper()
    logging.basicConfig(
        level=getattr(logging, lvl, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
        force=True,
    )

def clean_ticker(value: Any) -> str:
    return re.sub(r"\[.*?\]", "", str(value)).strip().replace(".", "-").replace("$", "")

def is_kr_common_operating_stock(ticker: str, name: str) -> bool:
    t = str(ticker).strip().zfill(6)
    if not t.isdigit() or t[-1] != "0":
        return False
    nm = str(name).strip()
    if re.search(r"(스팩\d*호|기업인수목적|리츠|REIT|선물|인버스|레버리지|우B|우C|\d+우|전환)", nm):
        return False
    if nm.endswith("우") and len(nm) >= 3:
        return False
    return True

# [수정 1] trim_incomplete_bar: 미국 동부시간(America/New_York) 서머타임 자동 반영
def trim_incomplete_bar(df: pd.DataFrame, market: str, enabled: bool = True) -> pd.DataFrame:
    if not enabled or df is None or df.empty:
        return df
    # Preserve acquisition evidence; wall time alone never promotes a cached row.
    safe = df
    if "_session_confirmed" in df:
        safe = _completed_cached_bars(Path(), df, market)
    if safe.empty:
        return safe
    ready = session_ready_times(safe.index[-1:], market).iloc[0]
    return safe if pd.notna(ready) and ready <= pd.Timestamp(datetime.now(timezone.utc)) else safe.iloc[:-1].copy()

def normalize_index(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    idx = pd.to_datetime(df.index)
    if getattr(idx, "tz", None) is not None:
        idx = idx.tz_localize(None)
    out = df.copy()
    out.index = idx
    out = out[~out.index.duplicated(keep="last")].sort_index()
    if "Close" in out.columns:
        out["Close"] = pd.to_numeric(out["Close"], errors="coerce")
        out = out.loc[out["Close"] > 0].copy()
        if out.empty:
            return out
        raw_open = pd.to_numeric(out["Open"], errors="coerce") if "Open" in out.columns else out["Close"]
        raw_low = pd.to_numeric(out["Low"], errors="coerce") if "Low" in out.columns else out["Close"]
        raw_vol = pd.to_numeric(out["Volume"], errors="coerce") if "Volume" in out.columns else pd.Series(1.0, index=out.index)
        out["IsHalt"] = (raw_open <= 0) | (raw_low <= 0) | (raw_vol <= 0) | raw_open.isna() | raw_low.isna()
        for col in ("Open", "High", "Low"):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
                bad = out[col].isna() | (out[col] <= 0) | out["IsHalt"]
                out.loc[bad, col] = out.loc[bad, "Close"]
        if all(c in out.columns for c in ("Open", "High", "Low", "Close")):
            out["High"] = out[["Open", "High", "Low", "Close"]].max(axis=1)
            out["Low"] = out[["Open", "High", "Low", "Close"]].min(axis=1)
    return out

def rescale_cached_for_dividend(cached_df: pd.DataFrame, new_df: pd.DataFrame, max_dev: float = 0.03, uniformity: float = 0.0007) -> pd.DataFrame | None:
    """배당락 등으로 과거 수정주가가 '일정 비율'로만 바뀐 경우(겹치는 구간의 신/구 종가 비율이 균일, |비율-1|<=3%),
    전체 재다운로드 대신 캐시 가격(O/H/L/C)에 그 비율을 곱해 이어붙인다. 분할 등 큰 괴리나 비균일 괴리는 None(=재다운로드)."""
    if cached_df is None or new_df is None or cached_df.empty or new_df.empty:
        return None
    if "Close" not in cached_df.columns or "Close" not in new_df.columns:
        return None
    ov = cached_df.index.intersection(new_df.index)
    if len(ov) < 3:
        return None
    c_old = pd.to_numeric(cached_df.loc[ov, "Close"], errors="coerce")
    c_new = pd.to_numeric(new_df.loc[ov, "Close"], errors="coerce")
    valid = (c_old > 0) & (c_new > 0)
    if int(valid.sum()) < 3:
        return None
    ratio = (c_new[valid] / c_old[valid])
    med = float(ratio.median())
    if abs(med - 1.0) > max_dev or float((ratio / med - 1.0).abs().max()) > uniformity:
        return None
    out = cached_df.copy()
    for col in ("Open", "High", "Low", "Close"):
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce") * med
    return out

def prefetch_histories(tickers: list[str], fetch_fn: Callable[[str], pd.DataFrame], workers: int = 4, pause: float = 0.06,
                       progress_cb: Callable[[int, int], None] | None = None) -> dict[str, pd.DataFrame]:
    """오래된 캐시 종목을 스레드풀로 선수집. 실패 종목은 결과에서 빠지고(호출측이 순차 폴백) 예외는 _swallow로 기록."""
    if workers <= 1 or len(tickers) < 2:
        return {}
    out: dict[str, pd.DataFrame] = {}
    total = len(tickers)

    def _one(t: str) -> tuple[str, pd.DataFrame | None]:
        if pause > 0:
            time.sleep(pause)
        try:
            return t, fetch_fn(t)
        except Exception as _exc:
            _swallow("prefetch_histories", _exc, t)
            return t, None

    done = 0
    with ThreadPoolExecutor(max_workers=int(workers)) as ex:
        futs = [ex.submit(_one, t) for t in tickers]
        for fu in as_completed(futs):
            t, df = fu.result()
            if df is not None and not df.empty:
                out[t] = df
            done += 1
            if progress_cb and (done % 50 == 0 or done == total):
                progress_cb(done, total)
    return out

def has_seam_mismatch(cached_df: pd.DataFrame, new_df: pd.DataFrame, tol: float = 0.002) -> bool:
    if cached_df.empty or new_df.empty or "Close" not in cached_df.columns or "Close" not in new_df.columns:
        return False
    ov = cached_df.index.intersection(new_df.index)
    if len(ov) == 0:
        return False
    c_old = pd.to_numeric(cached_df.loc[ov, "Close"], errors="coerce")
    c_new = pd.to_numeric(new_df.loc[ov, "Close"], errors="coerce")
    valid = (c_old > 0) & (c_new > 0)
    if not valid.any():
        return False
    max_diff = float(((c_old[valid] / c_new[valid]) - 1.0).abs().max())
    return max_diff > tol

def is_cache_fresh(path: Path, ttl_hours: float, refresh: bool = False) -> bool:
    return (not refresh) and path.exists() and ((time.time() - path.stat().st_mtime) < ttl_hours * 3600)

# CSAT exceptional sessions: regular cash-market close delayed to 16:30 KST.
# KRX annual CSAT market-hours notices / KICE annual examination schedules.
# exchange-calendars 4.13.2's CSAT exceptions stop in 2020. Maintain this
# explicit reviewed coverage, never extrapolate the exam date from a weekday.
_KR_CSAT = ("2021-11-18", "2022-11-17", "2023-11-16", "2024-11-14",
            "2025-11-13", "2026-11-19")
_KR_SPECIAL_COVERAGE_END = 2026
_SESSION_FINALITY_VERSION = 3
_KR_CSAT_FINALITY_VERSION = 2


def session_ready_times(index, market: str) -> pd.Series:
    """One close+buffer source for acquisition, trimming and refresh decisions.

    Unknown sessions/coverage are NaT (unsafe), not ordinary trading days.
    """
    kr = market.upper() in ("KR", "KOSPI", "KOSDAQ")
    calendar = get_calendar(market)
    dates = pd.DatetimeIndex(index).tz_localize(None).normalize()
    closes = calendar.schedule["close"].reindex(dates).copy()
    if kr:
        for day in _KR_CSAT:
            closes.loc[closes.index == pd.Timestamp(day)] = pd.Timestamp(day, tz="Asia/Seoul") + pd.Timedelta(hours=16, minutes=30)
        if (dates.year > _KR_SPECIAL_COVERAGE_END).any():
            LOG.warning("KR 특별장 일정 검토 범위(%s년) 이후의 일봉은 확정 처리하지 않습니다. 공식 일정 갱신 필요", _KR_SPECIAL_COVERAGE_END)
        closes.loc[dates.year > _KR_SPECIAL_COVERAGE_END] = pd.NaT
    return closes + pd.Timedelta(minutes=5)


def session_close_check_date(market: str, now: datetime | None = None) -> pd.Timestamp | None:
    zone = KST if market == "KR" else ZoneInfo("America/New_York")
    now = now or datetime.now(timezone.utc)
    day = pd.Timestamp(now.astimezone(zone).date())
    ready = session_ready_times([day], market).iloc[0]
    return day if pd.notna(ready) and pd.Timestamp(now) >= ready else None


def kr_close_check_date(now: datetime | None = None) -> pd.Timestamp | None:
    return session_close_check_date("KR", now)

def tag_completed_sessions(frame: pd.DataFrame, market: str) -> pd.DataFrame:
    """Attest freshly received rows only; calendar failures stay unknown/unsafe.

    This private pickle column is retained through merges, never exported. A later
    file write cannot promote a previously unconfirmed row.
    """
    out = frame.copy()
    out["_session_confirmed"] = False
    if out.empty:
        return out
    ready = session_ready_times(out.index, market) <= pd.Timestamp(datetime.now(timezone.utc))
    out["_session_confirmed"] = ready.fillna(False).to_numpy()
    out["_session_finality_version"] = _SESSION_FINALITY_VERSION if market.upper() in ("KR", "KOSPI", "KOSDAQ") else 2
    return out


def _completed_cached_bars(path: Path, cached: pd.DataFrame, market: str) -> pd.DataFrame:
    # Legacy files have no acquisition evidence. Preserve on disk, but refetch
    # before use. Never infer provenance from a restored file's mtime.
    if cached.empty or "_session_confirmed" not in cached:
        return cached.iloc[:0].copy()
    valid = cached["_session_confirmed"].astype(str).str.lower().isin(("true", "1", "1.0"))
    if market.upper() in ("KR", "KOSPI", "KOSDAQ"):
        dates = pd.DatetimeIndex(cached.index).tz_localize(None).normalize()
        # Earlier tags used the incomplete KRX calendar. Only affected dates
        # require reacquisition; ordinary confirmed history remains usable.
        affected = dates.isin(pd.to_datetime(_KR_CSAT))
        version = pd.to_numeric(cached.get("_session_finality_version", pd.Series(0, index=cached.index)), errors="coerce")
        valid &= (~affected | version.ge(_KR_CSAT_FINALITY_VERSION).to_numpy())
        # A true acquisition flag cannot turn an officially closed day into a bar.
        # Filter the read view only; do not overwrite historical cache evidence.
        valid &= ~dates.isin(pd.to_datetime(KR_CLOSED_DATES))
        valid &= dates.year <= _KR_SPECIAL_COVERAGE_END
    return cached.loc[valid].copy()


def kr_cached_intraday_bar(path: Path, cached: pd.DataFrame) -> bool:
    # A closed date is excluded, not an unfinished session to reacquire forever.
    dates = pd.DatetimeIndex(cached.index).tz_localize(None).normalize()
    cached = cached.loc[~dates.isin(pd.to_datetime(KR_CLOSED_DATES))]
    return len(_completed_cached_bars(path, cached, "KR")) != len(cached)


def kr_completed_cached_bars(path: Path, cached: pd.DataFrame) -> pd.DataFrame:
    return _completed_cached_bars(path, cached, "KR")


def us_close_check_date() -> pd.Timestamp | None:
    return session_close_check_date("US")


def us_completed_cached_bars(path: Path, cached: pd.DataFrame) -> pd.DataFrame:
    return _completed_cached_bars(path, cached, "US")

def kr_history_needs_refresh(path: Path, ttl: float, refresh: bool = False,
                             cached: pd.DataFrame | None = None) -> bool:
    """KR만 TTL과 마지막 일봉 날짜를 함께 검사한다(US 캐시 정책은 유지)."""
    if not is_cache_fresh(path, ttl, refresh):
        return True
    target = kr_close_check_date()
    try:
        cached = pd.read_pickle(path) if cached is None else cached
        return (cached.empty or (target is not None and pd.Timestamp(cached.index[-1]).date() < target.date())
                or kr_cached_intraday_bar(path, cached))
    except Exception:
        return True

def kr_price_health(benchmarks: dict[str, pd.DataFrame], price_dates: list[str],
                    unavailable_tickers: int = 0) -> dict[str, Any]:
    """실제 지수/종목 가격 날짜와 미확보 상태. 유니버스 날짜는 비교하지 않는다.

    지연 0건은 확보한 가격의 검사 결과이며 전체 모집단 검증 성공률이 아니다.
    후보 가격은 각 후보의 as_of, 지수 가격은 kr_index_as_of/manifest.as_of다.
    """
    target = kr_close_check_date()
    dates = {key: str(hist.index[-1].date()) if not hist.empty else None
             for key, hist in benchmarks.items()}
    health: dict[str, Any] = {"kr_close_check_date": str(target.date()) if target is not None else None,
                              "kr_index_as_of": dates, "kr_price_status": "not_due",
                              "kr_price_delayed_tickers": 0,
                              "kr_price_unavailable_tickers": unavailable_tickers,
                              "kr_special_hours_reviewed_through": _KR_SPECIAL_COVERAGE_END}
    if datetime.now(KST).year > _KR_SPECIAL_COVERAGE_END:
        health.update(
            kr_price_status="calendar_unreviewed",
            kr_price_message=(f"KR 특별장 일정 검토 범위는 {_KR_SPECIAL_COVERAGE_END}년까지입니다. "
                              "검토 범위 이후 일봉은 확정하지 않아 이전 가격을 표시할 수 있습니다. "
                              "공식 휴장·특별장 일정을 확인하고 갱신해야 합니다. 기술적 적격은 최신성 보증이 아닙니다."))
        return health
    if target is not None:
        date = str(target.date())
        delayed = sum(value < date for value in price_dates)
        missing_index = any(value is None or value < date for value in dates.values())
        health.update(kr_price_status="delayed_or_holiday" if delayed or missing_index or unavailable_tickers else "current",
                      kr_price_delayed_tickers=delayed)
        if delayed or missing_index or unavailable_tickers:
            health["kr_price_message"] = (f"KR {date} 마감 일봉 일부 미확보: 지수 "
                + ", ".join(f"{key} {value or '없음'}" for key, value in dates.items())
                + f" · 이전 일봉 종목 {delayed}개 · 시세 없음 {unavailable_tickers}개. 기존 자료 유지(휴장/공급 지연 확인 필요).")
    return health

def load_sector_overrides(cfg: dict) -> dict[str, tuple[str, str]]:
    path = ROOT / cfg["universe"].get("sector_overrides_csv", "sector_overrides.csv")
    if not path.exists():
        pd.DataFrame(columns=["market", "ticker", "macro_theme", "sector"]).to_csv(path, index=False, encoding="utf-8-sig")
        return {}
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        out: dict[str, tuple[str, str]] = {}
        for _, row in df.iterrows():
            mkt = str(row.get("market", "")).strip().upper()
            tkr = clean_ticker(row.get("ticker", ""))
            if mkt == "KR":
                tkr = tkr.zfill(6)
            sec = str(row.get("sector", "")).strip()
            m_th = str(row.get("macro_theme", "")).strip() or (sec.split(">")[0].strip() if ">" in sec else "사용자지정")
            if mkt and tkr and sec:
                out[f"{mkt}:{tkr}"] = (m_th, sec)
        return out
    except Exception as _exc:
        _swallow("load_sector_overrides", _exc, locals().get("tkr") or locals().get("ticker"))
        return {}

def get_config_version_hash(cfg: dict, overrides: dict[str, tuple[str, str]] | None = None) -> str:
    """[P0-3.3] 밸류체인 사전, 섹터 오버라이드, 시총 기준 변경 시 캐시를 자동 무효화하기 위한 설정 해시"""
    mkt_cfg = cfg.get("market", {})
    payload = {
        "kr_groups": _KR_GROUPS,
        "us_groups": _US_GROUPS,
        "kr_etfs": KR_THEME_ETFS,
        "overrides": sorted((overrides or {}).items()),
        "caps": [
            mkt_cfg.get("kospi_min_market_cap_krw"),
            mkt_cfg.get("kosdaq_min_market_cap_krw"),
            mkt_cfg.get("sp500_min_market_cap_usd"),
            mkt_cfg.get("nasdaq_min_market_cap_usd"),
            mkt_cfg.get("exclude_nasdaq_from_sp500"),
        ],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]

def evaluate_eps_growth_and_peg(cur_eps: float, old_eps: float, per: float) -> dict[str, Any]:
    """[P1-4.3] 특정 EPS 절대값(50) 하드코딩을 제거하고 흑자전환/적자전환/기저효과 왜곡을 구분 처리"""
    if pd.isna(cur_eps) or pd.isna(old_eps):
        return {"per": per, "eps_growth_pct": np.nan, "peg": np.nan, "eps_status": "MISSING"}
    if old_eps <= 0 and cur_eps > 0:
        return {"per": per, "eps_growth_pct": np.nan, "peg": np.nan, "eps_status": "TURNAROUND"}
    if cur_eps <= 0:
        st = "LOSS_TURN" if old_eps > 0 else "LOSS_CONT"
        return {"per": per, "eps_growth_pct": np.nan, "peg": np.nan, "eps_status": st}
    raw_g = (cur_eps / max(old_eps, 1e-6) - 1.0) * 100.0
    eps_g = round(float(np.clip(raw_g, -99.9, 999.0)), 1)
    peg = np.nan
    if pd.notna(per) and 1.5 <= per <= 150.0 and eps_g >= 5.0 and raw_g <= 500.0:
        peg = round(per / eps_g, 2)
    return {"per": per, "eps_growth_pct": eps_g, "peg": peg, "eps_status": "GROWTH" if eps_g > 0 else "DECLINE"}

def is_valid_krx_cap_table(df: pd.DataFrame, min_valid_rows: int = 100) -> bool:
    """[P0-3.2] KRX 시가총액 테이블이 비어 있거나 종가·시총이 0/소수만 반환되는 비정상 응답 감지"""
    if df is None or df.empty or any(c not in df.columns for c in ("종가", "시가총액", "거래대금")):
        return False
    close_num = pd.to_numeric(df["종가"], errors="coerce")
    cap_num = pd.to_numeric(df["시가총액"], errors="coerce")
    return int(((close_num > 0) & (cap_num > 0)).sum()) >= min_valid_rows

def fetch_kr_fundamentals(date_str: str, markets: list[str], cfg: dict) -> dict[str, dict[str, float]]:
    UNIVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_f = UNIVERSE_CACHE_DIR / f"kr_fundamentals_{date_str}.pkl"
    if is_cache_fresh(cache_f, 168.0, bool(cfg["runtime"].get("refresh_cache", False))):
        try:
            cached = pd.read_pickle(cache_f)
            if isinstance(cached, dict) and cached:
                return cached
        except Exception as _exc:
            _swallow("fetch_kr_fundamentals", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    from pykrx import stock
    f_map: dict[str, dict[str, float]] = {}
    valid_cur_dt = pd.Timestamp(date_str)
    for offset in range(8):
        d_cur = (pd.Timestamp(date_str) - pd.Timedelta(days=offset)).strftime("%Y%m%d")
        try:
            test_df = retry_call(stock.get_market_fundamental_by_ticker, d_cur, market=markets[0], retries=2, delay=0.5)
            if test_df is not None and not test_df.empty and "PER" in test_df.columns and (pd.to_numeric(test_df["PER"], errors="coerce") > 0).any():
                valid_cur_dt = pd.Timestamp(d_cur)
                break
        except Exception as _exc:
            _swallow("fetch_kr_fundamentals", _exc, locals().get("tkr") or locals().get("ticker"))
            continue

    cur_str = valid_cur_dt.strftime("%Y%m%d")
    prev_str = (valid_cur_dt - pd.Timedelta(days=365)).strftime("%Y%m%d")

    for mkt in markets:
        try:
            cur_f = retry_call(stock.get_market_fundamental_by_ticker, cur_str, market=mkt, retries=2, delay=0.5)
            old_f = retry_call(stock.get_market_fundamental_by_ticker, prev_str, market=mkt, retries=2, delay=0.5)
            if cur_f is None or cur_f.empty:
                continue
            cur_f = cur_f.reset_index()
            t_col = "티커" if "티커" in cur_f.columns else cur_f.columns[0]
            old_eps_map: dict[str, float] = {}
            if old_f is not None and not old_f.empty and "EPS" in old_f.columns:
                old_f = old_f.reset_index()
                ot_col = "티커" if "티커" in old_f.columns else old_f.columns[0]
                old_eps_map = dict(zip(old_f[ot_col].astype(str).str.zfill(6), pd.to_numeric(old_f["EPS"], errors="coerce")))

            for _, r in cur_f.iterrows():
                tkr = str(r[t_col]).strip().zfill(6)
                per = float(pd.to_numeric(r.get("PER", np.nan), errors="coerce"))
                eps = float(pd.to_numeric(r.get("EPS", np.nan), errors="coerce"))
                old_eps = float(old_eps_map.get(tkr, np.nan))
                f_map[tkr] = evaluate_eps_growth_and_peg(eps, old_eps, per)
        except Exception as _exc:
            _swallow("fetch_kr_fundamentals", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    if f_map:
        try:
            pd.to_pickle(f_map, cache_f)
        except Exception as _exc:
            _swallow("fetch_kr_fundamentals", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    return f_map

# =====================================================================
# 3. 4대 지수 유니버스 수집 & pykrx 전 종목 일괄 증분 동기화
# =====================================================================

def get_kr_universe(cfg: dict, overrides: dict[str, tuple[str, str]] | None = None) -> pd.DataFrame:
    UNIVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    overrides = overrides or {}
    kospi_min = int(cfg["market"].get("kospi_min_market_cap_krw", 1_000_000_000_000))
    kosdaq_min = int(cfg["market"].get("kosdaq_min_market_cap_krw", 400_000_000_000))
    markets = cfg["universe"].get("kr_markets", ["KOSPI", "KOSDAQ"])
    refresh = bool(cfg["runtime"].get("refresh_cache", False))
    cfg_hash = get_config_version_hash(cfg, overrides)
    cache_file = UNIVERSE_CACHE_DIR / f"kr_universe_v15_{kospi_min}_{kosdaq_min}_{cfg_hash}.pkl"
    name_cache_file = UNIVERSE_CACHE_DIR / "kr_ticker_names_cache.pkl"

    if is_cache_fresh(cache_file, float(cfg["runtime"].get("history_cache_ttl_hours", 12)), refresh):
        try:
            cached = pd.read_pickle(cache_file)
            if isinstance(cached, pd.DataFrame) and not cached.empty and "sub_market" in cached.columns:
                cached = cached.copy()
                cached["is_stale_cache"] = False
                LOG.info("[KR] 유니버스 캐시 로드 완료 (%d종목, hash=%s)", len(cached), cfg_hash)
                return cached
        except Exception as _exc:
            _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
            pass

    from pykrx import stock

    persistent_names: dict[str, str] = {}
    if name_cache_file.exists():
        try:
            persistent_names = pd.read_pickle(name_cache_file) or {}
        except Exception as _exc:
            _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
            persistent_names = {}

    for offset in range(0, 11):
        date = (datetime.now(KST) - timedelta(days=offset)).strftime("%Y%m%d")
        date_frames = []
        ok_date = True
        name_cache_dirty = False
        for market in markets:
            min_cap_mkt = kospi_min if market.upper() == "KOSPI" else kosdaq_min
            try:
                df = retry_call(stock.get_market_cap_by_ticker, date, market=market, retries=3, delay=0.8)
                if not is_valid_krx_cap_table(df, min_valid_rows=100):
                    ok_date = False
                    break
                close_num = pd.to_numeric(df["종가"], errors="coerce")
                cap_num = pd.to_numeric(df["시가총액"], errors="coerce")
                valid = (close_num > 0) & (cap_num > 0)
                fdf = df.loc[valid & (cap_num >= min_cap_mkt)].copy().reset_index()
                t_col = "티커" if "티커" in fdf.columns else fdf.columns[0]
                t_vals = fdf[t_col].astype(str).str.zfill(6).tolist()

                kr_sec, kr_nm = {}, {}
                try:
                    sdf = retry_call(stock.get_market_sector_classifications, date, market=market, retries=2, delay=0.6)
                    if sdf is not None and not sdf.empty:
                        sdf = sdf.reset_index()
                        tc = "티커" if "티커" in sdf.columns else sdf.columns[0]
                        sc = next((c for c in sdf.columns if "업종" in str(c)), None)
                        nc = next((c for c in sdf.columns if "종목명" in str(c)), None)
                        ts = sdf[tc].astype(str).str.zfill(6)
                        if sc:
                            kr_sec = dict(zip(ts, sdf[sc].astype(str).str.strip()))
                        if nc:
                            kr_nm = dict(zip(ts, sdf[nc].astype(str).str.strip()))
                            persistent_names.update({k: v for k, v in kr_nm.items() if v})
                            name_cache_dirty = True
                except Exception as _exc:
                    _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
                    pass

                names = []
                for t in t_vals:
                    if t in kr_nm and kr_nm[t]:
                        names.append(kr_nm[t])
                    elif t in persistent_names and persistent_names[t]:
                        names.append(persistent_names[t])
                    else:
                        try:
                            nm_fetched = stock.get_market_ticker_name(t)
                            if nm_fetched:
                                persistent_names[t] = str(nm_fetched).strip()
                                name_cache_dirty = True
                            names.append(persistent_names.get(t, t))
                        except Exception as _exc:
                            _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
                            names.append(t)

                raw_mkt_df = pd.DataFrame(
                    {
                        "ticker": t_vals,
                        "name": names,
                        "market_cap": pd.to_numeric(fdf["시가총액"], errors="coerce"),
                        "market": "KR",
                        "sub_market": market.upper(),
                        "raw_sector": [kr_sec.get(t, market) for t in t_vals],
                        "currency": "KRW",
                    }
                )
                keep_mask = [is_kr_common_operating_stock(t, n) for t, n in zip(raw_mkt_df["ticker"], raw_mkt_df["name"])]
                date_frames.append(raw_mkt_df.loc[keep_mask].reset_index(drop=True))
            except Exception as _exc:
                _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
                ok_date = False
                break

        if name_cache_dirty:
            try:
                pd.to_pickle(persistent_names, name_cache_file)
            except Exception as _exc:
                _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
                pass

        if ok_date and len(date_frames) == len(markets):
            out = pd.concat(date_frames, ignore_index=True).drop_duplicates("ticker").reset_index(drop=True)
            if not out.empty:
                etf_map = fetch_kr_etf_theme_map(date, cfg)
                fund_map = fetch_kr_fundamentals(date, markets, cfg)
                m_list, s_list, tg_list, peg_list, epsg_list, eps_st_list = [], [], [], [], [], []
                for row in out.itertuples(index=False):
                    tkr = str(row.ticker)
                    m_th, s_sec, tags = classify_theme_and_sector("KR", tkr, str(row.raw_sector), "", overrides, etf_map, str(row.name))
                    m_list.append(m_th)
                    s_list.append(s_sec)
                    tg_list.append(tags)
                    f_info = fund_map.get(tkr, {})
                    peg_list.append(f_info.get("peg", np.nan))
                    epsg_list.append(f_info.get("eps_growth_pct", np.nan))
                    eps_st_list.append(f_info.get("eps_status", "MISSING"))
                out["macro_theme"] = m_list
                out["sector"] = s_list
                out["theme_tags"] = tg_list
                out["peg"] = peg_list
                out["eps_growth_pct"] = epsg_list
                out["eps_status"] = eps_st_list
                out["universe_as_of"] = date
                out["is_stale_cache"] = False
                out = _capture_sector_sources(out, cfg, overrides,
                    source_as_of=datetime.strptime(date, "%Y%m%d").date().isoformat(), etf_map=etf_map)
                try:
                    out.to_pickle(cache_file)
                except Exception as _exc:
                    _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
                    pass
                LOG.info("[KR] 유니버스 신규 수집 완료 (기준일 %s, 보통주 %d종목)", date, len(out))
                return out

    fallback_files = sorted(UNIVERSE_CACHE_DIR.glob("kr_universe_*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if cache_file.exists():
        fallback_files.insert(0, cache_file)
    for fb in fallback_files:
        try:
            stale_df = pd.read_pickle(fb)
            if isinstance(stale_df, pd.DataFrame) and not stale_df.empty:
                stale_df = stale_df.copy()
                stale_df["is_stale_cache"] = True
                as_of_val = str(stale_df["universe_as_of"].iloc[0]) if "universe_as_of" in stale_df.columns else "알수없음"
                LOG.warning("⚠️ [KR] 최신 유니버스 수집 실패 -> 오래된 캐시(%s, 기준일:%s)로 대체합니다.", fb.name, as_of_val)
                return stale_df
        except Exception as _exc:
            _swallow("get_kr_universe", _exc, locals().get("tkr") or locals().get("ticker"))
            continue
    raise RuntimeError("한국 유니버스 데이터를 확보하지 못했습니다.")

def bulk_sync_kr_recent_ohlcv(universe_tickers: list[str], cfg: dict) -> dict[str, Any]:
    # 한 실행에서 당일 전종목 응답을 공유: 공급 지연 때 종목 수만큼 재요청하지 않는다.
    sync: dict[str, Any] = {"snapshot_available": None}
    if not cfg.get("universe", {}).get("use_pykrx_bulk_daily_sync", True):
        return sync
    if bool(cfg["runtime"].get("refresh_cache", False)):
        return sync
    try:
        from pykrx import stock
    except ImportError:
        return sync

    KR_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ttl = float(cfg["runtime"].get("history_cache_ttl_hours", 12))
    stale_tkrs = []
    latest_cached_dates: list[pd.Timestamp] = []

    for tkr in universe_tickers:
        cpath = KR_HISTORY_CACHE_DIR / f"{tkr}.pkl"
        if cpath.exists():
            try:
                cdf = pd.read_pickle(cpath)
                if (isinstance(cdf, pd.DataFrame) and len(cdf) >= 180
                        and kr_history_needs_refresh(cpath, ttl, cached=cdf)):
                    stale_tkrs.append(tkr)
                    completed = kr_completed_cached_bars(cpath, cdf)
                    latest_cached_dates.append(pd.Timestamp(completed.index[-1]).normalize())
            except Exception as _exc:
                _swallow("bulk_sync_kr_recent_ohlcv", _exc, locals().get("tkr") or locals().get("ticker"))
                pass

    if len(stale_tkrs) < 15 or not latest_cached_dates:
        return sync

    min_last_dt = min(latest_cached_dates)
    today_dt = pd.Timestamp(datetime.now(KST).date())
    days_gap = (today_dt - min_last_dt).days
    if days_gap < 0 or days_gap > 10:
        return sync

    daily_Snapshots: dict[pd.Timestamp, pd.DataFrame] = {}
    close_target = kr_close_check_date()
    # 당일을 먼저 조회한다. alternative=False로 휴장일을 전 거래일 값으로 둔갑시키지 않는다.
    offsets = list(range(days_gap, -1, -1))
    if close_target is not None:
        offsets = [0] + [offset for offset in offsets if offset != 0]
    for d_off in offsets:
        target_dt = today_dt - pd.Timedelta(days=d_off)
        if target_dt.weekday() >= 5:
            continue
        d_str = target_dt.strftime("%Y%m%d")
        try:
            snap = stock.get_market_ohlcv_by_ticker(d_str, market="ALL", alternative=False)
            if snap is not None and not snap.empty and "종가" in snap.columns:
                if (pd.to_numeric(snap["종가"], errors="coerce") > 0).sum() > 100:
                    daily_Snapshots[target_dt] = snap
        except Exception as _exc:
            _swallow("bulk_sync_kr_recent_ohlcv", _exc, locals().get("tkr") or locals().get("ticker"))
        if close_target is not None and target_dt == close_target:
            sync["snapshot_available"] = target_dt in daily_Snapshots
            if not sync["snapshot_available"]:
                LOG.warning("[KR] %s 당일 전종목 일봉 미확보(휴장/공급 지연 확인 필요). 기존 종목 캐시 유지", target_dt.date())
                return sync

    if not daily_Snapshots:
        return sync

    rename_cols = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume", "거래대금": "Turnover"}
    synced_cnt = 0
    for tkr in stale_tkrs:
        cpath = KR_HISTORY_CACHE_DIR / f"{tkr}.pkl"
        try:
            cdf = kr_completed_cached_bars(cpath, normalize_index(pd.read_pickle(cpath)))
            if cdf.empty:
                continue
            new_rows = []
            for dt_key, snap in sorted(daily_Snapshots.items()):
                if tkr in snap.index:
                    r = snap.loc[tkr]
                    row_dict = {rename_cols[k]: float(r[k]) for k in rename_cols if k in r}
                    if row_dict.get("Close", 0) > 0:
                        new_rows.append(pd.DataFrame([row_dict], index=[dt_key]))
            if not new_rows:
                continue
            new_df = tag_completed_sessions(normalize_index(tag_raw_price_quality(pd.concat(new_rows))), "KR")
            if has_seam_mismatch(cdf, new_df):
                continue
            if (close_target is not None and new_df.index[-1] < close_target
                    and new_df.index[-1] <= cdf.index[-1]):
                continue
            merged = normalize_index(pd.concat([cdf, new_df]))
            merged.to_pickle(cpath)
            synced_cnt += 1
        except Exception as _exc:
            _swallow("bulk_sync_kr_recent_ohlcv", _exc, locals().get("tkr") or locals().get("ticker"))
            continue
    if synced_cnt > 0:
        LOG.info("[KR] pykrx 일괄 증분 동기화 완료 (%d종목 캐시 고속 갱신)", synced_cnt)
    return sync

def _extract_wiki_table(tables: list[pd.DataFrame]) -> tuple[pd.DataFrame, Any, Any, Any, Any] | None:
    for raw_t in tables:
        if len(raw_t) < 50:
            continue
        t = raw_t.copy()
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [" ".join(str(p) for p in col if str(p) != "nan").strip() for col in t.columns]
        nmap = {re.sub(r"\[.*?\]", "", str(c)).strip().lower(): c for c in t.columns}
        scol = next((o for cn, o in nmap.items() if cn in ("symbol", "ticker", "ticker symbol") or "ticker" in cn or "symbol" in cn), None)
        ncol = next((o for cn, o in nmap.items() if "security" in cn or "company" in cn or cn == "name"), None)
        seccol = next((o for cn, o in nmap.items() if cn in ("gics sector", "icb industry", "sector")), None)
        subcol = next((o for cn, o in nmap.items() if "sub-industry" in cn or "subsector" in cn), None)
        if scol is not None:
            return t, scol, ncol, seccol, subcol
    return None


def _validate_nasdaq100_table(table: pd.DataFrame, symbol_col: Any) -> None:
    if not table[symbol_col].map(lambda value: isinstance(value, str) and
                                value.strip().casefold() not in ('', 'none', 'nan', 'null', '<na>')).all():
        raise ValueError("NASDAQ100 심볼 누락")
    symbols = [clean_ticker(value).upper() for value in table[symbol_col]]
    # Multiple share classes can exceed 100 securities; reject partial/foreign tables.
    if (not 100 <= len(symbols) <= 110 or len(set(symbols)) != len(symbols)
            or any(len(symbol) > 7 or not re.fullmatch(r"[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)?", symbol) for symbol in symbols)):
        raise ValueError("NASDAQ100 종목 목록의 수량·중복·심볼 검증 실패")


def _nasdaq100_official_table(headers: dict) -> tuple[pd.DataFrame, str, str, str, None]:
    """Read membership metadata only; price fields from this API are not used."""
    import requests
    response = retry_call(requests.get, "https://api.nasdaq.com/api/quote/list-type/nasdaq100",
                          headers={**headers, "Accept": "application/json", "Referer": "https://www.nasdaq.com/"},
                          timeout=20, retries=3, delay=1.2)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("NASDAQ 공식 API 응답 형식 오류")
    status = payload.get("status") or {}
    if status.get("rCode") not in (None, 200, "200"):
        raise ValueError("NASDAQ 공식 API 응답 상태 오류")
    data = payload.get("data") or {}
    rows = (data.get("data") or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("NASDAQ 공식 API 종목 목록 없음")
    # A HTTP 200 response can still be paginated or incomplete.
    total = int(str(data.get("totalrecords", "")).replace(",", ""))
    if len(rows) != total or int(data.get("offset") or 0) != 0:
        raise ValueError("NASDAQ 공식 API 종목 목록 일부만 반환됨")
    table = pd.DataFrame(rows)
    if not {"symbol", "companyName"}.issubset(table.columns):
        raise ValueError("NASDAQ 공식 API 필수 필드 누락")
    _validate_nasdaq100_table(table, "symbol")
    if not table["companyName"].map(lambda value: isinstance(value, str) and bool(value.strip())).all():
        raise ValueError("NASDAQ 공식 API 회사명 누락")
    if "sector" not in table:
        table["sector"] = ""
    table = table[["symbol", "companyName", "sector"]].copy()
    LOG.info("[US] NASDAQ100 공식 API 수집 완료 (%d증권)", len(table))
    return table, "symbol", "companyName", "sector", None


def _complete_us_universe(cached: Any, sources: list[str]) -> bool:
    if not isinstance(cached, pd.DataFrame) or cached.empty or "ticker" not in cached:
        return False
    if cached["ticker"].duplicated().any():
        return False
    for source, column in (("sp500", "in_sp500"), ("nasdaq100", "in_nasdaq100")):
        if source not in sources:
            continue
        if column not in cached:
            return False
        selected = cached[column].astype(str).str.lower().isin(["true", "1", "1.0"])
        if source == "sp500" and cached.loc[selected, "ticker"].nunique() < 400:
            return False
        if source == "nasdaq100":
            try:
                _validate_nasdaq100_table(cached[selected], "ticker")
            except (ValueError, TypeError, KeyError):
                return False
    return True


def get_us_universe(cfg: dict, overrides: dict[str, tuple[str, str]] | None = None) -> pd.DataFrame:
    import requests
    UNIVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sources = cfg["universe"].get("us_sources", ["sp500", "nasdaq100"])
    cfg_hash = get_config_version_hash(cfg, overrides)
    cache_file = UNIVERSE_CACHE_DIR / f"us_universe_v15_{'_'.join(sorted(sources))}_{cfg_hash}.pkl"
    if is_cache_fresh(cache_file, float(cfg["runtime"].get("universe_cache_ttl_hours", 24)), bool(cfg["runtime"].get("refresh_cache", False))):
        try:
            cached = pd.read_pickle(cache_file)
            if _complete_us_universe(cached, sources):
                cached = cached.copy()
                cached["is_stale_cache"] = False
                LOG.info("[US] 유니버스 캐시 로드 완료 (%d종목, hash=%s)", len(cached), cfg_hash)
                return cached
            LOG.warning("[US] 캐시 종목 목록 불완전 -> 최신 목록 다시 수집")
        except Exception as _exc:
            _swallow("get_us_universe", _exc, locals().get("tkr") or locals().get("ticker"))
            pass

    overrides = overrides or {}
    sp500_set: set[str] = set()
    ndx_set: set[str] = set()
    names, raw_sectors, sub_inds, sector_sources = {}, {}, {}, {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    urls = {
        "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        "nasdaq100": "https://en.wikipedia.org/wiki/Nasdaq-100",
    }
    for src in sources:
        if src not in urls:
            continue
        try:
            target = None
            source_url = urls[src]
            if src == "nasdaq100":
                try:
                    target = _nasdaq100_official_table(headers)
                    source_url = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
                except Exception as exc:
                    LOG.warning("[US] NASDAQ100 공식 API 실패(%s) -> 위키피디아 대체 시도", type(exc).__name__)
            if target is None:
                resp = retry_call(requests.get, urls[src], headers=headers, timeout=20, retries=3, delay=1.2)
                resp.raise_for_status()
                target = _extract_wiki_table(pd.read_html(io.StringIO(resp.text)))
            if target is None:
                LOG.warning("[US] 종목 목록 표를 찾지 못함 (%s)", src)
                continue
            table, scol, ncol, seccol, subcol = target
            if src == "nasdaq100":
                _validate_nasdaq100_table(table, scol)
            for _, row in table.iterrows():
                tkr = clean_ticker(row[scol]).upper()
                if not tkr or tkr.lower() == "nan" or len(tkr) > 7:
                    continue
                if src == "sp500":
                    sp500_set.add(tkr)
                elif src == "nasdaq100":
                    ndx_set.add(tkr)
                if ncol is not None and pd.notna(row[ncol]):
                    names[tkr] = str(row[ncol]).strip()
                if seccol is not None and pd.notna(row[seccol]) and not raw_sectors.get(tkr):
                    raw_sectors[tkr] = str(row[seccol]).strip()
                    sector_sources.setdefault(tkr, set()).add(source_url)
                if subcol is not None and pd.notna(row[subcol]) and not sub_inds.get(tkr):
                    sub_inds[tkr] = str(row[subcol]).strip()
                    sector_sources.setdefault(tkr, set()).add(source_url)
        except Exception as exc:
            LOG.warning("[US] 유니버스 원본 수집 실패 (%s): %s", src, exc)

    all_tkrs = sorted(sp500_set | ndx_set)
    # [P1-2] 한쪽 소스라도 대규모 누락(부분 수집) 시 캐시 오염 방지를 위해 전체 실패로 간주
    is_partial_fail = ("sp500" in sources and len(sp500_set) < 400) or ("nasdaq100" in sources and not 100 <= len(ndx_set) <= 110)
    if not all_tkrs or is_partial_fail:
        LOG.warning("[US] 유니버스 부분 수집 감지 (S&P500: %d, NDX: %d) -> 캐시 폴백 시도", len(sp500_set), len(ndx_set))
        for fb in ([cache_file] if cache_file.exists() else []) + sorted(UNIVERSE_CACHE_DIR.glob("us_universe_*.pkl"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                stale_df = pd.read_pickle(fb)
                if _complete_us_universe(stale_df, sources):
                    stale_df = stale_df.copy()
                    stale_df["is_stale_cache"] = True
                    LOG.warning("⚠️ [US] 최신 유니버스 수집 실패 -> 오래된 캐시(%s)로 대체합니다.", fb.name)
                    return stale_df
            except Exception as _exc:
                _swallow("get_us_universe", _exc, locals().get("tkr") or locals().get("ticker"))
                continue
        raise RuntimeError("미국 유니버스를 가져오지 못했습니다.")

    exclude_ndx_from_sp = bool(cfg["market"].get("exclude_nasdaq_from_sp500", True))
    m_list, s_list, tg_list, sub_mkt_list = [], [], [], []
    for t in all_tkrs:
        m_th, s_sec, tags = classify_theme_and_sector("US", t, raw_sectors.get(t, ""), sub_inds.get(t, ""), overrides, stock_name=names.get(t, ""))
        m_list.append(m_th)
        s_list.append(s_sec)
        tg_list.append(tags)
        sub_mkt_list.append("NASDAQ100" if t in ndx_set else "SP500")

    out = pd.DataFrame(
        {
            "ticker": all_tkrs,
            "name": [names.get(t, t) for t in all_tkrs],
            "market_cap": np.nan,
            "market": "US",
            "sub_market": sub_mkt_list,
            "in_sp500": [t in sp500_set for t in all_tkrs],
            "in_nasdaq100": [t in ndx_set for t in all_tkrs],
            "exclude_ndx_in_sp": exclude_ndx_from_sp,
            "macro_theme": m_list,
            "sector": s_list,
            "theme_tags": tg_list,
            "peg": np.nan,
            "eps_growth_pct": np.nan,
            "eps_status": "MISSING",
            "universe_as_of": datetime.now(KST).strftime("%Y%m%d"),
            "is_stale_cache": False,
            "currency": "USD",
        }
    )
    out = _capture_sector_sources(out, cfg, overrides, raw_sectors=raw_sectors,
        sub_industries=sub_inds, source_urls={key: sorted(value) for key, value in sector_sources.items()},
        source_as_of=datetime.now(ZoneInfo("America/New_York")).date().isoformat())
    try:
        out.to_pickle(cache_file)
    except Exception as _exc:
        _swallow("get_us_universe", _exc, locals().get("tkr") or locals().get("ticker"))
        pass
    LOG.info("[US] 유니버스 신규 수집 완료 (총 %d종목: NASDAQ100 %d / S&P500 %d)", len(out), len(ndx_set), len(sp500_set))
    return out

def _tag_kr_base_price_basis(frame: pd.DataFrame, provider, is_index: bool) -> pd.DataFrame:
    # Preserve the existing call signature. Only attest the actual provider's
    # declared adjusted=True default, never a legacy/bulk response or an index.
    import inspect
    if not is_index:
        try:
            parameter = inspect.signature(provider).parameters.get("adjusted")
            if parameter is not None and parameter.default is True:
                frame["_base_price_basis"] = "pykrx:date:adjusted=True"
        except (ValueError, TypeError):
            pass
    return frame


def fetch_kr_history(ticker: str, years: int, is_index: bool = False, cfg: dict | None = None,
                     sync_state: dict[str, Any] | None = None) -> pd.DataFrame:
    from pykrx import stock
    t_key = str(ticker).strip() if is_index else str(ticker).strip().zfill(6)
    KR_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cpath = KR_HISTORY_CACHE_DIR / f"{'idx_' if is_index else ''}{t_key}.pkl"
    refresh = bool(cfg["runtime"].get("refresh_cache", False)) if cfg else False
    ttl = float(cfg["runtime"].get("history_cache_ttl_hours", 12)) if cfg else 12.0
    end = datetime.now(KST)
    req_start = end - timedelta(days=365 * years + 20)
    req_start_naive = pd.Timestamp(req_start.date())
    cdf = pd.DataFrame()
    repair_start = None
    rename = {"시가": "Open", "고가": "High", "저가": "Low", "종가": "Close", "거래량": "Volume", "거래대금": "Turnover"}

    if not refresh and cpath.exists():
        try:
            raw_cached = normalize_index(pd.read_pickle(cpath))
            cdf = kr_completed_cached_bars(cpath, raw_cached)
            invalid = raw_cached.index.difference(cdf.index)
            invalid = invalid[~invalid.isin(pd.to_datetime(KR_CLOSED_DATES))]
            invalid = invalid[invalid >= req_start_naive]
            if len(invalid):
                repair_start = invalid.min()
        except Exception as _exc:
            _swallow("fetch_kr_history", _exc, locals().get("tkr") or locals().get("ticker"))
            cdf = pd.DataFrame()

    if not cdf.empty and "Close" in cdf.columns:
        covers = cdf.index[0] <= (req_start_naive + pd.Timedelta(days=15))
        if covers and repair_start is None and not kr_history_needs_refresh(cpath, ttl, refresh, cdf):
            return cdf.loc[cdf.index >= req_start_naive].copy()
        if (covers and not is_index and not refresh and sync_state
                and sync_state.get("snapshot_available") is False):
            # 전체 시장 당일 응답이 없으면 수백 종목의 같은 실패 요청을 생략한다.
            return cdf.loc[cdf.index >= req_start_naive].copy()
        if covers:
            inc_start = max(req_start_naive, min(cdf.index[-1] - pd.Timedelta(days=7), repair_start) if repair_start is not None else cdf.index[-1] - pd.Timedelta(days=7))
            try:
                fn = stock.get_index_ohlcv_by_date if is_index else stock.get_market_ohlcv_by_date
                npart = retry_call(fn, inc_start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), t_key, retries=2, delay=0.6)
                if npart is not None and not npart.empty:
                    norm_new = tag_completed_sessions(normalize_index(tag_raw_price_quality(npart.rename(columns=rename))), "KR")
                    _tag_kr_base_price_basis(norm_new, fn, is_index)
                    if norm_new.empty:
                        return cdf.loc[cdf.index >= req_start_naive].copy()
                    if not has_seam_mismatch(cdf, norm_new):
                        target = kr_close_check_date()
                        if (target is not None and norm_new.index[-1] < target
                                and norm_new.index[-1] <= cdf.index[-1]):
                            # 전일 겹침만 반환되면 mtime을 최신으로 만들지 않는다.
                            return cdf.loc[cdf.index >= req_start_naive].copy()
                        merged = normalize_index(pd.concat([cdf, norm_new]))
                        merged.to_pickle(cpath)
                        return kr_completed_cached_bars(cpath, merged).loc[lambda x: x.index >= req_start_naive].copy()
                    LOG.info("[%s] 수정주가 변경(이음새 괴리) 감지 -> 전체 기간 재다운로드", t_key)
                    cdf = pd.DataFrame()
                else:
                    # 증분 응답이 없다는 이유로 동일 공급자에 전체 기간을 다시 요청하지 않는다.
                    return cdf.loc[cdf.index >= req_start_naive].copy()
            except Exception as _exc:
                _swallow("fetch_kr_history", _exc, locals().get("tkr") or locals().get("ticker"))
                return cdf.loc[cdf.index >= req_start_naive].copy()

    try:
        fn = stock.get_index_ohlcv_by_date if is_index else stock.get_market_ohlcv_by_date
        df = retry_call(fn, req_start_naive.strftime("%Y%m%d"), end.strftime("%Y%m%d"), t_key, retries=3, delay=0.8)
    except Exception as _exc:
        _swallow("fetch_kr_history", _exc, locals().get("tkr") or locals().get("ticker"))
        df = None
    if df is None or df.empty:
        return cdf.loc[cdf.index >= req_start_naive].copy() if not cdf.empty else pd.DataFrame()

    res = tag_completed_sessions(normalize_index(tag_raw_price_quality(df.rename(columns=rename))), "KR")
    _tag_kr_base_price_basis(res, fn, is_index)
    if not cdf.empty and not has_seam_mismatch(cdf, res):
        res = normalize_index(pd.concat([cdf, res]))
    if not res.empty:
        try:
            res.to_pickle(cpath)
        except Exception as _exc:
            _swallow("fetch_kr_history", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    return kr_completed_cached_bars(cpath, res).loc[lambda x: x.index >= req_start_naive].copy()

def fetch_us_histories(tickers: list[str], years: int, cfg: dict | None = None) -> dict[str, pd.DataFrame]:
    import yfinance as yf
    if not tickers:
        return {}
    US_HISTORY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    refresh = bool(cfg["runtime"].get("refresh_cache", False)) if cfg else False
    ttl = float(cfg["runtime"].get("history_cache_ttl_hours", 12)) if cfg else 12.0
    req_start_ts = pd.Timestamp(datetime.now() - timedelta(days=365 * years + 30)).normalize()
    close_target = us_close_check_date()

    result, cached_map = {}, {}
    full_tkrs, inc_tkrs = [], []
    earliest_inc: pd.Timestamp | None = None

    for tkr in tickers:
        cp = US_HISTORY_CACHE_DIR / f"{tkr}.pkl"
        if not refresh and cp.exists():
            try:
                cdf = us_completed_cached_bars(cp, normalize_index(pd.read_pickle(cp)))
                if not cdf.empty and "Close" in cdf.columns:
                    cached_map[tkr] = cdf
                    covers = cdf.index[0] <= (req_start_ts + pd.Timedelta(days=15))
                    if (covers and is_cache_fresh(cp, ttl, refresh)
                            and (close_target is None or cdf.index[-1] >= close_target)):
                        result[tkr] = cdf.loc[cdf.index >= req_start_ts].copy()
                        continue
                    if covers:
                        inc_tkrs.append(tkr)
                        ldt = cdf.index[-1] - pd.Timedelta(days=7)
                        earliest_inc = ldt if earliest_inc is None or ldt < earliest_inc else earliest_inc
                        continue
            except Exception as _exc:
                _swallow("fetch_us_histories", _exc, locals().get("tkr") or locals().get("ticker"))
                pass
        full_tkrs.append(tkr)

    def _extract_yf_df(raw: pd.DataFrame, tkr: str, batch_size: int) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame()
        if isinstance(raw.columns, pd.MultiIndex):
            for level in range(raw.columns.nlevels):
                values = raw.columns.get_level_values(level)
                if tkr in values:
                    try:
                        out = raw.xs(tkr, axis=1, level=level, drop_level=True).copy()
                        if isinstance(out.columns, pd.MultiIndex):
                            out.columns = [
                                next((str(v) for v in col if str(v) in {"Open", "High", "Low", "Close", "Adj Close", "Volume", "Dividends", "Stock Splits"}), str(col[-1]))
                                for col in out.columns
                            ]
                        return out
                    except (KeyError, ValueError):
                        pass
            if batch_size == 1:
                out = raw.copy()
                out.columns = [str(c[-1] if isinstance(c, tuple) else c) for c in out.columns]
                return out
            return pd.DataFrame()
        return raw.copy() if batch_size == 1 else pd.DataFrame()

    def _dl_chunks(t_list: list[str], s_str: str, is_incremental: bool = False) -> list[str]:
        seam_refetch_tkrs: list[str] = []
        for i in range(0, len(t_list), 80):
            batch = t_list[i : i + 80]
            try:
                raw = retry_call(yf.download, tickers=batch, start=s_str, auto_adjust=True, group_by="ticker", progress=False, threads=True, timeout=25, retries=2)
            except Exception as exc:
                LOG.warning("[US] yfinance 배치 다운로드 오류 (%d종목): %s", len(batch), exc)
                continue
            for tkr in batch:
                try:
                    df = _extract_yf_df(raw, tkr, len(batch))
                    cleaned = tag_completed_sessions(normalize_index(tag_raw_price_quality(df)), "US")
                    cleaned["_base_price_basis"] = "yfinance:auto_adjust=True"
                    if cleaned.empty:
                        continue
                    if is_incremental and tkr in cached_map:
                        if (close_target is not None and cleaned.index[-1] < close_target
                                and cleaned.index[-1] <= cached_map[tkr].index[-1]):
                            result[tkr] = cached_map[tkr].loc[cached_map[tkr].index >= req_start_ts].copy()
                            continue  # 전일 겹침만 반환되면 정상 캐시와 mtime을 보존한다.
                        if has_seam_mismatch(cached_map[tkr], cleaned):
                            _resc = rescale_cached_for_dividend(cached_map[tkr], cleaned)
                            if _resc is None:
                                seam_refetch_tkrs.append(tkr)
                                cached_map.pop(tkr, None)
                                continue
                            cached_map[tkr] = _resc
                        cleaned = normalize_index(pd.concat([cached_map[tkr], cleaned]))
                    cleaned.to_pickle(US_HISTORY_CACHE_DIR / f"{tkr}.pkl")
                    result[tkr] = us_completed_cached_bars(US_HISTORY_CACHE_DIR / f"{tkr}.pkl", cleaned).loc[lambda x: x.index >= req_start_ts].copy()
                except Exception as _exc:
                    _swallow("_dl_chunks", _exc, locals().get("tkr") or locals().get("ticker"))
                    continue
        return seam_refetch_tkrs

    if inc_tkrs and earliest_inc is not None:
        refetch_needed = _dl_chunks(inc_tkrs, max(req_start_ts, earliest_inc).strftime("%Y-%m-%d"), is_incremental=True)
        full_tkrs.extend(refetch_needed)
    if full_tkrs:
        _dl_chunks(list(dict.fromkeys(full_tkrs)), req_start_ts.strftime("%Y-%m-%d"), is_incremental=False)
    for tkr in tickers:
        if tkr not in result and tkr in cached_map:
            result[tkr] = cached_map[tkr].loc[cached_map[tkr].index >= req_start_ts].copy()
    return result

def _fetch_single_us_cap(ticker: str, min_cap: float = 10_000_000_000, pause: float = 0.05,
                         lookup_status: dict[str, bool] | None = None) -> tuple[str, float | None, str, float, float]:
    import yfinance as yf
    if pause > 0:
        time.sleep(min(pause, 0.08))
    t = yf.Ticker(ticker)
    cap, sec, peg, eps_g = None, "", np.nan, np.nan
    if lookup_status is not None:
        lookup_status[ticker] = False
    try:
        fi = t.fast_info
        cap = fi.get("marketCap") if hasattr(fi, "get") else getattr(fi, "market_cap", None)
    except Exception as _exc:
        _swallow("_fetch_single_us_cap", _exc, locals().get("tkr") or locals().get("ticker"))
        cap = None
        if type(_exc).__name__ == "YFRateLimitError":
            # 같은 종목의 즉시 get_info 재요청도 제한된다. 기존 캐시로 폴백한다.
            return ticker, None, sec, peg, eps_g

    if cap is not None and float(cap) < min_cap:
        if lookup_status is not None:
            lookup_status[ticker] = True  # 시총 하한 미달은 재무정보 요청이 필요 없다.
        return ticker, float(cap), sec, peg, eps_g

    try:
        info = t.get_info()
        if cap is None:
            cap = info.get("marketCap")
        if cap is not None and float(cap) < min_cap:
            if lookup_status is not None:
                lookup_status[ticker] = True
            return ticker, float(cap), sec, peg, eps_g
        sec = str(info.get("sector") or "").strip()
        raw_peg = info.get("pegRatio") or info.get("trailingPegRatio")
        if raw_peg is not None and float(raw_peg) > 0:
            peg = round(float(raw_peg), 2)
        raw_eg = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
        if raw_eg is not None:
            eps_g = round(float(raw_eg) * 100.0, 1)
        if lookup_status is not None:
            lookup_status[ticker] = True
    except Exception as _exc:
        _swallow("_fetch_single_us_cap", _exc, locals().get("tkr") or locals().get("ticker"))
        pass
    return ticker, (float(cap) if cap is not None else None), sec, peg, eps_g

def update_us_cap_cache_entry(
    crec: dict[str, dict[str, Any]],
    tkr: str,
    cap: float | None,
    sec: str,
    peg: float,
    eg: float,
    now_ts: float,
    fundamentals_ok: bool = True,
) -> float | None:
    """[P0-3.2] 시가총액 조회 실패(cap=None 또는 <=0) 결과를 정상 최신 캐시로 저장하지 않고 이전 정상 캐시가 있으면 보존"""
    prev = crec.get(tkr)
    if not fundamentals_ok and prev and float(prev.get("cap") or 0) > 0:
        # fast_info만 성공하고 get_info가 제한된 경우도 정상 재무정보와 기준시각을 보존.
        return float(prev["cap"])
    if cap is not None and float(cap) > 0:
        crec[tkr] = {"cap": float(cap), "sector": sec, "peg": peg, "eps_g": eg,
                     "ts": now_ts if fundamentals_ok else 0, "ok": True}
        return float(cap)
    if prev and prev.get("cap") is not None and float(prev["cap"]) > 0:
        return float(prev["cap"])
    crec.pop(tkr, None)
    return None

def get_us_market_caps(
    tickers: list[str], min_cap_floor: float, pause: float, cfg: dict | None = None,
    health: dict[str, Any] | None = None,
) -> tuple[dict[str, float], dict[str, str], dict[str, float], dict[str, float]]:
    UNIVERSE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cfile = UNIVERSE_CACHE_DIR / "us_caps_sectors_v15.pkl"
    refresh = bool(cfg["runtime"].get("refresh_cache", False)) if cfg else False
    ttl = float(cfg["runtime"].get("universe_cache_ttl_hours", 24)) if cfg else 24.0
    crec: dict[str, dict[str, Any]] = {}
    if not refresh and cfile.exists():
        try:
            crec = pd.read_pickle(cfile) or {}
        except Exception as _exc:
            _swallow("get_us_market_caps", _exc, locals().get("tkr") or locals().get("ticker"))
            crec = {}

    now_ts = time.time()
    caps, secs, pegs, eps_gs = {}, {}, {}, {}
    stats = {"us_cap_requested": 0, "us_cap_fallback": 0, "us_cap_missing": 0}
    to_q = []
    for tkr in tickers:
        r = crec.get(tkr)
        cval = r.get("cap") if r else None
        if r and cval is not None and float(cval) > 0 and (now_ts - float(r.get("ts", 0))) < ttl * 3600:
            if float(cval) >= min_cap_floor:
                caps[tkr] = float(cval)
            if r.get("sector"):
                secs[tkr] = str(r["sector"])
            pegs[tkr] = float(r.get("peg", np.nan))
            eps_gs[tkr] = float(r.get("eps_g", np.nan))
        else:
            to_q.append(tkr)

    if to_q:
        stats["us_cap_requested"] = len(to_q)
        lookup_status: dict[str, bool] = {}
        max_w = 5 if pause <= 0.2 else 3
        with ThreadPoolExecutor(max_workers=max_w) as pool:
            futs = {pool.submit(_fetch_single_us_cap, t, min_cap_floor, pause, lookup_status): t for t in to_q}
            for idx, fut in enumerate(as_completed(futs), 1):
                tkr = futs[fut]
                try:
                    tkr, cap, sec, peg, eg = fut.result()
                    fundamentals_ok = lookup_status.get(tkr, True)
                    had_previous = bool(crec.get(tkr) and float(crec[tkr].get("cap") or 0) > 0)
                    eff_cap = update_us_cap_cache_entry(crec, tkr, cap, sec, peg, eg, now_ts, fundamentals_ok)
                    if eff_cap is None:
                        stats["us_cap_missing"] += 1
                    if had_previous and (cap is None or cap <= 0 or not fundamentals_ok):
                        stats["us_cap_fallback"] += 1
                        previous = crec.get(tkr, {})
                        sec, peg, eg = previous.get("sector", ""), previous.get("peg", np.nan), previous.get("eps_g", np.nan)
                    if eff_cap is not None and eff_cap >= min_cap_floor:
                        caps[tkr] = eff_cap
                    if sec:
                        secs[tkr] = sec
                    pegs[tkr] = peg
                    eps_gs[tkr] = eg
                except Exception as _exc:
                    _swallow("get_us_market_caps", _exc, locals().get("tkr") or locals().get("ticker"))
                    eff_cap = update_us_cap_cache_entry(crec, tkr, None, "", np.nan, np.nan, now_ts)
                    stats["us_cap_fallback" if eff_cap is not None else "us_cap_missing"] += 1
                    if eff_cap is not None and eff_cap >= min_cap_floor:
                        caps[tkr] = eff_cap
                    previous = crec.get(tkr, {})
                    if previous.get("sector"):
                        secs[tkr] = previous["sector"]
                    pegs[tkr] = previous.get("peg", np.nan)
                    eps_gs[tkr] = previous.get("eps_g", np.nan)
                if idx % 25 == 0 or idx == len(to_q):
                    log_progress(5 + int(25 * idx / max(len(to_q), 1)), "US 시가총액·PEG 확인", f"{idx}/{len(to_q)} 확인")
        try:
            pd.to_pickle(crec, cfile)
        except Exception as _exc:
            _swallow("get_us_market_caps", _exc, locals().get("tkr") or locals().get("ticker"))
            pass
    stats["us_fundamentals_missing"] = sum(pd.isna(pegs.get(tkr)) or pd.isna(eps_gs.get(tkr)) for tkr in caps)
    if health is not None:
        health.update(stats)
    if stats["us_cap_fallback"] or stats["us_cap_missing"]:
        LOG.warning("[US] 시가총액 조회 %d종목: 이전 정상 캐시 사용 %d · 유효 시총 없음 %d (최신 조회로 표시하지 않음)",
                    stats["us_cap_requested"], stats["us_cap_fallback"], stats["us_cap_missing"])
    return caps, secs, pegs, eps_gs

def _extract_earnings_dates(cal: Any) -> list[Any]:
    """yfinance calendar가 dict, DataFrame, Series 어떤 형태로 반환되어도 날짜 후보를 추출한다."""
    if cal is None:
        return []
    dt_candidates: list[Any] = []
    if isinstance(cal, dict):
        ed = cal.get("Earnings Date") or cal.get("Earnings High") or cal.get("Earnings Low")
        if isinstance(ed, (list, tuple, set, pd.Series, np.ndarray)):
            dt_candidates.extend(list(ed))
        elif ed is not None:
            dt_candidates.append(ed)
    elif isinstance(cal, pd.DataFrame) and not cal.empty:
        for idx_lbl in cal.index:
            if "earnings" in str(idx_lbl).lower():
                vals = cal.loc[idx_lbl]
                if isinstance(vals, (pd.Series, pd.DataFrame)):
                    dt_candidates.extend(vals.values.flatten().tolist())
                else:
                    dt_candidates.append(vals)
        for col_lbl in cal.columns:
            if "earnings" in str(col_lbl).lower():
                dt_candidates.extend(cal[col_lbl].dropna().tolist())
        if not dt_candidates:
            dt_candidates.extend(cal.values.flatten().tolist())
    return dt_candidates

def fetch_us_earnings_dates(tickers: list[str], cfg: dict) -> dict[str, tuple[str, int | None]]:
    """반환값: {ticker: (earnings_tag, days_until_earnings)}"""
    if not tickers or not cfg.get("reports", {}).get("check_us_earnings", True):
        return {}
    import yfinance as yf
    today_d = datetime.now(ZoneInfo("America/New_York")).date()
    def _get_one(tkr: str) -> tuple[str, str, int | None]:
        try:
            tk = yf.Ticker(tkr)
            dt_candidates = _extract_earnings_dates(tk.calendar)
            future_diffs: list[tuple[int, Any]] = []
            for d_obj in dt_candidates:
                if d_obj is None or pd.isna(d_obj):
                    continue
                try:
                    d_val = pd.Timestamp(d_obj).date()
                except Exception:
                    continue
                diff = (d_val - today_d).days
                if 0 <= diff <= 30:
                    future_diffs.append((diff, d_val))
            if future_diffs:
                future_diffs.sort(key=lambda x: x[0])
                diff, d_val = future_diffs[0]
                if diff <= 3:
                    return tkr, f"🚨실적D-{diff}({d_val.strftime('%m/%d')}·진입보류)", diff
                if diff <= 7:
                    return tkr, f"⚠️실적D-{diff}({d_val.strftime('%m/%d')}·비중50%)", diff
                if diff <= 21:
                    return tkr, f"실적D-{diff}({d_val.strftime('%m/%d')})", diff
        except Exception as _exc:
            _swallow("_get_one", _exc, locals().get("tkr") or locals().get("ticker"))
        return tkr, "", None

    out: dict[str, tuple[str, int | None]] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for tkr, tag, diff in pool.map(_get_one, list(dict.fromkeys(tickers))):
            if tag or diff is not None:
                out[tkr] = (tag, diff)
    return out

def apply_earnings_sizing_rules(df: pd.DataFrame, earn_map: dict[str, tuple[str, int | None]], market: str = "US") -> pd.DataFrame:
    """실적 D-3 이내는 신규 매수 적격(is_actionable)에서 제외하고, D-4~7은 권장 비중을 50% 축소한다. 한국은 실적 미확인 태그를 명시한다."""
    if df is None or df.empty:
        return df
    out = df.copy()
    if market.upper() == "KR":
        cur_tags = out.get("earnings_tag", pd.Series("", index=out.index)).fillna("").astype(str)
        out["earnings_tag"] = [t if t.strip() else "실적미확인" for t in cur_tags]
        out["earnings_days"] = np.nan
        return out

    tags, days_list = [], []
    for r in out.itertuples():
        tkr = str(r.ticker)
        val = earn_map.get(tkr, ("", None))
        if isinstance(val, tuple):
            tag, diff = val
        else:
            tag, diff = str(val), None
        tags.append(tag)
        days_list.append(diff if diff is not None else np.nan)

    out["earnings_tag"] = tags
    out["earnings_days"] = days_list

    d3_mask = out["earnings_days"].notna() & (out["earnings_days"] <= 3)
    if d3_mask.any():
        out.loc[d3_mask, "is_actionable"] = False

    d7_mask = out["earnings_days"].notna() & (out["earnings_days"] >= 4) & (out["earnings_days"] <= 7)
    if d7_mask.any():
        out.loc[d7_mask, "position_size_pct"] = (pd.to_numeric(out.loc[d7_mask, "position_size_pct"], errors="coerce") * 0.5).round(1)
        if "trade_risk_pct" in out.columns:
            out.loc[d7_mask, "trade_risk_pct"] = (pd.to_numeric(out.loc[d7_mask, "trade_risk_pct"], errors="coerce") * 0.5).round(2)
    return out

# =====================================================================
# 4. 지표 계산 & 탑다운 섹터 파워 / 로테이션 집계
# =====================================================================

_REGIME_PARAMS: dict[str, float] = {"band": 0.01, "confirm_days": 2, "hard_pct": 0.05, "sigma_mult": 0.75, "attack_max": 2, "collapse_min": 6}

def set_regime_params(cfg: dict) -> None:
    mr = cfg.get("market_regime", {})
    _REGIME_PARAMS["band"] = float(mr.get("risk_off_band_pct", 0.01))
    _REGIME_PARAMS["confirm_days"] = int(mr.get("risk_off_confirm_days", 2))
    _REGIME_PARAMS["hard_pct"] = float(mr.get("risk_off_hard_pct", 0.05))
    _REGIME_PARAMS["sigma_mult"] = float(mr.get("dist_down_sigma_mult", 0.75))
    _REGIME_PARAMS["attack_max"] = int(mr.get("dist_attack_max", 2))
    _REGIME_PARAMS["collapse_min"] = int(mr.get("dist_collapse_min", 6))

def _hysteresis_risk_off(close: pd.Series, below_200: bool = True, band: float = 0.01, confirm: int = 2, hard_pct: float = 0.05) -> tuple[bool, int]:
    """200일선 밴드+확인일 히스테리시스.
    진입: 종가 < 200선*(1-band) 가 confirm일 연속(또는 200선*(1-hard_pct) 아래면 즉시) -> RISK_OFF
    해제: 종가 > 200선*(1+band) 가 confirm일 연속 -> 해제. 반환: (현재 RISK_OFF 여부, 현재 상태의 연속 조건일수)"""
    sma200 = close.rolling(200).mean()
    sma50 = close.rolling(50).mean()
    c, s200, s50 = close.to_numpy(float), sma200.to_numpy(float), sma50.to_numpy(float)
    valid = np.where(~np.isnan(s200))[0]
    if len(valid) == 0:
        return False, 0
    t0 = int(valid[0])
    state = False
    streak = 0
    for t in range(t0, len(c)):
        if not state:
            below = c[t] < s200[t] * (1.0 - band) and (below_200 or s50[t] < s200[t])
            streak = streak + 1 if below else 0
            if streak >= confirm or c[t] < s200[t] * (1.0 - hard_pct):
                state, streak = True, 0
        else:
            above = c[t] > s200[t] * (1.0 + band)
            streak = streak + 1 if above else 0
            if streak >= confirm:
                state, streak = False, 0
    return state, streak

def _ftd_observation(history: pd.DataFrame, min_gain: float, min_dd: float, market: str | None) -> dict:
    """As-of snapshot of the first qualifying candidate after the selected swing low."""
    cols = ["High", "Low", "Close", "Volume"]
    if history is None or len(history) < 40 or not set(cols).issubset(history):
        return {"status": "unverified", "reason": "insufficient_bars"}
    lookback = history.tail(60)
    values = lookback[cols].apply(pd.to_numeric, errors="coerce")
    if (not isinstance(lookback.index, pd.DatetimeIndex) or not lookback.index.is_monotonic_increasing
            or not lookback.index.is_unique or not np.isfinite(values).all().all()
            or values.le(0).any().any() or not values.Close.between(values.Low, values.High).all()):
        return {"status": "unverified", "reason": "invalid_bars"}
    if market:
        for field in ("_session_confirmed", "_raw_ohlc_valid"):
            if field not in lookback or not lookback[field].eq(True).fillna(False).all():
                return {"status": "unverified", "reason": field.lstrip("_")}
        calendar = get_calendar(market)
        try:
            sessions = calendar.sessions_in_range(lookback.index[0], lookback.index[-1])
        except (TypeError, ValueError):
            return {"status": "unverified", "reason": "calendar_coverage"}
        if not sessions.equals(lookback.index):
            return {"status": "unverified", "reason": "session_gap"}
    lookback = values
    rec = lookback.tail(30)
    closes, lows, vols = (rec[key].to_numpy(float) for key in ("Close", "Low", "Volume"))
    dates = rec.index

    low_idx = int(np.argmin(lows[:-3]))
    rally_low = float(lows[low_idx])
    low_dt = dates[low_idx]

    prior_slice = lookback.loc[lookback.index <= low_dt]
    prior_high = float(prior_slice["High"].max())
    if rally_low > prior_high * (1.0 - min_dd):
        return {"status": "not_detected"}

    if closes[-1] < rally_low:
        return {"status": "not_detected"}

    first_ftd_idx = low_idx + 4
    last_ftd_idx = min(low_idx + 12, len(closes) - 1)
    for i in range(first_ftd_idx, last_ftd_idx + 1):
        ret = (closes[i] / closes[i - 1]) - 1.0
        vol_up = vols[i] > vols[i - 1]
        if closes[i] >= closes[i - 1] * (1.0 + min_gain) and vol_up:
            if (len(closes) - 1 - i) <= 10 and float(np.min(lows[i:])) >= lows[i] * 0.99:
                return {"status": "detected", "candidate_date": dates[i].date().isoformat(),
                        "swing_date": low_dt.date().isoformat(), "swing_low": rally_low,
                        "sessions_after_low": i - low_idx,
                        "gain_pct": round(float(ret) * 100, 3),
                        "volume_ratio": round(float(vols[i] / vols[i - 1]), 3)}
            return {"status": "not_detected"}
    return {"status": "not_detected"}

def _detect_ftd(history: pd.DataFrame, min_gain: float = 0.0125, min_dd: float = 0.08) -> tuple[bool, str]:
    """Numerical rule for a supplied bar series. Production also checks session evidence."""
    observation = _ftd_observation(history, min_gain, min_dd, None)
    date = observation.get("candidate_date")
    return (True, pd.Timestamp(date).strftime("%m/%d")) if date else (False, "")


def _benchmark_session_quality(history: pd.DataFrame, market: str, evaluated_at: datetime) -> tuple[str, str | None]:
    """Check the latest closed session at this run's cutoff with the acquisition buffer."""
    cutoff = pd.Timestamp(evaluated_at)
    local_day = cutoff.tz_convert(KST if market == "KR" else "America/New_York").tz_localize(None).normalize()
    calendar = get_calendar(market)
    try:
        last = calendar.date_to_session(local_day, direction="previous")
        sessions = calendar.sessions_in_range(calendar.session_offset(last, -26), last)
        ready = session_ready_times(sessions, market)
        complete = sessions[ready.le(cutoff).to_numpy()]
        if len(complete) < 26 or pd.isna(ready.iloc[-1]):
            return "calendar_unverified", None
    except (IndexError, ValueError):
        return "calendar_unverified", None
    expected = complete[-1].date().isoformat()
    recent = history.tail(26)
    if recent.index[0] < calendar.first_session or recent.index[-1] > calendar.last_session:
        return "calendar_unverified", expected
    if not recent.index.equals(calendar.sessions_in_range(recent.index[0], recent.index[-1])):
        return "distribution_session_gap", expected
    if "_session_confirmed" not in recent or not recent["_session_confirmed"].eq(True).fillna(False).all():
        return "unconfirmed_bars", expected
    if history.index[-1] != complete[-1]:
        return "benchmark_date_mismatch", expected
    return "ok", expected


def calc_market_regime(
    history: pd.DataFrame,
    below_200: bool = True,
    bench_name: str = "",
    breadth_info: dict[str, Any] | None = None,
    *, market: str | None = None, evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    b_info = breadth_info or {}
    close = (pd.to_numeric(history["Close"], errors="coerce") if history is not None and "Close" in history
             else pd.Series(dtype=float))
    as_of_str = close.index[-1].date().isoformat() if not close.empty else None
    ftd = (_ftd_observation(history, 0.0125, 0.08, market) if market in ("US", "KR")
           else {"status": "unverified", "reason": "market_unspecified"})
    ftd["version"] = "swing-ftd-v1.1"
    if len(close) < 200 or not np.isfinite(close).all() or not close.gt(0).all():
        return {"regime": "CAUTION", "regime_quality": "insufficient_price",
                "signal_light": "🟡 CAUTION (지수데이터부족 · 신규배분 보류)", "benchmark": bench_name,
                "as_of": as_of_str, "benchmark_close": np.nan, "benchmark_sma200": np.nan, "ftd": ftd, **b_info}
    sma20, sma50, sma200 = close.rolling(20).mean().iloc[-1], close.rolling(50).mean().iloc[-1], close.rolling(200).mean().iloc[-1]
    last = close.iloc[-1]
    rp = _REGIME_PARAMS
    risk_off, _ = _hysteresis_risk_off(close, below_200, rp["band"], int(rp["confirm_days"]), rp["hard_pct"])
    below_sma200_now = bool(last < sma200)
    quality, expected = (_benchmark_session_quality(history, market, evaluated_at)
                         if market and evaluated_at is not None else ("ok", None))
    dist_days = None
    volumes = pd.to_numeric(history.tail(26).get("Volume", pd.Series(dtype=float)), errors="coerce")
    valid_volume = len(volumes) == 26 and np.isfinite(volumes).all() and volumes.gt(0).all()
    if valid_volume and quality in ("ok", "benchmark_date_mismatch"):
        c_arr = close.tail(26).reset_index(drop=True)
        # 하락 기준: 최근 변동성(직전 60일 일간수익률 표준편차)의 sigma_mult배, 단 고전 기준(-0.2%)보다 느슨하지 않게.
        # 랜덤워크 귀무모형에서 25일 평균 분배일 약 2.1일(고전기준 약 4.2일) -> 임계값의 우연 발동확률 3~8%
        _ret_hist = close.pct_change()
        _sig = float(_ret_hist.iloc[-85:-25].std()) if len(_ret_hist) >= 100 else 0.01
        _thr = min(-0.002, -float(rp["sigma_mult"]) * _sig) if (np.isfinite(_sig) and rp["sigma_mult"] > 0) else -0.002
        dist_flags = (c_arr.pct_change() <= _thr) & (volumes.reset_index(drop=True).diff() > 0)
        # 분배일별 소멸: 해당 분배일 종가 대비 +5% 이상 종가 도달 시 개별 제거 (O'Neil)
        dist_days = int(sum(
            bool(dist_flags.iloc[j]) and not (j + 1 < len(c_arr) and float(c_arr.iloc[j + 1:].max()) >= float(c_arr.iloc[j]) * 1.05)
            for j in range(1, len(c_arr))
        ))

    if dist_days is None and quality == "ok":
        quality = "insufficient_volume"

    breadth_valid = bool(b_info.get("breadth_valid", False)) and pd.notna(b_info.get("pct_above_50dma", np.nan))
    pct_above_50 = float(b_info.get("pct_above_50dma")) if breadth_valid else np.nan
    breadth_collapse = bool(breadth_valid and dist_days is not None and dist_days >= int(rp["collapse_min"]) and pct_above_50 < 25.0)
    eff_risk_off = bool(risk_off or breadth_collapse)
    if eff_risk_off:
        why = f"200일선 하회 확정(±{rp['band']*100:.0f}%밴드·{int(rp['confirm_days'])}일확인)" if risk_off else f"내부붕괴(분배{dist_days}일·50일상회{pct_above_50:.0f}%)"
        sig = f"🔴 RISK_OFF ({why} · 권장비중 50% 축소)"
        reg_state = "RISK_OFF"
    elif dist_days is None:
        sig = "🟡 CAUTION (분배일 미확인 · 신규배분 보류)"
        reg_state = "CAUTION"
    elif not breadth_valid:
        sig = f"🟡 CAUTION (시장 내부체력 데이터 부족: {b_info.get('breadth_status', '표본부족')} · 리스크 0.75% 축소)"
        reg_state = "CAUTION"
    elif last >= sma50 >= sma200 and dist_days <= int(rp["attack_max"]) and pct_above_50 >= 45.0:
        sig = "🟢 ATTACK (정배열·체력양호 · 주도주 적극 공략)"
        reg_state = "ATTACK"
    else:
        sig = "🟡 CAUTION (200일선 하회 관찰중 · 확정 전 · 리스크 0.75% 축소)" if below_sma200_now else "🟡 CAUTION (선별 접근 · 추격매수 금지 · 리스크 0.75%)"
        reg_state = "CAUTION"
    if quality != "ok" and "신규배분 보류" not in sig:
        sig += " · 벤치마크 자료 확인 필요 · 신규배분 보류"
    if ftd["status"] == "detected" and reg_state in ("RISK_OFF", "CAUTION"):
        ftd_dt = pd.Timestamp(ftd["candidate_date"]).strftime("%m/%d")
        sig = f"{sig} · 🌱FTD확인({ftd_dt})"

    return {
        "regime": reg_state,
        "regime_quality": quality,
        "expected_as_of": expected,
        "ftd": ftd,
        "benchmark": bench_name,
        "as_of": as_of_str,
        "signal_light": sig,
        "distribution_days_25d": dist_days,
        "below_sma200_now": below_sma200_now,
        "benchmark_close": float(last),
        "benchmark_sma20": float(sma20),
        "benchmark_sma50": float(sma50),
        "benchmark_sma200": float(sma200),
        **b_info,
    }

def compute_universe_breadth(raw_rows: list[dict[str, Any]], expected_total: int | None = None, benchmark_as_of: str | None = None) -> dict[str, Any]:
    """유니버스 내부 체력. 기대 유니버스 모수 대비 유효 지표 표본과 커버리지가 충분할 때만 breadth를 산출한다."""
    rows = list(raw_rows or [])
    total = max(len(rows), int(expected_total or 0))

    def _bool_value(value: Any) -> bool | None:
        if value is None or pd.isna(value):
            return None
        if isinstance(value, str):
            v = value.strip().lower()
            if v in {"true", "1", "yes", "y"}:
                return True
            if v in {"false", "0", "no", "n"}:
                return False
            return None
        return bool(value)

    valid_rows = []
    for row in rows:
        if benchmark_as_of is not None:
            try:
                aligned = bool(benchmark_as_of) and pd.Timestamp(row.get("as_of")).normalize() == pd.Timestamp(benchmark_as_of).normalize()
            except (ValueError, TypeError):
                aligned = False
            if not aligned:
                continue
        b50 = _bool_value(row.get("above_50dma"))
        b200 = _bool_value(row.get("above_200dma"))
        if b50 is not None and b200 is not None:
            valid_rows.append((row, b50, b200))

    valid_count = len(valid_rows)
    coverage_pct = (valid_count / total * 100.0) if total else 0.0
    min_sample, min_coverage = 30, 60.0
    valid = valid_count >= min_sample and coverage_pct >= min_coverage
    if not valid:
        status = "표본없음" if total == 0 else f"유효표본부족({valid_count}/{min_sample}, 커버리지 {coverage_pct:.0f}%)"
        return {"breadth_total": total, "breadth_valid_count": valid_count,
                "breadth_coverage_pct": round(coverage_pct, 1), "breadth_valid": False,
                "breadth_status": status, "pct_above_50dma": np.nan,
                "pct_above_200dma": np.nan, "nh_nl_label": "산출불가(표본부족)", "tier_a_total": 0}

    ab50 = sum(1 for _, b50, _ in valid_rows if b50)
    ab200 = sum(1 for _, _, b200 in valid_rows if b200)
    nh = nl = tier_a = 0
    for row, _, _ in valid_rows:
        try:
            high_dist = float(row.get("distance_from_high_pct", np.nan))
            if np.isfinite(high_dist) and high_dist >= -0.03:
                nh += 1
        except (TypeError, ValueError):
            pass
        try:
            low_dist = float(row.get("distance_from_low_pct", np.nan))
            if np.isfinite(low_dist) and low_dist <= 0.03:
                nl += 1
        except (TypeError, ValueError):
            pass
        if _bool_value(row.get("strict_minervini")) is True:
            tier_a += 1

    return {"breadth_total": total, "breadth_valid_count": valid_count,
            "breadth_coverage_pct": round(coverage_pct, 1), "breadth_valid": True,
            "breadth_status": "정상", "pct_above_50dma": round(ab50 / valid_count * 100.0, 1),
            "pct_above_200dma": round(ab200 / valid_count * 100.0, 1),
            "nh_nl_label": f"고점 3% 근접 {nh} / 저점 3% 근접 {nl}", "tier_a_total": tier_a}

def _detect_swing_vcp_and_base(
    hist: pd.DataFrame, atr14_pct: float, atr_contracting: bool, has_recent_halt: bool
) -> tuple[bool, bool, int, float, float]:
    """고점 이후 스윙 파동(반등 3% 이상 시 파동 종료)의 낙폭 수축 여부, 최소 베이스 길이(25일),
    단기 20일 고가(high_20d)와 베이스 피벗 고가(base_pivot_price: 최근 15~40일 중 거래량 건조 구간을 거친 베이스 저항 고점)를 산출한다."""
    if hist is None or len(hist) < 25:
        p20 = hist.iloc[:-1] if (hist is not None and len(hist) > 1) else hist
        h20 = float(p20["High"].max()) if (p20 is not None and not p20.empty and "High" in p20) else 0.0
        return False, False, 0, h20, h20

    prior_20d = hist.iloc[-21:-1] if len(hist) >= 21 else hist.iloc[:-1]
    high_20d = float(prior_20d["High"].max()) if (not prior_20d.empty and "High" in prior_20d) else float(prior_20d["Close"].max())

    # 최근 15~45봉 베이스 구간 탐색
    win45 = hist.iloc[-46:-1] if len(hist) >= 46 else hist.iloc[:-1]
    highs45 = win45["High"].to_numpy(float) if "High" in win45 else win45["Close"].to_numpy(float)
    closes45 = win45["Close"].to_numpy(float)
    lows45 = win45["Low"].to_numpy(float) if "Low" in win45 else closes45

    peak_idx = int(np.argmax(highs45))
    base_len_days = len(closes45) - peak_idx

    # 베이스 피벗(base_pivot_price): 최근 15~40일 구간 내 거래량 건조/수축이 동반된 베이스 상단 고점
    win_base = hist.iloc[-41:-1] if len(hist) >= 41 else hist.iloc[:-1]
    if len(win_base) >= 15 and "Volume" in win_base.columns:
        v5 = win_base["Volume"].rolling(5).mean()
        v50 = hist["Volume"].rolling(50).mean().reindex(win_base.index).bfill()
        dry_days = (v5 <= v50 * 0.85).fillna(False)
        if bool(dry_days.iloc[-15:].any()):
            base_pivot_price = float(win_base["High"].max()) if "High" in win_base else float(win_base["Close"].max())
        else:
            base_pivot_price = float(win_base.iloc[:-5]["High"].max()) if len(win_base) > 15 and "High" in win_base else high_20d
    else:
        base_pivot_price = high_20d

    # 스윙 파동 추출 (고점 이후 반등 +3.0% 이상이면 하나의 수축 파동 완료)
    swing_dds: list[float] = []
    if base_len_days >= 12:
        c_post = closes45[peak_idx:]
        l_post = lows45[peak_idx:]
        h_post = highs45[peak_idx:]
        cur_hi = float(h_post[0])
        cur_lo = float(l_post[0])
        in_pullback = False
        for k in range(1, len(c_post)):
            if l_post[k] < cur_lo:
                cur_lo = float(l_post[k])
                if cur_hi > 0 and (cur_hi - cur_lo) / cur_hi >= 0.025:
                    in_pullback = True
            # 저점 대비 +3% 이상 반등하면 파동 1개 종료 및 새 파동 고점 갱신 대기
            if in_pullback and cur_lo > 0 and (c_post[k] / cur_lo - 1.0) >= 0.03:
                dd_val = (cur_hi - cur_lo) / max(cur_hi, 1e-9)
                swing_dds.append(float(dd_val))
                cur_hi = float(max(h_post[k], c_post[k]))
                cur_lo = float(l_post[k])
                in_pullback = False
            elif not in_pullback and h_post[k] > cur_hi:
                cur_hi = float(h_post[k])
                cur_lo = float(l_post[k])
        # 마지막 진행 중인 파동 추가
        if cur_hi > 0 and cur_lo < cur_hi:
            swing_dds.append(float((cur_hi - cur_lo) / cur_hi))

    # 고정 10일×3칸 보조 판정 (스윙 파동이 1개뿐인 계단식 수축 호환)
    def _max_dd(sub_df: pd.DataFrame) -> float:
        if sub_df is None or len(sub_df) < 3:
            return 1.0
        c_ = sub_df["Close"].astype(float)
        return float(-(c_ / c_.cummax() - 1.0).min())

    range_w1 = _max_dd(hist.iloc[-30:-20]) if len(hist) >= 30 else 1.0
    range_w2 = _max_dd(hist.iloc[-20:-10]) if len(hist) >= 20 else 1.0
    range_w3 = _max_dd(hist.tail(10))
    _vcp_w3_cap = min(0.095, max(0.06, atr14_pct * 2.2))

    swing_contracting = False
    if len(swing_dds) >= 2:
        swing_contracting = bool(
            swing_dds[0] >= 0.05
            and swing_dds[-1] <= _vcp_w3_cap
            and all(swing_dds[i] > swing_dds[i + 1] * 1.08 for i in range(len(swing_dds) - 1))
        )
    window_contracting = bool(
        (range_w1 >= 0.05)
        and (range_w1 > range_w2 * 1.10)
        and (range_w2 > range_w3 * 1.10)
        and (range_w3 <= _vcp_w3_cap)
    )

    raw_vcp_pattern = bool((swing_contracting or window_contracting) and atr_contracting and not has_recent_halt)
    is_true_vcp = bool(raw_vcp_pattern and base_len_days >= 25)
    is_short_vcp = bool(raw_vcp_pattern and base_len_days < 25)
    return is_true_vcp, is_short_vcp, int(base_len_days), float(base_pivot_price), float(high_20d)


def classify_alltoo_setup(
    last: float, sma20: float, sma50: float, sma150: float, sma200: float,
    dist_high: float, is_breakout: bool, is_true_vcp: bool, range_10d_pct: float,
    vol_dry_up: float, pocket_pivot: bool, atr14_pct: float,
    megacap_undercut_rally: bool = False,
    is_20d_breakout: bool = False,
    is_short_vcp: bool = False,
) -> tuple[str, bool]:
    d20 = (last / sma20 - 1) if sma20 > 0 else 0.0
    d50 = (last / sma50 - 1) if sma50 > 0 else 0.0
    atr_from_50 = (d50 / atr14_pct) if atr14_pct > 0 else 0.0
    aligned = last > sma50 > sma150 > sma200

    if d20 > 0.14 or atr_from_50 > 4.2:
        return "⚠️ 단기과열", True
    if is_breakout:
        return "🔥 피벗돌파", False
    if is_true_vcp and aligned and dist_high >= -0.15:
        if np.isfinite(vol_dry_up) and vol_dry_up <= 0.75:
            return "🎯 VCP·거래량건조", False
        return "🎯 VCP수축", False
    if is_short_vcp and aligned and dist_high >= -0.15:
        return "🎯 단기수축", False
    if dist_high >= -0.12 and range_10d_pct <= 0.10 and aligned:
        return "🎯 베이스수축", False
    if pocket_pivot and aligned and dist_high >= -0.15:
        return "⚡ 포켓피봇", False
    if is_20d_breakout and aligned:
        return "📈 20일고점연속", False
    if (aligned and ((-0.015 <= d20 <= 0.03) or (-0.015 <= d50 <= 0.03))) or megacap_undercut_rally:
        return "🧲 이평선눌림목", False
    return ("📈 정배열진행", False) if aligned else ("👀 추세회복", False)

def _calc_weighted_rs(w_map: dict[str, float], src: dict[str, float]) -> float:
    valid_pairs = [(float(w), float(src[k])) for k, w in w_map.items() if k in src and pd.notna(src.get(k)) and np.isfinite(src.get(k))]
    if not valid_pairs:
        return 0.0
    w_sum = sum(w for w, _ in valid_pairs)
    return float(sum(w * val for w, val in valid_pairs) / w_sum) if w_sum > 0 else 0.0

def compute_features(
    hist: pd.DataFrame,
    benchmark: pd.DataFrame,
    cfg: dict,
    rs_mode: str = "STANDARD",
    is_risk_off: bool = False,
    is_megacap: bool = False,
) -> dict[str, Any] | None:
    min_days = int(cfg["screen"]["min_history_days"])
    close = hist["Close"].dropna()
    if len(close) < min_days:
        return None
    # All latest-bar features must refer to the last valid close session.
    hist = hist.loc[:close.index[-1]]

    as_of_date = close.index[-1].strftime("%Y-%m-%d")
    last = float(close.iloc[-1])
    stale_bars = 0
    if benchmark is not None and not benchmark.empty and "Close" in benchmark.columns:
        _b_idx = benchmark["Close"].dropna().index
        if len(_b_idx) > 0:
            stale_bars = int((_b_idx > close.index[-1]).sum())
    is_stale_price = bool(stale_bars >= 2)
    sma10_series = close.rolling(10).mean()
    sma10 = float(sma10_series.iloc[-1])
    sma20 = float(close.rolling(20).mean().iloc[-1])
    sma50 = float(close.rolling(50).mean().iloc[-1])
    sma150 = float(close.rolling(150).mean().iloc[-1])
    sma200_series = close.rolling(200).mean()
    sma200 = float(sma200_series.iloc[-1])
    sma200_prev20 = float(sma200_series.iloc[-21]) if len(sma200_series.dropna()) >= 21 else sma200
    sma200_rising = bool(sma200 >= sma200_prev20)

    high252, low252 = float(close.tail(252).max()), float(close.tail(252).min())
    dist_high = last / high252 - 1.0
    dist_low = last / low252 - 1.0 if low252 > 0 else 1.0
    above_low_30 = bool(last >= low252 * 1.30)

    strict_minervini = bool(
        (last > sma50 > sma150 > sma200)
        and sma200_rising
        and above_low_30
        and (dist_high >= -0.25)
    )
    tier_grade = "Tier A" if strict_minervini else ("Tier B" if last >= sma200 else "추세회복")

    rets, brets, rel = {}, {}, {}
    b_close = benchmark["Close"].dropna() if benchmark is not None and not benchmark.empty else pd.Series(dtype=float)
    aligned_b = b_close.reindex(close.index).ffill() if not b_close.empty else pd.Series(dtype=float)

    for d, k in [(21, "rs_21d"), (63, "rs_63d"), (126, "rs_126d"), (252, "rs_252d")]:
        if len(close) > d:
            rets[k] = float(close.iloc[-1] / close.iloc[-d - 1] - 1.0)
            if len(aligned_b.dropna()) > d:
                brets[k] = float(aligned_b.iloc[-1] / aligned_b.iloc[-d - 1] - 1.0)
                rel[k] = rets[k] - brets[k]
            else:
                brets[k], rel[k] = np.nan, np.nan
        else:
            rets[k], brets[k], rel[k] = np.nan, np.nan, np.nan

    # [P0-2] 필수 RS 기간(63일 또는 126일)에 대한 벤치마크나 종목 데이터가 없으면 신뢰 불가로 제외
    if pd.isna(rel.get("rs_63d")) and pd.isna(rel.get("rs_126d")):
        return None

    std_w = cfg["screen"].get("weights", {"rs_63d": 0.25, "rs_126d": 0.35, "rs_252d": 0.40})
    fast_w = cfg["screen"].get("fast_weights", {"rs_21d": 0.35, "rs_63d": 0.45, "rs_126d": 0.20})

    # [버그 2 수정] 상장 252거래일 미만 종목은 백분위 랭킹(rank) 전에 원시 RS 자체에 보수적 감쇠(0.88)를 적용
    short_history = bool(len(close) < 252)
    raw_rs_std = _calc_weighted_rs(std_w, rel)
    raw_rs_fast = _calc_weighted_rs(fast_w, rel)
    raw_rs = raw_rs_fast if str(rs_mode).upper() == "FAST" else raw_rs_std
    pure_rs = _calc_weighted_rs(std_w, rets)

    active_w = fast_w if str(rs_mode).upper() == "FAST" else std_w
    raw_rs_prev20 = raw_rs
    pure_rs_prev20 = pure_rs
    short_history_prev = short_history
    if len(close) > 150:
        c_p20 = close.iloc[:-20]
        b_p20 = aligned_b.iloc[:-20] if not aligned_b.empty else pd.Series(dtype=float)
        short_history_prev = bool(len(c_p20) < 252)
        rel_p20 = {}
        rets_p20 = {}
        for d, k in [(21, "rs_21d"), (63, "rs_63d"), (126, "rs_126d"), (252, "rs_252d")]:
            if len(c_p20) > d:
                r_c = float(c_p20.iloc[-1] / c_p20.iloc[-d - 1] - 1.0)
                rets_p20[k] = r_c
                # [버그 4 수정] 4주 전 벤치마크 데이터가 부족하면 0.0으로 둔갑시키지 않고 np.nan 처리
                if len(b_p20.dropna()) > d:
                    r_b = float(b_p20.iloc[-1] / b_p20.iloc[-d - 1] - 1.0)
                    rel_p20[k] = r_c - r_b
                else:
                    rel_p20[k] = np.nan
            else:
                rel_p20[k] = np.nan
                rets_p20[k] = np.nan
        if pd.notna(rel_p20.get("rs_63d")) or pd.notna(rel_p20.get("rs_126d")):
            raw_rs_prev20 = _calc_weighted_rs(active_w, rel_p20)
        pure_rs_prev20 = _calc_weighted_rs(std_w, rets_p20)

    rs_line_new_high = False
    if not aligned_b.empty:
        valid_b = aligned_b > 0
        if valid_b.sum() >= 180:
            rs_line = (close[valid_b] / aligned_b[valid_b]).tail(252)
            if not rs_line.empty and float(rs_line.iloc[-1]) >= float(rs_line.max()) * 0.995:
                rs_line_new_high = True
    rs_blue_dot = bool(rs_line_new_high and (dist_high <= -0.02))

    # Keep session positions: dropna() would turn yesterday's volume into today's.
    vol = pd.to_numeric(hist["Volume"], errors="coerce") if "Volume" in hist else pd.Series(dtype=float)
    vol = vol.where(np.isfinite(vol) & (vol >= 0))
    volume_observed = bool(len(vol) and pd.notna(vol.iloc[-1]) and vol.iloc[-1] > 0)
    vol_ratio, vol_ratio_50d, vol_dry, pocket_pivot = np.nan, np.nan, np.nan, False
    vol_50d_prev = np.nan
    if len(vol) >= 51:
        v20 = float(vol.iloc[-21:-1].mean(skipna=False))
        vol_50d_prev = float(vol.iloc[-51:-1].mean(skipna=False))
        vol_ratio = float(vol.iloc[-1] / v20) if v20 > 0 else np.nan
        vol_ratio_50d = float(vol.iloc[-1] / vol_50d_prev) if vol_50d_prev > 0 else np.nan
        _v5_cur = float(vol.tail(5).mean(skipna=False))
        _v5_pre = float(vol.iloc[-6:-1].mean(skipna=False)) if len(vol) >= 6 else _v5_cur
        _is_up_surge = bool(len(close) >= 2 and close.iloc[-1] > close.iloc[-2] and np.isfinite(vol_ratio) and vol_ratio >= 1.2)
        vol_dry = float((min(_v5_cur, _v5_pre) if _is_up_surge else _v5_cur) / vol_50d_prev) if vol_50d_prev > 0 else np.nan
        # [P1-4] 포켓피봇 Off-by-one 수정: 전체에서 shift를 먼저 적용해 p10의 첫날 하락 여부 보존
        if len(hist) >= 12 and vol.tail(11).notna().all():
            is_down = hist["Close"] < hist["Close"].shift(1)
            p10_dvols = hist["Volume"].iloc[-11:-1][is_down.iloc[-11:-1]]
            max_dv = float(p10_dvols.max()) if not p10_dvols.empty else 0.0
            if float(hist["Close"].iloc[-1]) > float(hist["Close"].iloc[-2]) and max_dv > 0 and float(hist["Volume"].iloc[-1]) > max_dv:
                pocket_pivot = True

    # [P1-4] 최근 20거래일 내 거래 정지일(IsHalt) 유무 체크 (VCP/변동성 왜곡 방지)
    recent_halts = int(hist["IsHalt"].tail(20).sum()) if "IsHalt" in hist.columns else 0
    has_recent_halt = recent_halts > 0

    stock_dist_days_25d = 0
    if "Volume" in hist.columns and len(hist) >= 26:
        # 20일 평균은 전체 이력에서 롤링한 뒤 최근 25일만 집계 (26행 슬라이스에서 롤링하면 최근 6일만 판정되는 버그 방지)
        c_chg = hist["Close"].pct_change()
        v_diff = hist["Volume"].diff()
        v20_arr = hist["Volume"].rolling(20).mean().shift(1)
        is_dist = (c_chg <= -0.01) & (v_diff > 0) & (hist["Volume"] > v20_arr)
        stock_dist_days_25d = int(is_dist.tail(25).sum())

    tr = pd.concat([
        hist["High"] - hist["Low"],
        (hist["High"] - hist["Close"].shift(1)).abs(),
        (hist["Low"] - hist["Close"].shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr_series = tr.rolling(14).mean()
    atr14_val = float(atr_series.iloc[-1]) if len(atr_series) >= 14 else last * 0.035
    atr14_prev20 = float(atr_series.iloc[-21]) if len(atr_series.dropna()) >= 21 else atr14_val
    atr14_pct = float(atr14_val / last) if last > 0 else 0.035
    atr_contracting = bool(atr14_val <= atr14_prev20 * 0.88)

    def _safe_range(sub_df: pd.DataFrame) -> float:
        if sub_df.empty: return 1.0
        c_hi, c_lo = float(sub_df["Close"].max()), float(sub_df["Close"].min())
        h_hi = float(sub_df["High"].max()) if "High" in sub_df else c_hi
        l_lo = float(sub_df["Low"].min()) if "Low" in sub_df and float(sub_df["Low"].min()) > 0 else c_lo
        raw_r = (h_hi / l_lo - 1.0) if l_lo > 0 else 1.0
        close_r = (c_hi / c_lo - 1.0) if c_lo > 0 else 1.0
        return close_r if raw_r > close_r * 2.2 else raw_r

    range_5d = _safe_range(hist.tail(5))
    range_10d = _safe_range(hist.tail(10))
    range_20d = _safe_range(hist.tail(20))

    is_true_vcp, is_short_vcp, base_len_days, base_pivot_price, high_20d = _detect_swing_vcp_and_base(
        hist, atr14_pct, atr_contracting, has_recent_halt
    )

    # 피벗 이원화: 돌파 라벨은 베이스 피벗(base_pivot_price) 기준, 20일 고가(high_20d)는 단기 연속/눌림목 보조용으로 분리
    pivot_price = base_pivot_price if base_pivot_price > 0 else high_20d
    dist_to_pivot_pct = (last / pivot_price - 1.0) if pivot_price > 0 else 0.0
    dist_to_20d_high_pct = (last / high_20d - 1.0) if high_20d > 0 else 0.0
    up_day = bool(len(close) >= 2 and last >= float(close.iloc[-2]) * 1.01)
    vol_surge = bool(np.isfinite(vol_50d_prev) and vol_50d_prev > 0 and float(vol.iloc[-1]) >= 1.5 * vol_50d_prev)
    buy_zone_low, buy_zone_high = pivot_price * 0.975, pivot_price * 1.05
    in_buy_zone = bool(buy_zone_low <= last <= buy_zone_high)
    is_breakout = bool(up_day and vol_surge and pivot_price <= last <= buy_zone_high)
    is_20d_breakout = bool(up_day and vol_surge and high_20d <= last <= high_20d * 1.05 and not is_breakout)

    entry_ref = max(last, pivot_price) if (in_buy_zone and last < pivot_price) else last
    atr_stop = entry_ref - 2.0 * atr14_val
    sma20_stop = sma20 * 0.99 if (0 < sma20 < entry_ref) else atr_stop
    raw_stop_price = max(atr_stop, sma20_stop)
    raw_stop_dist_pct = (raw_stop_price / entry_ref - 1.0) if entry_ref > 0 else -0.07
    max_stop_limit = float(cfg["screen"].get("max_acceptable_stop_pct", 0.08))
    wide_stop_limit = float(cfg["screen"].get("wide_stop_max_pct", 0.12))
    stop_warning = bool(abs(raw_stop_dist_pct) > max_stop_limit)
    is_wide_stop = bool(max_stop_limit < abs(raw_stop_dist_pct) <= wide_stop_limit)
    rec_stop_price = raw_stop_price
    stop_dist_pct = raw_stop_dist_pct

    slippage_pct = float(cfg["screen"].get("slippage_pct", 0.0015))
    risk_pct = float(cfg["screen"].get("risk_off_risk_per_trade_pct", 0.005) if is_risk_off else cfg["screen"].get("risk_per_trade_pct", 0.01))
    max_pos_cap = float(cfg["screen"].get("max_position_size_pct", 25.0))
    eff_stop_abs = max(abs(raw_stop_dist_pct) + slippage_pct, 0.02)
    position_size_pct = _weight_floor(min(max_pos_cap, (risk_pct / eff_stop_abs) * 100.0))
    trade_risk_pct = round(position_size_pct * eff_stop_abs, 2)

    down_resilient = False
    down_day_excess = 0.0
    if not aligned_b.empty and len(close) >= 64 and len(aligned_b.dropna()) >= 64:
        s_ret63 = close.pct_change().tail(63)
        b_ret63 = aligned_b.pct_change().tail(63)
        b_down_mask = b_ret63 < -0.002
        if int(b_down_mask.sum()) >= 5:
            diff_on_down = (s_ret63[b_down_mask] - b_ret63[b_down_mask]).dropna()
            if not diff_on_down.empty:
                down_day_excess = round(float(diff_on_down.mean() * 100.0), 2)
                down_win_rate = float((s_ret63[b_down_mask] >= -0.001).mean())
                down_resilient = bool(down_day_excess >= 0.45 and down_win_rate >= 0.45)

    below_10dma_2d = bool(
        len(close) >= 2
        and pd.notna(sma10_series.iloc[-2])
        and (close.iloc[-1] < sma10)
        and (close.iloc[-2] < float(sma10_series.iloc[-2]))
    )
    below_20dma_vol = bool((last < sma20) and np.isfinite(vol_ratio) and vol_ratio >= 1.2)
    _day_ret = float(close.iloc[-1] / close.iloc[-2] - 1.0) if len(close) >= 2 else 0.0
    _bar_hi = float(hist["High"].iloc[-1]) if "High" in hist.columns else last
    _bar_lo = float(hist["Low"].iloc[-1]) if "Low" in hist.columns else last
    _close_pos = (last - _bar_lo) / max(_bar_hi - _bar_lo, 1e-9) if _bar_hi > _bar_lo else 0.5
    climax_down_bar = bool(_day_ret <= -0.035 and np.isfinite(vol_ratio) and vol_ratio >= 1.8 and _close_pos <= 0.25)
    below_50dma_vol = bool((last < sma50) and ((np.isfinite(vol_ratio) and vol_ratio >= 1.2) or (last / sma50 - 1.0 <= -0.015)))
    if below_50dma_vol:
        exit_signal = "🚨50일선이탈(전량청산검토)"
    elif climax_down_bar:
        exit_signal = "✂️대량장대음봉(비중축소검토)"
    elif below_20dma_vol:
        exit_signal = "✂️20일선거래량이탈(비중축소)"
    elif below_10dma_2d:
        exit_signal = "⚠️10일선2일이탈"
    elif stock_dist_days_25d >= STOCK_DIST_EXIT_DAYS:
        exit_signal = f"⚠️분배일과다({stock_dist_days_25d}일)"
    else:
        exit_signal = "✅정상"

    d20_pct = (last / sma20 - 1.0) if sma20 > 0 else np.nan
    d50_pct = (last / sma50 - 1.0) if sma50 > 0 else np.nan
    atr_from_50 = (d50_pct / atr14_pct) if atr14_pct > 0 and pd.notna(d50_pct) else 0.0
    recent_low_5d = float(hist["Low"].tail(5).min()) if "Low" in hist.columns else float(close.tail(5).min())
    megacap_undercut_rally = bool(
        is_megacap
        and (sma50 > sma150 > sma200)
        and (sma50 * 0.96 <= recent_low_5d < sma50)
        and (last >= sma50)
        and up_day
    )
    setup_st, is_overextended = classify_alltoo_setup(
        last, sma20, sma50, sma150, sma200, dist_high, is_breakout, is_true_vcp, range_10d, vol_dry, pocket_pivot, atr14_pct,
        megacap_undercut_rally=megacap_undercut_rally,
        is_20d_breakout=is_20d_breakout,
        is_short_vcp=is_short_vcp,
    )

    in_pullback_zone = bool(pivot_price * 0.88 <= last <= buy_zone_high) and up_day
    base_setup_ready = bool(
        strict_minervini
        and not is_overextended
        and not has_recent_halt
        and not is_stale_price
        and volume_observed
        and exit_signal == "✅정상"
        and (
            (in_buy_zone and any(k in setup_st for k in ("돌파", "VCP", "수축")))
            or (in_pullback_zone and any(k in setup_st for k in ("포켓", "눌림목")))
        )
    )
    is_actionable = bool(base_setup_ready and not stop_warning)
    wide_stop_actionable = bool(base_setup_ready and is_wide_stop)

    turnover = hist["Turnover"].dropna() if "Turnover" in hist else (close * vol).dropna()

    return {
        "as_of": as_of_date,
        "stale_bars": stale_bars,
        "is_stale_price": is_stale_price,
        "last_close": last,
        "pivot_price": pivot_price,
        "tactical_calculation_version": "tactical-session-v2",
            "compass_calculation_version": "regime-budget-v1.1",
        "pivot_inputs_as_of": hist.index[-2].strftime("%Y-%m-%d"),
        "buy_zone_low": buy_zone_low,
        "buy_zone_high": buy_zone_high,
        "intraday_pivot_exceeded": bool(float(hist["High"].iloc[-1]) > pivot_price),
        "close_at_or_above_pivot": bool(last >= pivot_price),
        "volume_observed": volume_observed,
        "base_pivot_price": base_pivot_price,
        "high_20d": high_20d,
        "base_len_days": base_len_days,
        "dist_to_pivot_pct": dist_to_pivot_pct,
        "in_buy_zone": in_buy_zone,
        "is_breakout": is_breakout,
        "is_20d_breakout": is_20d_breakout,
        "is_true_vcp": is_true_vcp,
        "is_short_vcp": is_short_vcp,
        "is_actionable": is_actionable,
        "wide_stop_actionable": wide_stop_actionable,
        "is_wide_stop": is_wide_stop,
        "stop_price": rec_stop_price,
        "stop_dist_pct": stop_dist_pct,
        "raw_stop_dist_pct": raw_stop_dist_pct,
        "stop_warning": stop_warning,
        "position_size_pct": position_size_pct,
        "trade_risk_pct": trade_risk_pct,
        "down_resilient": down_resilient,
        "down_day_excess_pct": down_day_excess,
        "exit_signal": exit_signal,
        "dist_days_25d": stock_dist_days_25d,
        "atr_from_50dma": round(atr_from_50, 2),
        "tier": tier_grade,
        "strict_minervini": strict_minervini,
        "rs_line_new_high": rs_line_new_high,
        "rs_blue_dot": rs_blue_dot,
        "sma10": sma10,
        "sma20": sma20,
        "sma50": sma50,
        "sma150": sma150,
        "sma200": sma200,
        "sma200_rising": sma200_rising,
        "high_252d": high252,
        "low_252d": low252,
        "atr14_pct": round(atr14_pct, 4),
        "distance_from_high_pct": dist_high,
        "distance_from_low_pct": dist_low,
        "dist_sma20_pct": d20_pct,
        "dist_sma50_pct": d50_pct,
        "range_5d_pct": range_5d,
        "range_10d_pct": range_10d,
        "range_20d_pct": range_20d,
        "vol_dry_up_ratio": vol_dry,
        "pocket_pivot": pocket_pivot,
        "setup_stage": setup_st,
        "return_21d": rets["rs_21d"],
        "return_63d": rets["rs_63d"],
        "return_126d": rets["rs_126d"],
        "return_252d": rets["rs_252d"],
        "relative_21d": rel["rs_21d"],
        "relative_63d": rel["rs_63d"],
        "relative_126d": rel["rs_126d"],
        "relative_252d": rel["rs_252d"],
        "raw_rs": raw_rs,
        "raw_rs_std": raw_rs_std,
        "raw_rs_fast": raw_rs_fast,
        "raw_rs_prev20": raw_rs_prev20,
        "pure_rs_prev20": pure_rs_prev20,
        "pure_rs": pure_rs,
        "rs_score": float(np.clip(50 + 100 * raw_rs, 1, 99)),
        "short_history": short_history,
        "short_history_prev": short_history_prev,
        "rs_score_fast": float(np.clip(50 + 100 * raw_rs_fast, 1, 99)),
        "rs_change_4w": 0.0,
        "volume_ratio": vol_ratio,
        "vol_ratio_50d": vol_ratio_50d,
        "avg_turnover_20d": float(turnover.tail(20).mean()) if len(turnover) >= 20 else np.nan,
        "avg_turnover_5d": float(turnover.tail(5).mean()) if len(turnover) >= 5 else np.nan,
        "trend_template": bool(last > sma50 > sma150 > sma200),
        "above_200dma": bool(last > sma200),
        "above_50dma": bool(last > sma50),
    }

def apply_percentile_rs_and_filter(
    raw_rows: list[dict[str, Any]], cfg: dict, market_label: str
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    if not raw_rows:
        return [], {}
    df = pd.DataFrame(raw_rows)
    df["rs_score"] = (df["raw_rs"].rank(pct=True) * 98.0 + 1.0).round(1)
    df["rs_score_fast"] = (df["raw_rs_fast"].rank(pct=True) * 98.0 + 1.0).round(1)
    prev_pct = (df["raw_rs_prev20"].rank(pct=True) * 98.0 + 1.0).round(1)
    if "short_history" in df.columns:
        df["rs_score"] = (df["rs_score"] - IPO_RS_PENALTY * df["short_history"].fillna(False).astype(float)).clip(1, 99).round(1)
        df["rs_score_fast"] = (df["rs_score_fast"] - IPO_RS_PENALTY * df["short_history"].fillna(False).astype(float)).clip(1, 99).round(1)
        prev_pct = (prev_pct - IPO_RS_PENALTY * df["short_history_prev"].fillna(False).astype(float)).clip(1, 99).round(1)
    df["rs_change_4w"] = (df["rs_score"] - prev_pct).round(1)

    passed = []
    fail_map: dict[str, dict[str, Any]] = {}
    for rec in df.to_dict("records"):
        ok, _, reason = score_candidate(rec, cfg, market_label)
        if ok:
            rec["reason"] = reason
            passed.append(rec)
        else:
            fail_map[str(rec["ticker"])] = {
                "ticker": str(rec["ticker"]),
                "name": str(rec.get("name", "")),
                "sub_market": str(rec.get("sub_market", market_label)),
                "rs_score": float(rec.get("rs_score", 0.0)),
                "last_close": float(rec.get("last_close", 0.0)),
                "drop_reason": reason,
                "exit_signal": str(rec.get("exit_signal", "")),
            }
    return passed, fail_map

def score_candidate(features: dict[str, Any], cfg: dict, market: str) -> tuple[bool, float, str]:
    del market
    s = cfg["screen"]
    fails = []
    if features.get("is_stale_price", False):
        fails.append(f"가격데이터지연({int(features.get('stale_bars', 2))}거래일)")
    if features["rs_score"] < float(s["min_rs_score"]):
        fails.append(f"RS미달({features['rs_score']:.1f}<{s['min_rs_score']})")
    if pd.notna(features["relative_63d"]) and features["relative_63d"] < float(s["min_rs_63d"]):
        fails.append("63일상대수익약세")
    if pd.notna(features["relative_126d"]) and features["relative_126d"] < float(s["min_rs_126d"]):
        fails.append("126일상대수익약세")
    if features["last_close"] < features["sma200"] * float(s["min_price_vs_200dma"]):
        fails.append("200일선이탈")
    if features["distance_from_high_pct"] < -float(s["near_high_pct"]):
        fails.append(f"고점괴리과다({features['distance_from_high_pct']*100:.1f}%)")
    if fails:
        return False, features["rs_score"], " · ".join(fails)

    reasons = []
    if features.get("rs_blue_dot", False):
        reasons.append("🔵RS블루닷(선행신고가)")
    elif features.get("rs_line_new_high", False):
        reasons.append("⚡RS신고가")
    if features.get("is_breakout", False):
        reasons.append("🔥거래량피벗돌파")
    if features.get("is_true_vcp", False):
        if np.isfinite(features.get("vol_dry_up_ratio", np.nan)) and float(features["vol_dry_up_ratio"]) <= 0.75:
            reasons.append("🎯VCP+거래량건조")
        else:
            reasons.append("🎯VCP수축")
    elif features.get("range_10d_pct", 1.0) <= 0.10:
        reasons.append("10일베이스수축")
    if features.get("pocket_pivot", False):
        reasons.append("포켓피봇")
    if features.get("down_resilient", False):
        reasons.append("🛡️조정장방어")
    if features.get("eps_status") == "TURNAROUND":
        reasons.append("✨흑자전환")
    elif pd.notna(features.get("peg")) and 0 < float(features["peg"]) <= 1.5:
        reasons.append(f"저PEG({features['peg']:.2f})")
    if features.get("strict_minervini", False):
        reasons.append("미너비니TierA")
    else:
        reasons.append("정배열" if features["trend_template"] else "추세회복")
    return True, features["rs_score"], " · ".join(reasons)

def load_historical_tracking(today_str: str) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    if not REPORT_DIR.exists():
        return {}, {}
    date_dirs = sorted(
        [d for d in REPORT_DIR.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name) and d.name < today_str],
        key=lambda x: x.name,
        reverse=True,
    )[:25]
    if not date_dirs:
        return {}, {}

    daily_sets: list[set[str]] = []
    seen_as_of_signatures: set[str] = set()
    prev_day_info: dict[str, dict[str, float]] = {}
    prev_selected_map: dict[str, dict[str, Any]] = {}

    for ddir in date_dirs:
        day_tkrs: set[str] = set()
        day_as_ofs: set[str] = set()
        all_csvs = [
            f for f in ddir.glob("*_candidates_all.csv")
            if f.name.split("_")[0].upper() in ("KOSPI", "KOSDAQ", "NASDAQ100", "SP500")
        ]
        if not all_csvs:
            compact_csv = ddir / "all_candidates.csv"
            if compact_csv.exists():
                all_csvs = [compact_csv]
            else:
                all_csvs = list(ddir.glob("*_candidates_all.csv")) + list(ddir.glob("*_candidates.csv"))

        for cf in all_csvs:
            try:
                df = pd.read_csv(cf, dtype={"ticker": str})
                if df.empty or "ticker" not in df.columns:
                    continue
                file_track = cf.name.split("_")[0].upper()
                for r in df.itertuples():
                    mkt_prefix = str(getattr(r, "sub_market", file_track)).upper()
                    is_kr = mkt_prefix in ("KR", "KOSPI", "KOSDAQ")
                    as_of_value = getattr(r, "as_of", ddir.name)
                    day_as_ofs.add(f"{mkt_prefix}:{as_of_value}")
                    tkr = str(getattr(r, "ticker", "")).strip()
                    tkr = tkr.zfill(6) if is_kr and tkr.isdigit() else tkr.upper()
                    if not tkr:
                        continue
                    day_tkrs.add(tkr)
                    if not daily_sets and tkr not in prev_day_info:
                        prev_day_info[tkr] = {
                            "prev_rs": float(getattr(r, "rs_score", np.nan)),
                            "prev_rank": float(getattr(r, "rank", np.nan)),
                        }
            except Exception as _exc:
                _swallow("load_historical_tracking", _exc, locals().get("tkr") or locals().get("ticker"))
                continue

        # [수정 4] 직전 영업일의 Top 20 선발 리스트(minimal_output 모드 시 sub_market 컬럼 보존)
        if not prev_selected_map:
            sel_csvs = [
                ddir / f"{tk.lower()}_candidates.csv"
                for tk in ("KOSPI", "KOSDAQ", "NASDAQ100", "SP500")
                if (ddir / f"{tk.lower()}_candidates.csv").exists()
            ]
            if not sel_csvs and (ddir / "all_candidates.csv").exists():
                sel_csvs = [ddir / "all_candidates.csv"]
            for scf in sel_csvs:
                try:
                    sdf = pd.read_csv(scf, dtype={"ticker": str})
                    file_trk = scf.name.split("_")[0].upper()
                    for r in sdf.itertuples():
                        row_trk = str(getattr(r, "sub_market", file_trk)).strip().upper()
                        if row_trk == "ALL" or not row_trk:
                            row_trk = str(getattr(r, "market", "KR")).strip().upper()
                        is_kr = row_trk in ("KR", "KOSPI", "KOSDAQ")
                        tkr = str(r.ticker).strip()
                        tkr = tkr.zfill(6) if is_kr and tkr.isdigit() else tkr.upper()
                        prev_selected_map[tkr] = {
                            "ticker": tkr,
                            "name": str(getattr(r, "name", tkr)),
                            "sub_market": row_trk,
                            "prev_rank": int(getattr(r, "rank", 0)),
                            "prev_rs": float(getattr(r, "rs_score", np.nan)),
                            "prev_setup": str(getattr(r, "setup_stage", "")),
                        }
                except Exception as _exc:
                    _swallow("load_historical_tracking", _exc, locals().get("tkr") or locals().get("ticker"))
                    pass

        sig_key = "|".join(sorted(day_as_ofs)) if day_as_ofs else ddir.name
        if day_tkrs and sig_key not in seen_as_of_signatures:
            seen_as_of_signatures.add(sig_key)
            daily_sets.append(day_tkrs)

    tracking: dict[str, dict[str, Any]] = {}
    all_seen = set().union(*daily_sets) if daily_sets else set()
    for tkr in all_seen:
        streak = 0
        for dset in daily_sets:
            if tkr in dset:
                streak += 1
            else:
                break
        p_info = prev_day_info.get(tkr, {})
        tracking[tkr] = {
            "streak_prev": streak,
            "prev_rs": p_info.get("prev_rs", np.nan),
            "prev_rank": p_info.get("prev_rank", np.nan),
        }
    return tracking, prev_selected_map

# =====================================================================
# 5. Top-Down 섹터 엔진 및 개별 종목 선발
# =====================================================================

def _make_strength_bar(score: float, width: int = 8) -> str:
    clamped = max(0.0, min(100.0, float(score)))
    filled = int(round((clamped / 100.0) * width))
    return "█" * filled + "░" * (width - filled)

def _color_span(text_str: str, hex_color: str, bold: bool = False) -> str:
    inner = f"<b>{text_str}</b>" if bold else text_str
    return f'<span style="color:{hex_color};">{inner}</span>'

def _classify_rotation_phase(median_rs: float, delta_4w: float, pct_50dma: float) -> str:
    if median_rs >= 70.0 and delta_4w >= 4.0 and pct_50dma >= 60.0:
        return _color_span("🚀 강세가속 (Leading ↑)", "#059669", bold=True)
    if median_rs >= 70.0 and delta_4w >= -2.0:
        return _color_span("🟢 주도유지 (Leading)", "#059669", bold=False)
    if median_rs < 70.0 and delta_4w >= 5.0 and pct_50dma >= 50.0:
        return _color_span("🔥 신규부상 (Improving ↑)", "#2563EB", bold=True)
    if median_rs >= 68.0 and delta_4w < -2.0:
        return _color_span("⚠️ 탄력둔화 (Weakening ↘)", "#DC2626", bold=False)
    if median_rs < 50.0 and delta_4w < 0:
        return _color_span("🥶 소외/약세 (Lagging ↓)", "#64748B", bold=False)
    return "⚪ 중립순환 (Neutral)"

def build_topdown_sector_tables(
    all_raw_rows: list[dict[str, Any]],
    passed_df: pd.DataFrame,
    hist_track: dict[str, dict[str, Any]] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    if not all_raw_rows:
        return pd.DataFrame(), pd.DataFrame(), {}

    udf = pd.DataFrame(all_raw_rows).drop_duplicates("ticker").copy()
    udf["macro_theme"] = udf.get("macro_theme", pd.Series("기타·일반산업", index=udf.index)).fillna("기타·일반산업").replace("", "기타·일반산업").astype(str)
    udf["sector"] = udf.get("sector", pd.Series("일반산업", index=udf.index)).fillna("일반산업").replace("", "일반산업").astype(str)

    udf["univ_rs"] = (udf["pure_rs"].rank(pct=True) * 98.0 + 1.0).round(1)
    udf["univ_rs_prev20"] = (udf["pure_rs_prev20"].rank(pct=True) * 98.0 + 1.0).round(1)
    if "short_history" in udf.columns:
        udf["univ_rs"] = (udf["univ_rs"] - IPO_RS_PENALTY * udf["short_history"].fillna(False).astype(float)).clip(1, 99)
        udf["univ_rs_prev20"] = (udf["univ_rs_prev20"] - IPO_RS_PENALTY * udf["short_history_prev"].fillna(False).astype(float)).clip(1, 99)
    udf["univ_rs_delta4w"] = (udf["univ_rs"] - udf["univ_rs_prev20"]).round(1)

    passed_tkrs = set(passed_df["ticker"].astype(str)) if (passed_df is not None and not passed_df.empty) else set()
    hist_track = hist_track or {}
    new_tkrs = {
        tkr for tkr in passed_tkrs
        if int((hist_track.get(tkr) or {}).get("streak_prev", 0)) == 0
    }
    udf["is_passed"] = udf["ticker"].astype(str).isin(passed_tkrs)
    udf["is_new_pass"] = udf["ticker"].astype(str).isin(new_tkrs)
    udf["is_vcp_or_brk"] = udf["is_breakout"].astype(bool) | udf["is_true_vcp"].astype(bool)

    sub_records = []
    for (m_theme, sec), grp in udf.groupby(["macro_theme", "sector"], as_index=False):
        u_cnt = len(grp)
        p_cnt = int(grp["is_passed"].sum())
        tier_a_cnt = int(grp["strict_minervini"].sum())
        ab50_pct = round(float(grp["above_50dma"].mean() * 100.0), 1)
        pass_rate = round(p_cnt / max(u_cnt, 1) * 100.0, 1)
        med_rs = round(float(grp["univ_rs"].median()), 1)
        top3_rs = round(float(grp["univ_rs"].nlargest(min(3, u_cnt)).mean()), 1)
        delta_4w = round(float(grp["univ_rs_delta4w"].median()), 1)
        setup_cnt = int(grp["is_vcp_or_brk"].sum())
        act_cnt = int(grp["is_actionable"].sum())
        new_cnt = int(grp["is_new_pass"].sum())

        # 소규모 표본(종목 5개 미만) Shrinkage 적용
        raw_power = 0.35 * med_rs + 0.20 * top3_rs + 0.25 * ab50_pct + 0.20 * pass_rate
        shrink_w = u_cnt / (u_cnt + 5)
        sec_power = round((shrink_w * raw_power) + ((1 - shrink_w) * 50.0), 1)

        pass_grp = grp[grp["is_passed"]].sort_values(["strict_minervini", "univ_rs", "ticker"], ascending=[False, False, True])
        leader_row = pass_grp.iloc[0] if not pass_grp.empty else grp.sort_values(["univ_rs", "ticker"], ascending=[False, True]).iloc[0]

        sub_records.append({
            "macro_theme": m_theme, "sector": sec, "univ_total": u_cnt, "passed_cnt": p_cnt,
            "pass_rate_pct": pass_rate, "tier_a_cnt": tier_a_cnt, "pct_above_50dma": ab50_pct,
            "median_rs": med_rs, "top3_rs": top3_rs, "rs_delta_4w": delta_4w,
            "setup_cnt": setup_cnt, "actionable_cnt": act_cnt, "new_cnt": new_cnt,
            "sector_power": sec_power, "rotation_phase": _classify_rotation_phase(med_rs, delta_4w, ab50_pct),
            "leader_ticker": str(leader_row["ticker"]), "leader_name": str(leader_row["name"]),
            "leader_rs": float(leader_row["univ_rs"]), "leader_setup": str(leader_row["setup_stage"]),
            "leader_blue_dot": bool(leader_row.get("rs_blue_dot", False)),
        })

    sub_df = pd.DataFrame(sub_records)
    if not sub_df.empty:
        sub_df = sub_df.sort_values(["sector_power", "passed_cnt", "median_rs", "macro_theme", "sector"], ascending=[False, False, False, True, True]).reset_index(drop=True)
        sub_df.insert(0, "sub_rank", range(1, len(sub_df) + 1))
    sub_power_map = dict(zip(zip(sub_df["macro_theme"], sub_df["sector"]), sub_df["sector_power"])) if not sub_df.empty else {}

    macro_records = []
    for m_theme, grp in udf.groupby("macro_theme", as_index=False):
        if "미분류" in m_theme or "기타" in m_theme:
            continue
        u_cnt = len(grp)
        p_cnt = int(grp["is_passed"].sum())
        tier_a_cnt = int(grp["strict_minervini"].sum())
        ab50_pct = round(float(grp["above_50dma"].mean() * 100.0), 1)
        pass_rate = round(p_cnt / max(u_cnt, 1) * 100.0, 1)
        med_rs = round(float(grp["univ_rs"].median()), 1)
        delta_4w = round(float(grp["univ_rs_delta4w"].median()), 1)
        act_cnt = int(grp["is_actionable"].sum())
        new_cnt = int(grp["is_new_pass"].sum())
        macro_power = round(0.40 * med_rs + 0.35 * ab50_pct + 0.25 * pass_rate, 1)

        theme_subs = sub_df[sub_df["macro_theme"] == m_theme] if not sub_df.empty else pd.DataFrame()
        top_sub_name = str(theme_subs.iloc[0]["sector"]) if not theme_subs.empty else "-"
        top_sub_leader = f"{theme_subs.iloc[0]['leader_name']}({theme_subs.iloc[0]['leader_ticker']})" if not theme_subs.empty else "-"

        macro_records.append({
            "macro_theme": m_theme, "sub_sector_cnt": int(grp["sector"].nunique()),
            "univ_total": u_cnt, "passed_cnt": p_cnt, "pass_rate_pct": pass_rate,
            "tier_a_cnt": tier_a_cnt, "pct_above_50dma": ab50_pct, "median_rs": med_rs,
            "rs_delta_4w": delta_4w, "actionable_cnt": act_cnt, "new_cnt": new_cnt, "macro_power": macro_power,
            "rotation_phase": _classify_rotation_phase(med_rs, delta_4w, ab50_pct),
            "top_sub_sector": top_sub_name, "top_leader": top_sub_leader,
        })

    macro_df = pd.DataFrame(macro_records)
    if not macro_df.empty:
        macro_df = macro_df.sort_values(["macro_power", "passed_cnt", "median_rs", "macro_theme"], ascending=[False, False, False, True]).reset_index(drop=True)
        macro_df.insert(0, "macro_rank", range(1, len(macro_df) + 1))

    return macro_df, sub_df, sub_power_map

def _annotate_sector_and_history(
    rows: list[dict[str, Any]],
    global_sector_ranks: dict[str, int],
    hist_track: dict[str, dict[str, Any]],
    sub_power_map: dict[str, float] | None = None,
    leading_sectors: set[str] | None = None,
) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["macro_theme"] = df.get("macro_theme", pd.Series("기타·일반산업", index=df.index)).fillna("기타·일반산업").replace("", "기타·일반산업").astype(str)
    df["sector"] = df.get("sector", pd.Series("일반산업", index=df.index)).fillna("일반산업").replace("", "일반산업").astype(str)

    sub_power_map = sub_power_map or {}
    leading_sectors = leading_sectors or set()
    df["sector_power"] = df.apply(lambda row: float(sub_power_map.get((row["macro_theme"], row["sector"]), 50.0)), axis=1).round(1)
    df["is_leading_sector"] = df.apply(lambda row: (row["macro_theme"], row["sector"]) in leading_sectors, axis=1)
    df["topdown_score"] = (0.75 * df["rs_score"] + 0.25 * df["sector_power"]).round(2)

    df = df.sort_values(
        ["strict_minervini", "is_actionable", "topdown_score", "rs_score", "pure_rs", "distance_from_high_pct", "ticker"],
        ascending=[False, False, False, False, False, False, True], kind="stable",
    ).reset_index(drop=True)
    df.insert(0, "overall_rank", range(1, len(df) + 1))

    local_sec_rank = df.groupby("sector")["pure_rs"].rank(ascending=False, method="first").astype(int)
    df["sector_rank"] = [global_sector_ranks.get(str(t), lr) for t, lr in zip(df["ticker"], local_sec_rank)]

    status_list, rs_delta_list, streak_list = [], [], []
    for r in df.itertuples():
        tkr = str(r.ticker)
        h = hist_track.get(tkr)
        if not h or int(h.get("streak_prev", 0)) == 0:
            streak_days = 1
            rs_delta = np.nan
            status_tag = "🆕 신규진입"
        else:
            streak_days = int(h["streak_prev"]) + 1
            prev_rs = float(h.get("prev_rs", np.nan))
            rs_delta = round(float(r.rs_score) - prev_rs, 1) if pd.notna(prev_rs) else np.nan
            if pd.notna(rs_delta) and rs_delta >= 5.0:
                status_tag = f"🚀 RS급등({streak_days}일)"
            else:
                status_tag = f"📌 {streak_days}일유지"
        streak_list.append(streak_days)
        rs_delta_list.append(rs_delta)
        status_list.append(status_tag)

    df["streak_days"] = streak_list
    df["rs_delta"] = rs_delta_list
    df["status_tag"] = status_list
    if "earnings_tag" not in df.columns:
        df["earnings_tag"] = ""
    return df

def _select_track_candidates(
    rows: list[dict[str, Any]],
    cfg: dict,
    global_sector_ranks: dict[str, int],
    hist_track: dict[str, dict[str, Any]],
    sub_power_map: dict[str, float] | None = None,
    leading_sectors: set[str] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    all_passed = _annotate_sector_and_history(rows, global_sector_ranks, hist_track, sub_power_map, leading_sectors)
    if all_passed.empty:
        return all_passed, all_passed, all_passed

    main_pool = all_passed[all_passed["tier"] != "추세회복"].copy()
    turnaround_df = all_passed[all_passed["tier"] == "추세회복"].copy().reset_index(drop=True)

    max_total = int(cfg["market"].get("max_candidates_per_track", 20))
    max_sec = int(cfg["market"].get("max_per_sector", 4))
    picked, sec_cnts = [], {}
    for row in main_pool.to_dict("records"):
        sec = row.get("sector") or "일반산업"
        if sec_cnts.get(sec, 0) >= max_sec:
            continue
        picked.append(row)
        sec_cnts[sec] = sec_cnts.get(sec, 0) + 1
        if len(picked) >= max_total:
            break
    out = pd.DataFrame(picked)
    if not out.empty:
        out.insert(0, "rank", range(1, len(out) + 1))
    all_passed.insert(0, "rank", range(1, len(all_passed) + 1))
    return out, all_passed, turnaround_df

# =====================================================================
# 6. 트랙 실행 및 리포트 데이터 생성
# =====================================================================

def _risk_regime(record: dict) -> str:
    if not isinstance(record, dict) or record.get("regime_quality", "ok") != "ok":
        return "UNKNOWN"
    state = record.get("regime")
    if state in ("ATTACK", "CAUTION", "RISK_OFF"):
        return state
    if state is None or state == "RISK_ON":
        # Old snapshots used a binary state and a three-way signal prefix.
        match = re.match(r"^[🟢🟡🔴\s]*(ATTACK|CAUTION|RISK_OFF)\b", str(record.get("signal_light", "")))
        if match:
            return match[1]
    return "UNKNOWN"


def _weight_floor(weight: float) -> float:
    return float(np.floor(weight * 10 + 1e-9) / 10)


def sync_track_sizing_with_final_regime(rows: list[dict[str, Any]], final_regime: dict[str, Any], cfg: dict) -> None:
    """유니버스 Breadth가 반영된 최종 시장 레짐(ATTACK / CAUTION / RISK_OFF)에 맞춰 전 종목 포지션 비중을 재정렬한다."""
    if not rows:
        return
    state = _risk_regime(final_regime)
    sc = cfg.get("screen", {})
    risk_pct = {"RISK_OFF": float(sc.get("risk_off_risk_per_trade_pct", 0.005)),
                "CAUTION": float(sc.get("caution_risk_per_trade_pct", 0.0075)),
                "ATTACK": float(sc.get("risk_per_trade_pct", 0.01))}.get(state, 0.)
    max_pos_cap = float(sc.get("max_position_size_pct", 25.0))
    slippage_pct = float(sc.get("slippage_pct", 0.0015))
    for r in rows:
        raw_stp = pd.to_numeric(r.get("raw_stop_dist_pct"), errors="coerce")
        if not np.isfinite(raw_stp):
            r.update(position_size_pct=0., trade_risk_pct=0.)
            continue
        eff_stop_abs = max(abs(raw_stp) + slippage_pct, 0.02)
        pos_sz = _weight_floor(min(max_pos_cap, (risk_pct / eff_stop_abs) * 100.0))
        r["position_size_pct"] = pos_sz
        r["trade_risk_pct"] = round(pos_sz * eff_stop_abs, 2)

def run_kr_tracks(
    cfg: dict,
    overrides: dict[str, tuple[str, str]],
    hist_track: dict[str, dict[str, Any]],
    *, evaluated_at: datetime | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.DataFrame, dict[str, dict[str, Any]], dict[str, Any]]:
    years = int(cfg["market"]["history_years"])
    pause = float(cfg["runtime"].get("request_pause_seconds", 0.08))
    min_price = float(cfg["market"].get("kr_min_price", 3000.0))
    rs_mode = str(cfg["screen"].get("rs_mode", "STANDARD"))
    ttl = float(cfg["runtime"].get("history_cache_ttl_hours", 12))
    refresh = bool(cfg["runtime"].get("refresh_cache", False))
    below_200 = bool(cfg["market_regime"].get("risk_off_below_200dma", True))
    trim_bar = bool(cfg["market"].get("trim_incomplete_intraday_bar", True))

    kospi_min_val = float(cfg["market"].get("kospi_min_avg_daily_value_krw", 5_000_000_000))
    kosdaq_min_val = float(cfg["market"].get("kosdaq_min_avg_daily_value_krw", 3_000_000_000))

    universe = _protect_sector_assignments(get_kr_universe(cfg, overrides), cfg, overrides)
    sector_review_input = _sector_review_input(universe, cfg)
    kr_sync = bulk_sync_kr_recent_ohlcv(universe["ticker"].astype(str).str.zfill(6).tolist(), cfg)

    kr_workers = int(cfg["runtime"].get("kr_fetch_workers", 4))
    _kr_all = universe["ticker"].astype(str).str.zfill(6).tolist()
    _kr_need = [t for t in _kr_all if kr_history_needs_refresh(KR_HISTORY_CACHE_DIR / f"{t}.pkl", ttl, refresh)]
    prefetched_kr = prefetch_histories(
        _kr_need, lambda t: fetch_kr_history(t, years, is_index=False, cfg=cfg, sync_state=kr_sync), kr_workers, min(pause, 0.06),
        progress_cb=lambda d, n: log_progress(5 + int(5 * d / max(n, 1)), "KR 히스토리 병렬 수집", f"{d}/{n}"),
    )
    if _kr_need:
        LOG.info("[KR] 오래된 캐시 %d종목 중 %d종목 병렬 선수집 (workers=%d)", len(_kr_need), len(prefetched_kr), kr_workers)

    kospi_bench = trim_incomplete_bar(fetch_kr_history(str(cfg["market_regime"].get("kospi_benchmark", "1001")), years, is_index=True, cfg=cfg), "KR", trim_bar)
    kosdaq_bench = trim_incomplete_bar(fetch_kr_history(str(cfg["market_regime"].get("kosdaq_benchmark", "2001")), years, is_index=True, cfg=cfg), "KR", trim_bar)

    pre_reg_kospi = calc_market_regime(kospi_bench, below_200, f"KOSPI ({cfg['market_regime'].get('kospi_benchmark', '1001')})")
    pre_reg_kosdaq = calc_market_regime(kosdaq_bench, below_200, f"KOSDAQ ({cfg['market_regime'].get('kosdaq_benchmark', '2001')})")

    raw_by_track: dict[str, list[dict[str, Any]]] = {"KOSPI": [], "KOSDAQ": []}
    breadth_by_track: dict[str, list[dict[str, Any]]] = {"KOSPI": [], "KOSDAQ": []}
    health_stats = {"kr_universe": len(universe), "kr_processed": 0, "kr_failed": 0, "kr_unclassified": 0, "kr_stale_tickers": 0, "stale_cache": bool(universe["is_stale_cache"].iloc[0]) if ("is_stale_cache" in universe.columns and not universe.empty) else False, "as_of": str(universe["universe_as_of"].iloc[0]) if ("universe_as_of" in universe.columns and not universe.empty) else "-"}
    # Backward-compatible alias: market-cap/universe date, NOT stock close date.
    # Preserve the original YYYYMMDD string (or '-' when unavailable).
    health_stats["kr_universe_as_of"] = health_stats["as_of"]
    total = len(universe)
    kr_price_dates: list[str] = []

    for idx, item in enumerate(universe.itertuples(index=False), 1):
        try:
            tkr = str(item.ticker).zfill(6)
            sub_mkt = str(getattr(item, "sub_market", "KOSPI")).upper()
            b_hist = kospi_bench if sub_mkt == "KOSPI" else kosdaq_bench
            is_risk_off = (pre_reg_kospi if sub_mkt == "KOSPI" else pre_reg_kosdaq).get("regime") == "RISK_OFF"
            min_turnover = kospi_min_val if sub_mkt == "KOSPI" else kosdaq_min_val

            cpath = KR_HISTORY_CACHE_DIR / f"{tkr}.pkl"
            _pre = prefetched_kr.pop(tkr, None)
            if _pre is not None:
                was_fresh = True
                hist = trim_incomplete_bar(_pre, "KR", trim_bar)
            else:
                was_fresh = not kr_history_needs_refresh(cpath, ttl, refresh)
                hist = trim_incomplete_bar(fetch_kr_history(tkr, years, is_index=False, cfg=cfg, sync_state=kr_sync), "KR", trim_bar)
            if not was_fresh and pause > 0:
                time.sleep(min(pause, 0.06))
            if not hist.empty:
                kr_price_dates.append(str(hist.index[-1].date()))
            if hist.empty or float(hist["Close"].iloc[-1]) < min_price:
                continue

            # Breadth(시장 내부 체력)는 거래대금 필터 이전에 전체 유니버스(200거래일 이상)로 측정
            if len(hist) >= int(cfg["screen"].get("min_history_days", 200)):
                _c_s = hist["Close"].dropna()
                if len(_c_s) >= 200:
                    _last_c = float(_c_s.iloc[-1])
                    _s50 = float(_c_s.rolling(50).mean().iloc[-1])
                    _s150 = float(_c_s.rolling(150).mean().iloc[-1])
                    _s200_ser = _c_s.rolling(200).mean()
                    _s200 = float(_s200_ser.iloc[-1])
                    _s200_up = bool(_s200 >= float(_s200_ser.iloc[-21])) if len(_s200_ser.dropna()) >= 21 else True
                    _h252, _l252 = float(_c_s.tail(252).max()), float(_c_s.tail(252).min())
                    _dh = _last_c / _h252 - 1.0 if _h252 > 0 else -1.0
                    _dl = _last_c / _l252 - 1.0 if _l252 > 0 else 1.0
                    breadth_by_track.setdefault(sub_mkt, []).append({
                        "as_of": str(_c_s.index[-1].date()),
                        "above_50dma": bool(_last_c > _s50),
                        "above_200dma": bool(_last_c > _s200),
                        "distance_from_high_pct": _dh,
                        "distance_from_low_pct": _dl,
                        "strict_minervini": bool((_last_c > _s50 > _s150 > _s200) and _s200_up and (_last_c >= _l252 * 1.30) and (_dh >= -0.25)),
                    })

            if "Turnover" in hist and len(hist) >= 20:
                avg_t20 = float(hist["Turnover"].tail(20).mean())
                avg_t5 = float(hist["Turnover"].tail(5).mean())
                if max(avg_t20, avg_t5) < min_turnover:
                    continue

            feat = compute_features(hist, b_hist, cfg, rs_mode=rs_mode, is_risk_off=is_risk_off, is_megacap=(float(item.market_cap) >= 20_000_000_000_000))
            if feat is None:
                continue
            health_stats["kr_processed"] += 1
            if feat.get("is_stale_price", False):
                health_stats["kr_stale_tickers"] = health_stats.get("kr_stale_tickers", 0) + 1
            sec_str = str(getattr(item, "sector", "일반산업"))
            if "미분류" in sec_str:
                health_stats["kr_unclassified"] += 1

            raw_by_track.setdefault(sub_mkt, []).append({
                **feat, "ticker": tkr, "name": str(item.name), "market": "KR",
                "sub_market": sub_mkt, "market_cap": float(item.market_cap), "currency": "KRW",
                "macro_theme": str(getattr(item, "macro_theme", "기타·일반산업")),
                "sector": sec_str, "theme_tags": str(getattr(item, "theme_tags", "")),
                "peg": getattr(item, "peg", np.nan), "eps_growth_pct": getattr(item, "eps_growth_pct", np.nan), "eps_status": getattr(item, "eps_status", "MISSING"),
            })
        except Exception as _exc:
            _swallow("run_kr_tracks", _exc, locals().get("tkr") or locals().get("ticker"))
            health_stats["kr_failed"] += 1
        if idx % 25 == 0 or idx == total:
            log_progress(10 + int(38 * idx / max(total, 1)), "KR 종목 데이터 처리", f"{idx}/{total} 확인")

    health_stats.update(kr_price_health({"KOSPI": kospi_bench, "KOSDAQ": kosdaq_bench}, kr_price_dates,
                                       unavailable_tickers=len(universe) - len(kr_price_dates)))
    if health_stats.get("kr_price_message"):
        LOG.warning("[KR] %s", health_stats["kr_price_message"])
    _exp_kospi = int((universe["sub_market"].astype(str).str.upper() == "KOSPI").sum()) if "sub_market" in universe.columns else 0
    _exp_kosdaq = int((universe["sub_market"].astype(str).str.upper() == "KOSDAQ").sum()) if "sub_market" in universe.columns else 0
    regimes = {
        "KOSPI": calc_market_regime(kospi_bench, below_200, f"KOSPI ({cfg['market_regime'].get('kospi_benchmark', '1001')})", compute_universe_breadth(breadth_by_track.get("KOSPI") or raw_by_track.get("KOSPI", []), _exp_kospi, str(kospi_bench.index[-1].date()) if not kospi_bench.empty else ""), market="KR", evaluated_at=evaluated_at),
        "KOSDAQ": calc_market_regime(kosdaq_bench, below_200, f"KOSDAQ ({cfg['market_regime'].get('kosdaq_benchmark', '2001')})", compute_universe_breadth(breadth_by_track.get("KOSDAQ") or raw_by_track.get("KOSDAQ", []), _exp_kosdaq, str(kosdaq_bench.index[-1].date()) if not kosdaq_bench.empty else ""), market="KR", evaluated_at=evaluated_at),
    }
    for trk_k in ("KOSPI", "KOSDAQ"):
        sync_track_sizing_with_final_regime(raw_by_track.get(trk_k, []), regimes[trk_k], cfg)

    all_kr_raw = raw_by_track.get("KOSPI", []) + raw_by_track.get("KOSDAQ", [])
    kr_sec_totals = Counter(r["sector"] for r in all_kr_raw)

    passed_kospi, fail_kospi = apply_percentile_rs_and_filter(raw_by_track.get("KOSPI", []), cfg, "KOSPI")
    passed_kosdaq, fail_kosdaq = apply_percentile_rs_and_filter(raw_by_track.get("KOSDAQ", []), cfg, "KOSDAQ")
    combined_kr = pd.DataFrame(passed_kospi + passed_kosdaq)

    kr_macro_df, kr_sub_df, kr_sub_power_map = build_topdown_sector_tables(all_kr_raw, combined_kr, hist_track)
    _kr_top5 = kr_sub_df.head(5) if not kr_sub_df.empty else pd.DataFrame()
    if not _kr_top5.empty:
        _kr_abs = _kr_top5[(_kr_top5["sector_power"] >= 62.0) & (_kr_top5["median_rs"] >= 68.0) & (_kr_top5["pct_above_50dma"] >= 45.0)]
        kr_leading_secs = set(zip(_kr_abs["macro_theme"].astype(str), _kr_abs["sector"].astype(str)))
    else:
        kr_leading_secs = set()
    kr_sec_medians = dict(zip(kr_sub_df["sector"], kr_sub_df["median_rs"])) if not kr_sub_df.empty else {}

    global_kr_sec_ranks: dict[str, int] = {}
    if not combined_kr.empty:
        combined_kr = combined_kr.sort_values(["pure_rs", "ticker"], ascending=[False, True], kind="stable")
        combined_kr["sec_rk"] = combined_kr.groupby("sector")["pure_rs"].rank(ascending=False, method="first").astype(int)
        global_kr_sec_ranks = dict(zip(combined_kr["ticker"].astype(str), combined_kr["sec_rk"]))

    res_map, all_map, turn_map = {}, {}, {}
    for trk, p_rows in [("KOSPI", passed_kospi), ("KOSDAQ", passed_kosdaq)]:
        res_map[trk], all_map[trk], turn_map[trk] = _select_track_candidates(
            p_rows, cfg, global_kr_sec_ranks, hist_track, kr_sub_power_map, kr_leading_secs
        )
        res_map[trk] = apply_earnings_sizing_rules(res_map[trk], {}, market="KR")
        all_map[trk] = apply_earnings_sizing_rules(all_map[trk], {}, market="KR")
        LOG.info("[%s] 스크리닝 완료: 선발 %d종목 / 전체통과 %d종목", trk, len(res_map[trk]), len(all_map[trk]))

    kr_merged_all = pd.concat([all_map["KOSPI"], all_map["KOSDAQ"]], ignore_index=True) if (not all_map["KOSPI"].empty or not all_map["KOSDAQ"].empty) else pd.DataFrame()
    extra_meta = {
        "health": health_stats,
        "fail_map": {**fail_kospi, **fail_kosdaq},
        "sector_review_input": sector_review_input,
        "sec_totals": dict(kr_sec_totals),
        "sec_medians": kr_sec_medians,
        "macro_df": kr_macro_df,
        "sub_df": kr_sub_df,
        "turnaround": pd.concat(list(turn_map.values()), ignore_index=True) if turn_map else pd.DataFrame(),
    }
    return res_map, all_map, kr_merged_all, regimes, extra_meta

def run_us_tracks(
    cfg: dict,
    overrides: dict[str, tuple[str, str]],
    hist_track: dict[str, dict[str, Any]],
    *, evaluated_at: datetime | None = None,
) -> tuple[dict[str, pd.DataFrame], dict[str, pd.DataFrame], pd.DataFrame, dict[str, dict[str, Any]], dict[str, Any]]:
    years = int(cfg["market"]["history_years"])
    pause = float(cfg["runtime"].get("request_pause_seconds", 0.08))
    sp_min_cap = float(cfg["market"].get("sp500_min_market_cap_usd", 10_000_000_000))
    ndx_min_cap = float(cfg["market"].get("nasdaq_min_market_cap_usd", 15_000_000_000))
    sp_min_val = float(cfg["market"].get("sp500_min_avg_daily_value_usd", 30_000_000))
    ndx_min_val = float(cfg["market"].get("nasdaq_min_avg_daily_value_usd", 50_000_000))
    min_price = float(cfg["market"].get("us_min_price", 10.0))
    rs_mode = str(cfg["screen"].get("rs_mode", "STANDARD"))
    below_200 = bool(cfg["market_regime"].get("risk_off_below_200dma", True))
    trim_bar = bool(cfg["market"].get("trim_incomplete_intraday_bar", True))

    universe = _protect_sector_assignments(get_us_universe(cfg, overrides), cfg, overrides)
    sector_review_input = _sector_review_input(universe, cfg)
    min_cap_floor = min(sp_min_cap, ndx_min_cap)
    cap_health: dict[str, Any] = {}
    caps, extra_sec, pegs, eps_gs = get_us_market_caps(universe["ticker"].tolist(), min_cap_floor, pause, cfg=cfg, health=cap_health)
    universe = universe[universe["ticker"].isin(caps)].copy()
    universe["market_cap"] = universe["ticker"].map(caps)

    spy_tkr = str(cfg["market_regime"].get("sp500_benchmark", "SPY"))
    qqq_tkr = str(cfg["market_regime"].get("nasdaq_benchmark", "QQQ"))
    histories = fetch_us_histories(list(set(universe["ticker"].tolist() + [spy_tkr, qqq_tkr])), years, cfg=cfg)
    spy_hist = trim_incomplete_bar(histories.get(spy_tkr, pd.DataFrame()), "US", trim_bar)
    qqq_hist = trim_incomplete_bar(histories.get(qqq_tkr, pd.DataFrame()), "US", trim_bar)

    pre_reg_ndx = calc_market_regime(qqq_hist, below_200, f"NASDAQ 100 ({qqq_tkr})")
    pre_reg_sp = calc_market_regime(spy_hist, below_200, f"S&P 500 ({spy_tkr})")

    raw_by_track: dict[str, list[dict[str, Any]]] = {"NASDAQ100": [], "SP500": []}
    breadth_by_track: dict[str, list[dict[str, Any]]] = {"NASDAQ100": [], "SP500": []}
    health_stats = {"us_universe": len(universe), "us_processed": 0, "us_failed": 0, "us_stale_tickers": 0, "stale_cache": bool(universe["is_stale_cache"].iloc[0]) if ("is_stale_cache" in universe.columns and not universe.empty) else False}
    health_stats.update(cap_health)
    total = len(universe)
    exclude_ndx_in_sp = bool(cfg["market"].get("exclude_nasdaq_from_sp500", True))

    for idx, item in enumerate(universe.itertuples(index=False), 1):
        try:
            tkr = str(item.ticker)
            raw_h = histories.get(tkr)
            if raw_h is None or raw_h.empty:
                health_stats["us_failed"] += 1
                continue
            hist = trim_incomplete_bar(raw_h, "US", trim_bar)
            if hist.empty or float(hist["Close"].iloc[-1]) < min_price:
                continue

            dv_series = hist["Close"] * hist["Volume"]
            avg_dv20 = float(dv_series.tail(20).mean()) if len(dv_series) >= 20 else 0.0
            avg_dv5 = float(dv_series.tail(5).mean()) if len(dv_series) >= 5 else 0.0
            eff_dv = max(avg_dv20, avg_dv5)

            in_ndx = bool(getattr(item, "in_nasdaq100", False))
            in_sp = bool(getattr(item, "in_sp500", False))
            m_cap = float(item.market_cap)

            m_th, s_sec = str(getattr(item, "macro_theme", "")), str(getattr(item, "sector", ""))
            if not s_sec or s_sec == "일반산업":
                m_th, s_sec = map_us_sub_industry(extra_sec.get(tkr, ""), "")

            _eg_val = eps_gs.get(tkr, np.nan)
            _eps_st = "GROWTH" if (pd.notna(_eg_val) and float(_eg_val) > 0) else ("DECLINE" if pd.notna(_eg_val) else "MISSING")
            base_info = {
                "ticker": tkr, "name": str(item.name), "market": "US",
                "market_cap": m_cap, "currency": "USD",
                "macro_theme": m_th, "sector": s_sec, "theme_tags": s_sec,
                "peg": pegs.get(tkr, np.nan), "eps_growth_pct": _eg_val, "eps_status": _eps_st,
            }

            if len(hist) >= int(cfg["screen"].get("min_history_days", 200)):
                _c_s = hist["Close"].dropna()
                if len(_c_s) >= 200:
                    _last_c = float(_c_s.iloc[-1])
                    _s50 = float(_c_s.rolling(50).mean().iloc[-1])
                    _s150 = float(_c_s.rolling(150).mean().iloc[-1])
                    _s200_ser = _c_s.rolling(200).mean()
                    _s200 = float(_s200_ser.iloc[-1])
                    _s200_up = bool(_s200 >= float(_s200_ser.iloc[-21])) if len(_s200_ser.dropna()) >= 21 else True
                    _h252, _l252 = float(_c_s.tail(252).max()), float(_c_s.tail(252).min())
                    _dh = _last_c / _h252 - 1.0 if _h252 > 0 else -1.0
                    _dl = _last_c / _l252 - 1.0 if _l252 > 0 else 1.0
                    _b_item = {
                        "as_of": str(_c_s.index[-1].date()),
                        "above_50dma": bool(_last_c > _s50),
                        "above_200dma": bool(_last_c > _s200),
                        "distance_from_high_pct": _dh,
                        "distance_from_low_pct": _dl,
                        "strict_minervini": bool((_last_c > _s50 > _s150 > _s200) and _s200_up and (_last_c >= _l252 * 1.30) and (_dh >= -0.25)),
                    }
                    if in_ndx:
                        breadth_by_track["NASDAQ100"].append(_b_item)
                    if in_sp and (not exclude_ndx_in_sp or not in_ndx):
                        breadth_by_track["SP500"].append(_b_item)

            processed_flag = False
            is_stale_any = False
            if in_ndx and m_cap >= ndx_min_cap and eff_dv >= ndx_min_val:
                feat_q = compute_features(hist, qqq_hist, cfg, rs_mode=rs_mode, is_risk_off=(pre_reg_ndx.get("regime") == "RISK_OFF"), is_megacap=(m_cap >= 200_000_000_000))
                if feat_q is not None:
                    raw_by_track["NASDAQ100"].append({**feat_q, **base_info, "sub_market": "NASDAQ100"})
                    processed_flag = True
                    is_stale_any = is_stale_any or bool(feat_q.get("is_stale_price", False))

            if in_sp and (not exclude_ndx_in_sp or not in_ndx) and m_cap >= sp_min_cap and eff_dv >= sp_min_val:
                feat_s = compute_features(hist, spy_hist, cfg, rs_mode=rs_mode, is_risk_off=(pre_reg_sp.get("regime") == "RISK_OFF"), is_megacap=(m_cap >= 200_000_000_000))
                if feat_s is not None:
                    raw_by_track["SP500"].append({**feat_s, **base_info, "sub_market": "SP500"})
                    processed_flag = True
                    is_stale_any = is_stale_any or bool(feat_s.get("is_stale_price", False))

            if processed_flag:
                health_stats["us_processed"] += 1
                if is_stale_any:
                    health_stats["us_stale_tickers"] = health_stats.get("us_stale_tickers", 0) + 1
        except Exception as _exc:
            _swallow("run_us_tracks", _exc, locals().get("tkr") or locals().get("ticker"))
            health_stats["us_failed"] += 1
        if idx % 25 == 0 or idx == total:
            log_progress(55 + int(32 * idx / max(total, 1)), "US 종목별 추세/RS 계산", f"{idx}/{total} 확인")

    _exp_ndx = int(universe["in_nasdaq100"].sum()) if "in_nasdaq100" in universe.columns else 0
    _exp_sp = int((universe["in_sp500"] & (~universe["in_nasdaq100"] if exclude_ndx_in_sp else True)).sum()) if "in_sp500" in universe.columns else 0
    regimes = {
        "NASDAQ100": calc_market_regime(qqq_hist, below_200, f"NASDAQ 100 ({qqq_tkr})", compute_universe_breadth(breadth_by_track.get("NASDAQ100") or raw_by_track.get("NASDAQ100", []), _exp_ndx, str(qqq_hist.index[-1].date()) if not qqq_hist.empty else ""), market="US", evaluated_at=evaluated_at),
        "SP500": calc_market_regime(spy_hist, below_200, f"S&P 500 ({spy_tkr})", compute_universe_breadth(breadth_by_track.get("SP500") or raw_by_track.get("SP500", []), _exp_sp, str(spy_hist.index[-1].date()) if not spy_hist.empty else ""), market="US", evaluated_at=evaluated_at),
    }
    for trk_u in ("NASDAQ100", "SP500"):
        sync_track_sizing_with_final_regime(raw_by_track.get(trk_u, []), regimes[trk_u], cfg)

    all_us_raw_list = list({r["ticker"]: r for r in (raw_by_track.get("NASDAQ100", []) + raw_by_track.get("SP500", []))}.values())
    us_sec_totals = Counter(r["sector"] for r in all_us_raw_list)

    passed_ndx, fail_ndx = apply_percentile_rs_and_filter(raw_by_track.get("NASDAQ100", []), cfg, "NASDAQ100")
    passed_sp, fail_sp = apply_percentile_rs_and_filter(raw_by_track.get("SP500", []), cfg, "SP500")
    combined_us = pd.DataFrame(passed_ndx + passed_sp).drop_duplicates("ticker")

    us_macro_df, us_sub_df, us_sub_power_map = build_topdown_sector_tables(all_us_raw_list, combined_us, hist_track)
    _us_top5 = us_sub_df.head(5) if not us_sub_df.empty else pd.DataFrame()
    if not _us_top5.empty:
        _us_abs = _us_top5[(_us_top5["sector_power"] >= 62.0) & (_us_top5["median_rs"] >= 68.0) & (_us_top5["pct_above_50dma"] >= 45.0)]
        us_leading_secs = set(zip(_us_abs["macro_theme"].astype(str), _us_abs["sector"].astype(str)))
    else:
        us_leading_secs = set()
    us_sec_medians = dict(zip(us_sub_df["sector"], us_sub_df["median_rs"])) if not us_sub_df.empty else {}

    global_us_sec_ranks: dict[str, int] = {}
    if not combined_us.empty:
        combined_us = combined_us.sort_values(["pure_rs", "ticker"], ascending=[False, True], kind="stable")
        combined_us["sec_rk"] = combined_us.groupby("sector")["pure_rs"].rank(ascending=False, method="first").astype(int)
        global_us_sec_ranks = dict(zip(combined_us["ticker"].astype(str), combined_us["sec_rk"]))

    res_map, all_map, turn_map = {}, {}, {}
    for trk, p_rows in [("NASDAQ100", passed_ndx), ("SP500", passed_sp)]:
        res_map[trk], all_map[trk], turn_map[trk] = _select_track_candidates(
            p_rows, cfg, global_us_sec_ranks, hist_track, us_sub_power_map, us_leading_secs
        )
        LOG.info("[%s] 스크리닝 완료: 선발 %d종목 / 전체통과 %d종목", trk, len(res_map[trk]), len(all_map[trk]))

    target_us_tkrs: list[str] = []
    for trk in ("NASDAQ100", "SP500"):
        if not res_map[trk].empty:
            target_us_tkrs.extend(res_map[trk]["ticker"].astype(str).tolist())
        if not all_map[trk].empty:
            act_or_bz = all_map[trk][all_map[trk]["is_actionable"] | all_map[trk]["in_buy_zone"]]
            target_us_tkrs.extend(act_or_bz["ticker"].astype(str).tolist())
    earn_map = fetch_us_earnings_dates(target_us_tkrs, cfg)
    for trk in ("NASDAQ100", "SP500"):
        res_map[trk] = apply_earnings_sizing_rules(res_map[trk], earn_map, market="US")
        all_map[trk] = apply_earnings_sizing_rules(all_map[trk], earn_map, market="US")

    us_merged_all = pd.concat([all_map["NASDAQ100"], all_map["SP500"]], ignore_index=True).drop_duplicates("ticker").reset_index(drop=True) if (not all_map["NASDAQ100"].empty or not all_map["SP500"].empty) else pd.DataFrame()
    extra_meta = {
        "health": health_stats,
        "fail_map": {**fail_ndx, **fail_sp},
        "sector_review_input": sector_review_input,
        "sec_totals": dict(us_sec_totals),
        "sec_medians": us_sec_medians,
        "macro_df": us_macro_df,
        "sub_df": us_sub_df,
        "turnaround": pd.concat(list(turn_map.values()), ignore_index=True) if turn_map else pd.DataFrame(),
    }
    return res_map, all_map, us_merged_all, regimes, extra_meta

def compute_dropped_candidates(
    prev_selected_map: dict[str, dict[str, Any]],
    track_all_passed: dict[str, pd.DataFrame],
    track_results: dict[str, pd.DataFrame],
    fail_maps: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    if not prev_selected_map:
        return pd.DataFrame()
    observed_tracks = set(track_results) | set(track_all_passed)
    today_selected_tkrs: set[str] = set()
    today_passed_map: dict[str, dict[str, Any]] = {}
    for trk, df in track_results.items():
        if not df.empty:
            today_selected_tkrs.update(df["ticker"].astype(str).tolist())
    for trk, fdf in track_all_passed.items():
        if not fdf.empty:
            for r in fdf.to_dict("records"):
                today_passed_map[str(r["ticker"])] = r

    dropped_rows = []
    for tkr, pinfo in prev_selected_map.items():
        if tkr in today_selected_tkrs:
            continue
        if tkr in fail_maps:
            f = fail_maps[tkr]
            dropped_rows.append({
                "sub_market": pinfo["sub_market"], "ticker": tkr, "name": pinfo["name"],
                "prev_rank": pinfo["prev_rank"], "prev_rs": pinfo["prev_rs"],
                "curr_rs": f["rs_score"], "drop_reason": f["drop_reason"], "exit_signal": f["exit_signal"],
            })
        elif tkr in today_passed_map:
            p = today_passed_map[tkr]
            dropped_rows.append({
                "sub_market": pinfo["sub_market"], "ticker": tkr, "name": pinfo["name"],
                "prev_rank": pinfo["prev_rank"], "prev_rs": pinfo["prev_rs"],
                "curr_rs": float(p.get("rs_score", np.nan)),
                "drop_reason": f"섹터쿼터/순위하락(전체 {p.get('rank', '-')}위)",
                "exit_signal": str(p.get("exit_signal", "✅정상")),
            })
        elif pinfo.get("sub_market") in observed_tracks:
            # No observation is not a pass or a sell signal. Keep the identity
            # visible without inventing an RS value or a specific failure cause.
            dropped_rows.append({
                "sub_market": pinfo["sub_market"], "ticker": tkr, "name": pinfo["name"],
                "prev_rank": pinfo["prev_rank"], "prev_rs": pinfo["prev_rs"],
                "curr_rs": np.nan,
                "drop_reason": "관찰자료없음(유니버스·시세·선행필터 확인필요)",
                "exit_signal": "확인불가(관찰자료없음)",
            })
    return pd.DataFrame(dropped_rows)

_KR_SUBS = {"KOSPI", "KOSDAQ"}
_US_SUBS = {"NASDAQ100", "SP500"}

def _regimes_subset(regimes: dict[str, dict], mkt: str) -> dict[str, dict]:
    regimes = regimes or {}
    keys = _KR_SUBS if mkt == "KR" else (_US_SUBS if mkt == "US" else None)
    if keys is None:
        return regimes
    return {k: v for k, v in regimes.items() if k in keys}

def _row_market(market: Any, sub_market: Any) -> str:
    m = str(market or "").upper()
    if m in ("KR", "US"):
        return m
    sm = str(sub_market or "").upper()
    if sm in _KR_SUBS:
        return "KR"
    if sm in _US_SUBS:
        return "US"
    return "ALL"

def _exposure_rule(regimes: dict[str, dict], cfg: dict) -> tuple[float, float, bool]:
    """(총 노출 상한 %, 총 손절리스크 상한 %, 방어모드 여부) - 주어진 레짐 집합 기준"""
    states = [_risk_regime(r) for r in (regimes or {}).values()]
    if not states or "UNKNOWN" in states:
        return 0., 0., True
    attack_ratio = states.count("ATTACK") / len(states)
    risk_off_ratio = states.count("RISK_OFF") / len(states)
    if attack_ratio >= 0.75:
        exposure_cap = 100.0
    elif attack_ratio >= 0.25 and risk_off_ratio <= 0.25:
        exposure_cap = 70.0
    elif risk_off_ratio >= 0.75:
        exposure_cap = 25.0
    else:
        exposure_cap = 50.0
    sc = cfg.get("screen", {})
    is_def = risk_off_ratio >= 0.5
    risk_cap = float(sc.get("risk_off_max_portfolio_risk_pct", 2.5) if is_def else sc.get("max_portfolio_risk_pct", 5.0))
    return exposure_cap, risk_cap, is_def

def allocate_portfolio_risk_budget(action_df: pd.DataFrame, regimes: dict[str, dict], cfg: dict, held: dict | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    시장별(KR/US) 노출 한도·리스크 한도를 각 시장 자체 레짐으로 산출하고, 계좌 합계는
    노출 min(100%, 시장 한도 합) / 리스크 max(시장 한도)로 제한한다. 섹터 한도는 (시장, 섹터) 단위.
    action_df에 market/sub_market 컬럼이 없으면 전체 레짐을 쓰는 단일 그룹(ALL)으로 동작(하위호환).
    """
    if action_df is None or action_df.empty:
        return pd.DataFrame(), {"total_alloc_pct": 0.0, "total_risk_pct": 0.0, "exposure_cap_pct": 0.0, "risk_cap_pct": 0.0, "by_market": {}}

    regimes = regimes or {}
    if held is not None:
        reasons = list(held.get("reasons") or [])
        if held.get("complete") is not True:
            reasons.append("portfolio_unconfirmed")
        positions = held.get("positions", [])
        markets = {_row_market(getattr(r, "market", None), getattr(r, "sub_market", None)) for r in action_df.itertuples()}
        markets.update(p.get("market") for p in positions)
        if any(m not in ("KR", "US") or _exposure_rule(_regimes_subset(regimes, m), cfg)[0] == 0 for m in markets):
            reasons.append("regime_missing")
        for p in positions:
            if p.get("market") not in ("KR", "US") or not p.get("sector") or not p.get("macro_theme"):
                reasons.append("classification_missing")
            for key in ("exposure_pct", "risk_pct"):
                value = p.get(key)
                if not isinstance(value, (int, float)) or not np.isfinite(value) or value < 0:
                    reasons.append("held_value_invalid")
        for col in ("market", "ticker", "sector", "macro_theme"):
            if col not in action_df or action_df[col].isna().any() or action_df[col].astype(str).str.strip().eq("").any():
                reasons.append("candidate_classification_missing")
        for col in ("position_size_pct", "raw_stop_dist_pct"):
            if col not in action_df or not pd.to_numeric(action_df[col], errors="coerce").map(np.isfinite).all():
                reasons.append("candidate_risk_missing")
        if reasons:
            out = action_df.copy()
            out["portfolio_alloc_pct"] = 0.0
            out["portfolio_risk_contrib_pct"] = 0.0
            out["portfolio_alloc_note"] = "검증미완료"
            return out, {"status": "blocked", "reasons": sorted(set(reasons)), "total_alloc_pct": 0.0,
                         "total_risk_pct": 0.0, "by_market": {}}
    sc = cfg.get("screen", {})
    sec_cap = float(sc.get("max_sector_alloc_pct", 35.0))
    global_theme_cap = float(sc.get("max_global_theme_alloc_pct", 45.0))
    slip = float(sc.get("slippage_pct", 0.0015))

    out = action_df.copy()
    if held is not None:
        out = out.drop_duplicates(["market", "ticker"], keep="first")
    mk_list = [_row_market(getattr(r, "market", None), getattr(r, "sub_market", None)) for r in out.itertuples()]
    rule_markets = list(dict.fromkeys(mk_list + ([p["market"] for p in held["positions"]] if held is not None else [])))
    legacy_all = "market" not in out and "sub_market" not in out
    rules = {m: _exposure_rule(_regimes_subset(regimes, m) if m != "ALL" or legacy_all else {}, cfg)
             for m in rule_markets}
    total_exp_cap = min(100.0, sum(v[0] for v in rules.values())) if len(rules) > 1 else next(iter(rules.values()))[0]
    total_risk_cap = max(v[1] for v in rules.values())

    used_exp = {m: 0.0 for m in rules}
    used_risk = {m: 0.0 for m in rules}
    tot_exp = 0.0
    tot_risk = 0.0
    sec_used: dict[tuple[str, str], float] = {}
    theme_used: dict[str, float] = {}
    symbol_used = {}
    if held is not None:
        for p in held["positions"]:
            m, exposure, risk = p["market"], p["exposure_pct"], p["risk_pct"]
            used_exp[m] += exposure
            used_risk[m] += risk
            tot_exp += exposure
            tot_risk += risk
            sec = (m, p["sector"])
            sec_used[sec] = sec_used.get(sec, 0.0) + exposure
            theme_used[p["macro_theme"]] = theme_used.get(p["macro_theme"], 0.0) + exposure
            symbol = (m, p["code"])
            symbol_used[symbol] = symbol_used.get(symbol, 0.0) + exposure
    over_limit = held is not None and (tot_exp > total_exp_cap + 1e-9 or tot_risk > total_risk_cap + 1e-9
        or any(used_exp[m] > rules[m][0] + 1e-9 or used_risk[m] > rules[m][1] + 1e-9 for m in rules))
    initial_exp, initial_risk = tot_exp, tot_risk
    initial_market = {m: (used_exp[m], used_risk[m]) for m in rules}
    alloc_list, eff_risk_list, note_list = [], [], []

    for r, m in zip(out.itertuples(), mk_list):
        raw_pos = pd.to_numeric(getattr(r, "position_size_pct", None), errors="coerce")
        stop = pd.to_numeric(getattr(r, "raw_stop_dist_pct", None), errors="coerce")
        risk_known = np.isfinite(stop) and np.isfinite(raw_pos) and raw_pos >= 0.
        eff_stp = max(abs(stop) + slip, 0.02) if risk_known else np.nan
        sec = (m, str(getattr(r, "sector", "일반산업")))
        m_theme = str(getattr(r, "macro_theme", "") or getattr(r, "sector", "일반산업"))
        exp_cap_m, risk_cap_m, _ = rules[m]

        rem_exposure = max(0.0, min(exp_cap_m - used_exp[m], total_exp_cap - tot_exp))
        rem_sec = max(0.0, sec_cap - sec_used.get(sec, 0.0))
        rem_theme = max(0.0, global_theme_cap - theme_used.get(m_theme, 0.0))
        rem_risk_pos = max(0.0, min(risk_cap_m - used_risk[m], total_risk_cap - tot_risk) / eff_stp)

        if over_limit:
            raw_pos = 0.0
        if held is not None:
            rem_symbol = max(0.0, float(sc.get("max_position_size_pct", 25.0)) - symbol_used.get((m, str(r.ticker)), 0.0))
            raw_pos = min(raw_pos, rem_symbol)
        track = getattr(r, "sub_market", None)
        track_missing = track is not None and track not in _regimes_subset(regimes, m)
        adj_pos = _weight_floor(min(raw_pos, rem_exposure, rem_sec, rem_theme, rem_risk_pos)) if risk_known else 0.
        if track_missing or exp_cap_m == 0:
            adj_pos, note = 0., "레짐 미확인"
        elif not risk_known:
            adj_pos, note = 0., "종목 위험 미확인"
        elif adj_pos < 2.0:
            adj_pos, note = 0.0, "한도초과대기"
        elif adj_pos < raw_pos:
            note = "비중축소(한도)"
        else:
            note = "정상배분"

        r_contrib = adj_pos * eff_stp if adj_pos else 0.
        if held is not None:
            symbol_used[(m, str(r.ticker))] = symbol_used.get((m, str(r.ticker)), 0.0) + adj_pos
        used_exp[m] += adj_pos
        used_risk[m] += r_contrib
        tot_exp += adj_pos
        tot_risk += r_contrib
        sec_used[sec] = sec_used.get(sec, 0.) + adj_pos
        theme_used[m_theme] = theme_used.get(m_theme, 0.) + adj_pos
        alloc_list.append(adj_pos)
        eff_risk_list.append(r_contrib)
        note_list.append(note)

    out["portfolio_alloc_pct"] = alloc_list
    out["portfolio_risk_contrib_pct"] = eff_risk_list
    out["portfolio_alloc_note"] = note_list
    out["portfolio_market"] = mk_list
    meta = {
        "total_alloc_pct": tot_exp,
        "total_risk_pct": tot_risk,
        "exposure_cap_pct": total_exp_cap,
        "risk_cap_pct": total_risk_cap,
        "by_market": {m: {"alloc": used_exp[m], "risk": used_risk[m], "exposure_cap": rules[m][0], "risk_cap": rules[m][1]} for m in rules},
    }
    if held is not None:
        meta.update(status="blocked" if over_limit else "ready", reasons=["held_limits_exceeded"] if over_limit else [], held_alloc_pct=initial_exp, held_risk_pct=initial_risk,
                    combined_alloc_pct=tot_exp, combined_risk_pct=tot_risk,
                    total_alloc_pct=tot_exp-initial_exp, total_risk_pct=tot_risk-initial_risk)
        for m in rules:
            meta["by_market"][m]["alloc"] -= initial_market[m][0]
            meta["by_market"][m]["risk"] -= initial_market[m][1]
    return out, meta

def _make_bench_getter(cfg: dict) -> Callable[[str, str], pd.DataFrame]:
    """sub_market/market 에 맞는 벤치마크 캐시(DataFrame)를 돌려주는 함수를 만든다."""
    mr = cfg.get("market_regime", {})
    files = {
        "KOSPI": KR_HISTORY_CACHE_DIR / f"idx_{mr.get('kospi_benchmark', '1001')}.pkl",
        "KOSDAQ": KR_HISTORY_CACHE_DIR / f"idx_{mr.get('kosdaq_benchmark', '2001')}.pkl",
        "NASDAQ100": US_HISTORY_CACHE_DIR / f"{mr.get('nasdaq_benchmark', 'QQQ')}.pkl",
        "SP500": US_HISTORY_CACHE_DIR / f"{mr.get('sp500_benchmark', 'SPY')}.pkl",
    }
    cache: dict[str, pd.DataFrame] = {}

    def _get(sub_mkt: str, mkt: str) -> pd.DataFrame:
        key = sub_mkt if sub_mkt in files else ("KOSPI" if mkt == "KR" else "SP500")
        if key not in cache:
            bdf = pd.DataFrame()
            if files[key].exists():
                try:
                    bdf = normalize_index(pd.read_pickle(files[key]))
                    if key in ("KOSPI", "KOSDAQ"):
                        bdf = kr_completed_cached_bars(files[key], bdf)
                    else:
                        bdf = us_completed_cached_bars(files[key], bdf)
                except Exception as _exc:
                    _swallow("_make_bench_getter", _exc, key)
            cache[key] = bdf
        return cache[key]
    return _get

def compute_forward_validation(today_str: str, cfg: dict) -> pd.DataFrame:
    if not cfg.get("reports", {}).get("enable_forward_validation", True):
        return pd.DataFrame()

    past_dirs = sorted(
        [d for d in REPORT_DIR.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name) and d.name < today_str],
        key=lambda x: x.name,
    )[-120:] if REPORT_DIR.exists() else []

    pub_dir = ROOT / str(cfg.get("publish", {}).get("dir", "published"))
    cand_hist_path = pub_dir / "history" / "candidates_daily.csv"
    hist_snaps: dict[str, pd.DataFrame] = {}
    if cand_hist_path.exists():
        try:
            ch_df = pd.read_csv(cand_hist_path, dtype={"ticker": str})
            if not ch_df.empty and "date" in ch_df.columns and "setup_stage" in ch_df.columns:
                if "selected" in ch_df.columns:
                    ch_df = ch_df[ch_df["selected"].astype(str).str.lower().isin({"true", "1", "yes"})]
                for dt_val, grp in ch_df[ch_df["date"].astype(str) < today_str].groupby("date"):
                    hist_snaps[str(dt_val)] = grp
        except Exception as _exc:
            _swallow("compute_forward_validation_hist", _exc)

    dir_map = {d.name: d for d in past_dirs}
    all_snap_dates = sorted(set(dir_map.keys()) | set(hist_snaps.keys()))[-180:]
    if len(all_snap_dates) < 3:
        return pd.DataFrame()

    rcfg = cfg.get("reports", {})
    cooldown_days = int(rcfg.get("validation_cooldown_days", 14))
    _bench = _make_bench_getter(cfg)

    records = []
    price_cache: dict[str, pd.DataFrame] = {}
    last_counted: dict[str, pd.Timestamp] = {}

    def _load_cached_hist(mkt: str, tkr: str) -> pd.DataFrame:
        key = f"{mkt}:{tkr}"
        if key in price_cache:
            return price_cache[key]
        cpath = (KR_HISTORY_CACHE_DIR / f"{tkr.zfill(6)}.pkl") if mkt in ("KR", "KOSPI", "KOSDAQ") else (US_HISTORY_CACHE_DIR / f"{tkr}.pkl")
        if cpath.exists():
            try:
                price_cache[key] = pd.read_pickle(cpath)
                if mkt in ("KR", "KOSPI", "KOSDAQ"):
                    price_cache[key] = kr_completed_cached_bars(cpath, price_cache[key])
                else:
                    price_cache[key] = us_completed_cached_bars(cpath, price_cache[key])
                return price_cache[key]
            except Exception as _exc:
                _swallow("_load_cached_hist", _exc, tkr)
        price_cache[key] = pd.DataFrame()
        return price_cache[key]

    rt_cost_pct = float(cfg.get("screen", {}).get("round_trip_cost_pct", 0.003)) * 100.0

    def _hret(frame: pd.DataFrame, entry_p: float, n: int, cost: float) -> float:
        return ((float(frame["Close"].iloc[n - 1]) / entry_p - 1.0) * 100.0 - cost) if len(frame) >= n else np.nan

    for s_date in all_snap_dates:
        if s_date in dir_map and (dir_map[s_date] / "all_candidates.csv").exists():
            try:
                df = pd.read_csv(dir_map[s_date] / "all_candidates.csv", dtype={"ticker": str})
            except Exception as _exc:
                _swallow("compute_forward_validation", _exc, s_date)
                df = hist_snaps.get(s_date, pd.DataFrame())
        else:
            df = hist_snaps.get(s_date, pd.DataFrame())
        if df.empty or "setup_stage" not in df.columns:
            continue
        snap_dt = pd.Timestamp(s_date)
        for r in df.itertuples():
            mkt = str(getattr(r, "market", "KR")).upper()
            sub_mkt = str(getattr(r, "sub_market", "")).upper()
            tkr = str(r.ticker).zfill(6) if mkt == "KR" else str(r.ticker).upper()
            as_of = pd.Timestamp(getattr(r, "as_of", snap_dt))
            if pd.isna(as_of):
                as_of = snap_dt
            # 쿨다운 중복제거: 같은 종목이 연속으로 리스트에 남아도 cooldown_days 안에서는 1건만 집계
            ck = f"{mkt}:{tkr}"
            if ck in last_counted and (as_of - last_counted[ck]).days < cooldown_days:
                continue
            hdf = _load_cached_hist(mkt, tkr)
            if hdf.empty or "Close" not in hdf.columns:
                continue
            after = hdf.loc[hdf.index > as_of]
            if len(after) < 5:
                continue
            entry_bar = after.iloc[0]
            if bool(entry_bar.get("IsHalt", False)):
                continue
            entry_p = float(entry_bar["Open"]) if ("Open" in after.columns and float(entry_bar["Open"]) > 0) else float(entry_bar["Close"])
            if entry_p <= 0:
                continue
            last_counted[ck] = as_of

            ret = {n: _hret(after, entry_p, n, rt_cost_pct) for n in (5, 10, 20)}
            if len(after) >= 10:
                low_10d = float(after["Low"].iloc[0:10].min()) if "Low" in after.columns else float(after["Close"].iloc[0:10].min())
                mae_10d = (low_10d / entry_p - 1.0) * 100.0 - rt_cost_pct
            else:
                mae_10d = np.nan

            # 같은 진입일(시가) 기준 벤치마크 수익률 -> 초과수익
            ex = {5: np.nan, 10: np.nan, 20: np.nan}
            bdf = _bench(sub_mkt, mkt)
            if not bdf.empty and entry_bar.name in bdf.index:
                b_after = bdf.loc[bdf.index >= entry_bar.name]
                if not b_after.empty:
                    b_open = float(b_after["Open"].iloc[0]) if ("Open" in b_after.columns and float(b_after["Open"].iloc[0]) > 0) else float(b_after["Close"].iloc[0])
                    if b_open > 0:
                        for n in (5, 10, 20):
                            b_r = _hret(b_after, b_open, n, 0.0)
                            if pd.notna(ret[n]) and pd.notna(b_r):
                                ex[n] = ret[n] - b_r
            records.append({
                "setup_stage": str(r.setup_stage), "ticker": ck,
                "ret_5d": ret[5], "ret_10d": ret[10], "ret_20d": ret[20], "mae_10d": mae_10d,
                "ex_5d": ex[5], "ex_10d": ex[10], "ex_20d": ex[20],
            })

    if not records:
        return pd.DataFrame()
    vdf = pd.DataFrame(records)

    def _agg(g: pd.DataFrame, label: str) -> dict[str, Any]:
        def _m(col: str) -> float:
            x = g[col].dropna()
            return round(float(x.mean()), 2) if len(x) else np.nan
        r5 = g["ret_5d"].dropna()
        e10 = g["ex_10d"].dropna()
        r10 = g["ret_10d"].dropna()
        return {
            "setup_stage": label, "sample_cnt": int(g["ret_5d"].count()), "uniq_cnt": int(g["ticker"].nunique()),
            "win_rate_5d": round(float((r5 > 0).mean() * 100.0), 1) if len(r5) else np.nan,
            "avg_ret_5d": _m("ret_5d"), "avg_ret_10d": _m("ret_10d"), "avg_ret_20d": _m("ret_20d"),
            "med_ret_10d": round(float(r10.median()), 2) if len(r10) else np.nan,
            "avg_ex_10d": _m("ex_10d"), "avg_ex_20d": _m("ex_20d"),
            "win_rate_ex_10d": round(float((e10 > 0).mean() * 100.0), 1) if len(e10) else np.nan,
            "avg_mae_10d": _m("mae_10d"),
        }

    rows = [_agg(g, str(k)) for k, g in vdf.groupby("setup_stage")]
    summary = pd.DataFrame(rows).sort_values("avg_ex_10d", ascending=False, na_position="last").reset_index(drop=True)
    base = pd.DataFrame([_agg(vdf, "📊 전체(기준선)")])
    return pd.concat([summary, base], ignore_index=True)

# =====================================================================
# 7. 가독성 개선: Top-Down Markdown 리포트 & HTML 뷰어 렌더러
# =====================================================================
def _esc_md(text: Any) -> str:
    return str(text if text is not None else "").replace("|", "·").replace("\n", " ").strip()

def _tv_url(market: str, ticker: str) -> str:
    tkr = _esc_md(ticker)
    if market.upper() in ("KR", "KOSPI", "KOSDAQ"):
        return f"https://kr.tradingview.com/chart/?symbol=KRX:{tkr}"
    return f"https://kr.tradingview.com/chart/?symbol={tkr.replace('-', '.')}"

def _info_url(market: str, ticker: str) -> tuple[str, str]:
    tkr = _esc_md(ticker)
    if market.upper() in ("KR", "KOSPI", "KOSDAQ"):
        return f"https://finance.naver.com/item/main.naver?code={tkr}", "네이버"
    return f"https://finance.yahoo.com/quote/{tkr}", "Yahoo"

def _format_stock_link_cell(market: str, ticker: str, name: str, earnings_tag: str = "") -> str:
    tv = _tv_url(market, ticker)
    inf_u, inf_lbl = _info_url(market, ticker)
    e_badge = f" {_color_span(_esc_md(earnings_tag), '#DC2626', bold=True)}" if earnings_tag else ""
    return f"**[{_esc_md(name)}]({tv})** (`{ticker}`) · [{inf_lbl}]({inf_u}){e_badge}"

def _format_setup_consistent(setup_stage: str, rs_blue_dot: bool = False) -> str:
    s = _esc_md(setup_stage)
    bd = " 🔵블루닷" if rs_blue_dot else ""
    if "과열" in s: return _color_span(s, "#DC2626", bold=True) + bd
    if any(k in s for k in ("돌파", "VCP", "수축", "포켓")): return _color_span(s, "#059669", bold=True) + bd
    if "눌림목" in s: return _color_span(s, "#2563EB", bold=True) + bd
    return s + bd

def _format_status_consistent(status_tag: str) -> str:
    s = _esc_md(status_tag)
    if "신규" in s or "급등" in s: return _color_span(s, "#059669", bold=True)
    return _color_span(s, "#2563EB", bold=False)

def _format_exit_cell(exit_sig: str, dist_days: int) -> str:
    s = _esc_md(exit_sig)
    if "🚨" in s or "⚠️" in s: return _color_span(f"{s} (분배{dist_days}일)", "#DC2626", bold=True)
    return _color_span(f"✅정상 (분배{dist_days}일)", "#059669", bold=False)

def _render_topdown_sector_section(mkt_label: str, macro_df: pd.DataFrame, sub_df: pd.DataFrame) -> list[str]:
    lines: list[str] = []
    if (macro_df is None or macro_df.empty) and (sub_df is None or sub_df.empty):
        return lines

    lines.append(f"### 🧭 {mkt_label} 탑다운 섹터 강도 & 로테이션 매트릭스")
    if macro_df is not None and not macro_df.empty:
        lines.append(f"#### ① [{mkt_label}] 주섹터(대테마) 강도 & 내부 체력 비교")
        lines.append("| 순위 | 주섹터 (대테마) | 테마 파워 게이지 | 로테이션 국면 | 50일선 상회% | RS 중앙값 (4주Δ) | 통과율 (통과/전체·TierA) | 핵심 선도 서브섹터 (대장주) |")
        lines.append("| :---: | :--- | :--- | :--- | :---: | :---: | :--- | :--- |")
        for r in macro_df.itertuples():
            bar = _make_strength_bar(r.macro_power, width=8)
            pwr_str = f"`{bar}` **{r.macro_power:.0f}**"
            d4w_str = f"{r.rs_delta_4w:+.1f}"
            ab50_col = "#059669" if r.pct_above_50dma >= 55.0 else ("#DC2626" if r.pct_above_50dma < 35.0 else "#334155")
            ab50_str = _color_span(f"{r.pct_above_50dma:.0f}%", ab50_col, bold=(r.pct_above_50dma >= 55.0))
            pass_str = f"**{r.pass_rate_pct:.0f}%** ({r.passed_cnt}/{r.univ_total} · A:{r.tier_a_cnt} · 🆕신규:{r.new_cnt} · 🎯액션:{r.actionable_cnt})"
            lines.append(
                f"| **{r.macro_rank}** | **{_esc_md(r.macro_theme)}** ({r.sub_sector_cnt}개 서브) | {pwr_str} | {r.rotation_phase} | "
                f"{ab50_str} | **{r.median_rs:.1f}** ({d4w_str}) | {pass_str} | **{_esc_md(r.top_sub_sector)}** · {_esc_md(r.top_leader)} |"
            )
        lines.append("")

    if sub_df is not None and not sub_df.empty:
        lines.append(f"#### ② [{mkt_label}] 서브섹터(밸류체인) 강도 Top 12 & 대장주")
        lines.append("| 순위 | 주섹터 ▸ 서브섹터 | 서브섹터 강도 | 로테이션 국면 | 50일선 상회% | RS 중앙 (상위3·4주Δ) | 통과율 (돌파·VCP응집) | 섹터 1등 대장주 (셋업) |")
        lines.append("| :---: | :--- | :--- | :--- | :---: | :---: | :--- | :--- |")
        for r in sub_df.head(12).itertuples():
            bar = _make_strength_bar(r.sector_power, width=8)
            pwr_str = f"`{bar}` **{r.sector_power:.0f}**"
            ab50_col = "#059669" if r.pct_above_50dma >= 60.0 else ("#DC2626" if r.pct_above_50dma < 35.0 else "#334155")
            ab50_str = _color_span(f"{r.pct_above_50dma:.0f}%", ab50_col, bold=(r.pct_above_50dma >= 60.0))
            rs_info = f"**{r.median_rs:.1f}** (Top3 {r.top3_rs:.0f} · {r.rs_delta_4w:+.1f})"
            pass_info = f"**{r.pass_rate_pct:.0f}%** ({r.passed_cnt}/{r.univ_total} · 🔥수축/돌파:{r.setup_cnt} · 🆕신규:{r.new_cnt})"
            l_tv = _tv_url(mkt_label, str(r.leader_ticker))
            bd = " 🔵" if r.leader_blue_dot else ""
            leader_cell = f"**[{_esc_md(r.leader_name)}]({l_tv})** (`{r.leader_ticker}` · RS {r.leader_rs:.0f} · {_esc_md(r.leader_setup)}{bd})"
            theme_sec = f"{_esc_md(r.macro_theme)} ▸ **{_esc_md(r.sector)}**"
            lines.append(
                f"| **{r.sub_rank}** | {theme_sec} | {pwr_str} | {r.rotation_phase} | {ab50_str} | {rs_info} | {pass_info} | {leader_cell} |"
            )
        lines.append("")
    return lines

def _render_2tier_candidate_table(track_key: str, section_title: str, df: pd.DataFrame, is_action_table: bool = False) -> list[str]:
    lines: list[str] = []
    lines.append(f"### {section_title}")
    if df.empty:
        lines.append("> 조건을 만족하는 종목이 없습니다.\n")
        return lines

    lines.append("| 순위 | 종목명 (티커 · 정보) | 소속 섹터 (강도 · 서열) | 셋업 단계 (선행신호) | RS (전일비 · 4주Δ) | 피벗거리 (바이존) | 손절폭 · 권장비중 | 상태 · 보유청산신호 |")
    lines.append("| :---: | :--- | :--- | :--- | :---: | :---: | :---: | :--- |")

    for r in df.itertuples():
        trk_mkt = getattr(r, "sub_market", track_key)
        rank_value = getattr(r, "rank", None)
        if rank_value is None or pd.isna(rank_value):
            rank_value = getattr(r, "overall_rank", "-")
        tier_value = str(getattr(r, "tier", ""))
        rk_label = f"**{rank_value}** ({tier_value[-1]})" if tier_value in ("Tier A", "Tier B") else f"**{rank_value}**"
        if is_action_table:
            rk_label = f"**{trk_mkt}** #{rank_value}"

        stock_cell = _format_stock_link_cell(trk_mkt, str(r.ticker), str(r.name), str(getattr(r, "earnings_tag", "")))
        sec_pwr = float(getattr(r, "sector_power", 50.0))
        is_lead_sec = bool(getattr(r, "is_leading_sector", False))
        sec_rk = int(getattr(r, "sector_rank", 99))
        lead_badge = _color_span("👑주도", "#059669", bold=True) if is_lead_sec else "일반"
        sec_cell = f"{lead_badge} **{_esc_md(r.sector)}** (파워{sec_pwr:.0f} · {sec_rk}위)"
        setup_cell = _format_setup_consistent(str(r.setup_stage), bool(getattr(r, "rs_blue_dot", False)))

        rs_d = getattr(r, "rs_delta", np.nan)
        d_str = f"{rs_d:+.1f}" if pd.notna(rs_d) else "-"
        rs_4w = getattr(r, "rs_change_4w", 0.0)
        rs_cell = f"**{r.rs_score:.1f}** ({d_str} · 4주{rs_4w:+.0f})"

        dpiv = float(getattr(r, "dist_to_pivot_pct", 0.0)) * 100.0
        if getattr(r, "in_buy_zone", False):
            piv_cell = _color_span(f"{dpiv:+.1f}% (바이존)", "#059669", bold=True)
        elif dpiv > 5.0:
            piv_cell = _color_span(f"{dpiv:+.1f}% (추격주의)", "#DC2626", bold=False)
        else:
            piv_cell = f"{dpiv:+.1f}% (대기)"

        raw_stp = float(getattr(r, "raw_stop_dist_pct", -0.05)) * 100.0
        pos_sz = float(getattr(r, "position_size_pct", 10.0))
        port_alloc = getattr(r, "portfolio_alloc_pct", None)
        if getattr(r, "stop_warning", False):
            risk_cell = _color_span(f"{raw_stp:.1f}% (폭초과·비중{pos_sz:.0f}%)", "#DC2626", bold=True)
        elif is_action_table and port_alloc is not None and pd.notna(port_alloc):
            risk_cell = f"{raw_stp:.1f}% · **포트 {float(port_alloc):.1f}%** (개별 {pos_sz:.0f}%)"
        else:
            risk_cell = f"{raw_stp:.1f}% · **비중 {pos_sz:.1f}%**"

        st_part = _format_status_consistent(str(getattr(r, "status_tag", "🆕 신규진입")))
        ex_part = _format_exit_cell(str(getattr(r, "exit_signal", "✅정상")), int(getattr(r, "dist_days_25d", 0)))
        status_exit_cell = f"{st_part} / {ex_part}"

        lines.append(f"| {rk_label} | {stock_cell} | {sec_cell} | {setup_cell} | {rs_cell} | {piv_cell} | {risk_cell} | {status_exit_cell} |")

    lines.append("")
    lines.append("<details>")
    lines.append(f"<summary>🔎 <b>{section_title} — 세부 가격·변동폭·펀더멘털 상세표 펼치기</b></summary>")
    lines.append("")
    lines.append("| 종목명 (티커) | 대테마 ▸ 세부섹터 (서열) | 종가 / 피벗가 / 권장손절가 | 52주고점괴리 | 5일/10일/20일 변동폭 | 50일선 ATR배수 | FAST RS / PEG |")
    lines.append("| :--- | :--- | :--- | :---: | :---: | :---: | :---: |")

    for r in df.itertuples():
        trk_mkt = getattr(r, "sub_market", track_key)
        is_row_kr = str(trk_mkt).upper() in ("KR", "KOSPI", "KOSDAQ")
        p_fmt = (lambda v: f"{v:,.0f}원") if is_row_kr else (lambda v: f"${v:,.2f}")
        price_detail = f"**{p_fmt(r.last_close)}** / 피벗 {p_fmt(r.pivot_price)} / 손절 {p_fmt(r.stop_price)}"
        sec_info = f"{_esc_md(r.macro_theme)} ▸ **{_esc_md(r.sector)}** ({int(getattr(r, 'sector_rank', 99))}위)"
        dh_str = f"{float(r.distance_from_high_pct)*100:+.1f}%"
        rng_str = f"{float(getattr(r, 'range_5d_pct', 0))*100:.1f}% / {float(r.range_10d_pct)*100:.1f}% / {float(getattr(r, 'range_20d_pct', 0))*100:.1f}%"
        atr_m = f"{float(getattr(r, 'atr_from_50dma', 0.0)):.1f}배"
        peg_v = getattr(r, "peg", np.nan)
        peg_s = f"{float(peg_v):.2f}" if pd.notna(peg_v) and float(peg_v) > 0 else "-"
        fast_rs = float(getattr(r, "rs_score_fast", r.rs_score))
        lines.append(f"| **{_esc_md(r.name)}** (`{r.ticker}`) | {sec_info} | {price_detail} | {dh_str} | {rng_str} | {atr_m} | {fast_rs:.1f} / {peg_s} |")

    lines.append("</details>")
    lines.append("")
    return lines

def generate_sector_dashboard_png(
    d_str: str, kr_all: pd.DataFrame, us_all: pd.DataFrame,
    kr_sec_totals: dict[str, int], us_sec_totals: dict[str, int],
    out_paths: list[Path],
) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patheffects as pe
        from matplotlib import font_manager
        from matplotlib.colors import LinearSegmentedColormap
    except ImportError:
        return False
    ko_fonts = ["Malgun Gothic", "NanumGothic", "AppleSDGothicNeo", "Noto Sans CJK KR"]
    available = {f.name for f in font_manager.fontManager.ttflist}
    chosen_font = next((f for f in ko_fonts if f in available), None)
    if chosen_font: plt.rcParams["font.family"] = chosen_font
    plt.rcParams["axes.unicode_minus"] = False

    all_passed_map = {"KR (KOSPI+KOSDAQ)": (kr_all, kr_sec_totals), "US (NASDAQ100+S&P500)": (us_all, us_sec_totals)}
    active_mkts = [m for m, (fdf, _) in all_passed_map.items() if fdf is not None and not fdf.empty]
    if not active_mkts: return False

    fig, axes = plt.subplots(len(active_mkts), 1, figsize=(16, 8.0 * len(active_mkts)), dpi=160, facecolor="#F8FAFC")
    if len(active_mkts) == 1: axes = [axes]
    power_cmap = LinearSegmentedColormap.from_list("sector_power", ["#FDE047", "#F59E0B", "#EA580C", "#DC2626", "#991B1B"], N=256)

    for ax, mkt in zip(axes, active_mkts):
        ax.set_facecolor("#FFFFFF")
        fdf, sec_tots = all_passed_map[mkt]
        sec_df = fdf.groupby(["macro_theme", "sector"], as_index=False).agg(
            cnt=("ticker", "count"), tier_a=("tier", lambda s: int((s == "Tier A").sum())),
            avg_rs=("rs_score", "mean"), avg_dh=("distance_from_high_pct", lambda s: float(s.mean() * 100.0)),
        )
        if sec_df.empty: continue
        sec_df["univ_total"] = sec_df["sector"].map(lambda s: max(int(sec_tots.get(s, 1)), 1))
        sec_df["pass_rate"] = (sec_df["cnt"] / sec_df["univ_total"] * 100.0).clip(0.0, 100.0)
        tier_a_pct = (sec_df["tier_a"] / sec_df["cnt"].clip(lower=1)) * 100.0
        rs_norm = ((sec_df["avg_rs"] - 65.0) / 34.0).clip(0.0, 1.0) * 100.0
        sec_df["sector_power"] = (0.45 * rs_norm + 0.35 * sec_df["pass_rate"] + 0.20 * tier_a_pct).round(1)
        sec_df = sec_df.sort_values(["sector_power", "pass_rate", "cnt"], ascending=[False, False, False]).head(12).reset_index(drop=True)

        x_vals, y_vals = sec_df["avg_dh"].to_numpy(dtype=float), sec_df["avg_rs"].to_numpy(dtype=float)
        x_min, x_max = float(np.min(x_vals)), float(np.max(x_vals))
        y_min, y_max = float(np.min(y_vals)), float(np.max(y_vals))
        x_span, y_span = max(x_max - x_min, 8.0), max(y_max - y_min, 12.0)
        xlim_left, xlim_right = x_min - x_span * 0.22, min(2.5, max(x_max + x_span * 0.24, -6.0))
        ylim_bottom, ylim_top = max(65.0, y_min - y_span * 0.24), min(103.0, y_max + y_span * 0.26)
        ax.set_xlim(xlim_left, xlim_right)
        ax.set_ylim(ylim_bottom, ylim_top)

        lz_x, lz_y = max(xlim_left, -12.0), max(ylim_bottom, 80.0)
        if xlim_right > lz_x and ylim_top > lz_y:
            ax.axvspan(lz_x, xlim_right, ymin=(lz_y - ylim_bottom) / (ylim_top - ylim_bottom), ymax=1.0, color="#ECFDF5", alpha=0.70, zorder=0)
            ax.text(xlim_right - (xlim_right - xlim_left) * 0.015, ylim_top - (ylim_top - ylim_bottom) * 0.035, "🎯 초강세 주도 섹터 존", ha="right", va="top", fontsize=11, fontweight="bold", color="#047857", bbox=dict(boxstyle="round,pad=0.35", fc="#D1FAE5", ec="#34D399", lw=1.0, alpha=0.9), zorder=2)
        ax.axvline(-12.0, color="#64748B", linestyle="--", linewidth=1.2, alpha=0.75, zorder=1)
        ax.axhline(80.0, color="#059669", linestyle="--", linewidth=1.3, alpha=0.75, zorder=1)

        sizes = sec_df["cnt"] * 220 + 380
        sc = ax.scatter(x_vals, y_vals, s=sizes, c=sec_df["sector_power"], cmap=power_cmap, vmin=25, vmax=95, alpha=0.88, edgecolors="#1E293B", linewidth=2.0, zorder=3)
        for x_v, y_v, pr_v in zip(x_vals, y_vals, sec_df["pass_rate"]):
            txt_c = ax.text(x_v, y_v, f"{pr_v:.0f}%", ha="center", va="center", fontsize=10.5, fontweight="extra bold", color="#0F172A", zorder=4)
            txt_c.set_path_effects([pe.withStroke(linewidth=3.0, foreground="#FFFFFF")])

        xr, yr = xlim_right - xlim_left, ylim_top - ylim_bottom
        nx, ny = (x_vals - xlim_left) / xr, (y_vals - ylim_bottom) / yr
        lbl_nx, lbl_ny = nx.copy(), np.where(ny > 0.80, ny - 0.085, ny + 0.075)
        for _ in range(80):
            for i in range(len(lbl_nx)):
                for j in range(i + 1, len(lbl_nx)):
                    dx, dy = lbl_nx[i] - lbl_nx[j], lbl_ny[i] - lbl_ny[j]
                    min_dx, min_dy = 0.145, 0.082
                    if abs(dx) < min_dx and abs(dy) < min_dy:
                        push_y, push_x = (min_dy - abs(dy)) * 0.38, (min_dx - abs(dx)) * 0.18
                        lbl_ny[i] += push_y if dy >= 0 else -push_y
                        lbl_ny[j] -= push_y if dy >= 0 else -push_y
                        lbl_nx[i] += push_x if dx >= 0 else -push_x
                        lbl_nx[j] -= push_x if dx >= 0 else -push_x
            lbl_nx, lbl_ny = np.clip(lbl_nx, 0.08, 0.92), np.clip(lbl_ny, 0.07, 0.93)

        for idx, r in enumerate(sec_df.itertuples()):
            sub_leader = fdf[fdf["sector"] == r.sector].sort_values("pure_rs", ascending=False).iloc[0]
            leader_nm = str(sub_leader["name"])[:13]
            is_hot = float(r.sector_power) >= 70.0
            box_ec, box_fc = ("#059669", "#F0FDF4") if is_hot else ("#94A3B8", "#FFFFFF")
            lbl_text = f"{r.sector} (통과율 {r.pass_rate:.0f}% · {r.cnt}/{r.univ_total})\n대장: {leader_nm} · RS {r.avg_rs:.1f}"
            tx, ty = xlim_left + lbl_nx[idx] * xr, ylim_bottom + lbl_ny[idx] * yr
            ax.annotate(lbl_text, xy=(r.avg_dh, r.avg_rs), xytext=(tx, ty), ha="center", va="center", fontsize=10.5, fontweight="bold", color="#0F172A", linespacing=1.35, bbox=dict(boxstyle="round,pad=0.40", fc=box_fc, ec=box_ec, lw=1.4, alpha=0.94), arrowprops=dict(arrowstyle="-", color="#475569", lw=1.2, shrinkA=4, shrinkB=10, connectionstyle="arc3,rad=0.08"), zorder=5)

        ax.set_title(f"[{mkt}] 섹터 로테이션 & 통과율 파워 맵 ({d_str})", fontsize=14.5, fontweight="bold", color="#0F172A", pad=16)
        ax.set_xlabel("52주 신고가 대비 평균 괴리율 (%)", fontsize=12, fontweight="bold", color="#334155", labelpad=10)
        ax.set_ylabel("섹터 평균 RS 점수", fontsize=12, fontweight="bold", color="#334155", labelpad=10)
        ax.grid(True, linestyle=":", linewidth=1.0, color="#CBD5E1", alpha=0.85, zorder=0)
        cbar = fig.colorbar(sc, ax=ax, pad=0.018, aspect=28)
        cbar.set_label("Sector Power Score", fontsize=11.0, fontweight="bold", color="#1E293B", labelpad=10)

    plt.tight_layout(pad=2.2)
    saved_ok = False
    for p in out_paths:
        try:
            fig.savefig(p, bbox_inches="tight", pad_inches=0.35, facecolor=fig.get_facecolor())
            saved_ok = True
        except Exception as _exc: _swallow("generate_sector_dashboard_png", _exc)
    plt.close(fig)
    return saved_ok

def _compute_exposure_guide(regimes: dict[str, dict]) -> str:
    """단일 시장(--market KR/US) 및 전체 시장(ALL) 모두 일관되게 작동하도록 지수 비율 기반으로 신규 아이디어 한도 가이드를 산출"""
    states = [_risk_regime(r) for r in (regimes or {}).values()]
    if not states or "UNKNOWN" in states:
        return "참고 가이드 미산출 (레짐 자료 확인 필요)"
    attack_ratio = states.count("ATTACK") / len(states)
    risk_off_ratio = states.count("RISK_OFF") / len(states)
    if attack_ratio >= 0.75:
        return "🟢 공격적 (전체 시장 참고 범위 80~100%)"
    if attack_ratio >= 0.25 and risk_off_ratio <= 0.25:
        return "🟡 선별적 (전체 시장 참고 범위 50~70%)"
    if risk_off_ratio >= 0.75:
        return "🔴 방어적 (전체 시장 참고 범위 0~25% · 신규 돌파 자제 · 현금 확보 우선)"
    return "🟡 보수적 (전체 시장 참고 범위 30~50%)"

def build_action_table(track_results: dict[str, pd.DataFrame], regimes: dict[str, dict], cfg: dict) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """선정 종목 전체 / 즉시 매수 검토(액션) 종목+포트 배분 / 포트 메타 - MD 리포트와 게시 데이터가 공유하는 단일 출처"""
    selected_frames = [df for df in (track_results or {}).values() if df is not None and not df.empty]
    all_sel_df = pd.concat(selected_frames, ignore_index=True) if selected_frames else pd.DataFrame()
    action_df = all_sel_df[all_sel_df["is_actionable"] == True].copy() if not all_sel_df.empty and "is_actionable" in all_sel_df.columns else pd.DataFrame()
    if not action_df.empty:
        ties = [c for c in ("market", "sub_market", "ticker") if c in action_df]
        action_df = action_df.sort_values(["is_leading_sector", "is_breakout", "is_true_vcp", "topdown_score", "rs_score"] + ties, ascending=[False]*5 + [True]*len(ties), kind="stable").reset_index(drop=True)
    action_df, port_meta = allocate_portfolio_risk_budget(action_df, regimes, cfg)
    return all_sel_df, action_df, port_meta

def format_markdown_report(
    d_str: str, track_results: dict[str, pd.DataFrame], track_all_passed: dict[str, pd.DataFrame],
    kr_merged_all: pd.DataFrame, us_merged_all: pd.DataFrame, regimes: dict[str, dict],
    extra_meta: dict[str, Any], dropped_df: pd.DataFrame, val_df: pd.DataFrame, cfg: dict, has_png_chart: bool = False,
) -> str:
    lines = [f"# 📈 Top-Down Trend Leader Watchlist ({d_str})", ""]

    all_sel_df, action_df, port_meta = build_action_table(track_results, regimes, cfg)

    lead_act_cnt = int(action_df["is_leading_sector"].sum()) if (not action_df.empty and "is_leading_sector" in action_df.columns) else 0
    new_total = int(all_sel_df["status_tag"].astype(str).str.contains("신규").sum()) if not all_sel_df.empty else 0
    brk_total = int(all_sel_df["is_breakout"].sum()) if not all_sel_df.empty and "is_breakout" in all_sel_df.columns else 0
    vcp_total = int(all_sel_df["is_true_vcp"].sum()) if not all_sel_df.empty and "is_true_vcp" in all_sel_df.columns else 0
    drop_total = len(dropped_df) if dropped_df is not None else 0

    kr_macro_df, kr_sub_df = extra_meta.get("kr_macro_df", pd.DataFrame()), extra_meta.get("kr_sub_df", pd.DataFrame())
    us_macro_df, us_sub_df = extra_meta.get("us_macro_df", pd.DataFrame()), extra_meta.get("us_sub_df", pd.DataFrame())

    kr_top_theme = f"{kr_macro_df.iloc[0]['macro_theme']} ▸ {kr_sub_df.iloc[0]['sector']}" if (not kr_macro_df.empty and not kr_sub_df.empty) else "-"
    us_top_theme = f"{us_macro_df.iloc[0]['macro_theme']} ▸ {us_sub_df.iloc[0]['sector']}" if (not us_macro_df.empty and not us_sub_df.empty) else "-"
    exposure_guide = _compute_exposure_guide(regimes)

    h_kr, h_us = extra_meta.get("kr_health", {}), extra_meta.get("us_health", {})
    health_line = f"KR 유니버스 {h_kr.get('kr_universe', 0)}종목(처리 {h_kr.get('kr_processed', 0)} · 실패 {h_kr.get('kr_failed', 0)} · 미분류 {h_kr.get('kr_unclassified', 0)}) | US 유니버스 {h_us.get('us_universe', 0)}종목(처리 {h_us.get('us_processed', 0)} · 실패 {h_us.get('us_failed', 0)})"
    _sw = swallowed_summary()
    if _sw:
        health_line += f" | ⚠️ {_sw}"

    lines.append("> **📌 Top-Down Executive Summary (오늘의 시장 큰 그림)**")
    stale_warn = " · ⚠️ **오래된 캐시 폴백 사용됨(네트워크 확인 필요)**" if (h_kr.get("stale_cache") or h_us.get("stale_cache")) else ""
    lines.append(f"> - **권장 포트폴리오 노출도:** **{exposure_guide}** (신규 후보군 가상 포트배분 합계: **{port_meta['total_alloc_pct']:.1f}%** / 총 손절리스크: **{port_meta['total_risk_pct']:.2f}%** [한도 {port_meta['risk_cap_pct']:.1f}%]){stale_warn}")
    _mk_parts = []
    for _m, _flag in (("KR", "🇰🇷"), ("US", "🇺🇸")):
        _sub = {k: v for k, v in regimes.items() if k in (_KR_SUBS if _m == "KR" else _US_SUBS)}
        if not _sub:
            continue
        _ec, _rc, _ = _exposure_rule(_sub, cfg)
        _bm = (port_meta.get("by_market") or {}).get(_m, {})
        _mk_parts.append(f"{_flag} {_m} 노출한도 **{_ec:.0f}%** (배분 {_bm.get('alloc', 0.0):.1f}% · 손절리스크 {_bm.get('risk', 0.0):.2f}%/{_rc:.1f}%)")
    if _mk_parts:
        lines.append("> - **시장별 노출 한도:** " + " | ".join(_mk_parts))
    lines.append(f"> - **시장 최강 주도 섹터:** 🇰🇷 **KR:** `{kr_top_theme}` | 🇺🇸 **US:** `{us_top_theme}`")
    lines.append(f"> - **즉시 매수 검토 (Buy Zone):** 총 **{len(action_df)}종목** 중 **👑주도섹터 소속 {lead_act_cnt}종목** (🔥피벗돌파 {brk_total} · 🎯진짜VCP {vcp_total}) | **신규 진입:** {new_total}종목 | **전일 이탈:** {drop_total}종목")
    lines.append(f"> - **색상 범례:** {_color_span('🟢초록 = 매수적합/주도섹터/가속', '#059669', True)} · {_color_span('🔴빨강 = 위험/과열/탄력둔화/이평이탈', '#DC2626', True)} · {_color_span('🔵파랑 = 신규부상/눌림목/블루닷', '#2563EB', True)}")
    lines.append(f"> - **데이터 헬스:** {health_line}")
    if h_kr.get("kr_price_message"):
        lines.append(f"> - **⚠️ 시세 기준일 확인:** {h_kr['kr_price_message']}")
    if h_us.get("us_cap_fallback") or h_us.get("us_cap_missing"):
        lines.append(f"> - **⚠️ US 시가총액 조회:** 이전 정상 캐시 사용 {h_us.get('us_cap_fallback', 0)}종목 · 유효 시총 없음 {h_us.get('us_cap_missing', 0)}종목(유니버스 제외). 캐시 폴백은 최신 재무정보가 아닙니다.")
    lines.append("")

    lines.append("## 1. 🚦 [Step 1: Market] 4대 지수 레짐 & 시장 내부 체력 (Breadth)")
    definitions = metric_definitions(cfg)
    lines.append(f"> Breadth: {definitions['breadth_nasdaq100']} / {definitions['breadth_sp500']}. {definitions['breadth_filters']}")
    lines.append(f"> {definitions['near_52w']}")
    lines.append("| 트랙 | 벤치마크 (기준일) | 상태 · 시장 신호등 | 지수 종가 (50일 / 200일선) | 25일 분배일 | 50일선 상회% (체력 게이지) | 200일선 상회% | 52주 종가 고점·저점 3% 근접 (Tier A) |")
    lines.append("| :---: | :--- | :--- | :--- | :---: | :--- | :---: | :---: |")
    for trk in ["KOSPI", "KOSDAQ", "NASDAQ100", "SP500"]:
        if trk not in regimes: continue
        reg = regimes[trk]
        b_close = f"{reg.get('benchmark_close', 0):,.2f}" if pd.notna(reg.get("benchmark_close")) else "-"
        b_50 = f"{reg.get('benchmark_sma50', 0):,.0f}" if pd.notna(reg.get("benchmark_sma50")) else "-"
        b_200 = f"{reg.get('benchmark_sma200', 0):,.0f}" if pd.notna(reg.get("benchmark_sma200")) else "-"
        p50_val = pd.to_numeric(reg.get("pct_above_50dma"), errors="coerce")
        p200_val = pd.to_numeric(reg.get("pct_above_200dma"), errors="coerce")
        if reg.get("breadth_valid") is False or pd.isna(p50_val):
            p50, p200 = "산출불가", "산출불가"
        else:
            p50_bar = _make_strength_bar(p50_val, width=6)
            p50 = f"`{p50_bar}` **{p50_val:.1f}%**"
            p200 = f"{p200_val:.1f}%" if pd.notna(p200_val) else "자료 없음"
        nh_nl = f"{reg.get('nh_nl_label', '-')} (A:{reg.get('tier_a_total', 0)})"
        lines.append(f"| **{trk}** | {reg.get('benchmark', trk)} ({reg.get('as_of', d_str)}) | `{reg.get('regime', '-')}` {reg.get('signal_light', '-')} | **{b_close}** ({b_50} / {b_200}) | {reg.get('distribution_days_25d', '-')}일 | {p50} | {p200} | {nh_nl} |")
    lines.append("")

    lines.append("## 2. 🧭 [Step 2: Sector & Theme] 주섹터 / 서브섹터 강도 & 로테이션 지형도")
    lines.append(f"> {definitions['leading']} {definitions['rs_4w']}")
    if has_png_chart:
        lines.append("![Sector Rotation Map](./sector_overview.png)")
        lines.append("")
    if not kr_macro_df.empty or not kr_sub_df.empty:
        lines.extend(_render_topdown_sector_section("🇰🇷 KR (KOSPI+KOSDAQ)", kr_macro_df, kr_sub_df))
    if not us_macro_df.empty or not us_sub_df.empty:
        lines.extend(_render_topdown_sector_section("🇺🇸 US (NASDAQ100+S&P500)", us_macro_df, us_sub_df))

    lines.append("## 3. 🎯 [Step 3: Actionable Setups] 오늘의 실전 액션 후보 (👑주도섹터 우선 정렬 · Buy Zone)")
    lines.append(f"> {definitions['actionable']}")
    observed_candidates = pd.concat([frame for frame in track_all_passed.values() if not frame.empty], ignore_index=True) if any(not frame.empty for frame in track_all_passed.values()) else pd.DataFrame()
    observation = price_observation(regimes, observed_candidates, datetime.now(KST).isoformat())
    for track, status in observation["tracks"].items():
        if status["session_unconfirmed"] or status["lagging_candidate_rows"]:
            lines.append(f"> ⚠️ {track}: 지수 기준일 {status['benchmark_as_of']} / 마감시각 기준 확인 대상 평일 {status['clock_reference_date']} · 벤치마크보다 뒤처진 후보 {status['lagging_candidate_rows']}개. 휴장·공급 지연 여부 및 최신 시세 확인 필요.")
    lines.extend(_render_2tier_candidate_table("ALL", "즉시 진입 검토 가능한 액션 리스트 (주도섹터 + 바이존 -2.5%~+5% + 수축/돌파)", action_df, is_action_table=True))

    if not kr_merged_all.empty or "KOSPI" in track_results or "KOSDAQ" in track_results:
        lines.append("## 4. 🇰🇷 [Step 4-A: KR Watchlist] 한국 시장 트랙별 리더 Top 20")
        if "KOSPI" in track_results:
            lines.extend(_render_2tier_candidate_table("KOSPI", "🇰🇷 KOSPI 리더 Top 20 (시총 1조+ · 거래대금 50억+ · 탑다운 정렬)", track_results["KOSPI"]))
        if "KOSDAQ" in track_results:
            lines.extend(_render_2tier_candidate_table("KOSDAQ", "🇰🇷 KOSDAQ 리더 Top 20 (시총 4,000억+ · 거래대금 30억+ · 탑다운 정렬)", track_results["KOSDAQ"]))

    if not us_merged_all.empty or "NASDAQ100" in track_results or "SP500" in track_results:
        lines.append("## 5. 🇺🇸 [Step 4-B: US Watchlist] 미국 시장 트랙별 리더 Top 20")
        if "NASDAQ100" in track_results:
            lines.extend(_render_2tier_candidate_table("NASDAQ100", "🇺🇸 NASDAQ 100 기술·성장 리더 Top 20 (시총 $15B+ · 거래대금 $50M+)", track_results["NASDAQ100"]))
        if "SP500" in track_results:
            lines.extend(_render_2tier_candidate_table("SP500", "🇺🇸 S&P 500 Core 리더 Top 20 (시총 $10B+ · 거래대금 $30M+)", track_results["SP500"]))

    lines.append("## 6. 🛡️ [Step 5: Defense & Validation] 워치리스트 이탈 종목 및 셋업 실증 검증")
    lines.append(f"> **{definitions['official_performance']}** 게시본 run_id·해시와 완료/진입 검증 표본 수를 확인하세요.")
    lines.append("### 📉 전일 대비 워치리스트 이탈 종목 (보유자 점검용)")
    if dropped_df is None or dropped_df.empty:
        lines.append("> 직전 영업일 Top 20 대비 이탈한 종목이 없거나 비교할 과거 리포트가 없습니다.\n")
    else:
        lines.append("| 트랙 | 종목명 (티커) | 직전 순위 (RS) | 금일 RS | 이탈 사유 | 보유 청산 신호 |")
        lines.append("| :---: | :--- | :---: | :---: | :--- | :---: |")
        for r in dropped_df.itertuples():
            c_rs = f"{r.curr_rs:.1f}" if pd.notna(r.curr_rs) else "-"
            lines.append(f"| **{r.sub_market}** | **{_esc_md(r.name)}** (`{r.ticker}`) | #{r.prev_rank} ({r.prev_rs:.1f}) | {c_rs} | {_color_span(_esc_md(r.drop_reason), '#DC2626', True)} | {_esc_md(r.exit_signal)} |")
        lines.append("")

    turn_df = extra_meta.get("turnaround_all", pd.DataFrame())
    if turn_df is not None and not turn_df.empty:
        lines.append("<details>")
        lines.append(f"<summary>👀 <b>추세회복 (200일선 하회 턴어라운드 관찰군 {len(turn_df)}종목) 별도 보기</b></summary>")
        lines.append("")
        lines.extend(_render_2tier_candidate_table("ALL", "200일선 아래에서 단기 상대강도가 부각된 추세회복 종목", turn_df.head(15), is_action_table=True))
        lines.append("</details>")
        lines.append("")

    if val_df is not None and not val_df.empty:
        lines.append("### 🧪 참고용 후보 포워드 집계 (미검증 · 공식 전략 성과 아님)")
        lines.append(f"> **{definitions['legacy_validation']}** 거래일 누락 시 종목과 벤치마크의 비교 종료일도 다를 수 있습니다.")
        lines.append("| 셋업 단계 | 표본(고유종목) | +5일 승률 | +10일 평균 | +10일 중앙값 | +10일 초과수익 | 초과수익 승률 | +20일 초과수익 | MAE(10일) |")
        lines.append("| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |")
        def _f(v, fmt):
            return fmt.format(v) if pd.notna(v) else "-"
        for r in val_df.itertuples():
            warn = " ⚠️표본부족" if int(r.sample_cnt) < 20 else ""
            lines.append(
                f"| **{_esc_md(r.setup_stage)}**{warn} | {int(r.sample_cnt)}건({int(r.uniq_cnt)}) | {_f(r.win_rate_5d, '{:.1f}%')} | "
                f"{_f(r.avg_ret_10d, '{:+.2f}%')} | {_f(r.med_ret_10d, '{:+.2f}%')} | **{_f(r.avg_ex_10d, '{:+.2f}%p')}** | "
                f"{_f(r.win_rate_ex_10d, '{:.1f}%')} | {_f(r.avg_ex_20d, '{:+.2f}%p')} | {_f(r.avg_mae_10d, '{:.2f}%')} |"
            )
        lines.append("> 기존 계산값을 호환성을 위해 유지합니다. 게시 이전 진입·대체 시가·거래일 불일치 가능성이 있어 확정 성과 또는 셋업 우열의 근거로 사용하지 마세요.")
        lines.append("")

    return "\n".join(lines)

def generate_html_viewer(
    d_str: str, track_results: dict[str, pd.DataFrame], regimes: dict[str, dict],
    dropped_df: pd.DataFrame, val_df: pd.DataFrame, out_paths: list[Path],
) -> None:
    all_df = pd.concat([df for df in track_results.values() if not df.empty], ignore_index=True) if track_results else pd.DataFrame()
    rows_html = []
    for r in all_df.itertuples():
        mkt = str(getattr(r, "sub_market", "KR"))
        is_kr = mkt in ("KR", "KOSPI", "KOSDAQ")
        p_fmt = (lambda v: f"{v:,.0f}원") if is_kr else (lambda v: f"${v:,.2f}")
        tv, inf_u, inf_l = _tv_url(mkt, str(r.ticker)), *_info_url(mkt, str(r.ticker))
        is_act = "Y" if getattr(r, "is_actionable", False) else "N"
        is_new = "Y" if "신규" in str(getattr(r, "status_tag", "")) else "N"
        dpiv = float(getattr(r, "dist_to_pivot_pct", 0.0)) * 100.0
        raw_stp = float(getattr(r, "raw_stop_dist_pct", -0.05)) * 100.0
        pos_sz = float(getattr(r, "position_size_pct", 10.0))
        bd = " 🔵블루닷" if getattr(r, "rs_blue_dot", False) else ""
        earn = f" <span class='badge-warn'>{html.escape(str(getattr(r, 'earnings_tag', '')))}</span>" if getattr(r, "earnings_tag", "") else ""
        setup_cls = "badge-green" if any(k in str(r.setup_stage) for k in ("돌파", "VCP", "수축", "포켓")) else ("badge-red" if "과열" in str(r.setup_stage) else "badge-blue")
        piv_cls = "text-green" if getattr(r, "in_buy_zone", False) else ("text-red" if dpiv > 5.0 else "")
        stp_cls = "text-red" if getattr(r, "stop_warning", False) else ""
        ex_cls = "text-red" if ("🚨" in str(r.exit_signal) or "⚠️" in str(r.exit_signal)) else "text-green"
        rank_value = getattr(r, "rank", getattr(r, "overall_rank", "-")) if pd.notna(getattr(r, "rank", None)) else getattr(r, "overall_rank", "-")

        rows_html.append(f"<tr data-track='{html.escape(mkt)}' data-action='{is_act}' data-new='{is_new}'><td><b>{html.escape(mkt)}</b> #{html.escape(str(rank_value))} ({html.escape(str(getattr(r, 'tier', '')) )})</td><td><a href='{tv}' target='_blank'><b>{html.escape(str(r.name))}</b></a> <code>{html.escape(str(r.ticker))}</code> · <a href='{inf_u}' target='_blank'>{inf_l}</a>{earn}</td><td>{html.escape(str(r.macro_theme))} ▸ <b>{html.escape(str(r.sector))}</b> ({int(getattr(r, 'sector_rank', 99))}위)</td><td><span class='{setup_cls}'>{html.escape(str(r.setup_stage))}{bd}</span></td><td data-sort='{r.rs_score:.2f}'><b>{r.rs_score:.1f}</b> (4주 {float(getattr(r, 'rs_change_4w', 0)):+.0f})</td><td data-sort='{dpiv:.2f}' class='{piv_cls}'><b>{dpiv:+.1f}%</b> ({p_fmt(r.last_close)} / 피벗 {p_fmt(r.pivot_price)})</td><td data-sort='{raw_stp:.2f}' class='{stp_cls}'>{raw_stp:.1f}% (손절 {p_fmt(r.stop_price)})</td><td data-sort='{pos_sz:.2f}'><b>{pos_sz:.1f}%</b></td><td>{html.escape(str(getattr(r, 'status_tag', '')))}</td><td class='{ex_cls}'>{html.escape(str(r.exit_signal))} (분배{int(getattr(r, 'dist_days_25d', 0))}일)</td></tr>")

    reg_cards = []
    for trk in ["KOSPI", "KOSDAQ", "NASDAQ100", "SP500"]:
        if trk in regimes:
            rg = regimes[trk]
            def _breadth_text(field):
                value = pd.to_numeric(rg.get(field), errors="coerce")
                return f"{value:.1f}%" if rg.get("breadth_valid") is not False and pd.notna(value) else "산출불가"
            reg_cards.append(f"<div class='reg-card'><b>{trk}</b> ({rg.get('as_of', d_str)})<br><span>{html.escape(str(rg.get('signal_light', '')))}</span><br><small>50일선 상회: <b>{_breadth_text('pct_above_50dma')}</b> · 200일선 상회: {_breadth_text('pct_above_200dma')} · 분배일: {rg.get('distribution_days_25d') if rg.get('distribution_days_25d') is not None else '—'}일</small></div>")

    html_doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8"><title>Trend Leader Watchlist ({d_str})</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Malgun Gothic", sans-serif; background:#F8FAFC; color:#0F172A; margin:20px; }}
  h1 {{ font-size:22px; margin-bottom:10px; }}
  .reg-grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:12px; margin-bottom:16px; }}
  .reg-card {{ background:#FFF; border:1px solid #CBD5E1; border-radius:8px; padding:12px; line-height:1.5; }}
  .toolbar {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px; align-items:center; }}
  .toolbar select, .toolbar input, .toolbar button {{ padding:6px 10px; border:1px solid #94A3B8; border-radius:6px; font-size:13px; background:#FFF; cursor:pointer; }}
  .table-wrap {{ overflow-x:auto; background:#FFF; border:1px solid #CBD5E1; border-radius:8px; max-height:78vh; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; white-space:nowrap; }}
  th, td {{ padding:8px 12px; border-bottom:1px solid #E2E8F0; text-align:left; }}
  th {{ position:sticky; top:0; background:#F1F5F9; font-weight:700; cursor:pointer; user-select:none; z-index:2; }}
  tr:hover {{ background:#F8FAFC; }}
  .badge-green {{ background:#D1FAE5; color:#047857; padding:3px 7px; border-radius:5px; font-weight:700; }}
  .badge-red {{ background:#FEE2E2; color:#B91C1C; padding:3px 7px; border-radius:5px; font-weight:700; }}
  .badge-blue {{ background:#DBEAFE; color:#1D4ED8; padding:3px 7px; border-radius:5px; font-weight:600; }}
  .badge-warn {{ background:#FEF3C7; color:#B45309; padding:2px 6px; border-radius:4px; font-weight:700; font-size:11px; }}
  .text-green, .text-red {{ font-weight:700; }} .text-green{{color:#059669;}} .text-red{{color:#DC2626;}}
  a {{ color:#1E40AF; text-decoration:none; }} a:hover {{ text-decoration:underline; }}
</style>
</head>
<body>
<h1>📈 Top-Down Trend Leader Watchlist ({d_str}) — Interactive Viewer</h1>
<p>Breadth는 필터링된 전략용 모집단이며 전체 지수 Breadth와 다를 수 있습니다. SP500은 기본 설정에서 ex-NDX입니다. 실제 모집단과 공식 모의 성과는 JSON/Markdown의 정의·검증 상태를 확인하세요.</p>
<div class="reg-grid">{''.join(reg_cards)}</div>
<div class="toolbar">
  <select id="fTrack" onchange="applyFilter()"><option value="ALL">전체 트랙 (4대 지수)</option><option value="KOSPI">🇰🇷 KOSPI (1조+)</option><option value="KOSDAQ">🇰🇷 KOSDAQ (4,000억+)</option><option value="NASDAQ100">🇺🇸 NASDAQ 100 ($15B+)</option><option value="SP500">🇺🇸 S&P 500 Core ($10B+)</option></select>
  <select id="fAction" onchange="applyFilter()"><option value="ALL">전체 리더 리스트</option><option value="Y">🎯 오늘의 실전 액션 (Buy Zone)만 보기</option></select>
  <select id="fNew" onchange="applyFilter()"><option value="ALL">전체 상태</option><option value="Y">🆕 오늘 신규진입 종목만</option></select>
  <input type="text" id="fSearch" placeholder="종목명 / 티커 / 섹터 검색..." oninput="applyFilter()">
</div>
<div class="table-wrap">
<table id="watchTable">
  <thead><tr><th onclick="sortTable(0)">트랙·순위 ↕</th><th onclick="sortTable(1)">종목명 (티커·정보) ↕</th><th onclick="sortTable(2)">대테마 ▸ 세부섹터 (서열) ↕</th><th onclick="sortTable(3)">셋업 단계 ↕</th><th onclick="sortTable(4, true)">RS (4주Δ) ↕</th><th onclick="sortTable(5, true)">피벗거리 (종가/피벗) ↕</th><th onclick="sortTable(6, true)">손절폭 (손절가) ↕</th><th onclick="sortTable(7, true)">권장비중 ↕</th><th onclick="sortTable(8)">변동상태 ↕</th><th onclick="sortTable(9)">보유청산신호 ↕</th></tr></thead>
  <tbody>{''.join(rows_html)}</tbody>
</table>
</div>
<script>
function applyFilter() {{ const trk=document.getElementById('fTrack').value, act=document.getElementById('fAction').value, nw=document.getElementById('fNew').value, q=document.getElementById('fSearch').value.toLowerCase(); document.querySelectorAll('#watchTable tbody tr').forEach(tr => {{ tr.style.display = ((trk==='ALL'||tr.dataset.track===trk) && (act==='ALL'||tr.dataset.action===act) && (nw==='ALL'||tr.dataset.new===nw) && (!q||tr.innerText.toLowerCase().includes(q))) ? '' : 'none'; }}); }}
let sortAsc = false;
function sortTable(colIdx, isNum=false) {{ sortAsc = !sortAsc; const tbody = document.querySelector('#watchTable tbody'); const rows = Array.from(tbody.querySelectorAll('tr')); rows.sort((a, b) => {{ const ca = a.children[colIdx], cb = b.children[colIdx]; if (isNum) {{ return sortAsc ? (parseFloat(ca.dataset.sort||'0') - parseFloat(cb.dataset.sort||'0')) : (parseFloat(cb.dataset.sort||'0') - parseFloat(ca.dataset.sort||'0')); }} return sortAsc ? ca.innerText.localeCompare(cb.innerText) : cb.innerText.localeCompare(ca.innerText); }}); rows.forEach(r => tbody.appendChild(r)); }}
</script>
</body>
</html>"""
    for p in out_paths:
        try: p.write_text(html_doc, encoding="utf-8")
        except Exception as _exc: _swallow("generate_html_viewer", _exc)

def save_reports(
    track_results: dict[str, pd.DataFrame], track_all_passed: dict[str, pd.DataFrame],
    kr_merged_all: pd.DataFrame, us_merged_all: pd.DataFrame, regimes: dict[str, dict],
    extra_meta: dict[str, Any], dropped_df: pd.DataFrame, val_df: pd.DataFrame, cfg: dict,
) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    d_str = datetime.now(KST).strftime("%Y-%m-%d")
    daily_dir = REPORT_DIR / d_str
    daily_dir.mkdir(parents=True, exist_ok=True)
    ex_reg = dict(regimes or {})
    all_rows = [df for df in track_results.values() if df is not None and not df.empty]
    combined_df = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    combined_df.to_csv(daily_dir / "all_candidates.csv", index=False, encoding="utf-8-sig")

    reports_cfg = cfg.get("reports", {})
    minimal_output = bool(reports_cfg.get("minimal_output", True))
    if minimal_output:
        for pattern in ("latest_*.csv", "latest_watchlist.html", "sector_overview.png", "latest_regimes.json"):
            for old_file in REPORT_DIR.glob(pattern):
                try: old_file.unlink()
                except OSError: pass
        for old_dir in REPORT_DIR.iterdir():
            if not old_dir.is_dir() or not re.match(r"^\d{4}-\d{2}-\d{2}$", old_dir.name): continue
            for pattern in ("*_candidates.csv", "*_candidates_all.csv", "kr_candidates*.csv", "us_candidates*.csv", "dropped_candidates.csv", "*.png", "*.html"):
                for old_file in old_dir.glob(pattern):
                    if old_file.name == "all_candidates.csv": continue
                    try: old_file.unlink()
                    except OSError: pass
    if not minimal_output or bool(reports_cfg.get("save_debug_csv", False)):
        for trk, df in track_results.items():
            full_df = track_all_passed.get(trk, df)
            df.to_csv(daily_dir / f"{trk.lower()}_candidates.csv", index=False, encoding="utf-8-sig")
            full_df.to_csv(daily_dir / f"{trk.lower()}_candidates_all.csv", index=False, encoding="utf-8-sig")
        if "KOSPI" in track_results or "KOSDAQ" in track_results:
            pd.concat([track_results.get("KOSPI", pd.DataFrame()), track_results.get("KOSDAQ", pd.DataFrame())], ignore_index=True).to_csv(daily_dir / "kr_candidates.csv", index=False, encoding="utf-8-sig")
            kr_merged_all.to_csv(daily_dir / "kr_candidates_all.csv", index=False, encoding="utf-8-sig")
        if "NASDAQ100" in track_results or "SP500" in track_results:
            pd.concat([track_results.get("NASDAQ100", pd.DataFrame()), track_results.get("SP500", pd.DataFrame())], ignore_index=True).to_csv(daily_dir / "us_candidates.csv", index=False, encoding="utf-8-sig")
            us_merged_all.to_csv(daily_dir / "us_candidates_all.csv", index=False, encoding="utf-8-sig")
        if dropped_df is not None and not dropped_df.empty:
            dropped_df.to_csv(daily_dir / "dropped_candidates.csv", index=False, encoding="utf-8-sig")

    has_png = False
    if bool(reports_cfg.get("enable_png_chart", False)):
        has_png = generate_sector_dashboard_png(
            d_str, kr_merged_all, us_merged_all,
            extra_meta.get("kr_sec_totals", {}), extra_meta.get("us_sec_totals", {}),
            [daily_dir / "sector_overview.png"],
        )
    if bool(reports_cfg.get("enable_html_viewer", False)):
        generate_html_viewer(d_str, track_results, ex_reg, dropped_df, val_df, [daily_dir / "watchlist.html"])

    md_content = format_markdown_report(d_str, track_results, track_all_passed, kr_merged_all, us_merged_all, ex_reg, extra_meta, dropped_df, val_df, cfg, has_png_chart=has_png)
    rep = daily_dir / "watchlist.md"
    rep.write_text(md_content, encoding="utf-8")
    (REPORT_DIR / "latest_watchlist.md").write_text(md_content, encoding="utf-8")

    keep_days = int(reports_cfg.get("keep_daily_files", 30))
    if keep_days > 0:
        cutoff_date = (datetime.now(KST) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
        for sub in REPORT_DIR.iterdir():
            if sub.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", sub.name) and sub.name < cutoff_date:
                shutil.rmtree(sub, ignore_errors=True)
    return rep

# =====================================================================
# 8. 합성 데이터 기반 내장 단위 테스트 (--self-test)
# =====================================================================
# =====================================================================
# 8. 게시 계층 (Data Contract) - 엔진 -> 저장소/앱 사이의 유일한 인터페이스
#    앱은 이 파일들만 읽는다(엔진 import 금지). 컬럼 계약은 manifest.json의 contract에도 실려 나간다.
# =====================================================================
ENGINE_VERSION = "v22.3.3-tactical-session-v2"
SCHEMA_VERSION = "1.0"

WATCHLIST_COLS = [
    "market", "sub_market", "ticker", "name", "as_of", "sector", "macro_theme", "tier", "setup_stage", "status_tag",
    "tactical_calculation_version", "pivot_inputs_as_of", "buy_zone_low", "buy_zone_high",
    "intraday_pivot_exceeded", "close_at_or_above_pivot", "volume_observed",
    "is_actionable", "in_buy_zone", "is_breakout", "is_true_vcp", "strict_minervini", "is_leading_sector",
    "rs_blue_dot", "rs_line_new_high", "down_resilient", "earnings_tag",
    "rs_score", "rs_change_4w", "topdown_score", "sector_power", "last_close", "pivot_price", "base_pivot_price", "high_20d", "base_len_days", "dist_to_pivot_pct",
    "stop_price", "stop_dist_pct", "is_wide_stop", "wide_stop_actionable", "position_size_pct", "volume_ratio", "vol_ratio_50d", "vol_dry_up_ratio", "range_10d_pct",
    "down_day_excess_pct", "distance_from_high_pct", "dist_sma20_pct", "dist_sma50_pct", "atr_from_50dma",
    "sma200_rising", "trend_template", "dist_days_25d", "exit_signal",
    "peg", "eps_growth_pct", "eps_status",
    "portfolio_alloc_pct", "portfolio_risk_contrib_pct", "portfolio_alloc_note",
]
CONTRACT_REQUIRED = {
    "watchlist.csv": ["market", "sub_market", "ticker", "name", "as_of", "setup_stage", "rs_score", "last_close"],
    "candidates.csv": ["market", "sub_market", "ticker", "name", "as_of", "tier", "rs_score", "last_close", "selected"],
    "sectors.csv": ["market", "level", "macro_theme", "sector_power"],
    "dropped.csv": ["sub_market", "ticker", "name", "drop_reason"],
    "validation.csv": ["setup_stage", "sample_cnt"],
}
CONTRACT_JSON = ("manifest.json", "summary.json")
CHARTS_FILE = "charts.json.gz"
SIGNAL_LEDGER_COLS = ["first_seen", "as_of", "market", "sub_market", "ticker", "name", "sector", "setup_stage", "tier",
                      "entry_ref", "stop_price", "rs_score", "topdown_score", "published_at", "generated_at", "workflow_run_id", "run_id", "regime",
                      "rs_change_4w", "sector_power", "signal_close", "config_hash", "engine_version", "engine_commit"] + list(SIGNAL_FLAGS)

def _json_safe(o: Any) -> Any:
    if isinstance(o, dict):
        return {str(k): _json_safe(v) for k, v in o.items() if not isinstance(v, (pd.DataFrame, pd.Series))}
    if isinstance(o, (list, tuple, set)):
        return [_json_safe(v) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        f = float(o)
        return None if (np.isnan(f) or np.isinf(f)) else round(f, 6)
    if isinstance(o, (pd.Timestamp, datetime)):
        return o.isoformat()
    if o is None or isinstance(o, str):
        return o
    return str(o)

_HTML_TAG_RE = re.compile(r"</?(?:span|b|i|br|small|div|font|sub|sup|code|a|details|summary)\b[^>]*>", re.I)

def _strip_html_df(df: pd.DataFrame) -> pd.DataFrame:
    """MD 리포트용 HTML 조각(<span style=...> 등)을 게시 데이터에서 제거한다. 문자열 컬럼만 처리."""
    if df is None or df.empty:
        return df
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object or pd.api.types.is_string_dtype(out[c].dtype):
            out[c] = out[c].map(lambda v: _HTML_TAG_RE.sub("", v).replace("&nbsp;", " ").strip() if isinstance(v, str) else v)
    return out

def _ensure_market_col(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    if "market" not in out.columns:
        out["market"] = ""
    sub = out["sub_market"] if "sub_market" in out.columns else pd.Series("", index=out.index)
    out["market"] = [m if str(m).upper() in ("KR", "US") else _row_market(None, sm) for m, sm in zip(out["market"], sub)]
    out["market"] = out["market"].replace("ALL", "")
    return out

def _write_csv(df: pd.DataFrame, path: Path, fallback_cols: list[str]) -> int:
    if df is None or df.empty:
        pd.DataFrame(columns=fallback_cols).to_csv(path, index=False, encoding="utf-8")
        return 0
    df.to_csv(path, index=False, encoding="utf-8")
    return len(df)

def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

def _sector_frame(extra_meta: dict[str, Any]) -> pd.DataFrame:
    frames = []
    for mk, pre in (("KR", "kr"), ("US", "us")):
        for lvl, key in (("macro", f"{pre}_macro_df"), ("sector", f"{pre}_sub_df")):
            df = extra_meta.get(key)
            if isinstance(df, pd.DataFrame) and not df.empty:
                t = df.copy()
                t.insert(0, "level", lvl)
                t.insert(0, "market", mk)
                frames.append(t)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["market", "level", "macro_theme", "sector_power"])

def _chart_rows(*frames: pd.DataFrame) -> pd.DataFrame:
    """Preserve source priority without mutating selection or risk inputs."""
    usable = [frame for frame in frames if frame is not None and not frame.empty]
    if not usable:
        return pd.DataFrame(columns=["market", "ticker"])
    rows = pd.concat(usable, ignore_index=True).copy()
    rows["market"] = rows["market"].astype(str).str.strip().str.upper()
    rows["ticker"] = [str(t).strip().zfill(6) if m == "KR" else str(t).strip().upper()
                      for m, t in zip(rows["market"], rows["ticker"])]
    return rows.drop_duplicates(subset=["market", "ticker"], keep="first")


def _build_charts(rows: pd.DataFrame, bars: int, max_n: int | None,
                  *, missing: dict[str, str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if rows is None or rows.empty:
        return out
    for r in rows.itertuples():
        mkt = str(getattr(r, "market", "")).upper()
        tkr = str(r.ticker).zfill(6) if mkt == "KR" else str(r.ticker).upper()
        key = f"{mkt}:{tkr}"
        def unavailable(reason: str) -> None:
            if missing is not None:
                missing[key] = reason
        if max_n is not None and len(out) >= max_n:
            unavailable("limit_reached")
            continue
        cpath = (KR_HISTORY_CACHE_DIR / f"{tkr}.pkl") if mkt == "KR" else (US_HISTORY_CACHE_DIR / f"{tkr}.pkl")
        if not cpath.exists():
            unavailable("cache_missing")
            continue
        try:
            try:
                h = pd.read_pickle(cpath)
            except Exception as _exc:
                unavailable("cache_read_error")
                _swallow("_build_charts", _exc, tkr)
                continue
            if mkt == "KR":
                h = kr_completed_cached_bars(cpath, h)
            else:
                h = us_completed_cached_bars(cpath, h)
            if h.empty:
                unavailable("no_confirmed_bars")
                continue
            as_of = pd.Timestamp(getattr(r, "as_of", None)) if pd.notna(getattr(r, "as_of", None)) else h.index.max()
            h = h.loc[h.index <= as_of]
            if h.empty:
                unavailable("no_bars_as_of")
                continue
            if not {"Open", "High", "Low", "Close", "Volume"}.issubset(h.columns):
                unavailable("missing_ohlcv")
                continue
            if len(h) < 30:
                unavailable("insufficient_bars")
                continue
            c = h["Close"].astype(float)
            sm = {20: c.rolling(20).mean(), 50: c.rolling(50).mean(), 200: c.rolling(200).mean()}
            t = h.tail(bars)
            def _arr(series: pd.Series) -> list[Any]:
                # Preserve calculation precision; formatting belongs to the UI.
                return [None if pd.isna(x) else float(x) for x in series]
            out[f"{mkt}:{tkr}"] = {
                "d": [d.strftime("%Y-%m-%d") for d in t.index],
                "o": _arr(t["Open"]), "h": _arr(t["High"]), "l": _arr(t["Low"]), "c": _arr(t["Close"]),
                "v": [None if pd.isna(x) else int(x) for x in t["Volume"]],
                "s20": _arr(sm[20].loc[t.index]), "s50": _arr(sm[50].loc[t.index]), "s200": _arr(sm[200].loc[t.index]),
                "pivot": _json_safe(getattr(r, "pivot_price", None)), "stop": _json_safe(getattr(r, "stop_price", None)),
                "market": mkt, "ticker": tkr, "snapshot_as_of": as_of.strftime("%Y-%m-%d"),
                "pivot_inputs_as_of": _json_safe(getattr(r, "pivot_inputs_as_of", None)),
                "line_basis": "current_snapshot_not_historical_signal",
            }
        except Exception as _exc:
            unavailable("chart_build_error")
            _swallow("_build_charts", _exc, tkr)
    return out


def _charts_for_publication(action: pd.DataFrame, selected: pd.DataFrame,
                            candidates: pd.DataFrame, pcfg: dict) -> tuple[dict, dict]:
    """Use only existing OHLCV caches; account for every requested identity."""
    scope = str(pcfg.get("chart_scope", "all_candidates"))
    if scope not in ("all_candidates", "selected"):
        raise ValueError(f"Unknown chart_scope: {scope}")
    requested = _chart_rows(action, selected, candidates)
    targeted = requested if scope == "all_candidates" else _chart_rows(action, selected)
    bars = int(pcfg.get("chart_bars", 260))
    limit = None if scope == "all_candidates" else int(pcfg.get("chart_max", 60))
    if bars < 1 or (limit is not None and limit < 0):
        raise ValueError("Invalid chart publication limits")
    missing: dict[str, str] = {}
    charts = _build_charts(targeted, bars, limit, missing=missing)
    targeted_keys = set(targeted["market"] + ":" + targeted["ticker"])
    for r in requested.itertuples():
        key = f"{r.market}:{r.ticker}"
        if key not in targeted_keys:
            missing[key] = "outside_chart_scope"
    coverage = {"version": 1, "scope": scope, "requested": len(requested),
                "targeted": len(targeted), "available": len(charts), "bars": bars,
                "limit": limit, "missing": missing,
                "reason_counts": dict(Counter(missing.values()))}
    return charts, coverage

def _gzip_json(obj: Any) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:   # mtime=0: 내용이 같으면 바이트도 같아 git diff가 안 생김
        gz.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return buf.getvalue()

def validate_published_dir(d: Path) -> list[str]:
    """게시 폴더 검증: manifest/summary 파싱, 파일 존재·해시, 필수 컬럼, 차트 해석. 오류 문자열 목록(빈 리스트=정상)."""
    errs: list[str] = []
    try:
        man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        summ = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    except Exception as exc:
        return [f"manifest/summary 파싱 실패: {exc}"]
    if man.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version 불일치: {man.get('schema_version')}")
    if "base_counts.json" in (man.get("files") or {}):
        try:
            support = _base_count_support()
            errs.extend(support.validate_publication(json.loads((d / support.FILE_NAME).read_bytes()), man.get("run_id")))
        except Exception:
            errs.append("Base Count 관측 파일 읽기 실패")
    if not summ.get("regimes"):
        errs.append("summary.regimes 비어 있음")
    for name, meta in (man.get("files") or {}).items():
        p = d / name
        if not p.exists():
            errs.append(f"파일 누락: {name}")
            continue
        if _sha256_file(p) != meta.get("sha256"):
            errs.append(f"해시 불일치: {name}")
    for name, req in CONTRACT_REQUIRED.items():
        p = d / name
        if not p.exists():
            errs.append(f"필수 파일 누락: {name}")
            continue
        try:
            cols = list(pd.read_csv(p, nrows=0).columns)
            miss = [c for c in req if c not in cols]
            if miss:
                errs.append(f"{name} 필수 컬럼 누락: {miss}")
        except Exception as exc:
            errs.append(f"{name} 읽기 실패: {exc}")
    cp = d / CHARTS_FILE
    if cp.exists():
        try:
            charts = json.loads(gzip.decompress(cp.read_bytes()).decode("utf-8"))
            coverage = summ.get("chart_coverage")
            if coverage is not None:
                missing = coverage["missing"]
                if (not isinstance(charts, dict) or not isinstance(missing, dict)
                        or coverage["available"] != len(charts)
                        or coverage["requested"] != len(charts) + len(missing)
                        or set(charts) & set(missing)
                        or coverage["reason_counts"] != dict(Counter(missing.values()))):
                    errs.append("chart_coverage 집계 불일치")
        except Exception as exc:
            errs.append(f"{CHARTS_FILE} 해석 실패: {exc}")
    return errs

def _upsert_csv(path: Path, new_df: pd.DataFrame, keys: list[str]) -> None:
    if new_df is None or new_df.empty:
        return
    new_df = new_df.copy()
    if path.exists():
        try:
            old = pd.read_csv(path, dtype=str)
            if not set(keys).issubset(old.columns):
                raise ValueError("history identity columns missing")
            comb = pd.concat([old, new_df.astype(str)], ignore_index=True)
        except Exception as _exc:
            _swallow("_upsert_csv", _exc, path.name)
            raise ValueError("기존 이력 읽기 실패: 원본을 보존하고 갱신을 중단합니다") from _exc
    else:
        comb = new_df.astype(str)
    comb = _strip_html_df(comb.drop_duplicates(subset=keys, keep="last"))   # 과거 행의 HTML도 함께 정리(자가치유)
    path.parent.mkdir(parents=True, exist_ok=True)
    comb.to_csv(path, index=False, encoding="utf-8")

def update_signal_ledger(base: Path, action_df: pd.DataFrame, d_str: str, cooldown_days: int = 14, *, published_at: str | None = None, generated_at: str | None = None, run_id: str | None = None, regimes: dict | None = None, config_hash: str | None = None) -> int:
    """신규 액션 신호 이벤트 원장(append-only). 같은 종목은 cooldown_days 안에서는 다시 기록하지 않는다."""
    if action_df is None or action_df.empty:
        return 0
    path = base / "history" / "signals.csv"
    last_seen: dict[str, pd.Timestamp] = {}
    if path.exists():
        try:
            old = pd.read_csv(path, dtype=str)
            for m, t, fs in zip(old["market"], old["ticker"], old["first_seen"]):
                k = f"{m}:{t}"
                ts = pd.Timestamp(fs)
                if k not in last_seen or ts > last_seen[k]:
                    last_seen[k] = ts
        except Exception as _exc:
            _swallow("update_signal_ledger", _exc)
    today = pd.Timestamp(d_str)
    rows = []
    for r in action_df.itertuples():
        k = f"{getattr(r, 'market', '')}:{r.ticker}"
        if k in last_seen and 0 <= (today - last_seen[k]).days < cooldown_days:
            continue
        pv = getattr(r, "pivot_price", np.nan)
        _dpiv = float(getattr(r, "dist_to_pivot_pct", 0.0) or 0.0)
        last = float(getattr(r, "last_close", 0.0) or 0.0)
        entry_ref = max(last, float(pv)) if (pd.notna(pv) and float(pv) > 0 and float(pv)*0.975 <= last < float(pv)) else getattr(r, "last_close", np.nan)
        rows.append({
            "published_at": published_at, "generated_at": generated_at,
            "workflow_run_id": os.getenv("GITHUB_RUN_ID"), "run_id": run_id,
            "regime": ((regimes or {}).get(getattr(r, "sub_market", "")) or {}).get("regime"),
            "first_seen": d_str, "as_of": getattr(r, "as_of", d_str), "market": getattr(r, "market", ""), "sub_market": getattr(r, "sub_market", ""),
            "ticker": r.ticker, "name": getattr(r, "name", ""), "sector": getattr(r, "sector", ""), "setup_stage": getattr(r, "setup_stage", ""),
            "tier": getattr(r, "tier", ""), "entry_ref": entry_ref, "stop_price": getattr(r, "stop_price", np.nan),
            "rs_score": getattr(r, "rs_score", np.nan), "topdown_score": getattr(r, "topdown_score", np.nan),
            "rs_change_4w": getattr(r, "rs_change_4w", None), "sector_power": getattr(r, "sector_power", None),
            "signal_close": getattr(r, "last_close", None), "config_hash": config_hash,
            "engine_version": ENGINE_VERSION, "engine_commit": os.getenv("GITHUB_SHA"),
            **{key: getattr(r, key, None) for key in SIGNAL_FLAGS},
        })
    if rows:
        _upsert_csv(path, pd.DataFrame(rows, columns=SIGNAL_LEDGER_COLS), ["first_seen", "market", "ticker"])
    return len(rows)

CAND_HIST_COLS = ["date", "as_of", "market", "sub_market", "ticker", "name", "sector", "macro_theme", "tier", "setup_stage",
                  "is_actionable", "selected", "rs_score", "rs_change_4w", "topdown_score", "sector_power", "last_close",
                  "stop_price", "dist_to_pivot_pct", "volume_ratio", "vol_ratio_50d", "streak_days", "peg", "eps_growth_pct", "eps_status"]
OUTCOME_COLS = ["first_seen", "as_of", "market", "sub_market", "track", "ticker", "name", "sector", "setup_stage", "tier",
                "entry_date", "entry_price", "stop_price", "risk_pct", "bars", "status", "cur_ret",
                "ret_5d", "ret_10d", "ret_20d", "ex_5d", "ex_10d", "ex_20d",
                "mfe_10d_pct", "mae_10d_pct", "stop_hit_10d",
                "mfe_pct", "mae_pct", "stop_hit", "r_10d",
                "stop_aware_ret_10d", "stop_aware_r_10d", "stop_exit_reason", "stop_exit_date",
                "calculation_version", "entry_price_source", "stop_price_basis", "evaluated_as_of",
                "eval_date_5d", "eval_date_10d", "eval_date_20d", "path_ohlc_complete_10d",
                "calendar_complete_5d", "calendar_complete_10d", "calendar_complete_20d", "entry_session_verified", "calendar_policy"]

def update_candidate_history(base: Path, cand: pd.DataFrame, d_str: str, keep_days: int = 400) -> int:
    """매일의 '상위 선정 ∪ 매수검토' 종목을 컴팩트하게 누적(date,market,ticker 키). 체류일·RS 추이·신규/이탈 분석용."""
    if cand is None or cand.empty:
        return 0
    c = cand.copy()
    act = c["is_actionable"].astype(str).str.lower().eq("true") if "is_actionable" in c.columns else pd.Series(False, index=c.index)
    sel = c["selected"].astype(str).str.lower().eq("true") if "selected" in c.columns else pd.Series(False, index=c.index)
    c = c[act | sel].copy()
    if c.empty:
        return 0
    c.insert(0, "date", d_str)
    cols = [x for x in CAND_HIST_COLS if x in c.columns]
    path = base / "history" / "candidates_daily.csv"
    _upsert_csv(path, _strip_html_df(c[cols]), ["date", "market", "ticker"])
    if keep_days > 0 and path.exists():
        try:
            h = pd.read_csv(path, dtype=str)
            cutoff = (pd.Timestamp(d_str) - timedelta(days=int(keep_days))).strftime("%Y-%m-%d")
            h = h[h["date"] >= cutoff]
            h.to_csv(path, index=False, encoding="utf-8")
        except Exception as _exc:
            _swallow("update_candidate_history", _exc)
    return len(c)

def update_signal_outcomes(base: Path, cfg: dict) -> int:
    """신호별 시간기준 수익과 손절 체결 가정 수익을 분리해 계산하고, 누락 캐시는 기존 이력을 보존한다."""
    sp = base / "history" / "signals.csv"
    if not sp.exists():
        return 0
    try:
        sig = pd.read_csv(sp, dtype=str)
    except Exception as _exc:
        _swallow("update_signal_outcomes", _exc)
        return 0

    out_path = sp.parent / "signal_outcomes.csv"
    try:
        old_out = pd.read_csv(out_path, dtype=str) if out_path.exists() else pd.DataFrame()
    except Exception as _exc:
        _swallow("update_signal_outcomes_old", _exc, out_path.name)
        return 0  # A read failure must never replace the existing history with a partial file.
    if not old_out.empty and not {"first_seen", "market", "ticker"}.issubset(old_out.columns):
        _swallow("update_signal_outcomes_old", ValueError("history identity columns missing"), out_path.name)
        return 0

    bench = _make_bench_getter(cfg)
    cost = float(cfg.get("screen", {}).get("round_trip_cost_pct", 0.003)) * 100.0
    price_cache: dict[str, pd.DataFrame] = {}
    rows: list[dict[str, Any]] = []

    # O(1) 해시 테이블로 기존 신호 결과 인덱싱 (O(N^2) 반복 검색 제거)
    old_lookup: dict[tuple[str, str, str], dict[str, Any]] = {}
    if not old_out.empty and {"first_seen", "market", "ticker"}.issubset(old_out.columns):
        for _rec in old_out.to_dict("records"):
            _m = str(_rec.get("market", "")).upper()
            _t = str(_rec.get("ticker", "")).zfill(6) if _m == "KR" else str(_rec.get("ticker", "")).upper()
            old_lookup[(str(_rec.get("first_seen", "")), _m, _t)] = _rec

    def _hret(frame: pd.DataFrame, entry_p: float, n: int, c_cost: float) -> float:
        return ((float(frame["Close"].iloc[n - 1]) / entry_p - 1.0) * 100.0 - c_cost) if len(frame) >= n else np.nan

    for r in sig.itertuples():
        mkt = str(r.market).upper()
        tkr = str(r.ticker).zfill(6) if mkt == "KR" else str(r.ticker).upper()
        l_key = (str(r.first_seen), mkt, tkr)
        prev_rec = old_lookup.get(l_key)
        if prev_rec is not None:
            _old_bars = float(pd.to_numeric(prev_rec.get("bars", 0), errors="coerce") or 0.0)
            _old_status = str(prev_rec.get("status", ""))
            _has_sa = pd.notna(prev_rec.get("stop_aware_ret_10d", np.nan)) and str(prev_rec.get("stop_aware_ret_10d", "")) not in ("", "nan")
            _has_10d_sep = pd.notna(prev_rec.get("mfe_10d_pct", np.nan)) and str(prev_rec.get("mfe_10d_pct", "")) not in ("", "nan")
            # 이미 20거래일 평가 완료(종료) 및 10일/20일 분리 지표까지 모두 채워진 신호는 파일 I/O 없이 즉시 스킵
            if _old_status == "종료" and _old_bars >= 20 and _has_sa and _has_10d_sep:
                continue
            # Existing KR results before the reviewed closure boundary are not
            # migrated in-place. A separately approved replay must retain lineage.
            _policy_asof = pd.to_datetime(getattr(r, "as_of", None), errors="coerce")
            if pd.isna(_policy_asof):
                _policy_asof = pd.to_datetime(r.first_seen, errors="coerce")
            if (mkt == "KR" and prev_rec.get("calendar_policy") != calendar_policy(mkt)
                    and (pd.isna(_policy_asof) or _policy_asof.date() <= pd.Timestamp(KR_CLOSED_DATES[-1]).date())):
                LOG.warning("KR 기존 달력 성과 보존: %s %s — 별도 재검증 필요", r.first_seen, tkr)
                continue

        key = f"{mkt}:{tkr}"
        if key not in price_cache:
            cpath = (KR_HISTORY_CACHE_DIR / f"{tkr}.pkl") if mkt == "KR" else (US_HISTORY_CACHE_DIR / f"{tkr}.pkl")
            try:
                frame = pd.read_pickle(cpath) if cpath.exists() else pd.DataFrame()
                if not frame.empty:
                    if mkt == "KR":
                        frame = kr_completed_cached_bars(cpath, frame)
                    else:
                        frame = us_completed_cached_bars(cpath, frame)
                    frame = frame.copy()
                    frame.index = pd.to_datetime(frame.index)
                    if frame.index.tz is not None: frame.index = frame.index.tz_localize(None)
                    frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
                    zone = KST if mkt == "KR" else ZoneInfo("America/New_York")
                    today = pd.Timestamp(datetime.now(timezone.utc).astimezone(zone).date())
                    frame = trim_incomplete_bar(frame.loc[frame.index <= today], mkt, True)
                price_cache[key] = frame
            except Exception as _exc:
                _swallow("update_signal_outcomes", _exc, tkr)
                price_cache[key] = pd.DataFrame()

        h = price_cache[key]
        raw_as_of = getattr(r, "as_of", None)
        as_of = pd.Timestamp(raw_as_of if raw_as_of and str(raw_as_of).lower() != "nan" else r.first_seen)
        base_row = {
            "first_seen": r.first_seen, "as_of": as_of.strftime("%Y-%m-%d"), "market": mkt,
            "sub_market": getattr(r, "sub_market", ""), "track": getattr(r, "sub_market", ""),
            "ticker": tkr, "name": getattr(r, "name", ""), "sector": getattr(r, "sector", ""),
            "setup_stage": getattr(r, "setup_stage", ""), "tier": getattr(r, "tier", ""),
        }
        if h.empty or "Close" not in h.columns:
            if prev_rec is None:
                rows.append({**base_row, "bars": 0, "status": "데이터없음"})
            continue

        after = h.loc[h.index > as_of]
        if after.empty:
            if prev_rec is None:
                rows.append({**base_row, "bars": 0, "status": "대기"})
            continue
        if prev_rec is not None:
            _old_bars = float(pd.to_numeric(prev_rec.get("bars", 0), errors="coerce") or 0.0)
            if _old_bars > len(after):
                continue

        eb = after.iloc[0]
        has_open = "Open" in after.columns and pd.notna(eb.get("Open")) and np.isfinite(float(eb["Open"])) and float(eb["Open"]) > 0
        entry_p = float(eb["Open"]) if has_open else float(eb["Close"])
        halt = str(eb.get("IsHalt", "false")).lower() in ("true", "1", "1.0")
        raw_open = str(eb.get("_raw_open_valid", "")).lower() in ("true","1","1.0")
        entry_source = ("open" if raw_open else "open_unverified") if has_open and not halt else "close_fallback_or_halt"
        if not np.isfinite(entry_p) or entry_p <= 0:
            if prev_rec is None:
                rows.append({**base_row, "bars": 0, "status": "진입가격오류"})
            continue

        try:
            stop = float(getattr(r, "stop_price", np.nan))
        except Exception:
            stop = np.nan
        win10 = after.iloc[:10]
        lows10 = win10["Low"] if "Low" in win10.columns else win10["Close"]
        highs10 = win10["High"] if "High" in win10.columns else win10["Close"]
        win = after.iloc[:20]
        lows = win["Low"] if "Low" in win.columns else win["Close"]
        highs = win["High"] if "High" in win.columns else win["Close"]
        risk_pct = (entry_p - stop) / entry_p * 100.0 if (np.isfinite(stop) and 0 < stop < entry_p) else np.nan
        ret = {n: _hret(after, entry_p, n, cost) for n in (5, 10, 20)}
        ex = {5: np.nan, 10: np.nan, 20: np.nan}
        calendar_complete = {5: False, 10: False, 20: False}
        bdf = bench(str(getattr(r, "sub_market", "")).upper(), mkt)
        next_sessions = bdf.index[bdf.index > as_of] if not bdf.empty else pd.DatetimeIndex([])
        entry_session_verified = as_of in bdf.index and len(next_sessions)>0 and next_sessions[0]==eb.name
        # Matching caches alone cannot detect a session missing from BOTH feeds.
        # Keep reference returns, but certify timing against the exchange calendar.
        exchange = None
        try:
            exchange = get_calendar(mkt)
            entry_session_verified = bool(entry_session_verified and exchange.is_session(as_of)
                                          and exchange.next_session(as_of) == eb.name)
        except (ValueError, KeyError, OverflowError):
            entry_session_verified = False
        if not bdf.empty and eb.name in bdf.index:
            bo = float(bdf.loc[eb.name, "Open"]) if "Open" in bdf and pd.notna(bdf.loc[eb.name, "Open"]) else np.nan
            if (str(bdf.loc[eb.name].get("IsHalt", "false")).lower() in ("true", "1", "1.0")
                    or str(bdf.loc[eb.name].get("_raw_open_valid", "")).lower() not in ("true","1","1.0")):
                bo = np.nan
            if np.isfinite(bo) and bo > 0:
                for n in (5, 10, 20):
                    if len(after) >= n and after.index[n-1] in bdf.index and pd.notna(ret[n]):
                        calendar_complete[n] = after.index[:n].tolist() == bdf.loc[(bdf.index >= eb.name) & (bdf.index <= after.index[n-1])].index.tolist()
                        try:
                            expected = exchange.sessions_in_range(eb.name, after.index[n-1]) if exchange is not None else []
                            calendar_complete[n] = bool(calendar_complete[n] and entry_session_verified
                                                       and after.index[:n].tolist() == list(expected))
                        except (ValueError, KeyError, OverflowError):
                            calendar_complete[n] = False
                        bc = float(bdf.loc[after.index[n-1], "Close"])
                        if np.isfinite(bc) and bc > 0:
                            ex[n] = ret[n] - (bc / bo - 1.0) * 100.0

        # Absolute historical stops cannot be silently rebased after split/dividend adjustments.
        recorded_close = pd.to_numeric(getattr(r, "signal_close", np.nan), errors="coerce")
        cached_close = float(h.loc[as_of, "Close"]) if as_of in h.index else np.nan
        stop_basis = "unverified"
        if pd.notna(recorded_close) and recorded_close > 0 and np.isfinite(cached_close):
            stop_basis = "matched" if abs(cached_close / recorded_close - 1.0) <= 1e-6 else "changed"
        path_complete = len(win10) >= 10 and all(col in win10 and
            pd.to_numeric(win10[col], errors="coerce").replace([np.inf, -np.inf], np.nan).gt(0).fillna(False).all()
            for col in ("Open", "High", "Low", "Close"))
        if "IsHalt" in win10:
            path_complete = path_complete and not win10["IsHalt"].astype(str).str.lower().isin(("true","1","1.0")).any()
        path_complete = path_complete and win10.get("_raw_ohlc_valid",pd.Series(index=win10.index,dtype=str)).astype(str).str.lower().isin(("true","1","1.0")).all()

        # 별도 10거래일 손절 시뮬레이션: 갭 하락은 시가, 장중 터치는 stop 가격 체결로 계산.
        stop_aware_ret, stop_aware_r = np.nan, np.nan
        stop_exit_reason, stop_exit_date = "손절가 미설정", ""
        if np.isfinite(stop) and 0 < stop < entry_p:
            _exit = np.nan
            for _dt, _bar in after.iloc[:10].iterrows():
                _op = float(_bar["Open"]) if "Open" in after.columns and pd.notna(_bar.get("Open")) else np.nan
                _lo = float(_bar["Low"]) if "Low" in after.columns and pd.notna(_bar.get("Low")) else float(_bar["Close"])
                if np.isfinite(_op) and _op <= stop:
                    _exit = _op
                    stop_exit_reason = "갭하락 손절(시가)"
                    stop_exit_date = _dt.strftime("%Y-%m-%d")
                    break
                if _lo <= stop:
                    _exit = stop
                    stop_exit_reason = "장중 손절(손절가)"
                    stop_exit_date = _dt.strftime("%Y-%m-%d")
                    break
            else:
                if len(after) >= 10:
                    _exit = float(after["Close"].iloc[9])
                    stop_exit_reason = "10일 보유(손절 미도달)"
                    stop_exit_date = after.index[9].strftime("%Y-%m-%d")
            if np.isfinite(_exit):
                stop_aware_ret = (_exit / entry_p - 1.0) * 100.0 - cost
                stop_aware_r = stop_aware_ret / risk_pct if risk_pct > 0 else np.nan

        rows.append({
            **base_row, "entry_date": eb.name.strftime("%Y-%m-%d"), "entry_price": entry_p,
            "stop_price": stop if np.isfinite(stop) else np.nan, "risk_pct": risk_pct,
            "bars": int(len(after)), "status": "종료" if len(after) >= 20 else "진행",
            "cur_ret": (float(after["Close"].iloc[min(len(after), 20) - 1]) / entry_p - 1.0) * 100.0 - cost,
            "ret_5d": ret[5], "ret_10d": ret[10], "ret_20d": ret[20],
            "ex_5d": ex[5], "ex_10d": ex[10], "ex_20d": ex[20],
            "mfe_10d_pct": (float(highs10.max()) / entry_p - 1.0) * 100.0,
            "mae_10d_pct": (float(lows10.min()) / entry_p - 1.0) * 100.0,
            "stop_hit_10d": bool(np.isfinite(stop) and float(lows10.min()) <= stop),
            "mfe_pct": (float(highs.max()) / entry_p - 1.0) * 100.0,
            "mae_pct": (float(lows.min()) / entry_p - 1.0) * 100.0,
            "stop_hit": bool(np.isfinite(stop) and float(lows.min()) <= stop),
            "r_10d": (ret[10] / risk_pct) if (pd.notna(ret[10]) and pd.notna(risk_pct) and risk_pct > 0) else np.nan,
            "stop_aware_ret_10d": stop_aware_ret, "stop_aware_r_10d": stop_aware_r,
            "stop_exit_reason": stop_exit_reason, "stop_exit_date": stop_exit_date,
            "calculation_version": CALCULATION_VERSION, "entry_price_source": entry_source,
            "calendar_policy": calendar_policy(mkt),
            "stop_price_basis": stop_basis, "evaluated_as_of": h.index[-1].strftime("%Y-%m-%d"),
            "path_ohlc_complete_10d": bool(path_complete),
            "entry_session_verified": bool(entry_session_verified),
            **{f"eval_date_{n}d": after.index[n-1].strftime("%Y-%m-%d") if len(after)>=n else None for n in (5,10,20)},
            **{f"calendar_complete_{n}d": calendar_complete[n] for n in (5,10,20)},
        })

    new_out = pd.DataFrame(rows, columns=OUTCOME_COLS)
    if new_out.empty and out_path.exists():
        return len(old_out)  # Preserve complete/held histories byte-for-byte.
    if not old_out.empty:
        for col in OUTCOME_COLS:
            if col not in old_out.columns:
                old_out[col] = np.nan
        merged = pd.concat([old_out[OUTCOME_COLS], new_out], ignore_index=True)
    else:
        merged = new_out
    if not merged.empty:
        key_cols = ["first_seen", "market", "ticker", "setup_stage"]
        # CSV empty strings round-trip as NaN; both mean an unspecified stage.
        merged["setup_stage"] = merged["setup_stage"].fillna("")
        merged = merged.drop_duplicates(subset=key_cols, keep="last")
    tmp = out_path.with_suffix(".csv.tmp")
    merged.to_csv(tmp, index=False, encoding="utf-8")
    os.replace(tmp, out_path)
    return len(merged)

def _base_count_support():
    if __package__:
        from . import base_count_publish
    else:
        import base_count_publish
    return base_count_publish


def _base_count_active(base: Path, cfg: dict) -> bool:
    return cfg.get("base_count", {}).get("enabled") is True or (base / "latest" / "base_counts.json").exists()


def publish_artifacts(d_str: str, track_results: dict[str, pd.DataFrame], track_all_passed: dict[str, pd.DataFrame],
                      regimes: dict[str, dict], extra_meta: dict[str, Any], dropped_df: pd.DataFrame, val_df: pd.DataFrame,
                      cfg: dict, base_dir: Path | None = None) -> Path | None:
    """Serialize optional observation transactions; keep the default publication unchanged."""
    pcfg = cfg.get("publish", {})
    if not pcfg.get("enabled", True):
        return None
    base = Path(base_dir) if base_dir else ROOT / str(pcfg.get("dir", "published"))
    args = (d_str, track_results, track_all_passed, regimes, extra_meta, dropped_df, val_df, cfg, base_dir)
    if not _base_count_active(base, cfg):
        return _publish_artifacts(*args)
    try:
        with _base_count_support().publication_lock(base):
            return _publish_artifacts(*args)
    except (ValueError, OSError) as exc:
        LOG.warning("[Base Count] 게시 잠금/저장 실패. 기존 게시 유지: %s", type(exc).__name__)
        return None


def _publish_artifacts(d_str: str, track_results: dict[str, pd.DataFrame], track_all_passed: dict[str, pd.DataFrame],
                      regimes: dict[str, dict], extra_meta: dict[str, Any], dropped_df: pd.DataFrame, val_df: pd.DataFrame,
                      cfg: dict, base_dir: Path | None = None) -> Path | None:
    """데이터 계약 파일을 임시 폴더에 만들고 검증한 뒤 latest/로 원자적으로 교체한다. 실패하면 기존 latest를 그대로 둔다."""
    pcfg = cfg.get("publish", {})
    if not pcfg.get("enabled", True):
        return None
    base = Path(base_dir) if base_dir else (ROOT / str(pcfg.get("dir", "published")))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    generated_at = datetime.now(KST).isoformat(timespec="seconds")
    stage = base / f".staging-{run_id}"
    sdir = stage / "latest"
    sdir.mkdir(parents=True, exist_ok=True)
    try:
        try:
            previous_market = read_previous_market(base)
        except Exception as exc:
            previous_market = None
            LOG.warning("[분석자료] 이전 게시본 비교 불가: %s", type(exc).__name__)
        all_sel_df, action_df, port_meta = build_action_table(track_results, regimes, cfg)
        all_sel_df, action_df = _ensure_market_col(all_sel_df), _ensure_market_col(action_df)
        frames = [df for df in (track_all_passed or {}).values() if df is not None and not df.empty]
        cand = _ensure_market_col(pd.concat(frames, ignore_index=True)) if frames else pd.DataFrame()
        sel_keys = set(zip(all_sel_df["market"].astype(str), all_sel_df["ticker"].astype(str))) if not all_sel_df.empty else set()
        if not cand.empty:
            cand["selected"] = [(str(m), str(t)) in sel_keys for m, t in zip(cand["market"], cand["ticker"])]
        else:
            cand = pd.DataFrame(columns=CONTRACT_REQUIRED["candidates.csv"])
        wl_cols = [c for c in WATCHLIST_COLS if c in action_df.columns] if not action_df.empty else WATCHLIST_COLS
        watch = action_df[wl_cols].copy() if not action_df.empty else pd.DataFrame(columns=WATCHLIST_COLS)
        sectors = _strip_html_df(_sector_frame(extra_meta))
        cand = _strip_html_df(cand)
        watch = _strip_html_df(watch)

        rows_meta: dict[str, int] = {}
        rows_meta["watchlist.csv"] = _write_csv(watch, sdir / "watchlist.csv", CONTRACT_REQUIRED["watchlist.csv"])
        rows_meta["candidates.csv"] = _write_csv(cand, sdir / "candidates.csv", CONTRACT_REQUIRED["candidates.csv"])
        rows_meta["sectors.csv"] = _write_csv(sectors, sdir / "sectors.csv", CONTRACT_REQUIRED["sectors.csv"])
        rows_meta["dropped.csv"] = _write_csv(_strip_html_df(dropped_df) if dropped_df is not None else pd.DataFrame(), sdir / "dropped.csv", CONTRACT_REQUIRED["dropped.csv"])
        rows_meta["validation.csv"] = _write_csv(val_df if val_df is not None else pd.DataFrame(), sdir / "validation.csv", CONTRACT_REQUIRED["validation.csv"])

        chart_started = time.perf_counter()
        charts, chart_coverage = _charts_for_publication(action_df, all_sel_df, cand, pcfg)
        chart_bytes = _gzip_json(charts)
        (sdir / CHARTS_FILE).write_bytes(chart_bytes)
        rows_meta[CHARTS_FILE] = len(charts)
        LOG.info("[차트] 대상 %d / 제공 %d / 미제공 %d (%s), %.2fs / %d bytes",
                 chart_coverage["requested"], len(charts), len(chart_coverage["missing"]),
                 chart_coverage["reason_counts"], time.perf_counter() - chart_started, len(chart_bytes))

        h_kr, h_us = extra_meta.get("kr_health", {}), extra_meta.get("us_health", {})
        mk_rules = {}
        for mk, subs in (("KR", _KR_SUBS), ("US", _US_SUBS)):
            sub_r = {k: v for k, v in regimes.items() if k in subs}
            if sub_r:
                ec, rc, _ = _exposure_rule(sub_r, cfg)
                usage = port_meta["by_market"].get(mk, {})
                mk_rules[mk] = {"exposure_cap": ec, "risk_cap": rc, "alloc": usage.get("alloc", 0.),
                                "risk": usage.get("risk", 0.)}
        kr_macro, us_macro = extra_meta.get("kr_macro_df", pd.DataFrame()), extra_meta.get("us_macro_df", pd.DataFrame())
        summary = {
            "d_str": d_str, "regimes": regimes, "exposure_guide": _compute_exposure_guide(regimes),
            "metric_definitions": metric_definitions(cfg),
            "price_observation": price_observation(regimes, cand, generated_at),
            "chart_coverage": chart_coverage,
            "portfolio": {k: v for k, v in port_meta.items() if k != "by_market"}, "market_limits": mk_rules,
            "counts": {
                "selected": int(len(all_sel_df)), "actionable": int(len(action_df)), "passed": int(len(cand)),
                "leading_actionable": int(action_df["is_leading_sector"].sum()) if (not action_df.empty and "is_leading_sector" in action_df.columns) else 0,
                "dropped": int(len(dropped_df)) if dropped_df is not None else 0,
            },
            "top_theme": {
                "KR": str(kr_macro.iloc[0]["macro_theme"]) if (isinstance(kr_macro, pd.DataFrame) and not kr_macro.empty) else None,
                "US": str(us_macro.iloc[0]["macro_theme"]) if (isinstance(us_macro, pd.DataFrame) and not us_macro.empty) else None,
            },
            "health": {"kr": h_kr, "us": h_us, "swallowed": dict(SWALLOWED.most_common(20))},
        }
        (sdir / "summary.json").write_text(json.dumps(_json_safe(summary), ensure_ascii=False, indent=1), encoding="utf-8")

        base_observation = None
        if _base_count_active(base, cfg):
            support = _base_count_support()
            def base_prices(market, ticker):
                path = (KR_HISTORY_CACHE_DIR if market == "KR" else US_HISTORY_CACHE_DIR) / f"{ticker}.pkl"
                return pd.read_pickle(path) if path.exists() else pd.DataFrame()
            base_observation = support.prepare_publication(base, cand, cfg.get("base_count", {}), base_prices,
                                                          run_id=run_id, generated_at=generated_at)
            if base_observation is not None:
                (sdir / support.FILE_NAME).write_bytes(support.json_bytes(base_observation))
                rows_meta[support.FILE_NAME] = len(base_observation["observations"])
        sector_review_doc = None
        if cfg.get("_sector_review_capture") or (base / "latest" / "sector_review.json").exists():
            sector_support = _sector_review_support()
            sector_review_doc = sector_support.prepare_publication(base,
                extra_meta.get("sector_review_inputs", {}) if cfg.get("_sector_review_capture") else {},
                run_id=run_id, generated_at=generated_at,
                decisions=cfg.get("_sector_review_decisions", ()))
            sector_review_doc["paused"] = not bool(cfg.get("_sector_review_capture"))
            (sdir / sector_support.FILE_NAME).write_bytes(sector_support.json_bytes(sector_review_doc))
            rows_meta[sector_support.FILE_NAME] = len(sector_review_doc["candidates"])
        files_meta = {}
        optional_files = ["base_counts.json"] if base_observation is not None else []
        if sector_review_doc is not None:
            optional_files.append("sector_review.json")
        for name in list(CONTRACT_REQUIRED) + [CHARTS_FILE, "summary.json"] + optional_files:
            p = sdir / name
            files_meta[name] = {"bytes": p.stat().st_size, "sha256": _sha256_file(p), "rows": rows_meta.get(name)}
        manifest = {
            "schema_version": SCHEMA_VERSION, "engine_version": ENGINE_VERSION, "run_id": run_id, "d_str": d_str,
            "generated_at": generated_at,
            "engine_commit": os.getenv("GITHUB_SHA"),
            "tactical_calculation_version": "tactical-session-v2",
            "compass_calculation_version": "regime-budget-v1.1",
            "as_of": {k: v.get("as_of") for k, v in regimes.items()},
            "config_hash": get_config_version_hash(cfg), "files": files_meta,
            "contract": {"required_columns": CONTRACT_REQUIRED, "watchlist_columns": WATCHLIST_COLS, "charts_file": CHARTS_FILE},
        }
        if base_observation is not None:
            manifest["contract"]["optional_observations"] = {"base_count": {"file": "base_counts.json", "schema": support.SCHEMA}}
        if sector_review_doc is not None:
            manifest["contract"].setdefault("optional_observations", {})["sector_review"] = {
                "file": "sector_review.json", "schema": sector_support.SCHEMA}
        (sdir / "manifest.json").write_text(json.dumps(_json_safe(manifest), ensure_ascii=False, indent=1), encoding="utf-8")

        errs = validate_published_dir(sdir)
        if errs:
            LOG.error("[게시] 검증 실패 -> 기존 latest 유지: %s", "; ".join(errs[:5]))
            return None

        latest, old = base / "latest", base / f".old-{run_id}"
        had_latest = latest.exists()
        if had_latest:
            os.replace(latest, old)
        try:
            os.replace(sdir, latest)
        except Exception:
            if had_latest and old.exists() and not latest.exists():
                os.replace(old, latest)
            raise
        shutil.rmtree(old, ignore_errors=True)

        daily = base / "daily" / d_str
        daily.mkdir(parents=True, exist_ok=True)
        for name in list(CONTRACT_REQUIRED) + ["summary.json", "manifest.json"] + optional_files:
            shutil.copy2(latest / name, daily / name)
        keep = int(pcfg.get("keep_daily", 60))
        if keep > 0 and (base / "daily").exists():
            cutoff = (pd.Timestamp(d_str) - timedelta(days=int(keep))).strftime("%Y-%m-%d")
            for sub in (base / "daily").iterdir():
                if sub.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", sub.name) and sub.name < cutoff:
                    shutil.rmtree(sub, ignore_errors=True)

        # history (append/upsert 원장)
        history_update_ok = True
        history_error_prefixes = ("update_signal_", "update_candidate_history", "publish_artifacts")
        history_errors_before = sum(v for k, v in SWALLOWED.items() if k.startswith(history_error_prefixes))
        try:
            update_signal_ledger(base, action_df, d_str, int(pcfg.get("signal_cooldown_days", 14)),
                                 generated_at=manifest["generated_at"], run_id=run_id, regimes=regimes,
                                 config_hash=manifest["config_hash"])
            update_candidate_history(base, cand, d_str, int(pcfg.get("candidates_hist_days", 400)))
            update_signal_outcomes(base, cfg)
            reg_rows = [{"as_of": v.get("as_of"), "track": k, "signal_light": v.get("signal_light"), "regime": v.get("regime"),
                         "benchmark": v.get("benchmark"), "benchmark_close": v.get("benchmark_close"), "benchmark_sma50": v.get("benchmark_sma50"),
                         "benchmark_sma200": v.get("benchmark_sma200"), "distribution_days_25d": v.get("distribution_days_25d"),
                         "pct_above_50dma": v.get("pct_above_50dma"), "pct_above_200dma": v.get("pct_above_200dma"), "regime_quality": v.get("regime_quality")}
                        for k, v in regimes.items() if v.get("as_of") and v.get("regime_quality", "ok") == "ok"]
            _upsert_csv(base / "history" / "regime.csv", pd.DataFrame(reg_rows), ["as_of", "track"])
            if not sectors.empty:
                sh = sectors.copy()
                sh.insert(0, "date", d_str)
                sh["sector"] = sh["sector"].fillna("") if "sector" in sh.columns else ""
                keep_cols = [c for c in ["date", "market", "level", "macro_theme", "sector", "sector_power", "median_rs", "rs_delta_4w", "passed_cnt", "rotation_phase"] if c in sh.columns]
                _upsert_csv(base / "history" / "sector_power.csv", sh[keep_cols], ["date", "market", "level", "macro_theme", "sector"])
        except Exception as _exc:
            history_update_ok = False
            _swallow("publish_artifacts", _exc, "history")
        history_errors_after = sum(v for k, v in SWALLOWED.items() if k.startswith(history_error_prefixes))
        try:
            research = publish_analysis_report(base, previous_market,
                history_update_ok=history_update_ok and history_errors_before == history_errors_after,
                round_trip_cost_pct=float(cfg.get("screen", {}).get("round_trip_cost_pct", 0.003))*100.0)
            LOG.info("[분석자료] %s 생성 완료 (run_id=%s)", research, run_id)
        except Exception as exc:
            _swallow("publish_analysis_report", exc)
            LOG.warning("[분석자료] 보조 자료 생성 실패. 기존 시장 게시 유지: %s", type(exc).__name__)
        # Independent observation artifacts: never mutate fixed outcomes or research statistics.
        try:
            if __package__:
                from .trend_tracking import update_tracking
            else:
                from trend_tracking import update_tracking
            def tracking_prices(market, ticker):
                path = (KR_HISTORY_CACHE_DIR if market == "KR" else US_HISTORY_CACHE_DIR) / f"{ticker}.pkl"
                if not path.exists(): return pd.DataFrame()
                raw = pd.read_pickle(path)
                frame = kr_completed_cached_bars(path, raw) if market == "KR" else us_completed_cached_bars(path, raw)
                if frame.empty: return frame
                frame = frame.copy(); frame.index = pd.to_datetime(frame.index)
                if frame.index.tz is not None: frame.index = frame.index.tz_localize(None)
                return frame.loc[~frame.index.duplicated(keep="last")].sort_index()
            updated = update_tracking(base, tracking_prices, datetime.now(timezone.utc),
                                      float(cfg.get("screen", {}).get("round_trip_cost_pct", .003))*100,
                                      run_id=run_id, generated_at=manifest["generated_at"])
            LOG.info("[중장기 추적] %d 신호 갱신 (가격 API 추가 없음)", updated)
        except Exception as exc:
            _swallow("trend_tracking", exc)
            LOG.warning("[중장기 추적] 보조 자료 갱신 실패: %s", type(exc).__name__)
        LOG.info("[게시] %s 게시 완료 (run_id=%s, 후보 %d / 액션 %d / 차트 %d)", latest, run_id, len(cand), len(action_df), len(charts))
        return latest
    except Exception as exc:
        _swallow("publish_artifacts", exc)
        LOG.error("[게시] 실패(리포트는 정상 생성됨): %s", exc, exc_info=True)
        return None
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def run_self_tests(cfg: dict) -> int:
    idx = pd.bdate_range("2025-01-02", periods=260)
    base_p = np.linspace(100.0, 150.0, 240)
    vcp_p = np.array([150.0, 148.5, 151.0, 149.0, 150.5, 149.5, 150.2, 149.8, 150.1, 149.9] * 2)
    prices = np.concatenate([base_p[:-1], vcp_p, [154.0]])
    vols = np.concatenate([np.full(259, 1_000_000.0), [2_200_000.0]])
    synth = pd.DataFrame({
        "Open": prices * 0.995,
        "High": np.where(np.arange(260) == 259, 154.5, np.minimum(prices * 1.008, 151.2)),
        "Low": prices * 0.992, "Close": prices, "Volume": vols, "Turnover": prices * vols,
    }, index=idx)
    bench = pd.DataFrame({"Close": np.linspace(100.0, 110.0, 260), "Volume": np.full(260, 5_000_000.0)}, index=idx)
    feat = compute_features(synth, bench, cfg, rs_mode="STANDARD", is_risk_off=False)
    assert feat is not None, "compute_features 반환 실패"
    assert feat["strict_minervini"] is True, "미너비니 정배열 판정 오류"
    assert feat["is_breakout"] is True, f"피벗 돌파 판정 실패 (dist_to_pivot={feat['dist_to_pivot_pct']})"
    assert feat["in_buy_zone"] is True, "바이존 판정 실패"
    assert feat["position_size_pct"] > 0, "권장 비중 계산 실패"

    summer_et = datetime(2026, 7, 1, 12, tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    winter_et = datetime(2026, 1, 1, 12, tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    assert summer_et.utcoffset() == timedelta(hours=-4), "미국 서머타임 오프셋 오류"
    assert winter_et.utcoffset() == timedelta(hours=-5), "미국 표준시 오프셋 오류"

    small_rows = []
    for i, raw_rs in enumerate((0.01, 0.02, 0.03), 1):
        small_rows.append({
            "ticker": f"T{i}", "name": f"Test{i}", "sub_market": "TEST", "raw_rs": raw_rs, "raw_rs_fast": raw_rs,
            "raw_rs_prev20": raw_rs, "relative_63d": 0.0, "relative_126d": 0.0, "last_close": 110.0, "sma200": 100.0,
            "distance_from_high_pct": -0.05, "exit_signal": "✅정상", "trend_template": False, "strict_minervini": False,
        })
    small_passed, small_failed = apply_percentile_rs_and_filter(small_rows, cfg, "TEST")
    assert len(small_passed) == 1 and len(small_failed) == 2, "소규모 유니버스 RS 백분위 계산 오류"
    assert small_passed[0]["rs_score"] >= float(cfg["screen"]["min_rs_score"]), "RS 백분위 필터 오류"

    # 탑다운 주섹터/서브섹터 집계 및 신규 진입 카운트 검증
    td_raw = [
        {**feat, "ticker": "012450", "name": "한화에어로스페이스", "macro_theme": "조선·방산·우주", "sector": "K-방산·우주항공"},
        {**feat, "ticker": "042700", "name": "한미반도체", "macro_theme": "AI·반도체", "sector": "AI반도체·HBM·본딩장비"},
    ]
    td_passed = pd.DataFrame(td_raw)
    m_df, s_df, p_map = build_topdown_sector_tables(td_raw, td_passed, {"012450": {"streak_prev": 3}, "042700": {"streak_prev": 0}})
    assert not m_df.empty and not s_df.empty and len(p_map) == 2, "탑다운 섹터 테이블 생성 오류"
    assert int(s_df.loc[s_df["sector"] == "AI반도체·HBM·본딩장비", "new_cnt"].iloc[0]) == 1, "섹터 신규진입 카운트 오류"

    # [검증 1] 5일/10일/20일 변동폭 독립 계산 및 손절가 무클램핑 검증
    assert feat["range_5d_pct"] != feat["range_20d_pct"], "5일/20일 변동폭 중복 대입 오류"
    assert abs(feat["stop_dist_pct"] - feat["raw_stop_dist_pct"]) < 1e-9, "손절가와 손절폭 기준 불일치 오류"

    # [검증 2] 벤치마크 데이터 결측 시 RS 0.0 둔갑 차단 검증 (P0-2)
    empty_bench = pd.DataFrame(columns=["Close", "Volume"])
    assert compute_features(synth, empty_bench, cfg) is None, "벤치마크 결측 시 RS 산출 차단 실패"

    # [검증 3] KRX 비정상 데이터 감지 및 US cap=None 캐시 오염 차단 검증 (P0-3.2)
    bad_krx = pd.DataFrame({"종가": [0, 0], "시가총액": [0, 0], "거래대금": [0, 0]})
    assert is_valid_krx_cap_table(bad_krx, min_valid_rows=10) is False, "KRX 비정상 0 데이터 감지 실패"
    mock_cap_cache: dict[str, dict] = {"NVDA": {"cap": 3_000_000_000_000.0, "sector": "Tech", "peg": 1.2, "eps_g": 50.0, "ts": 100.0}}
    preserved_cap = update_us_cap_cache_entry(mock_cap_cache, "NVDA", None, "", np.nan, np.nan, 200.0)
    assert preserved_cap == 3_000_000_000_000.0 and mock_cap_cache["NVDA"]["ts"] == 100.0, "US 시총 실패 시 이전 캐시 보존 오류"
    failed_new = update_us_cap_cache_entry(mock_cap_cache, "FAKE", None, "", np.nan, np.nan, 200.0)
    assert failed_new is None and "FAKE" not in mock_cap_cache, "US 시총 실패(None) 캐시 오염 차단 오류"

    # [검증 4] EPS 흑자전환(TURNAROUND) 및 소규모 EPS 예외 처리 검증 (P1-4.3)
    eps_turn = evaluate_eps_growth_and_peg(cur_eps=1200.0, old_eps=-300.0, per=15.0)
    assert eps_turn["eps_status"] == "TURNAROUND" and pd.isna(eps_turn["peg"]), "EPS 흑자전환 판정 오류"
    eps_small = evaluate_eps_growth_and_peg(cur_eps=40.0, old_eps=20.0, per=20.0)
    assert eps_small["eps_status"] == "GROWTH" and eps_small["eps_growth_pct"] == 100.0, "소규모 EPS 성장률 계산 오류"

    # [검증 5] 포트폴리오 리스크 한도(사용자 cfg 반영), 섹터 집중 한도, 단일 시장 노출 가이드 일치 검증 (P0-1, P1-1)
    custom_cfg = deep_merge(cfg, {"screen": {"max_portfolio_risk_pct": 3.0, "max_sector_alloc_pct": 30.0}})
    mock_actions = pd.DataFrame([
        {"ticker": f"A{i}", "sector": "AI반도체·HBM·본딩장비", "position_size_pct": 20.0, "raw_stop_dist_pct": -0.05}
        for i in range(6)
    ])
    kr_only_regimes = {"KOSPI": {"signal_light": "🟢 ATTACK"}, "KOSDAQ": {"signal_light": "🟢 ATTACK"}}
    assert "80~100%" in _compute_exposure_guide(kr_only_regimes), "단일 시장(KR) ATTACK 노출 가이드 비율 오류"
    alloc_df, alloc_meta = allocate_portfolio_risk_budget(mock_actions, kr_only_regimes, custom_cfg)
    assert alloc_meta["exposure_cap_pct"] == 100.0, "단일 시장(KR) 노출 상한 계산 오류"
    assert alloc_meta["total_risk_pct"] <= 3.01, "사용자 설정 포트폴리오 총 리스크 한도(3.0%) 미반영 오류"
    assert alloc_df["portfolio_alloc_pct"].sum() <= 30.1, "사용자 설정 단일 섹터 집중도 한도(30.0%) 초과"

    # [검증 6] 2-Pass 레짐 동기화: 내부 붕괴로 RISK_OFF 전환 시 종목 비중 절반 축소 검증
    sample_rows = [{"position_size_pct": 20.0, "trade_risk_pct": 1.0, "raw_stop_dist_pct": -0.0485}]
    sync_track_sizing_with_final_regime(sample_rows, {"regime": "RISK_OFF"}, cfg)
    assert sample_rows[0]["position_size_pct"] == 10.0, "2-Pass RISK_OFF 포지션 비중 축소 동기화 오류"

    # [4차 검증] 음수 RS IPO 감점 방향 / 분배일 개별 소멸 / VCP 낙폭 판별 / 종목 분배일 윈도우
    ipo_rows = [
        {"ticker": f"N{i}", "name": f"N{i}", "sub_market": "TEST", "raw_rs": r_, "raw_rs_fast": r_, "raw_rs_prev20": r_,
         "relative_63d": 0.0, "relative_126d": 0.0, "last_close": 110.0, "sma200": 100.0, "distance_from_high_pct": -0.05,
         "exit_signal": "✅정상", "trend_template": False, "strict_minervini": False,
         "short_history": sh, "short_history_prev": sh}
        for i, (r_, sh) in enumerate([(-0.3, False), (-0.3, True), (0.3, False), (0.3, True)])
    ]
    ipo_pass, ipo_fail = apply_percentile_rs_and_filter(ipo_rows, cfg, "TEST")
    sc_map = {str(x["ticker"]): float(x["rs_score"]) for x in ipo_pass}
    sc_map.update({k: v["rs_score"] for k, v in ipo_fail.items()})
    assert sc_map["N1"] < sc_map["N0"] and sc_map["N3"] < sc_map["N2"], "IPO 감점 방향 오류(음수 RS 포함)"

    flat = np.full(260, 100.0)
    vol_idx = np.full(260, 1_000_000.0)
    flat[-20] = 99.0; vol_idx[-20] = 2_000_000.0   # 분배일 1개
    flat[-19:] = 99.0
    flat[-5:] = 105.0                                 # +5% 이상 반등 -> 소멸
    idx_df = pd.DataFrame({"Close": flat, "Volume": vol_idx, "High": flat, "Low": flat, "Open": flat},
                          index=pd.bdate_range("2025-01-02", periods=260))
    assert calc_market_regime(idx_df, True, "T").get("distribution_days_25d", -1) == 0, "분배일 개별 소멸 규칙 오류"
    flat2 = flat.copy(); flat2[-5:] = 99.5            # 반등 없음 -> 유지
    idx_df2 = idx_df.copy(); idx_df2["Close"] = flat2; idx_df2["High"] = flat2; idx_df2["Low"] = flat2; idx_df2["Open"] = flat2
    assert calc_market_regime(idx_df2, True, "T").get("distribution_days_25d", -1) == 1, "분배일 유지 규칙 오류"

    def _mk_hist(closes):
        n_ = len(closes)
        c_ = np.array(closes, float)
        d_ = pd.DataFrame({"Open": c_, "High": c_ * 1.004, "Low": c_ * 0.996, "Close": c_, "Volume": np.full(n_, 1e6)},
                          index=pd.bdate_range("2025-01-02", periods=n_))
        b_ = pd.DataFrame({"Close": np.linspace(100, 110, n_), "Volume": 5e6}, index=d_.index)
        return compute_features(d_, b_, cfg)
    pre_ = list(np.linspace(60, 100, 230))
    def _wave(start, depth, n=10):
        lo_ = start * (1 - depth)
        return list(np.linspace(start, lo_, n // 2)) + list(np.linspace(lo_, start, n - n // 2))
    smooth_f = _mk_hist(pre_ + list(np.linspace(100, 110, 10)) + list(np.linspace(110, 115.5, 10)) + list(np.linspace(115.5, 118.4, 10)))
    tb_f = _mk_hist(pre_ + _wave(100, 0.12) + _wave(100, 0.06) + _wave(100, 0.025))
    assert smooth_f["is_true_vcp"] is False, "눌림 없는 상승이 VCP로 판정됨"
    assert tb_f["is_true_vcp"] is True, "교과서식 수축(12/6/2.5%)이 VCP로 판정되지 않음"

    # 종목 분배일: 20일 평균이 25일 전체에 적용되는지 (랜덤 데이터에서 평균 1일 이상 발생해야 함)
    rng_t = np.random.default_rng(7)
    dd_cnt = []
    for _ in range(200):
        cc = pd.Series(100 * np.exp(np.cumsum(rng_t.normal(0, 0.02, 80))))
        vv = pd.Series(rng_t.lognormal(0, 0.5, 80))
        dd_cnt.append(int(((cc.pct_change() <= -0.01) & (vv.diff() > 0) & (vv > vv.rolling(20).mean().shift(1))).tail(25).sum()))
    assert 1.5 < float(np.mean(dd_cnt)) < 4.0, "종목 분배일 기대빈도 이탈"

    # [히스테리시스 검증] 1일 이탈 무시 / 2일 확정 / 해제 확인 / 하드스톱 / 휩소 감소
    set_regime_params(cfg)
    def _mk_idx(path):
        p_ = np.array(path, float)
        return pd.DataFrame({"Close": p_, "Volume": np.full(len(p_), 1e6), "High": p_, "Low": p_, "Open": p_},
                            index=pd.bdate_range("2024-01-02", periods=len(p_)))
    up_ = list(np.linspace(100, 150, 300))                      # 200선 대비 충분히 위
    dip1 = _mk_idx(up_[:-1] + [up_[-1] * 0.90])                  # 마지막 1일만 -10% (하드스톱 미만 아님: 200선 대비 확인)
    s200_ = pd.Series(dip1["Close"]).rolling(200).mean().iloc[-1]
    one_day = _mk_idx(up_[:-1] + [s200_ * 0.985])                # 밴드 밖 1일 하회
    _valid_breadth = {"breadth_valid": True, "breadth_status": "정상", "breadth_total": 100, "pct_above_50dma": 50.0, "pct_above_200dma": 50.0}
    assert calc_market_regime(one_day, True, "T", _valid_breadth)["regime"] in ("ATTACK", "CAUTION"), "1일 이탈이 RISK_OFF로 전환됨"
    _empty_breadth = compute_universe_breadth([])
    assert _empty_breadth["breadth_valid"] is False and pd.isna(_empty_breadth["pct_above_50dma"]), "빈 유니버스가 0% 폭락으로 오인됨"
    assert calc_market_regime(_mk_idx(up_), True, "T", _empty_breadth)["regime"] == "CAUTION", "내부체력 데이터 부족 시 CAUTION 처리 실패"
    _breadth_good = [{"above_50dma": (i % 2 == 0), "above_200dma": (i % 3 == 0),
                      "distance_from_high_pct": -0.02, "distance_from_low_pct": 0.05,
                      "strict_minervini": (i % 4 == 0)} for i in range(30)]
    assert compute_universe_breadth(_breadth_good)["breadth_valid"] is True, "유효 표본 30개 breadth 산출 실패"
    _breadth_bad = _breadth_good + [{"above_50dma": None, "above_200dma": None} for _ in range(25)]
    assert compute_universe_breadth(_breadth_bad)["breadth_valid"] is False, "유효 표본 커버리지 부족을 감지하지 못함"
    assert compute_universe_breadth(_breadth_good, expected_total=100)["breadth_valid"] is False, "유니버스 대량 수집 실패 시 커버리지 착시 차단 실패"
    _breadth_string = [{"above_50dma": "False", "above_200dma": "True"} for _ in range(30)]
    _breadth_string_result = compute_universe_breadth(_breadth_string)
    assert _breadth_string_result["pct_above_50dma"] == 0.0 and _breadth_string_result["pct_above_200dma"] == 100.0, "문자열 boolean 파싱 오류"
    two_day = _mk_idx(up_[:-2] + [s200_ * 0.985, s200_ * 0.985])  # 2일 연속
    assert calc_market_regime(two_day, True, "T")["regime"] == "RISK_OFF", "2일 연속 이탈 확정 실패"
    hard = _mk_idx(up_[:-1] + [s200_ * 0.90])                    # 하드스톱 즉시
    assert calc_market_regime(hard, True, "T")["regime"] == "RISK_OFF", "하드스톱 즉시 전환 실패"
    inband = _mk_idx(up_[:-1] + [s200_ * 0.995])                 # 밴드 안쪽 하회는 무시
    assert calc_market_regime(inband, True, "T", _valid_breadth)["regime"] in ("ATTACK", "CAUTION"), "밴드 내 하회가 전환됨"
    # 해제: 하락 확정 후 +1% 밴드 위 1일만으로는 해제되지 않고 2일이면 해제
    base_ = up_[:-3] + [s200_ * 0.985] * 3
    s200_b = pd.Series(base_).rolling(200).mean().iloc[-1]
    still = _mk_idx(base_[:-1] + [s200_b * 1.02])
    assert calc_market_regime(still, True, "T")["regime"] == "RISK_OFF", "1일 회복만으로 해제됨"
    # 휩소: 200선 주변 진동 시 전환 횟수가 단순 비교보다 적어야 함
    rng_h = np.random.default_rng(11)
    osc = pd.Series(100 + np.cumsum(rng_h.normal(0, 0.8, 600)))
    s200_o = osc.rolling(200).mean()
    naive = (osc < s200_o).iloc[200:].astype(int).diff().abs().sum()
    flips = 0; prev = None
    for k in range(200, len(osc)):
        st_, _ = _hysteresis_risk_off(osc.iloc[:k + 1], True, 0.01, 2, 0.05)
        if prev is not None and st_ != prev: flips += 1
        prev = st_
    assert flips < naive, f"히스테리시스 휩소 감소 실패(naive={naive}, hyst={flips})"

    # [예외로깅 검증] 삼킨 예외가 집계되고, 파일 손상 시 조용히 사라지지 않음
    SWALLOWED.clear()
    _swallow("fetch_kr_history", ValueError("boom"), "005930")
    _swallow("fetch_kr_history", ValueError("boom2"), "000660")
    assert SWALLOWED["fetch_kr_history:ValueError"] == 2, "예외 집계 오류"
    assert "fetch_kr_history:ValueError×2" in swallowed_summary(), "예외 요약 오류"
    SWALLOWED.clear()
    import tempfile as _tf
    with _tf.TemporaryDirectory() as _td:
        _p = Path(_td) / "bad_overrides.csv"
        _p.write_bytes(b"\xff\xfe\x00bad,\x00")
        _cfg_t = deep_merge(cfg, {"universe": {"sector_overrides_csv": str(_p)}})
        _old_root = ROOT
        globals()["ROOT"] = Path("/")
        try:
            assert load_sector_overrides(_cfg_t) == {}
        finally:
            globals()["ROOT"] = _old_root
    assert any(k.startswith("load_sector_overrides:") for k in SWALLOWED), "오버라이드 파일 손상이 로깅되지 않음"
    SWALLOWED.clear()

    # [시장별 노출한도 검증] KR 공격/US 방어가 섞여도 각자 레짐으로 한도 산출, 계좌 합계는 100% 이내
    mix_reg = {"KOSPI": {"signal_light": "🟢 ATTACK"}, "KOSDAQ": {"signal_light": "🟢 ATTACK"},
               "NASDAQ100": {"signal_light": "🔴 RISK_OFF"}, "SP500": {"signal_light": "🔴 RISK_OFF"}}
    mix_actions = pd.DataFrame(
        [{"ticker": f"K{i}", "market": "KR", "sub_market": "KOSPI", "sector": f"KS{i}", "position_size_pct": 20.0, "raw_stop_dist_pct": -0.05} for i in range(4)]
        + [{"ticker": f"U{i}", "market": "US", "sub_market": "SP500", "sector": f"US{i}", "position_size_pct": 20.0, "raw_stop_dist_pct": -0.05} for i in range(4)]
    )
    mix_alloc, mix_meta = allocate_portfolio_risk_budget(mix_actions, mix_reg, cfg)
    bm = mix_meta["by_market"]
    assert bm["KR"]["exposure_cap"] == 100.0 and bm["US"]["exposure_cap"] == 25.0, "시장별 노출 상한 분리 오류"
    assert bm["US"]["alloc"] <= 25.0 + 0.1 and bm["KR"]["alloc"] > bm["US"]["alloc"], "US 방어 한도가 KR 배분을 막거나 US가 한도 초과"
    assert mix_meta["total_alloc_pct"] <= 100.0 + 0.1, "계좌 합계 노출 100% 초과"
    assert bm["US"]["risk_cap"] == 2.5 and bm["KR"]["risk_cap"] == 5.0, "시장별 리스크 한도 오류"

    # [분배일 재보정 검증] 저변동 지수의 -0.3% 하락(거래량 증가)은 더 이상 고전기준만큼 많이 잡히지 않아야 함
    rng_d = np.random.default_rng(3)
    cnts_new, cnts_classic = [], []
    for _ in range(150):
        lv_ = np.zeros(300)
        for k_ in range(1, 300): lv_[k_] = 0.97 * lv_[k_ - 1] + rng_d.normal(0, 0.08)
        c_ = 100 * np.exp(np.cumsum(rng_d.normal(0.0004, 0.009 * np.exp(lv_))))
        v_ = np.exp(rng_d.normal(0, 0.25, 300))
        h_ = pd.DataFrame({"Close": c_, "Volume": v_, "High": c_, "Low": c_, "Open": c_}, index=pd.bdate_range("2024-01-02", periods=300))
        set_regime_params(cfg)
        cnts_new.append(calc_market_regime(h_, True, "T")["distribution_days_25d"])
        _REGIME_PARAMS["sigma_mult"] = 0.0
        cnts_classic.append(calc_market_regime(h_, True, "T")["distribution_days_25d"])
        set_regime_params(cfg)
    assert np.mean(cnts_new) < np.mean(cnts_classic) - 1.0, "분배일 변동성 보정이 고전 기준보다 엄격하지 않음"
    assert np.mean(np.array(cnts_new) >= _REGIME_PARAMS["collapse_min"]) < 0.10, "내부붕괴 임계의 우연 발동확률이 너무 높음"

    # [포워드 검증 보정 테스트] 쿨다운 중복제거 + 벤치마크 초과수익 + 시간순 처리
    import tempfile as _tf2
    with _tf2.TemporaryDirectory() as _td2:
        _root = Path(_td2)
        _g = globals()
        _bk = (_g["REPORT_DIR"], _g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"])
        _g["REPORT_DIR"], _g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"] = _root / "reports", _root / "kr", _root / "us"
        try:
            for p_ in (_g["REPORT_DIR"], _g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"]): p_.mkdir(parents=True)
            idx_f = pd.bdate_range("2026-01-02", periods=80)
            px = pd.DataFrame({"Open": np.linspace(100, 130, 80), "High": np.linspace(101, 131, 80), "Low": np.linspace(99, 129, 80),
                               "Close": np.linspace(100, 130, 80), "Volume": 1e6}, index=idx_f)
            bx = pd.DataFrame({"Open": np.linspace(100, 110, 80), "High": np.linspace(100, 110, 80), "Low": np.linspace(100, 110, 80),
                               "Close": np.linspace(100, 110, 80), "Volume": 1e6}, index=idx_f)
            px.assign(_session_confirmed=True, _session_finality_version=2).to_pickle(_g["KR_HISTORY_CACHE_DIR"] / "005930.pkl")
            bx.assign(_session_confirmed=True, _session_finality_version=2).to_pickle(_g["KR_HISTORY_CACHE_DIR"] / "idx_1001.pkl")
            for k_, d_ in enumerate(["2026-01-12", "2026-01-13", "2026-01-14", "2026-02-02"]):
                dd_ = _g["REPORT_DIR"] / d_; dd_.mkdir()
                pd.DataFrame([{"ticker": "005930", "market": "KR", "sub_market": "KOSPI", "setup_stage": "🔥 피벗돌파", "as_of": d_}]).to_csv(dd_ / "all_candidates.csv", index=False)
            fv = compute_forward_validation("2026-04-01", cfg)
            row = fv[fv["setup_stage"] == "🔥 피벗돌파"].iloc[0]
            assert int(row["sample_cnt"]) == 2, f"쿨다운 중복제거 오류(sample={row['sample_cnt']})"  # 01-12, 02-02만 집계
            assert row["avg_ex_10d"] > 0 and pd.notna(row["avg_ex_10d"]), "벤치마크 초과수익 계산 오류"
            assert (fv["setup_stage"] == "📊 전체(기준선)").any(), "기준선 행 누락"
        finally:
            _g["REPORT_DIR"], _g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"] = _bk

    # [배당락 이음새 보정 / 병렬 선수집 검증]
    _idx_d = pd.bdate_range("2026-03-02", periods=40)
    _old = pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1e6}, index=_idx_d)
    _new_div = _old.iloc[-8:] * 0.995          # 균일 -0.5% (배당 조정)
    _new_div["Volume"] = 1e6
    _res = rescale_cached_for_dividend(_old, _new_div)
    assert _res is not None and abs(float(_res["Close"].iloc[0]) - 99.5) < 1e-6 and float(_res["Volume"].iloc[0]) == 1e6, "배당락 균일 보정 실패"
    _new_split = _old.iloc[-8:] * 0.5
    assert rescale_cached_for_dividend(_old, _new_split) is None, "분할(큰 괴리)을 배당으로 오인"
    _new_nonuni = _old.iloc[-8:].copy(); _new_nonuni["Close"] = np.r_[np.full(4, 99.5), np.full(4, 100.0)]
    assert rescale_cached_for_dividend(_old, _new_nonuni) is None, "비균일 괴리를 균일 보정으로 오인"

    _calls: list[str] = []
    def _fake_fetch(t: str) -> pd.DataFrame:
        time.sleep(0.05)
        _calls.append(t)
        if t == "BAD":
            raise RuntimeError("fail")
        return pd.DataFrame({"Close": [1.0]}, index=pd.to_datetime(["2026-01-02"]))
    _tks = [f"{i:06d}" for i in range(24)] + ["BAD"]
    _t0 = time.time()
    SWALLOWED.clear()
    _pf = prefetch_histories(_tks, _fake_fetch, workers=5, pause=0.0)
    _elapsed = time.time() - _t0
    assert len(_pf) == 24 and "BAD" not in _pf, "선수집 결과/실패 격리 오류"
    assert _elapsed < 25 * 0.05 * 0.6, f"병렬 선수집이 순차 대비 빨라지지 않음({_elapsed:.2f}s)"
    assert any(k.startswith("prefetch_histories:") for k in SWALLOWED), "선수집 실패가 기록되지 않음"
    SWALLOWED.clear()
    assert prefetch_histories(_tks, _fake_fetch, workers=1) == {}, "workers=1 은 순차 폴백(빈 dict)이어야 함"

    # [게시 계층 검증] 계약 파일 생성/검증, 차트, 원장 쿨다운·멱등, 검증 실패 시 latest 보존
    import tempfile as _tf3
    with _tf3.TemporaryDirectory() as _td3:
        _r = Path(_td3)
        _g = globals()
        _bk3 = (_g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"])
        _g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"] = _r / "kr", _r / "us"
        try:
            (_r / "kr").mkdir(); (_r / "us").mkdir()
            _ix = pd.bdate_range("2025-06-02", periods=300)
            for _p, _base in (((_r / "kr" / "005930.pkl"), 70000.0), ((_r / "us" / "NVDA.pkl"), 120.0)):
                _c = np.linspace(_base, _base * 1.5, 300)
                pd.DataFrame({"Open": _c, "High": _c * 1.01, "Low": _c * 0.99, "Close": _c, "Volume": 1e6}, index=_ix).assign(_session_confirmed=True, _session_finality_version=2).to_pickle(_p)
            _asof = _ix[-1].strftime("%Y-%m-%d")
            def _row(mkt, sub, tkr, act):
                return {"market": mkt, "sub_market": sub, "ticker": tkr, "name": tkr, "as_of": _asof, "sector": "S1", "macro_theme": "M1",
                        "tier": "A", "setup_stage": "🔥 피벗돌파", "status_tag": "", "is_actionable": act, "in_buy_zone": act, "is_breakout": act,
                        "is_true_vcp": False, "strict_minervini": True, "is_leading_sector": True, "rs_score": 90.0, "rs_change_4w": 3.0,
                        "topdown_score": 85.0, "sector_power": 70.0, "last_close": 100.0, "pivot_price": 99.0, "dist_to_pivot_pct": 0.01,
                        "stop_price": 94.0, "stop_dist_pct": -0.06, "raw_stop_dist_pct": -0.06, "position_size_pct": 15.0, "volume_ratio": 1.8,
                        "distance_from_high_pct": -0.01, "exit_signal": "✅정상"}
            _tr = {"KOSPI": pd.DataFrame([_row("KR", "KOSPI", "005930", True)]), "SP500": pd.DataFrame([_row("US", "SP500", "NVDA", True), _row("US", "SP500", "AMD", False)])}
            _all = {k: v.copy() for k, v in _tr.items()}
            _regs = {"KOSPI": {"signal_light": "🟢 ATTACK", "regime": "RISK_ON", "benchmark": "KOSPI", "as_of": _asof},
                     "SP500": {"signal_light": "🟡 CAUTION", "regime": "RISK_ON", "benchmark": "SPY", "as_of": _asof}}
            _sub = pd.DataFrame([{"macro_theme": "M1", "sector": "S1", "sector_power": 70.0, "median_rs": 80.0, "rs_delta_4w": 2.0, "passed_cnt": 2, "rotation_phase": "주도"}])
            _meta = {"kr_health": {"kr_failed": 0}, "us_health": {"us_failed": 1}, "kr_sub_df": _sub, "kr_macro_df": _sub[["macro_theme", "sector_power"]], "us_sub_df": _sub, "us_macro_df": _sub[["macro_theme", "sector_power"]]}
            _pub = _r / "published"
            _vdf = pd.DataFrame([{"setup_stage": "x", "sample_cnt": 3}])
            _out = publish_artifacts("2026-10-30", _tr, _all, _regs, _meta, pd.DataFrame(), _vdf, cfg, base_dir=_pub)
            assert _out is not None and validate_published_dir(_out) == [], "게시/검증 실패"
            _man = json.loads((_out / "manifest.json").read_text(encoding="utf-8"))
            assert _man["schema_version"] == SCHEMA_VERSION and set(_man["as_of"]) == {"KOSPI", "SP500"}, "manifest 내용 오류"
            _wl = pd.read_csv(_out / "watchlist.csv")
            assert len(_wl) == 2 and {"portfolio_alloc_pct", "portfolio_market"} - set(_wl.columns) == {"portfolio_market"}, "watchlist 컬럼 오류"
            _cd = pd.read_csv(_out / "candidates.csv")
            assert len(_cd) == 3 and int(_cd["selected"].sum()) == 3, "candidates 오류"
            _ch = json.loads(gzip.decompress((_out / CHARTS_FILE).read_bytes()))
            _expected_bars = min(int(cfg.get("publish", {}).get("chart_bars", 260)), len(_ix))
            assert set(_ch) == {"KR:005930", "US:NVDA"} and len(_ch["KR:005930"]["d"]) == _expected_bars and _ch["KR:005930"]["d"][-1] <= _asof, "차트 기간/기준일 오류"
            assert (_pub / "daily" / "2026-10-30" / "manifest.json").exists(), "daily 스냅샷 누락"
            _led = pd.read_csv(_pub / "history" / "signals.csv")
            assert len(_led) == 2, "신호 원장 기록 오류"
            # 같은 날 재게시: 원장·레짐 이력 멱등
            publish_artifacts("2026-10-30", _tr, _all, _regs, _meta, pd.DataFrame(), _vdf, cfg, base_dir=_pub)
            assert len(pd.read_csv(_pub / "history" / "signals.csv")) == 2 and len(pd.read_csv(_pub / "history" / "regime.csv")) == 2, "재게시 멱등성 오류"
            # 쿨다운: +5일은 신규 기록 없음, +20일은 기록
            publish_artifacts("2026-11-04", _tr, _all, _regs, _meta, pd.DataFrame(), _vdf, cfg, base_dir=_pub)
            assert len(pd.read_csv(_pub / "history" / "signals.csv")) == 2, "쿨다운 위반"
            publish_artifacts("2026-11-19", _tr, _all, _regs, _meta, pd.DataFrame(), _vdf, cfg, base_dir=_pub)
            assert len(pd.read_csv(_pub / "history" / "signals.csv")) == 4, "쿨다운 후 재기록 실패"
            # 검증 실패 시 기존 latest 보존
            _rid = json.loads((_pub / "latest" / "manifest.json").read_text(encoding="utf-8"))["run_id"]
            _orig_v = _g["validate_published_dir"]
            _g["validate_published_dir"] = lambda p: ["강제 실패"]
            try:
                assert publish_artifacts("2026-11-20", _tr, _all, _regs, _meta, pd.DataFrame(), _vdf, cfg, base_dir=_pub) is None
            finally:
                _g["validate_published_dir"] = _orig_v
            assert json.loads((_pub / "latest" / "manifest.json").read_text(encoding="utf-8"))["run_id"] == _rid, "검증 실패 시 latest가 바뀜"
            assert not [p for p in _pub.iterdir() if p.name.startswith(".staging") or p.name.startswith(".old")], "임시 폴더 잔존"
            # 내용이 같으면 차트 바이트도 같아야 함(git diff 최소화)
            assert _gzip_json({"a": [1, 2]}) == _gzip_json({"a": [1, 2]}), "gzip 비결정적"
        finally:
            _g["KR_HISTORY_CACHE_DIR"], _g["US_HISTORY_CACHE_DIR"] = _bk3

    # [v22 검증] HTML 정제 / 후보 이력 / 신호 결과(손절·R·초과수익) / 과거 행 자가치유
    _h = pd.DataFrame({"a": ['<span style="color:#059669;"><b>🚀 주도</b></span>', "plain <b>x</b>&nbsp;y", "RS<70 and >50"], "n": [1, 2, 3]})
    _hc = _strip_html_df(_h)
    assert _hc["a"].tolist() == ["🚀 주도", "plain x y", "RS<70 and >50"] and _hc["n"].tolist() == [1, 2, 3], "HTML 정제 오류"
    import tempfile as _tf4
    with _tf4.TemporaryDirectory() as _td4:
        _r4 = Path(_td4); _g4 = globals()
        _bk4 = (_g4["KR_HISTORY_CACHE_DIR"], _g4["US_HISTORY_CACHE_DIR"])
        _g4["KR_HISTORY_CACHE_DIR"], _g4["US_HISTORY_CACHE_DIR"] = _r4 / "kr", _r4 / "us"
        try:
            (_r4 / "kr").mkdir(); (_r4 / "us").mkdir(); (_r4 / "pub" / "history").mkdir(parents=True)
            _ix4 = pd.bdate_range("2026-09-01", periods=60)
            up = np.linspace(100, 130, 60); dn = np.r_[np.linspace(100, 100, 25), np.linspace(100, 80, 35)]
            for _tk, _c in (("AAA", up), ("BBB", dn)):
                tag_raw_price_quality(pd.DataFrame({"Open": _c, "High": _c * 1.01, "Low": _c * 0.99, "Close": _c, "Volume": 1e6}, index=_ix4)).assign(_session_confirmed=True, _session_finality_version=2).to_pickle(_r4 / "us" / f"{_tk}.pkl")
            tag_raw_price_quality(pd.DataFrame({"Open": np.linspace(100, 105, 60), "High": np.linspace(100, 105, 60), "Low": np.linspace(100, 105, 60),
                          "Close": np.linspace(100, 105, 60), "Volume": 1e6}, index=_ix4)).assign(_session_confirmed=True, _session_finality_version=2).to_pickle(_r4 / "us" / "SPY.pkl")
            _asof4 = _ix4[5].strftime("%Y-%m-%d")
            _sig4 = pd.DataFrame([
                {"first_seen": _asof4, "as_of": _asof4, "market": "US", "sub_market": "SP500", "ticker": "AAA", "name": "A", "sector": "S", "setup_stage": "x", "tier": "A", "entry_ref": 100, "stop_price": 95, "rs_score": 90, "topdown_score": 80},
                {"first_seen": _ix4[24].strftime("%Y-%m-%d"), "as_of": _ix4[24].strftime("%Y-%m-%d"), "market": "US", "sub_market": "SP500", "ticker": "BBB", "name": "B", "sector": "S", "setup_stage": "y", "tier": "B", "entry_ref": 100, "stop_price": 95, "rs_score": 80, "topdown_score": 70},
                {"first_seen": _ix4[-2].strftime("%Y-%m-%d"), "as_of": _ix4[-1].strftime("%Y-%m-%d"), "market": "US", "sub_market": "SP500", "ticker": "AAA", "name": "A", "sector": "S", "setup_stage": "z", "tier": "A", "entry_ref": 130, "stop_price": 120, "rs_score": 90, "topdown_score": 80},
            ])
            _sig4.to_csv(_r4 / "pub" / "history" / "signals.csv", index=False)
            assert update_signal_outcomes(_r4 / "pub", cfg) == 3, "결과 행 수 오류"
            _oc = pd.read_csv(_r4 / "pub" / "history" / "signal_outcomes.csv")
            _a, _b, _w = _oc.iloc[0], _oc.iloc[1], _oc.iloc[2]
            assert _a["status"] == "종료" and _a["ret_10d"] > 0 and bool(_a["stop_hit"]) is False and _a["ex_10d"] > 0 and _a["r_10d"] > 0, "상승 신호 결과 오류"
            assert bool(_b["stop_hit"]) is True and _b["mae_pct"] < -5 and _b["ret_20d"] < 0, "손절 터치 신호 결과 오류"
            assert _w["status"] == "대기" and pd.isna(_w["ret_5d"]), "신호 직후(대기) 처리 오류"
            _cand4 = pd.DataFrame([{"market": "US", "sub_market": "SP500", "ticker": "AAA", "name": "A", "as_of": _asof4, "sector": "S", "macro_theme": "M", "tier": "A", "setup_stage": "x",
                                    "is_actionable": True, "selected": True, "rs_score": 90.0, "last_close": 100.0},
                                   {"market": "US", "sub_market": "SP500", "ticker": "ZZZ", "name": "Z", "as_of": _asof4, "sector": "S", "macro_theme": "M", "tier": "C", "setup_stage": "q",
                                    "is_actionable": False, "selected": False, "rs_score": 71.0, "last_close": 50.0}])
            assert update_candidate_history(_r4 / "pub", _cand4, "2026-10-30") == 1, "후보 이력 필터 오류(선정/매수검토만 저장)"
            update_candidate_history(_r4 / "pub", _cand4, "2026-10-30")
            assert len(pd.read_csv(_r4 / "pub" / "history" / "candidates_daily.csv")) == 1, "후보 이력 멱등성 오류"
            # 과거에 HTML이 섞여 저장된 이력도 upsert 때 정리됨
            pd.DataFrame([{"date": "2026-10-01", "market": "KR", "level": "sector", "macro_theme": "M", "sector": "S", "rotation_phase": "<span style='color:red'>x</span>"}]).to_csv(_r4 / "pub" / "history" / "sector_power.csv", index=False)
            _upsert_csv(_r4 / "pub" / "history" / "sector_power.csv", pd.DataFrame([{"date": "2026-10-02", "market": "KR", "level": "sector", "macro_theme": "M", "sector": "S", "rotation_phase": "y"}]), ["date", "market", "level", "macro_theme", "sector"])
            assert "<" not in pd.read_csv(_r4 / "pub" / "history" / "sector_power.csv")["rotation_phase"].str.cat(), "과거 행 HTML 자가치유 실패"
        finally:
            _g4["KR_HISTORY_CACHE_DIR"], _g4["US_HISTORY_CACHE_DIR"] = _bk4

    # [검증 7] 설정 오버라이드 변경 시 캐시 해시 무효화 검증 (P0-3.3)
    h1 = get_config_version_hash(cfg, {})
    h2 = get_config_version_hash(cfg, {"KR:005930": ("커스텀테마", "커스텀섹터")})
    assert h1 != h2, "설정 오버라이드 변경 시 캐시 해시 무효화 실패"

    # [검증 8] 오래된 가격 데이터(Stale Data >= 2거래일) 매수 적격 차단 및 글로벌 테마 통합 한도 검증
    stale_synth = synth.iloc[:-3].copy()
    stale_feat = compute_features(stale_synth, bench, cfg)
    assert stale_feat is not None and stale_feat["is_stale_price"] is True and stale_feat["is_actionable"] is False, "오래된 가격 데이터 매수 차단 실패"
    ok_st, _, rsn_st = score_candidate({**stale_feat, "rs_score": 95.0}, cfg, "KR")
    assert ok_st is False and "가격데이터지연" in rsn_st, "오래된 가격 데이터 후보 탈락 처리 실패"

    theme_actions = pd.DataFrame([
        {"ticker": "K1", "market": "KR", "sub_market": "KOSPI", "macro_theme": "AI·반도체", "sector": "HBM", "position_size_pct": 25.0, "raw_stop_dist_pct": -0.04},
        {"ticker": "U1", "market": "US", "sub_market": "NASDAQ100", "macro_theme": "AI·반도체", "sector": "AI칩", "position_size_pct": 25.0, "raw_stop_dist_pct": -0.04},
    ])
    all_atk_reg = {"KOSPI": {"signal_light": "🟢 ATTACK"}, "NASDAQ100": {"signal_light": "🟢 ATTACK"}}
    th_alloc, _ = allocate_portfolio_risk_budget(theme_actions, all_atk_reg, cfg)
    assert th_alloc["portfolio_alloc_pct"].sum() <= float(cfg["screen"].get("max_global_theme_alloc_pct", 45.0)) + 0.1, "한·미 통합 매크로 테마 집중도 한도 초과"

    LOG.info("✅ 내장 단위 테스트 통과: 누적 이력·HTML 정제·게시 계층·KR 병렬수집, VCP/돌파, 벤치마크 결측 차단, 2-Pass 레짐 사이징, 포트폴리오 한도 검증 완료")
    return 0

def main() -> int:
    p = argparse.ArgumentParser(description="Trend Leader Daily Watchlist Screener (4-Track v14.5)")
    p.add_argument("--market", choices=["KR", "US", "ALL"], default="ALL")
    p.add_argument("--refresh-cache", action="store_true")
    p.add_argument("--rs-mode", choices=["STANDARD", "FAST"], default="STANDARD")
    p.add_argument("--self-test", action="store_true", help="합성 데이터 기반 단위 테스트 실행")
    args = p.parse_args()

    cfg = load_config().copy()
    if args.refresh_cache: cfg["runtime"]["refresh_cache"] = True
    if args.rs_mode: cfg["screen"]["rs_mode"] = args.rs_mode
    setup_logging(cfg)
    set_regime_params(cfg)

    if args.self_test: return run_self_tests(cfg)

    overrides = load_sector_overrides(cfg)
    overrides = _configure_sector_review(cfg, overrides, datetime.now(timezone.utc).isoformat())
    today_str = datetime.now(KST).strftime("%Y-%m-%d")
    hist_track, prev_selected_map = load_historical_tracking(today_str)

    track_results: dict[str, pd.DataFrame] = {}
    track_all_passed: dict[str, pd.DataFrame] = {}
    regimes: dict[str, dict] = {}
    kr_merged_all = pd.DataFrame()
    us_merged_all = pd.DataFrame()
    extra_meta: dict[str, Any] = {"kr_health": {}, "us_health": {}, "kr_sec_totals": {}, "us_sec_totals": {}, "kr_sec_medians": {}, "us_sec_medians": {}, "sector_review_inputs": {}}
    combined_fail_map: dict[str, dict[str, Any]] = {}
    turn_frames: list[pd.DataFrame] = []
    success_cnt = 0
    run_cutoff = datetime.now(timezone.utc)

    if args.market in ("KR", "ALL"):
        try:
            LOG.info("[KR] KOSPI(1조+) / KOSDAQ(4,000억+) 트랙 스크리닝 시작")
            kr_res, kr_all_map, kr_merged_all, kr_reg, kr_meta = run_kr_tracks(cfg, overrides, hist_track, evaluated_at=run_cutoff)
            track_results.update(kr_res)
            track_all_passed.update(kr_all_map)
            regimes.update(kr_reg)
            extra_meta["kr_health"] = kr_meta["health"]
            extra_meta["sector_review_inputs"]["KR"] = kr_meta.get("sector_review_input", {})
            extra_meta["kr_sec_totals"] = kr_meta["sec_totals"]
            extra_meta["kr_sec_medians"] = kr_meta["sec_medians"]
            extra_meta["kr_macro_df"] = kr_meta["macro_df"]
            extra_meta["kr_sub_df"] = kr_meta["sub_df"]
            combined_fail_map.update(kr_meta["fail_map"])
            if not kr_meta["turnaround"].empty: turn_frames.append(kr_meta["turnaround"])
            success_cnt += 1
        except Exception as exc:
            LOG.error("[KR] 시장 스크리닝 오류 발생 (격리 처리됨): %s", exc, exc_info=True)

    if args.market in ("US", "ALL"):
        try:
            LOG.info("[US] NASDAQ 100($15B+) / S&P 500($10B+) 트랙 스크리닝 시작")
            us_res, us_all_map, us_merged_all, us_reg, us_meta = run_us_tracks(cfg, overrides, hist_track, evaluated_at=run_cutoff)
            track_results.update(us_res)
            track_all_passed.update(us_all_map)
            regimes.update(us_reg)
            extra_meta["us_health"] = us_meta["health"]
            extra_meta["sector_review_inputs"]["US"] = us_meta.get("sector_review_input", {})
            extra_meta["us_sec_totals"] = us_meta["sec_totals"]
            extra_meta["us_sec_medians"] = us_meta["sec_medians"]
            extra_meta["us_macro_df"] = us_meta["macro_df"]
            extra_meta["us_sub_df"] = us_meta["sub_df"]
            combined_fail_map.update(us_meta["fail_map"])
            if not us_meta["turnaround"].empty: turn_frames.append(us_meta["turnaround"])
            success_cnt += 1
        except Exception as exc:
            LOG.error("[US] 시장 스크리닝 오류 발생 (격리 처리됨): %s", exc, exc_info=True)

    required_success = 2 if args.market == "ALL" else 1
    if success_cnt < required_success:
        LOG.error("요청한 시장 데이터가 모두 수집되지 않아 최신 게시 데이터를 갱신하지 않습니다. (성공 %d/%d)", success_cnt, required_success)
        return 1

    if SWALLOWED:
        LOG.warning("[진단] %s", swallowed_summary(12))
    extra_meta["turnaround_all"] = pd.concat(turn_frames, ignore_index=True) if turn_frames else pd.DataFrame()
    dropped_df = compute_dropped_candidates(prev_selected_map, track_all_passed, track_results, combined_fail_map)
    val_df = compute_forward_validation(today_str, cfg)

    rep_path = save_reports(track_results, track_all_passed, kr_merged_all, us_merged_all, regimes, extra_meta, dropped_df, val_df, cfg)
    LOG.info("리포트 및 HTML 뷰어 생성 완료: %s", rep_path)
    # 단일 시장 실행 결과로 KR+US 통합 latest를 덮어쓰지 않는다.
    if args.market != "ALL":
        LOG.warning("[게시 생략] --market %s 단독 실행은 통합 latest(KR+US)를 덮어쓰지 않습니다. 전체 게시에는 --market ALL을 사용하세요.", args.market)
        return 0
    if cfg.get("publish", {}).get("enabled", True):
        published = publish_artifacts(today_str, track_results, track_all_passed, regimes, extra_meta, dropped_df, val_df, cfg)
        if published is None:
            LOG.error("[게시 실패] 기존 latest는 유지했으며, 작업 실패로 종료합니다.")
            return 1
    else:
        LOG.info("[게시 비활성] 리포트만 생성했습니다.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

