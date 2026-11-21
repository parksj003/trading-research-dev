"""
Trend Leader Watchlist - Streamlit 앱 v6.3 (P0·P1·P2 전술·성능·정합성 업그레이드 완결판)
- 5대 워크플로 탭 · 트랙 연동 3단 국면 전술 스위치 · 멀티플 차트 그리드 · GARP 스캐너
- 핵심 보완 사항:
  1. ATTACK 테이블 분리: '즉시 배분 적격' / '와이드 스톱 관찰(8~12%)' / '바이존이나 제외(제외 사유 명시)'
  2. 보유 비중(KR/US) 입력 연동: 남은 한도 = 레짐 한도 - 보유 비중 기반 신규 가상 배분 스케일링
  3. 50일 돌파 거래량 배수(vol_ratio_50d) 및 베이스 피벗/20일 고가 분리 표시, 툴팁 정합성 교정
  4. 전체(KR+US) 조회 시 RS 옆 소속 트랙 고정 병기 및 탑다운 점수 기본 정렬
  5. @st.fragment 및 캔들 JSON 캐싱을 통한 멀티 차트 그리드 렌더링 속도 최적화
  6. 검증 탭 MAE 경로상 저점 안내 문구 명시
"""
from __future__ import annotations

import gzip
import hashlib
import html
import io
import json
import math
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
import portfolio_view
from base_count_view import load_base_counts, attach_base_counts, observation_filters, render_base_panel, add_base_overlay
from sector_review_view import load_sector_review, render_sector_review
from manual_watchlist_view import PAGE_LABEL as MANUAL_PAGE, render_manual_watchlist
from manual_snapshot_source import load_published_eod
from ohlcv_chart import add_price_traces
from trend_tracking_view import load_tracking, render_tracking
from exit_experiment_view import load_exit_publication, render_exit_experiments
from actionable_view import render_actionable_changes, active_context
from rotation_markers import rotation_marker_metrics, calculate_rotation_marker_sizes, sample_outlines
from rotation_trails import PALETTES, add_rotation_trails, effective_trail_mode
from stock_table_links import stock_dataframe
from gpt_export import ANALYSIS_PROMPT, build_analysis_export, stock_options, snapshot_export_errors
from gpt_prompts import MODE_DETAILS, build_prompt
from plotly.subplots import make_subplots

# ════════════════════════════════════════════════════════════════════
# 0. 설정 · 상수 · 툴팁 사전
# ════════════════════════════════════════════════════════════════════
KST = timezone(timedelta(hours=9))
SUPPORTED_SCHEMA_MAJOR = "1"
STALE_DAYS = 4
MANIFEST_TTL_SEC = 300
MIN_SAMPLE = 20
TRACKS = ("KOSPI", "KOSDAQ", "NASDAQ100", "SP500")
CHART_PERIODS = (20, 45, 80, 130, 260)
HTML_TAG_RE = re.compile(r"</?(?:span|b|i|br|small|div|font|sub|sup|code|a|details|summary)\b[^>]*>", re.I)

PALETTE = {
    "green": "#059669",
    "green_soft": "#10B981",
    "green_bg": "#D1FAE5",
    "red": "#E11D48",
    "red_bg": "#FFE4E6",
    "amber": "#D97706",
    "amber_bg": "#FEF3C7",
    "blue": "#2563EB",
    "blue_soft": "#60A5FA",
    "indigo": "#4F46E5",
    "purple": "#7C3AED",
    "slate": "#64748B",
    "slate_light": "#E2E8F0",
    "dark": "#0F172A",
}

SOFT_POWER_SCALE = [
    [0.0, "#1D4ED8"],
    [0.30, "#60A5FA"],
    [0.55, "#FDE047"],
    [0.80, "#FB923C"],
    [1.0, "#E11D48"],
]

TOOLTIPS = {
    "name": "종목의 한글/영문 공식 명칭입니다. 클릭 시 트레이딩뷰 차트로 연결됩니다.",
    "ticker": "KRX 표준 티커 또는 글로벌 주식 심볼 코드입니다.",
    "sub_market": "종목이 속한 하위 소속 트랙(KOSPI, KOSDAQ, NASDAQ100, SP500)입니다.",
    "sector": "기업이 영위하는 핵심 실적 사업군 및 밸류체인 세부 섹터입니다.",
    "macro_theme": "자금이 쏠리는 대형 산업 테마 그룹입니다.",
    "setup_stage": "프로젝트의 가격·거래량 규칙으로 분류한 기준일의 셋업입니다. 최초 신호 이벤트나 정형 패턴 확정을 뜻하지 않습니다.",
    "signal_badges": "블루닷(선행신고가), 조정장 방어력, 실적발표 D-Day 임박 여부 알파 시그널입니다.",
    "tier": "미너비니 트렌드템플레이트 충족 여부 (Tier A: 정배열 정석 / Tier B: 200일선 상회)",
    "rs_score": "종목 RS: 소속 트랙의 계산 모집단에서 벤치마크 대비 상대수익률을 1~99 백분위로 환산한 점수입니다. 섹터 중앙 RS는 해당 시장의 필터링된 계산 모집단에서 순수 수익률을 순위화합니다.",
    "rs_with_track": "전체/복수 트랙 조회 시 모집단 착시를 막기 위해 RS 점수와 소속 트랙을 함께 표시합니다.",
    "median_rs": "섹터 RS: 해당 시장(KR 또는 US)의 필터링된 계산 모집단에서 순수 수익률 백분위의 섹터 중앙값입니다. 전체 상장종목을 뜻하지 않습니다.",
    "rs_change_4w": "현재 계산 모집단에서 현재와 20거래일 전 원시 RS를 각각 백분위화한 차이입니다. 섹터는 종목별 차이의 중앙값이며, 과거 저장 당시 RS와의 차이가 아닙니다.",
    "topdown_score": "종목 자체 RS(75%)와 소속 섹터 파워(25%)를 결합한 탑다운 총괄 점수입니다.",
    "sector_power": "해당 세부섹터 소속 종목들의 중앙 RS, 상위 종목 강도, 50일선 상회율을 종합한 0~100 점수입니다.",
    "last_close": "가장 최근 마감된 정규장 종가입니다.",
    "pivot_price": "최근 15~40일 베이스 구간의 저항 고점(베이스 피벗) 가격입니다.",
    "high_20d": "직전 20거래일 최고가로, 단기 연속 상승 및 눌림목 참조 기준가입니다.",
    "dist_to_pivot_pct": "베이스 피벗 가격 대비 현재 주가의 괴리율입니다. 초록색 바이존(-2.5%~+5%) 구간이 안전 진입 타점입니다.",
    "stop_price": "ATR(14) 및 20일 이동평균선 지지 기반으로 산출된 권장 손절 기준가입니다.",
    "stop_dist_pct": "진입가(또는 현재가) 대비 권장 손절가까지의 낙폭(%)입니다.",
    "position_size_pct": "계좌 리스크 예산(ATTACK 1% / CAUTION 0.75% / RISK_OFF 0.5%)과 손절폭을 나누어 산출한 1개 종목당 권장 매수 비중(%)입니다.",
    "portfolio_alloc_pct": "보유 비중을 차감한 잔여 시장 한도 및 세부섹터 한도를 고려하여 배분된 신규 비중(%)입니다.",
    "volume_ratio": "당일 거래량이 직전 20일 평균 거래량의 몇 배인지 나타내는 단기 거래량 지표입니다.",
    "vol_ratio_50d": "당일 거래량이 직전 50일 평균 거래량의 몇 배인지 나타내며, 피벗 돌파 판정(1.5배 이상 필수)의 기준 지표입니다.",
    "vol_dry_up_ratio": "최근 5일 평균 거래량이 50일 평균 대비 얼마나 말라붙었는지(Dry-up, <=0.75) 나타내는 VCP 핵심 지표입니다.",
    "range_10d_pct": "최근 10거래일 동안의 고저가 변동 폭(%)으로, 베이스 수축 수준을 가늠합니다.",
    "down_day_excess_pct": "지수 하락일에 시장 벤치마크 대비 평균적으로 몇 %p 초과 상승했는지 보여주는 조정장 방어력 지표입니다.",
    "distance_from_high_pct": "52주 최고가 대비 현재 주가의 낙폭(%)입니다. 0%에 가까울수록 신고가 임박 종목입니다.",
    "dist_sma50_pct": "50일 이동평균선 대비 현재 주가의 이격도(%)입니다.",
    "dist_days_25d": "최근 25거래일 동안 기관 매물 출회를 뜻하는 거래량 동반 하락일(분배일) 누적 횟수입니다 (5일 이상 주의).",
    "exit_signal": "10일선/20일선/50일선 이탈 또는 분배일 과다로 인해 경고를 보내는 청산 모니터링 상태입니다.",
    "exclusion_reason": "게시 필드로 재구성한 참고 설명입니다. 상세 제외 사유를 확인할 수 없는 경우 별도로 표시하며, 섹터쿼터 등 미확인 원인을 단정하지 않습니다.",
    "streak": "상위 20선 또는 매수검토 리스트에 연속으로 체류한 영업일수입니다.",
    "peg": "PER을 EPS 이익성장률로 나눈 값으로, 1.5 이하일 때 이익성장 대비 저평가(GARP)로 평가합니다.",
    "eps_growth_pct": "전년 동기 대비 EPS 순이익 성장률(%)입니다.",
    "fund_badge": "PEG 배수와 흑자전환(TURNAROUND) 여부를 결합한 실적 밸류에이션 뱃지입니다.",
}

LABELS = {
    "bc_label": "베이스 순번", "bc_display": "베이스 순번", "bc_status_label": "베이스 상태", "bc_quality_label": "베이스 관측 품질",
    "name": "종목명",
    "ticker": "티커",
    "market": "시장",
    "sub_market": "소속 트랙",
    "sector": "세부 섹터",
    "macro_theme": "매크로 테마",
    "setup_stage": "셋업 단계",
    "signal_badges": "주요 신호",
    "tier": "추세 등급",
    "exit_signal": "청산신호",
    "exclusion_reason": "제외 사유",
    "fund_badge": "실적 평가",
    "rs_score": "RS",
    "rs_with_track": "RS (트랙)",
    "rs_change_4w": "RS 4주Δ",
    "topdown_score": "탑다운점수",
    "sector_power": "섹터파워",
    "last_close": "현재가",
    "pivot_price": "베이스피벗",
    "high_20d": "20일고가",
    "dist_to_pivot_pct": "피벗괴리",
    "stop_price": "손절가",
    "stop_dist_pct": "손절폭",
    "position_size_pct": "권장비중",
    "portfolio_alloc_pct": "포트배분",
    "portfolio_alloc_note": "배분 메모",
    "vol_ratio_50d": "돌파거래량(50일)",
    "volume_ratio": "거래량(20일)",
    "vol_dry_up_ratio": "거래량건조(5/50일)",
    "range_10d_pct": "10일변동폭",
    "down_day_excess_pct": "하락일초과%",
    "distance_from_high_pct": "52주고점괴리",
    "dist_sma50_pct": "50일선이격",
    "dist_days_25d": "25일분배일",
    "streak": "체류일",
    "peg": "PEG",
    "eps_growth_pct": "EPS성장률",
    "rotation_phase": "로테이션 국면",
    "pct_above_50dma": "50일선상회",
    "median_rs": "중앙 RS",
    "rs_delta_4w": "RS 4주Δ",
    "passed_cnt": "통과종목",
    "actionable_cnt": "기술적 적격 수",
    "univ_total": "전체종목",
    "sub_sector_cnt": "하위 섹터 수",
    "top_sub_sector": "최강 하위섹터",
    "top_leader": "대표 대장주",
    "leader_name": "대장주",
    "leader_setup": "대장주 셋업",
}

st.set_page_config(
    page_title="Trend Leader Watchlist",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ════════════════════════════════════════════════════════════════════
# 1. 데이터 소스 (로컬 폴더 / GitHub API)
# ════════════════════════════════════════════════════════════════════
def _secret(name: str, default: str = "") -> str:
    return str(portfolio_view.setting(name, default))


def get_source() -> dict[str, str]:
    d = _secret("DATA_DIR")
    if d:
        return {"mode": "local", "dir": d}
    repo, token = _secret("DATA_REPO"), _secret("GITHUB_TOKEN")
    if repo and token:
        return {
            "mode": "github", "repo": repo, "token": token,
            "root": _secret("DATA_ROOT", "published").strip("/"),
            "branch": _secret("DATA_BRANCH", "main"),
        }
    return {}


def _read_raw(src: dict[str, str], rel: str) -> bytes | None:
    if src["mode"] == "local":
        p = Path(src["dir"]) / rel
        return p.read_bytes() if p.exists() else None
    url = f"https://api.github.com/repos/{src['repo']}/contents/{src['root']}/{rel}"
    headers = {
        "Authorization": f"Bearer {src['token']}",
        "Accept": "application/vnd.github.raw+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    last_error = None
    for attempt in range(3):
        try:
            r = requests.get(url, params={"ref": src["branch"]}, timeout=(5, 25), headers=headers)
            if r.status_code == 404:
                return None
            if r.status_code in (429, 500, 502, 503, 504):
                r.raise_for_status()
            r.raise_for_status()
            return r.content
        except requests.RequestException as exc:
            last_error = exc
            if attempt >= 2:
                break
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"GitHub 파일 조회 실패: {rel} ({last_error})") from last_error


@st.cache_data(ttl=MANIFEST_TTL_SEC, show_spinner=False)
def fetch_manifest(src_key: str, _src: dict[str, str]) -> dict[str, Any] | None:
    raw = _read_raw(_src, "latest/manifest.json")
    return json.loads(raw.decode("utf-8")) if raw else None


@st.cache_data(ttl=MANIFEST_TTL_SEC, show_spinner=False, max_entries=128)
def fetch_file(src_key: str, run_id: str, rel: str, _src: dict[str, str]) -> bytes | None:
    return _read_raw(_src, rel)


# ════════════════════════════════════════════════════════════════════
# 2. 로딩 · 정제 (Bundle)
# ════════════════════════════════════════════════════════════════════
@dataclass
class Bundle:
    man: dict[str, Any]
    summ: dict[str, Any]
    watch: pd.DataFrame = field(default_factory=pd.DataFrame)
    cand: pd.DataFrame = field(default_factory=pd.DataFrame)
    sectors: pd.DataFrame = field(default_factory=pd.DataFrame)
    dropped: pd.DataFrame = field(default_factory=pd.DataFrame)
    val: pd.DataFrame = field(default_factory=pd.DataFrame)
    h_regime: pd.DataFrame = field(default_factory=pd.DataFrame)
    h_sector: pd.DataFrame = field(default_factory=pd.DataFrame)
    h_cand: pd.DataFrame = field(default_factory=pd.DataFrame)
    ledger: pd.DataFrame = field(default_factory=pd.DataFrame)
    outcomes: pd.DataFrame = field(default_factory=pd.DataFrame)
    charts: dict[str, Any] = field(default_factory=dict)
    trend_tracking: pd.DataFrame = field(default_factory=pd.DataFrame)
    trend_daily: pd.DataFrame = field(default_factory=pd.DataFrame)
    trend_source: dict = field(default_factory=dict)
    export_snapshot_verified: bool | None = None
    export_errors: list[str] = field(default_factory=list)
    base_observations: pd.DataFrame = field(default_factory=pd.DataFrame)
    base_source: dict = field(default_factory=lambda: {'status':'absent'})
    load_errors: list[str] = field(default_factory=list)
    core_errors: list[str] = field(default_factory=list)
    exit_experiments: dict = field(default_factory=dict)
    exit_experiment_source: dict = field(default_factory=lambda: {"status":"absent"})
    sector_review: dict = field(default_factory=dict)
    sector_review_source: dict = field(default_factory=lambda: {'status':'absent'})


def tf(s: pd.Series) -> pd.Series:
    truthy = {"true", "1", "1.0", "yes", "y", "t"}
    return s.astype(str).str.strip().str.lower().isin(truthy)


def clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    out = df.copy()
    for c in out.columns:
        if out[c].dtype == object or pd.api.types.is_string_dtype(out[c].dtype):
            out[c] = out[c].map(
                lambda v: HTML_TAG_RE.sub("", v).replace("&nbsp;", " ").strip() if isinstance(v, str) else v
            )
    if "ticker" in out.columns:
        out["ticker"] = out["ticker"].astype(str)
    return out


def normalize_sectors_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "sector_power" in out.columns and "macro_power" in out.columns:
        sp = pd.to_numeric(out["sector_power"], errors="coerce")
        mp = pd.to_numeric(out["macro_power"], errors="coerce")
        out["sector_power"] = sp.fillna(mp)
    if "leader_name" in out.columns and "top_leader" in out.columns:
        ln = out["leader_name"].replace({"None": np.nan, "nan": np.nan, "": np.nan})
        tl = out["top_leader"].replace({"None": np.nan, "nan": np.nan, "": np.nan})
        out["leader_name"] = ln.fillna(tl).fillna("-")
    return out


def _verify_sha256(raw: bytes | None, rel: str, man: dict[str, Any]) -> bool:
    if raw is None or not rel.startswith("latest/"):
        return True
    fname = rel.split("/", 1)[-1]
    meta = (man.get("files") or {}).get(fname, {})
    exp_hash = meta.get("sha256")
    if exp_hash and hashlib.sha256(raw).hexdigest() != exp_hash:
        return False
    return True


class IncompleteBundle(Exception):
    """Carry a degraded result outside the successful-result cache."""
    def __init__(self, bundle: Bundle, hash_errors: list[str]):
        super().__init__("게시 자료 일부 확인 불가")
        self.bundle = bundle
        self.hash_errors = hash_errors


def _forget_file(src_key, run_id, rel, src):
    clear = getattr(fetch_file, "clear", None)
    if callable(clear):
        clear(src_key, run_id, rel, src)


@st.cache_data(ttl=MANIFEST_TTL_SEC, show_spinner=False, max_entries=8)
def _load_bundle_cached(src_key: str, run_id: str, _src: dict[str, str], _man: dict[str, Any]) -> tuple[Bundle, list[str]]:
    csv_spec = (
        ("watch", "latest/watchlist.csv"), ("cand", "latest/candidates.csv"), ("sectors", "latest/sectors.csv"),
        ("dropped", "latest/dropped.csv"), ("val", "latest/validation.csv"),
        ("h_regime", "history/regime.csv"), ("h_sector", "history/sector_power.csv"), ("h_cand", "history/candidates_daily.csv"),
        ("ledger", "history/signals.csv"), ("outcomes", "history/signal_outcomes.csv"),
    )
    rels = ["latest/summary.json", "latest/charts.json.gz"] + [rel for _, rel in csv_spec]
    if "base_counts.json" in (_man.get("files") or {}):
        rels.append("latest/base_counts.json")
    if "sector_review.json" in (_man.get("files") or {}):
        rels.append("latest/sector_review.json")
    def read_bundle_file(rel):
        try:
            return fetch_file(src_key, run_id, rel, _src), None
        except Exception:
            return None, rel + " 조회 실패"
    with ThreadPoolExecutor(max_workers=6) as pool:
        fetched = dict(zip(rels, pool.map(read_bundle_file, rels)))
    raw_map = {rel: result[0] for rel, result in fetched.items()}
    load_errors = [result[1] for result in fetched.values() if result[1]]
    core_files = {"latest/summary.json", "latest/watchlist.csv", "latest/candidates.csv", "latest/sectors.csv"}
    core_errors = [rel for rel in core_files if raw_map.get(rel) is None]
    for rel in core_errors:
        _forget_file(src_key, run_id, rel, _src)

    hash_errors: list[str] = []
    for rel, raw_bytes in raw_map.items():
        if raw_bytes is not None and not _verify_sha256(raw_bytes, rel, _man):
            hash_errors.append(rel)
            _forget_file(src_key, run_id, rel, _src)
            if rel in core_files:
                core_errors.append(rel)

    summ_raw = raw_map.get("latest/summary.json") if "latest/summary.json" not in hash_errors else None
    try:
        summary = json.loads(summ_raw.decode("utf-8")) if summ_raw else {}
        if not isinstance(summary, dict):
            raise ValueError("summary.json 최상위 구조가 객체(dict)가 아닙니다.")
    except Exception:
        summary = {}
        core_errors.append("latest/summary.json")
        _forget_file(src_key, run_id, "latest/summary.json", _src)

    b = Bundle(man=_man, summ=summary)
    b.load_errors = load_errors
    b.core_errors = core_errors
    b.export_errors = snapshot_export_errors(raw_map, _man)
    if not summary:
        b.export_errors.append("summary.json 내용 확인 불가")
        b.core_errors.append("latest/summary.json")
        _forget_file(src_key, run_id, "latest/summary.json", _src)
    for attr, rel in csv_spec:
        raw_csv = raw_map.get(rel)
        if raw_csv is None or rel in hash_errors:
            setattr(b, attr, pd.DataFrame())
            continue
        try:
            setattr(b, attr, clean_frame(pd.read_csv(io.BytesIO(raw_csv), dtype={"ticker": str})))
        except Exception:
            setattr(b, attr, pd.DataFrame())
            b.load_errors.append(rel + " CSV 읽기 실패")
            _forget_file(src_key, run_id, rel, _src)
            if attr in ("watch", "cand", "sectors"):
                b.export_errors.append(rel + " CSV 읽기 실패")
                b.core_errors.append(rel)

    b.sectors = normalize_sectors_df(b.sectors)
    b.h_sector = normalize_sectors_df(b.h_sector)
    raw_ch = raw_map.get("latest/charts.json.gz") if "latest/charts.json.gz" not in hash_errors else None
    if raw_ch:
        try:
            charts = json.loads(gzip.decompress(raw_ch).decode("utf-8"))
            b.charts = charts if isinstance(charts, dict) else {}
        except Exception:
            b.charts = {}
            b.load_errors.append("latest/charts.json.gz 읽기 실패")
            _forget_file(src_key, run_id, "latest/charts.json.gz", _src)
    b.export_snapshot_verified = not b.export_errors
    b.base_observations,b.base_source=load_base_counts(raw_map.get('latest/base_counts.json'),_man)
    b.sector_review,b.sector_review_source=load_sector_review(raw_map.get('latest/sector_review.json'),_man)
    try:
        b.trend_tracking,b.trend_daily,b.trend_source=load_tracking(str(_man.get('run_id','')),lambda rel:fetch_file(src_key,str(_man.get('run_id','')),rel,_src))
    except Exception:
        b.trend_source={'status':'unavailable','reason':'중장기 추적 보조 자료 조회 실패'}
        b.load_errors.append("중장기 추적 보조 자료 조회 실패")
    if b.load_errors or b.core_errors or hash_errors or b.export_errors:
        raise IncompleteBundle(b, hash_errors)
    return b, hash_errors


def load_bundle_cached(src_key: str, run_id: str, _src: dict[str, str], _man: dict[str, Any]) -> tuple[Bundle, list[str]]:
    try:
        return _load_bundle_cached(src_key, run_id, _src, _man)
    except IncompleteBundle as exc:
        return exc.bundle, exc.hash_errors


load_bundle_cached.clear = _load_bundle_cached.clear


def load_bundle(src_key: str, src: dict[str, str], man: dict[str, Any]) -> Bundle:
    b, hash_errors = load_bundle_cached(src_key, str(man.get("run_id", "")), src, man)
    if hash_errors:
        st.error(f"⚠️ 게시 파일 무결성(SHA-256) 불일치 감지: {', '.join(hash_errors)} — 게시 도중 버전 경합이 발생했을 수 있으니 '🔄 새로고침'을 눌러주세요.")
    if b.core_errors:
        st.error("핵심 게시 자료를 확인할 수 없어 분석을 보류합니다: " + ", ".join(sorted(set(b.core_errors))))
        st.stop()
    if b.load_errors:
        st.warning("일부 보조 자료를 표시할 수 없습니다: " + ", ".join(b.load_errors))
    return b


def contract_messages(b: Bundle) -> list[str]:
    msgs = []
    major = str(b.man.get("schema_version", "")).split(".")[0]
    if major != SUPPORTED_SCHEMA_MAJOR:
        msgs.append(f"데이터 스키마 {b.man.get('schema_version')} ← 앱은 {SUPPORTED_SCHEMA_MAJOR}.x 기준입니다.")
    frames = {"watchlist.csv": b.watch, "candidates.csv": b.cand, "sectors.csv": b.sectors, "dropped.csv": b.dropped, "validation.csv": b.val}
    for name, req in ((b.man.get("contract") or {}).get("required_columns") or {}).items():
        df = frames.get(name)
        if df is not None and not df.empty:
            miss = [c for c in req if c not in df.columns]
            if miss:
                msgs.append(f"{name}: 필수 컬럼 누락 {miss}")
    if not b.outcomes.empty:
        if "ret_10d" not in b.outcomes.columns:
            msgs.append("signal_outcomes.csv: ret_10d 컬럼이 없어 10일 성과 검증을 수행할 수 없습니다.")
        if "market" not in b.outcomes.columns:
            msgs.append("signal_outcomes.csv: market 컬럼이 없어 시장별 비교는 '미분류'로 표시됩니다.")
    return msgs


# ════════════════════════════════════════════════════════════════════
# 3. 공통 유틸 · 펀더멘털/시그널 배지 포맷터 · 차트 빌더
# ════════════════════════════════════════════════════════════════════
def age_days(as_of: str | None) -> int | None:
    try:
        return (datetime.now(KST).date() - pd.Timestamp(as_of).date()).days
    except Exception:
        return None


def light_kind(sig: Any) -> str:
    s = str(sig)
    return "ATTACK" if "ATTACK" in s else ("RISK_OFF" if "RISK_OFF" in s else "CAUTION")


def detect_default_mode(regs: dict[str, Any], track_choice: str) -> str:
    if not regs:
        return "CAUTION"
    if track_choice in regs:
        kind = compass_regime(regs[track_choice])
        return kind if kind != "UNKNOWN" else "CAUTION"
    if track_choice == "KR (KOSPI+KOSDAQ)":
        target_keys = [k for k in ("KOSPI", "KOSDAQ") if k in regs]
    elif track_choice == "US (NASDAQ100+SP500)":
        target_keys = [k for k in ("NASDAQ100", "SP500") if k in regs]
    else:
        target_keys = list(regs.keys())
    kinds = [compass_regime(regs[k]) for k in target_keys]
    if not kinds:
        return "CAUTION"
    if all(k == "ATTACK" for k in kinds):
        return "ATTACK"
    if all(k == "RISK_OFF" for k in kinds):
        return "RISK_OFF"
    return "CAUTION"


def fmt_price(v: Any, mkt: str) -> str:
    try:
        return f"{float(v):,.0f}원" if str(mkt).upper() in ("KR", "KOSPI", "KOSDAQ") else f"${float(v):,.2f}"
    except Exception:
        return "-"


def pct(v: Any, nd: int = 1) -> str:
    try:
        return f"{float(v) * 100:+.{nd}f}%"
    except Exception:
        return "-"


def format_fund_badge(peg: Any, eps_g: Any, eps_status: Any) -> str:
    st_str = str(eps_status or "").strip().upper()
    try:
        p_val = float(peg)
    except Exception:
        p_val = np.nan
    try:
        eg_val = float(eps_g)
    except Exception:
        eg_val = np.nan

    eg_part = f" (EPS {eg_val:+.0f}%)" if np.isfinite(eg_val) else ""
    if st_str == "TURNAROUND":
        return f"✨흑자전환{eg_part}"
    if np.isfinite(p_val) and p_val > 0:
        if p_val <= 1.0:
            return f"💎초저PEG {p_val:.2f}{eg_part}"
        if p_val <= 1.5:
            return f"✅저PEG {p_val:.2f}{eg_part}"
        return f"PEG {p_val:.2f}{eg_part}"
    if np.isfinite(eg_val):
        return f"EPS {eg_val:+.0f}%"
    if st_str in ("LOSS_TURN", "LOSS_CONT"):
        return "적자지속/전환"
    return "-"


def format_signal_badges(
    blue_dot: Any, rs_nh: Any, down_res: Any, earn_tag: Any,
    rs_4w: Any = np.nan, d20_pct: Any = np.nan, atr_from_50: Any = np.nan,
) -> str:
    parts = []
    if str(blue_dot).lower() == "true":
        parts.append("🔵블루닷")
    elif str(rs_nh).lower() == "true":
        parts.append("⚡RS신고가")
    if str(down_res).lower() == "true":
        parts.append("🛡️방어력")
    try:
        r4 = float(rs_4w)
        d20 = float(d20_pct)
        if np.isfinite(r4) and r4 <= -6.0 and (not np.isfinite(d20) or d20 < 0):
            parts.append("📉RS둔화")
    except Exception:
        pass
    try:
        a50 = float(atr_from_50)
        if np.isfinite(a50) and a50 >= 3.5:
            parts.append("🔥이격과열")
    except Exception:
        pass
    e_str = str(earn_tag or "").strip()
    if e_str and e_str.lower() not in ("nan", "none"):
        parts.append(e_str)
    return " · ".join(parts) if parts else "-"


def is_garp_or_turnaround(df: pd.DataFrame, max_peg: float = 1.5) -> pd.Series:
    peg_s = pd.to_numeric(df.get("peg", pd.Series(np.nan, index=df.index)), errors="coerce")
    st_s = df.get("eps_status", pd.Series("", index=df.index)).astype(str).str.upper()
    return ((peg_s > 0) & (peg_s <= max_peg)) | (st_s == "TURNAROUND")


def num(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def ext_links(mkt: str, tkr: str) -> tuple[str, str, str]:
    is_kr = str(mkt).upper() in ("KR", "KOSPI", "KOSDAQ")
    t = str(tkr).zfill(6) if is_kr else str(tkr).upper()
    tv = f"https://kr.tradingview.com/chart/?symbol=KRX:{t}" if is_kr else f"https://kr.tradingview.com/chart/?symbol={t.replace('-', '.')}"
    info = f"https://finance.naver.com/item/main.naver?code={t}" if is_kr else f"https://finance.yahoo.com/quote/{t}"
    lbl = "네이버" if is_kr else "Yahoo"
    return tv, info, lbl


def list_matrix(h_cand: pd.DataFrame, only_selected: bool = True) -> tuple[pd.DataFrame, list[str]]:
    if h_cand.empty or "date" not in h_cand.columns:
        return pd.DataFrame(), []
    d = h_cand.copy()
    if only_selected and "selected" in d.columns:
        d = d[tf(d["selected"])]
    if d.empty:
        return pd.DataFrame(), []
    d["key"] = d["market"].astype(str) + "|" + d["ticker"].astype(str)
    mat = pd.crosstab(d["key"], d["date"]).clip(upper=1).astype(bool)
    dates = sorted(mat.columns)
    return mat[dates], dates


def streak_ending_last(mat: pd.DataFrame) -> pd.Series:
    if mat.empty:
        return pd.Series(dtype=int)
    arr = mat.to_numpy(bool)[:, ::-1]
    return pd.Series(np.cumprod(arr, axis=1).sum(axis=1), index=mat.index)


def latest_rows(h_cand: pd.DataFrame, date: str) -> pd.DataFrame:
    d = h_cand[h_cand["date"] == date].copy()
    d["key"] = d["market"].astype(str) + "|" + d["ticker"].astype(str)
    return d.drop_duplicates("key").set_index("key")


def is_dark_mode() -> bool:
    override = st.session_state.get("ui_theme_mode", "자동")
    if override == "다크":
        return True
    if override == "라이트":
        return False
    try:
        ctx_theme = getattr(getattr(st, "context", None), "theme", None)
        if ctx_theme is not None:
            t_val = ctx_theme.get("type") if isinstance(ctx_theme, dict) else getattr(ctx_theme, "type", None)
            if t_val:
                return str(t_val).lower() == "dark"
        return str(st.get_option("theme.base") or "light").lower() == "dark"
    except Exception:
        return False


def get_chart_theme() -> dict[str, str]:
    if is_dark_mode():
        return {
            "mode": "dark",
            "template": "plotly_dark",
            "text": "#F8FAFC", "title": "#FFFFFF", "subtext": "#CBD5E1",
            "plot": "rgba(15, 23, 42, 0.38)", "grid": "rgba(148, 163, 184, 0.18)",
            "axis": "#E2E8F0", "legend": "rgba(15, 23, 42, 0.85)", "hover": "#0F172A",
            "rem_bar": "#334155", "rem_text": "#E2E8F0", "line_primary": "#F8FAFC",
        }
    return {
        "mode": "light",
        "template": "plotly_white",
        "text": "#0F172A", "title": "#0F172A", "subtext": "#475569",
        "plot": "rgba(248, 250, 252, 0.72)", "grid": "rgba(148, 163, 184, 0.28)",
        "axis": "#334155", "legend": "rgba(255, 255, 255, 0.88)", "hover": "#FFFFFF",
        "rem_bar": "#E2E8F0", "rem_text": "#334155", "line_primary": "#0F172A",
    }


def show_plotly(fig: go.Figure, key: str) -> None:
    t_mode = "dark" if is_dark_mode() else "light"
    st.plotly_chart(fig, width="stretch", theme=None, key=f"{key}_{t_mode}")


def clean_chart(fig: go.Figure, height: int = 320, legend: bool = True, title: str | None = None) -> go.Figure:
    theme = get_chart_theme()
    top_m = 48 if title else (36 if legend else 14)
    fig.update_layout(
        template=theme["template"],
        height=height,
        font=dict(family="Pretendard, -apple-system, BlinkMacSystemFont, Malgun Gothic, sans-serif", size=12, color=theme["text"]),
        title=dict(text=f"<b>{title}</b>", font=dict(size=14, color=theme["title"]), x=0, xanchor="left") if title else None,
        margin=dict(l=10, r=14, t=top_m, b=14), paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor=theme["plot"],
        showlegend=legend,
        legend=dict(orientation="h", y=1.12, x=0, bgcolor=theme["legend"], font=dict(color=theme["text"])),
        hoverlabel=dict(bgcolor=theme["hover"], font=dict(color=theme["text"])),
        annotationdefaults=dict(font=dict(color=theme["text"])), hovermode="closest",
    )
    fig.update_xaxes(showgrid=True, gridcolor=theme["grid"], zeroline=False, tickfont=dict(color=theme["axis"]), title_font=dict(color=theme["text"]))
    fig.update_yaxes(showgrid=True, gridcolor=theme["grid"], zeroline=False, tickfont=dict(color=theme["axis"]), title_font=dict(color=theme["text"]))
    for trace in fig.data:
        tpos = getattr(trace, "textposition", None)
        if tpos in ("outside", "top center"):
            cur_col = getattr(getattr(trace, "textfont", None), "color", None)
            if cur_col in (None, PALETTE["dark"], "#0F172A", "white", "#FFFFFF", "#334155", "#475569"):
                trace.update(textfont=dict(color=theme["text"]))
    if fig.layout.annotations:
        for ann in fig.layout.annotations:
            a_col = getattr(getattr(ann, "font", None), "color", None)
            if a_col in (None, PALETTE["dark"], "#0F172A", "#334155", "#475569"):
                ann.font.color = theme["text"]
    return fig


def finite_number(value: Any, default: float = np.nan) -> float:
    """Preserve valid zero; missing or non-finite inputs remain unknown."""
    try:
        result = float(value) if not isinstance(value, (bool, np.bool_)) else np.nan
    except (TypeError, ValueError, OverflowError):
        return default
    return result if np.isfinite(result) else default


def number_text(value: Any, nd: int = 1) -> str:
    value = finite_number(value)
    return f"{value:.{nd}f}" if np.isfinite(value) else "자료 없음"


def capacity_bar_chart(items: list[dict[str, Any]], title: str, suffix: str = "%", height: int = 210) -> go.Figure:
    theme = get_chart_theme()
    labels = [it["label"] for it in items][::-1]
    used = [finite_number(it["used"]) for it in items][::-1]
    caps = [finite_number(it["cap"]) for it in items][::-1]
    max_x = max([finite_number(it.get("max_axis"), max([v * 1.2 for v in (c, u) if np.isfinite(v)] + [10.0]))
                 for it, c, u in zip(items[::-1], caps, used)] + [10.0])
    rem = [max(0.0, c - u) if np.isfinite(c) and np.isfinite(u) else np.nan for u, c in zip(used, caps)]
    colors = [it.get("color", PALETTE["indigo"]) for it in items][::-1]

    used_texts = [f"<b>{u:.1f}{suffix}</b> (한도 {c:.1f}{suffix})" if u >= max_x * 0.22 else "" for u, c in zip(used, caps)]
    rem_texts = [
        f"<b>{u:.1f}{suffix}</b> / 한도 {c:.1f}{suffix} (여유 {r:.1f}{suffix})" if u < max_x * 0.22 else (f"여유 {r:.1f}{suffix}" if r >= max_x * 0.14 else "")
        for u, c, r in zip(used, caps, rem)
    ]
    for i, (u, c) in enumerate(zip(used, caps)):
        if not np.isfinite(u) or not np.isfinite(c):
            used_texts[i] = "한도 자료 없음" if not np.isfinite(c) else "배분 자료 없음"
            rem_texts[i] = f"한도 {c:.1f}{suffix}" if np.isfinite(c) else ""

    fig = go.Figure()
    fig.add_bar(
        y=labels, x=used, orientation="h", name="현재 사용",
        marker=dict(color=colors, cornerradius=4),
        text=used_texts,
        textposition="inside", insidetextanchor="middle", textfont=dict(color="#FFFFFF", size=12),
        hovertemplate="<b>%{y}</b><br>신규 배분: %{x:.2f}" + suffix + "<extra></extra>",
    )
    fig.add_bar(
        y=labels, x=rem, orientation="h", name="한도 여유분",
        marker=dict(color=theme["rem_bar"], cornerradius=4),
        text=rem_texts,
        textposition="inside", insidetextanchor="middle", textfont=dict(color=theme["rem_text"], size=11),
        hovertemplate="<b>%{y}</b><br>한도까지 여유: %{x:.2f}" + suffix + "<extra></extra>",
    )
    fig.update_layout(barmode="stack", xaxis=dict(range=[0, max_x], ticksuffix=suffix))
    return clean_chart(fig, height=height, legend=False, title=title)


def chart_period_label(bars: int) -> str:
    months = {20: "1개월", 45: "2개월", 80: "4개월", 130: "6개월", 260: "1년"}
    return f"약 {months[bars]} ({bars}봉)"


def candle_chart(ch: dict[str, Any], mkt: str, title: str | None = None, bars: int = 130, compact: bool = False) -> go.Figure:
    up, down = PALETTE["red"], PALETTE["green_soft"]

    d = ch["d"][-bars:]
    o = ch["o"][-bars:]
    h = ch["h"][-bars:]
    l = ch["l"][-bars:]
    c = ch["c"][-bars:]
    v = ch["v"][-bars:]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.76, 0.24], vertical_spacing=0.03)
    add_price_traces(fig, ch, bars, compact, up, down,
                     {20: PALETTE["blue"], 50: PALETTE["amber"], 200: PALETTE["purple"]})

    piv = ch.get("pivot")
    stp = ch.get("stop")
    if piv:
        fig.add_hrect(
            y0=float(piv) * 0.975, y1=float(piv) * 1.05,
            fillcolor=PALETTE["green"], opacity=0.10, line_width=0,
            annotation_text="" if compact else "🎯 바이존 (-2.5%~+5%)",
            annotation_position="top left", row=1, col=1,
        )
        fig.add_hline(
            y=piv, line=dict(color=PALETTE["green"], dash="dash", width=1.2),
            annotation_text="피벗" if compact else f"피벗 ({fmt_price(piv, mkt)})",
            annotation_position="top right", row=1, col=1,
        )
    if stp:
        fig.add_hline(
            y=stp, line=dict(color=PALETTE["red"], dash="dash", width=1.2),
            annotation_text="손절" if compact else f"손절 ({fmt_price(stp, mkt)})",
            annotation_position="bottom right", row=1, col=1,
        )

    vol_colors = [up if (cv is not None and ov is not None and cv >= ov) else down for ov, cv in zip(o, c)]
    fig.add_trace(go.Bar(x=d, y=v, name="거래량", marker_color=vol_colors or "#94A3B8", opacity=0.65), row=2, col=1)
    fig.update_xaxes(type="category", nticks=5 if compact else 8, rangeslider_visible=False)
    fig.update_yaxes(showticklabels=not compact, row=2, col=1)

    height = 300 if compact else 520
    return clean_chart(fig, height=height, legend=not compact, title=title)


# [P2-12] (run_id, 티커, 봉수, 테마, 컴팩트여부) 단위로 캔들 차트 JSON을 캐시하여 페이지/정렬 변경 시 재생성 방지
@st.cache_data(ttl=MANIFEST_TTL_SEC, show_spinner=False, max_entries=512)
def cached_candle_json(run_id: str, ck: str, mkt: str, bars: int, compact: bool, theme_mode: str, _ch: dict[str, Any]) -> str:
    del run_id, ck, theme_mode
    fig = candle_chart(_ch, mkt, title=None, bars=bars, compact=compact)
    return fig.to_json()


def render_banner(b: Bundle) -> None:
    man, summ = b.man, b.summ
    asof = man.get("as_of") or {}
    st.caption(
        f"📅 생성 {man.get('generated_at', '미확인')} · 게시 시각 {man.get('published_at') or '미기록'} · 엔진 {man.get('engine_version', '?')} · 스키마 {man.get('schema_version', '?')} · "
        + " · ".join(f"**{k}** {v}" for k, v in asof.items())
    )
    stale = [f"{k} ({v})" for k, v in asof.items() if (age_days(v) is not None and age_days(v) > STALE_DAYS)]
    if stale:
        st.warning("⏰ 오래된 기준일 감지: " + ", ".join(stale) + " — 데이터 수집 워크플로 상태를 확인하세요.")
    h = summ.get("health") or {}
    kr_h, us_h = h.get("kr") or {}, h.get("us") or {}
    if kr_h.get("kr_price_status") in ("delayed_or_holiday", "calendar_unreviewed"):
        st.warning(kr_h.get("kr_price_message") or
                   ("KR 특별장 일정 검토 범위를 벗어났습니다. 이전 가격을 표시할 수 있으므로 일정 검토·갱신이 필요합니다."
                    if kr_h.get("kr_price_status") == "calendar_unreviewed" else
                    f"KR {kr_h.get('kr_close_check_date', '')} 마감 일봉 미확보 — 기존 자료를 표시합니다. 휴장/공급 지연 여부를 확인하세요."))
    if us_h.get("us_cap_fallback") or us_h.get("us_cap_missing"):
        st.warning(f"US 시가총액·재무정보: 이전 정상 캐시 사용 {us_h.get('us_cap_fallback', 0)}종목 · "
                   f"유효 시총 없음 {us_h.get('us_cap_missing', 0)}종목(분석 대상 제외). 캐시 폴백은 최신 조회 결과가 아닙니다.")
    observation = summ.get("price_observation") or {}
    for track, item in (observation.get("tracks") or {}).items():
        if item.get("session_unconfirmed") or item.get("lagging_candidate_rows"):
            st.warning(f"{track} 가격 확인 필요: 지수 {item.get('benchmark_as_of') or '미확인'} · "
                       f"마감시각 기준 확인 대상 평일 {item.get('clock_reference_date') or '미확인'} · "
                       f"벤치마크보다 뒤처진 후보 {item.get('lagging_candidate_rows', 0)}개. "
                       "휴장/공급 지연을 구분할 수 없습니다. 기술적 적격은 최신성 보증이 아닙니다.")
    fails = int(kr_h.get("kr_failed", 0)) + int(us_h.get("us_failed", 0))
    stale_tkrs = int(kr_h.get("kr_stale_tickers", 0)) + int(us_h.get("us_stale_tickers", 0))
    sw = h.get("swallowed") or {}
    if fails or stale_tkrs or sw:
        with st.expander(f"⚠️ 데이터 헬스 알림: 수집 실패 {fails}건 · 지연 캐시 제외 {stale_tkrs}건 · 내부 예외 {sum(sw.values()) if sw else 0}건"):
            st.json({"kr": kr_h, "us": us_h, "swallowed": sw})
    for m in contract_messages(b):
        st.warning(m)


# ════════════════════════════════════════════════════════════════════
# 4. 탭 1: 🧭 국면·나침반 (보유 비중 차감 잔여 한도 연동)
# ════════════════════════════════════════════════════════════════════
def build_exclusion_reason(df: pd.DataFrame) -> pd.Series:
    """Explain available fields without inventing an authoritative exclusion cause."""
    if df.empty:
        return pd.Series(dtype=str)
    reasons = []
    def text(value):
        return "" if pd.isna(value) else str(value).strip()
    def flag(value):
        return text(value).lower() in ("true", "1", "1.0", "yes")
    for r in df.itertuples():
        if flag(getattr(r, "actionable_now", False)):
            reasons.append("✅ 적격")
            continue
        r_list = []
        tier = text(getattr(r, "tier", None))
        if not tier:
            r_list.append("정배열 상태 미확인")
        elif tier != "Tier A":
            r_list.append(f"정배열 미완({tier})")
        stp_abs = abs(pd.to_numeric(pd.Series([getattr(r, "stop_dist_pct", np.nan)]), errors="coerce").iloc[0])
        if not np.isfinite(stp_abs):
            stp_abs = np.nan
        stp_text = f"{stp_abs*100:.1f}%" if np.isfinite(stp_abs) else "자료 없음"
        if flag(getattr(r, "is_wide_stop", False)) or (0.08 < stp_abs <= 0.12):
            r_list.append(f"와이드스톱({stp_text} · 8~12% 관찰군)")
        elif stp_abs > 0.12:
            r_list.append(f"손절폭 과다({stp_text} > 12%)")
        elif flag(getattr(r, "stop_warning", False)):
            r_list.append(f"손절폭 경고({stp_text})")
        ex_sig = text(getattr(r, "exit_signal", None))
        if not ex_sig:
            r_list.append("청산 상태 미확인")
        elif ex_sig != "✅정상":
            r_list.append(f"청산경고({ex_sig})")
        e_tag = text(getattr(r, "earnings_tag", None))
        days = re.findall(r"(?<![A-Za-z0-9])D-(\d+)(?![A-Za-z0-9])", e_tag)
        if "진입보류" in e_tag or any(int(day) <= 3 for day in days):
            r_list.append(f"실적블랙아웃({e_tag})")
        setup_st = text(getattr(r, "setup_stage", None))
        if not setup_st:
            r_list.append("셋업 미확인")
        elif "과열" in setup_st:
            r_list.append("단기과열")
        elif "단기수축" in setup_st:
            r_list.append("베이스기간미달(25일미만 단기수축)")
        elif not any(k in setup_st for k in ("돌파", "VCP", "수축", "포켓", "눌림목")):
            r_list.append(f"셋업대기({setup_st})")
        if not r_list:
            if text(getattr(r, "selected", None)).lower() in ("false", "0", "0.0", "no"):
                r_list.append("Top20 미선정(세부 사유 미확인)")
            else:
                r_list.append("상세 제외 사유 미확인")
        reasons.append(" · ".join(r_list))
    return pd.Series(reasons, index=df.index)


def compute_remaining_portfolio(b: Bundle) -> dict[str, Any]:
    """사용자 보유 비중(KR/US) 입력을 반영하여 남은 신규 배분 한도와 스케일 팩터를 계산한다."""
    summ = b.summ or {}
    port = summ.get("portfolio") or {}
    lim = summ.get("market_limits") or {}

    held_inputs = st.session_state.get("_held_inputs", st.session_state)
    held_confirmed = bool(held_inputs.get("held_confirmed", False))
    held_kr = float(held_inputs.get("held_kr_pct", 0.0)) if held_confirmed else 0.0
    held_us = float(held_inputs.get("held_us_pct", 0.0)) if held_confirmed else 0.0

    kr_cap = finite_number((lim.get("KR") or {}).get("exposure_cap"))
    us_cap = finite_number((lim.get("US") or {}).get("exposure_cap"))

    rem_kr_cap = max(0.0, kr_cap - held_kr) if np.isfinite(kr_cap) else np.nan
    rem_us_cap = max(0.0, us_cap - held_us) if np.isfinite(us_cap) else np.nan

    def published_usage(market, field):
        usage = lim.get(market) or {}
        if field in usage:
            return finite_number(usage[field])
        # Older summaries omitted alloc/risk when this market had no candidates.
        watch = getattr(b, "watch", None)
        count = (summ.get("counts") or {}).get("actionable")
        if (market in lim and isinstance(watch, pd.DataFrame) and count == len(watch)
                and (watch.empty or "market" in watch and not watch.market.eq(market).any())):
            return 0.
        return np.nan

    raw_kr_alloc = published_usage("KR", "alloc")
    raw_us_alloc = published_usage("US", "alloc")

    def scale(remaining, allocation):
        if allocation == 0:
            return 1.0
        return min(1.0, remaining / allocation) if np.isfinite(remaining) and allocation > 0 else np.nan
    scale_kr = scale(rem_kr_cap, raw_kr_alloc)
    scale_us = scale(rem_us_cap, raw_us_alloc)

    if held_confirmed:
        total_cap = finite_number(port.get("exposure_cap_pct"))
        remaining_total = max(0.0, total_cap - held_kr - held_us) if np.isfinite(total_cap) else np.nan
        proposed_total = raw_kr_alloc * scale_kr + raw_us_alloc * scale_us
        common_scale = scale(remaining_total, proposed_total)
        # Preserve relative market proposals and round down at the display precision.
        # The table consumes these same scales, so it must share the global cap.
        if raw_kr_alloc > 0:
            value = raw_kr_alloc * scale_kr * common_scale
            scale_kr = math.floor(value * 10 + 1e-9) / 10 / raw_kr_alloc if np.isfinite(value) else np.nan
        if raw_us_alloc > 0:
            value = raw_us_alloc * scale_us * common_scale
            scale_us = math.floor(value * 10 + 1e-9) / 10 / raw_us_alloc if np.isfinite(value) else np.nan

    adj_kr_alloc = round(raw_kr_alloc * scale_kr, 1)
    adj_us_alloc = round(raw_us_alloc * scale_us, 1)

    raw_kr_risk = published_usage("KR", "risk")
    raw_us_risk = published_usage("US", "risk")
    adj_kr_risk = raw_kr_risk * scale_kr
    adj_us_risk = raw_us_risk * scale_us

    watch = getattr(b, "watch", None)
    if held_confirmed and isinstance(watch, pd.DataFrame) and {"market", "portfolio_alloc_pct"}.issubset(watch):
        scaled = allocation_view(watch, {"scale_kr": scale_kr, "scale_us": scale_us, "held_confirmed": True})

        def row_totals(market, allocation, risk):
            group = scaled.loc[scaled.market.eq(market)]
            original = group["_published_alloc_pct"]
            adjusted = group["portfolio_alloc_pct"]
            # Only reconcile complete, matching publications.
            if (not np.isfinite(allocation) or original.isna().any() or adjusted.isna().any()
                    or not np.isclose(original.sum(), allocation, atol=1e-7)):
                return np.nan, np.nan
            total = round(float(adjusted.sum()), 1)
            if total == 0:
                return total, 0.
            if "portfolio_risk_contrib_pct" not in group:
                return total, np.nan
            used = group.loc[adjusted.gt(0)]
            contributions = pd.to_numeric(used["portfolio_risk_contrib_pct"], errors="coerce")
            if not np.isfinite(risk) or not np.isfinite(contributions).all():
                return total, np.nan
            return total, (contributions * used["portfolio_alloc_pct"] / used["_published_alloc_pct"]).sum()

        adj_kr_alloc, adj_kr_risk = row_totals("KR", raw_kr_alloc, raw_kr_risk)
        adj_us_alloc, adj_us_risk = row_totals("US", raw_us_alloc, raw_us_risk)

    return {
        "held_confirmed": held_confirmed,
        "held_kr": held_kr, "held_us": held_us,
        "kr_cap": kr_cap, "us_cap": us_cap,
        "rem_kr_cap": rem_kr_cap, "rem_us_cap": rem_us_cap,
        "scale_kr": scale_kr, "scale_us": scale_us,
        "adj_kr_risk": adj_kr_risk, "adj_us_risk": adj_us_risk,
        "adj_kr_alloc": adj_kr_alloc, "adj_us_alloc": adj_us_alloc,
        "adj_total_alloc": round(adj_kr_alloc + adj_us_alloc, 1),
        "adj_total_risk": round(adj_kr_risk + adj_us_risk, 2),
    }


def allocation_view(frame: pd.DataFrame, rem_info: dict[str, Any] | None) -> pd.DataFrame:
    """Session-only allocation display. Keep published proposals and signals intact."""
    out = frame.copy()
    if "portfolio_alloc_pct" not in out:
        return out
    source = "_published_alloc_pct"
    if source not in out:
        out[source] = pd.to_numeric(out["portfolio_alloc_pct"], errors="coerce")
    out["portfolio_alloc_pct"] = out[source]
    if rem_info is not None and "market" in out:
        scales = out["market"].astype(str).str.upper().map({"KR": rem_info["scale_kr"], "US": rem_info["scale_us"]})
        scaled = out[source] * scales
        out["portfolio_alloc_pct"] = (np.floor(scaled * 10 + 1e-9) / 10
                                       if rem_info.get("held_confirmed") else scaled.round(1))
    return out


def remember_held_input(field: str) -> None:
    values = dict(st.session_state["_held_inputs"])
    values[field] = st.session_state["_" + field]
    st.session_state["_held_inputs"] = values
    st.session_state[field] = values[field]


def remember_playbook_input(field: str) -> None:
    st.session_state["_saved_" + field] = st.session_state[field]


def render_portfolio_visuals(b: Bundle, rem_info: dict[str, Any]) -> None:
    summ = b.summ
    port = summ.get("portfolio") or {}
    lim = summ.get("market_limits") or {}
    risk_cap = finite_number(port.get("risk_cap_pct"))

    c1, c2, c3 = st.columns([1.15, 1.15, 1.2])
    with c1:
        with st.container(border=True):
            items_exp = []
            if "KR" in lim:
                lbl_kr = f"🇰🇷 KR 신규한도 (보유 {number_text(rem_info['held_kr'], 0)}% 차감)" if rem_info["held_confirmed"] else "🇰🇷 KR 신규 아이디어 한도"
                items_exp.append({
                    "label": lbl_kr,
                    "used": rem_info["adj_kr_alloc"],
                    "cap": rem_info["rem_kr_cap"],
                    "max_axis": max(10.0, rem_info["rem_kr_cap"] * 1.15) if np.isfinite(rem_info["rem_kr_cap"]) else 100.0,
                    "color": PALETTE["indigo"],
                })
            if "US" in lim:
                lbl_us = f"🇺🇸 US 신규한도 (보유 {number_text(rem_info['held_us'], 0)}% 차감)" if rem_info["held_confirmed"] else "🇺🇸 US 신규 아이디어 한도"
                items_exp.append({
                    "label": lbl_us,
                    "used": rem_info["adj_us_alloc"],
                    "cap": rem_info["rem_us_cap"],
                    "max_axis": max(10.0, rem_info["rem_us_cap"] * 1.15) if np.isfinite(rem_info["rem_us_cap"]) else 100.0,
                    "color": PALETTE["blue"],
                })
            if not items_exp:
                items_exp.append({
                    "label": "신규 아이디어 배분",
                    "used": rem_info["adj_total_alloc"],
                    "cap": finite_number(port.get("exposure_cap_pct")),
                    "max_axis": 100.0,
                    "color": PALETTE["indigo"],
                })
            if any(not np.isfinite(item["cap"]) for item in items_exp):
                st.caption("노출 한도 자료 없음 — 한도를 확인하기 전에는 배분 가능 여부를 확정할 수 없습니다.")
            show_plotly(capacity_bar_chart(items_exp, "📊 신규 아이디어 한도 대비 가상 배분 (%)", "%", height=215), key="plt_cap_exp")

    with c2:
        with st.container(border=True):
            max_r = max(5.5, risk_cap * 1.3) if np.isfinite(risk_cap) else 5.5
            items_risk = [{"label": "신규 합산 손절리스크", "used": rem_info["adj_total_risk"], "cap": risk_cap, "max_axis": max_r, "color": PALETTE["amber"]}]
            for mk, v in lim.items():
                if mk not in ("KR", "US"):
                    continue
                items_risk.append({
                    "label": f"{'🇰🇷' if mk == 'KR' else '🇺🇸'} {mk} 신규 리스크",
                    "used": rem_info[f"adj_{mk.lower()}_risk"],
                    "cap": finite_number(v.get("risk_cap")),
                    "max_axis": max_r,
                    "color": "#F59E0B",
                })
            if any(not np.isfinite(item["cap"]) for item in items_risk):
                st.caption("리스크 한도 자료 없음 — 0 또는 기본 한도로 대체하지 않습니다.")
            show_plotly(capacity_bar_chart(items_risk, "🛡️ 리스크 예산 대비 신규 후보군 손절 위험 (%)", "%", height=215), key="plt_cap_risk")

    with c3:
        with st.container(border=True):
            w = num(allocation_view(b.watch, rem_info), "portfolio_alloc_pct", "position_size_pct", "rs_score") if not b.watch.empty else pd.DataFrame()
            if not w.empty and "portfolio_alloc_pct" in w.columns and w["portfolio_alloc_pct"].fillna(0).sum() > 0:
                w_pos = w[w["portfolio_alloc_pct"] > 0].copy()
                w_pos["sector"] = w_pos["sector"].fillna("기타")
                sec_grp = w_pos.groupby("sector", as_index=False).agg(alloc=("portfolio_alloc_pct", "sum")).sort_values("alloc", ascending=False)
                # Long sector labels make a compact pie hard to scan. Show absolute
                # proposed portfolio weight by sector, with the tail consolidated.
                focus = sec_grp.head(4).copy()
                tail = sec_grp.iloc[4:]["alloc"].sum()
                if tail > 0:
                    focus = pd.concat([focus, pd.DataFrame([{"sector": "그 외 섹터", "alloc": tail}])], ignore_index=True)
                focus = focus.iloc[::-1]
                fig_d = go.Figure(go.Bar(
                    x=focus["alloc"], y=focus["sector"], orientation="h",
                    text=[f"{v:.1f}%" for v in focus["alloc"]],
                    textposition="outside",
                    marker=dict(color=PALETTE["indigo"], cornerradius=4),
                    hovertemplate="<b>%{y}</b><br>계좌 대비 신규 가상 배분: %{x:.1f}%<extra></extra>",
                ))
                fig_d.update_xaxes(range=[0, max(1.0, float(focus["alloc"].max()) * 1.33)],
                                   title="신규 비중 (%)")
                fig_d.update_yaxes(automargin=True)
                chart = clean_chart(fig_d, height=225, legend=False, title="🎯 신규 배분 상위 섹터")
                chart.update_layout(margin=dict(l=105, r=24, t=48, b=40))
                show_plotly(chart, key="plt_sec_pie")
            else:
                st.caption("현재 배분 비중(>0%)이 할당된 액션 종목이 없습니다.")


def compact_explainer(summary: str, title: str, details: str) -> None:
    """Show a short always-visible note with optional detailed guidance."""
    st.caption(summary)
    with st.expander(f"ⓘ {title}", expanded=False):
        st.markdown(details)


def compass_regime(record) -> str:
    if record is None or record.get("regime_quality") == "insufficient_price":
        return "UNKNOWN"
    state = record.get("regime")
    if state in ("ATTACK", "CAUTION", "RISK_OFF"):
        return state
    if pd.isna(state) or state == "RISK_ON":
        match = re.match(r"^[🟢🟡🔴\s]*(ATTACK|CAUTION|RISK_OFF)\b", str(record.get("signal_light", "")))
        if match:
            return match[1]
    return "UNKNOWN"


def price_benchmark(track, record) -> str:
    raw = record.get("benchmark")
    published = str(raw).strip() if isinstance(raw, str) else ""
    for symbol in ("QQQ", "SPY"):
        if published == symbol or published.endswith(f"({symbol})"):
            return f"{symbol} ETF"
    if track in ("KOSPI", "KOSDAQ"):
        return published or track
    return published if published and "ex-NDX" not in published else "벤치마크 미확인"


def compass_ftd_text(record) -> str:
    obs = record.get("ftd")
    if isinstance(obs, dict) and obs.get("version") == "swing-ftd-v1.1":
        if obs.get("status") == "not_detected":
            return "FTD 미감지 · 해당 기준일의 반등 확인 조건 미충족"
        if obs.get("status") == "detected":
            day = pd.to_datetime(str(obs.get("candidate_date")), format="%Y-%m-%d", errors="coerce")
            as_of = pd.to_datetime(str(record.get("as_of")), format="%Y-%m-%d", errors="coerce")
            if pd.notna(day) and pd.notna(as_of) and day <= as_of:
                return f"FTD 감지 · 후보일 {day:%Y-%m-%d} · 기준일 현재 유효한 반등 참고 신호"
        return "FTD 검증 불가 · 가격·거래량·거래일 근거 미확인"
    legacy = re.search(r"FTD확인\((\d{2}/\d{2})\)", str(record.get("signal_light", "")))
    if legacy:
        return f"FTD 기존 게시 표기 {legacy[1]} · 당시 입력 검증 정보 없음"
    return "FTD 검증 정보 없음"


def valid_breadth_pct(record: dict[str, Any], field: str) -> float:
    """Invalid/out-of-range Breadth never renders as a credible percentage."""
    if record.get("breadth_valid") is False:
        return float("nan")
    raw = pd.to_numeric(record.get(field), errors="coerce")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float("nan")
    return value if np.isfinite(value) and 0 <= value <= 100 else float("nan")


def regime_comparison_frame(regs: dict[str, Any]) -> pd.DataFrame:
    """Four-track, presentation-only snapshot; never modify the engine regime."""
    def value(record: dict[str, Any], field: str) -> float:
        v = pd.to_numeric(record.get(field), errors="coerce")
        return float(v) if pd.notna(v) and np.isfinite(v) else float("nan")

    status_labels = {
        "ATTACK": "🟢 공략",
        "CAUTION": "🟡 관찰",
        "RISK_OFF": "🔴 방어",
        "UNKNOWN": "⚪ 미확인",
    }
    rows = []
    for trk in TRACKS:
        r = regs.get(trk) or {}
        valid = r.get("breadth_valid") is not False
        p50 = valid_breadth_pct(r, "pct_above_50dma")
        p200 = valid_breadth_pct(r, "pct_above_200dma")
        close, sma200 = value(r, "benchmark_close"), value(r, "benchmark_sma200")
        gap200 = (close / sma200 - 1.0) * 100 if np.isfinite(close) and np.isfinite(sma200) and sma200 > 0 else float("nan")
        dist = value(r, "distribution_days_25d")
        if np.isfinite(p50) or np.isfinite(p200):
            breadth_label = " / ".join(f"{v:g}%" if np.isfinite(v) else "—" for v in (p50, p200))
        else:
            breadth_label = "미확인" if valid else "산출불가"
        raw_date = r.get("as_of")
        as_of = str(raw_date).strip() if raw_date is not None else ""
        if as_of.lower() in ("", "nan", "nat", "none", "null"):
            as_of = "미확인"
        rows.append({
            "지수": "SP500 ex-NDX" if trk == "SP500" else trk,
            "레짐": status_labels[compass_regime(r)] + (" · 자료 확인" if r.get("regime_quality", "ok") != "ok" else ""),
            "50일/200일↑": breadth_label,
            "분배(25일)": f"{int(dist)}일" if np.isfinite(dist) and dist >= 0 else "—",
            "200일 이격": f"{gap200:+.1f}%" if np.isfinite(gap200) else "—",
            "가격 기준일": as_of[:10],
        })
    return pd.DataFrame(rows, columns=["지수", "레짐", "50일/200일↑", "분배(25일)", "200일 이격", "가격 기준일"])


def set_compass_focus(track: str) -> None:
    """Selection survives a widget rerun and is shared with the history chart."""
    st.session_state["mk_focus_trk"] = track


def render_regime_cards_and_breadth(regs: dict[str, Any], definitions: dict | None = None) -> None:
    """One compact index selector, all-market health matrix, and focused price strip.

    Breath/dispersion numbers are shown once in the comparison matrix, not
    repeated as progress bars and large KPI cards above and below it.
    """
    order = [k for k in TRACKS if isinstance(regs.get(k), dict)]
    if not order:
        st.info("게시된 지수별 레짐 자료가 없습니다.")
        return

    focused = st.session_state.get("mk_focus_trk")
    if focused not in order:
        focused = order[0]
        st.session_state["mk_focus_trk"] = focused

    statuses = {"ATTACK": "🟢 공략", "CAUTION": "🟡 관찰",
                "RISK_OFF": "🔴 방어", "UNKNOWN": "⚪ 미확인"}
    with st.container(border=True):
        st.markdown("##### 🚦 지수 선택 · 시장 내부 체력 · 핵심 가격")
        st.caption("지수를 선택하면 아래 가격 요약과 상세 이력이 바뀝니다. 내부 체력은 비교표에서 한 번만 보여줍니다.")
        cols = st.columns(len(order), gap="small")
        for trk, col in zip(order, cols):
            rec = regs[trk]
            with col:
                st.button("SP500 ex-NDX" if trk == "SP500" else trk,
                          key=f"mk_pick_{trk}", width="stretch",
                          type="primary" if trk == focused else "secondary",
                          on_click=set_compass_focus, args=(trk,))
                kind = compass_regime(rec)
                raw_date = str(rec.get("as_of") or "미확인").strip()
                date = "미확인" if raw_date.lower() in ("", "none", "nan", "nat", "null") else raw_date[:10]
                st.caption(f"{statuses[kind]} · {date}")

        frame = regime_comparison_frame(regs)
        st.dataframe(
            frame, hide_index=True, width="stretch", height=35 * len(frame) + 46,
            column_config={
                "지수": st.column_config.TextColumn("전략 트랙", width="medium"),
                "레짐": st.column_config.TextColumn("레짐", width="small"),
                "50일/200일↑": st.column_config.TextColumn(
                    "Breadth 50/200↑", width="medium",
                    help="전략용 모집단에서 50·200일 이동평균선 상회 종목 비율"),
                "분배(25일)": st.column_config.TextColumn("분배(25일)", width="small"),
                "200일 이격": st.column_config.TextColumn("200일 이격", width="small"),
                "가격 기준일": st.column_config.TextColumn("가격 기준일", width="medium"),
            },
        )
        focused = st.session_state["mk_focus_trk"]
        r = regs[focused]
        def number(field: str) -> float:
            x = pd.to_numeric(r.get(field), errors="coerce")
            return float(x) if pd.notna(x) and np.isfinite(x) else float("nan")

        close = number("benchmark_close")
        sma50 = number("benchmark_sma50")
        sma200 = number("benchmark_sma200")
        tier = r.get("tier_a_total")
        tier_text = str(tier) if r.get("breadth_valid") is not False and pd.notna(tier) else "미확인"
        basis = price_benchmark(focused, r)
        is_us = focused in ("NASDAQ100", "SP500")
        title = f"{basis} · 벤치마크 가격 요약" if is_us else f"{focused} · 선택 지수 가격 요약"
        st.markdown(f"**📌 {title}**")
        row = st.columns(4, gap="small")
        for col, (label, value) in zip(row, (
            (f"{basis} 종가" if is_us else "지수 종가", f"{close:,.2f}" if np.isfinite(close) else "—"),
            ("50일 이동평균", f"{sma50:,.2f}" if np.isfinite(sma50) else "—"),
            ("200일 이동평균", f"{sma200:,.2f}" if np.isfinite(sma200) else "—"),
            ("Tier A 후보", tier_text),
        )):
            with col:
                st.caption(label)
                st.markdown(f"**{value}**")
        near = str(r.get("nh_nl_label") or "미확인").replace(
            "신고", "고점 3% 근접").replace("신저", "저점 3% 근접")
        ftd_text = compass_ftd_text(r)
        if ftd_text.startswith("FTD 감지") and compass_regime(r) == "RISK_OFF":
            ftd_text += " · 방어 레짐과 신규 위험 예산 유지"
        st.caption(ftd_text)
        if r.get("regime_quality", "ok") != "ok":
            expected = r.get("expected_as_of")
            reference = f" · 기대 거래일 {expected}" if expected else ""
            st.warning(f"벤치마크 자료 확인 필요{reference} · 신규 모델 배분 보류")
        st.caption(f"52주 종가 {near} · 실제 신고가·신저가 돌파 건수가 아닙니다.")

    st.caption(
        "가격·분배일·FTD는 게시 벤치마크 기준입니다. 미국 기본값은 Nasdaq-100 추종 QQQ와 S&P 500 추종 SPY입니다. "
        "Breadth·종목 검색은 NASDAQ100과 S&P500 ex-NDX 모집단을 사용합니다."
    )
    missing = [t for t in order if regs[t].get("breadth_valid") is False]
    if missing:
        st.caption("⚠️ Breadth 산출불가: " + ", ".join(missing) + " · 0%나 약세로 해석하지 마세요.")
    if r.get("breadth_valid") is False:
        st.warning(f"{focused} Breadth 산출불가: {r.get('breadth_status') or '유효 표본 미확인'}")
    if compass_regime(r) == "UNKNOWN":
        st.warning("선택 지수의 레짐 신호가 미확인입니다. 임의로 방어·관찰로 분류하지 않습니다.")
    definitions = definitions or {}
    with st.expander("ⓘ Breadth 모집단 · 52주 고저점 산식", expanded=False):
        if r.get("breadth_valid_count") is not None and r.get("breadth_total") is not None:
            st.caption(f"{focused} 유효 표본 {r['breadth_valid_count']} / 기대 {r['breadth_total']}종목")
        st.markdown(
            "- **SP500:** " + definitions.get(
                "breadth_sp500",
                "S&P500 ex-NDX 전략용 표본이며 전체 S&P500 Breadth가 아닙니다.")
            + "\n- **NASDAQ100:** " + definitions.get(
                "breadth_nasdaq100", "NASDAQ100 전략용 Breadth")
            + "\n- **필터 조건:** " + definitions.get(
                "breadth_filters",
                "시총·최소 주가·유효 시세·이력 필터를 적용합니다. Breadth 모집단은 최종 후보와 다릅니다.")
            + "\n- **52주 고저점 근접:** " + definitions.get(
                "near_52w",
                "최근 최대 252거래일 종가 기준 3% 근접 개수입니다. 돌파 건수가 아닙니다.")
        )



def render_market_snapshot_and_history(b: Bundle) -> None:
    """Lazy two-row drill-down: three index comparisons, then one full-width history."""
    regs = (b.summ or {}).get("regimes") or {}
    order = [k for k in TRACKS if isinstance(regs.get(k), dict)]
    if not order:
        return

    st.session_state.setdefault("mk_detail_open", False)
    is_open = bool(st.session_state["mk_detail_open"])
    st.button("📈 상세 정보 닫기 ▲" if is_open else "📈 상세 정보 보기 · 추가 그래프 ▼",
                 key="mk_detail_button", width="stretch", on_click=st.session_state.update,
                 args=({"mk_detail_open": not is_open},))
    if not is_open:
        return

    # Keep the full identities in hover labels; shorten only the narrow x-axis.
    short_names = {"KOSPI": "KOSPI", "KOSDAQ": "KOSDAQ",
                   "NASDAQ100": "NDX100", "SP500": "SP ex-NDX"}
    axis_names = [short_names.get(t, t) for t in order]
    price_names = [price_benchmark(t, regs[t]) if t in ("NASDAQ100", "SP500") else t for t in order]
    st.markdown("##### 📊 4대 지수 비교 · Breadth / 이격도 / 분배일")
    st.caption("동일한 지수 순서로 시장 내부 체력·추세 이격·매도 압력을 비교합니다. 데이터 누락은 0으로 보정하지 않습니다.")
    col_breadth, col_distance, col_distdays = st.columns(3, gap="small")

    with col_breadth:
        with st.container(border=True):
            b_df = pd.DataFrame([
                {"트랙": t, **{label: valid_breadth_pct(regs[t], field)
                              for label, field in (("50일선 상회(%)", "pct_above_50dma"),
                                                   ("200일선 상회(%)", "pct_above_200dma"))}}
                for t in order
            ])
            fig_b = go.Figure()
            for field, label, color in (
                ("50일선 상회(%)", "50일↑", PALETTE["green_soft"]),
                ("200일선 상회(%)", "200일↑", PALETTE["blue_soft"]),
            ):
                values = b_df[field]
                fig_b.add_bar(
                    x=axis_names, y=values, name=label,
                    customdata=order,
                    marker=dict(color=color, cornerradius=3),
                    text=[f"{v:g}%" if np.isfinite(v) else "자료 없음" for v in values],
                    textposition="outside",
                    textfont=dict(size=9),
                    hovertemplate="<b>%{customdata}</b><br>" + label + ": %{y:.1f}%<extra></extra>",
                )
            fig_b.add_hline(y=45, line_dash="dot", line_color=PALETTE["green"])
            fig_b.add_hline(y=25, line_dash="dot", line_color=PALETTE["red"])
            fig_b.update_layout(barmode="group", yaxis=dict(range=[0, 111], ticksuffix="%"))
            fig_b = clean_chart(fig_b, height=290, legend=True, title="🌡️ Breadth 비교")
            fig_b.update_layout(margin=dict(l=22, r=5, t=70, b=30))
            fig_b.update_xaxes(tickfont=dict(size=9), automargin=True)
            show_plotly(fig_b, key="plt_breadth_bar")
            st.caption("점선: 공략 45% · 붕괴 경고 25% (전략 기준)")

    rows = []
    for t in order:
        r = regs[t]
        bc, s50, s200 = (finite_number(r.get(c)) for c in
                        ("benchmark_close", "benchmark_sma50", "benchmark_sma200"))
        rows.append({
            "트랙": t,
            "50일선 이격(%)": ((bc / s50 - 1.0) * 100.0) if s50 > 0 and np.isfinite(bc) else np.nan,
            "200일선 이격(%)": ((bc / s200 - 1.0) * 100.0) if s200 > 0 and np.isfinite(bc) else np.nan,
            "25일 분배일": finite_number(r.get("distribution_days_25d")),
        })
    sdf = pd.DataFrame(rows)

    with col_distance:
        with st.container(border=True):
            missing = sdf.loc[sdf[["50일선 이격(%)", "200일선 이격(%)"]].isna().any(axis=1), "트랙"]
            fig1 = go.Figure()
            for field, label, color in (
                ("50일선 이격(%)", "50일선", PALETTE["blue_soft"]),
                ("200일선 이격(%)", "200일선", PALETTE["purple"]),
            ):
                fig1.add_bar(
                    x=price_names, y=sdf[field], name=label, customdata=price_names,
                    marker=dict(color=color, cornerradius=3),
                    text=[f"{v:+.1f}%" if np.isfinite(v) else "자료 없음" for v in sdf[field]],
                    textposition="outside", textfont=dict(size=9),
                    hovertemplate="<b>%{customdata}</b><br>" + label + " 이격: %{y:+.1f}%<extra></extra>",
                )
            fig1.add_hline(y=0, line_dash="dash", line_color=PALETTE["slate"])
            fig1.update_layout(barmode="group", yaxis=dict(ticksuffix="%"))
            fig1 = clean_chart(fig1, height=290, legend=True, title="📏 50·200일선 이격도")
            fig1.update_layout(margin=dict(l=25, r=5, t=70, b=30))
            fig1.update_xaxes(tickfont=dict(size=9), automargin=True)
            show_plotly(fig1, key="plt_idx_dist")
            if not missing.empty:
                st.caption("이격 자료 없음: " + ", ".join(missing))
            else:
                st.caption("0% 위/아래 = 지수의 이동평균선 상회/하회")

    with col_distdays:
        with st.container(border=True):
            missing = sdf.loc[sdf["25일 분배일"].isna(), "트랙"]
            values = sdf["25일 분배일"]
            colors = [PALETTE["slate"] if not np.isfinite(v) else PALETTE["red"] if v >= 6
                      else (PALETTE["amber"] if v >= 3 else PALETTE["green"]) for v in values]
            fig2 = go.Figure(go.Bar(
                x=price_names, y=values, customdata=price_names,
                marker=dict(color=colors, cornerradius=3),
                text=[f"{int(v)}일" if np.isfinite(v) else "자료 없음" for v in values],
                textposition="outside", textfont=dict(size=10),
                hovertemplate="<b>%{customdata}</b><br>25일 누적 분배일: %{y:.0f}일<extra></extra>",
            ))
            fig2.add_hline(y=6, line_dash="dot", line_color=PALETTE["red"])
            fig2.add_hline(y=2, line_dash="dot", line_color=PALETTE["green"])
            upper = finite_number(values.max(), 0)
            fig2.update_layout(yaxis=dict(range=[0, max(8, int(upper) + 2)], dtick=2))
            fig2 = clean_chart(fig2, height=290, legend=False, title="📊 25일 누적 분배일")
            fig2.update_layout(margin=dict(l=22, r=5, t=53, b=30))
            fig2.update_xaxes(tickfont=dict(size=9), automargin=True)
            show_plotly(fig2, key="plt_idx_ddays")
            if not missing.empty:
                st.caption("분배일 자료 없음: " + ", ".join(missing))
            else:
                st.caption("점선: 공략 상한 2일 · 위험 임계 6일")

    st.caption("Breadth는 전략 모집단, 이격도와 분배일은 가격 벤치마크 기준입니다.")

    focused = st.session_state.get("mk_focus_trk", order[0])
    if focused not in order:
        focused = order[0]
    focused_label = price_benchmark(focused, regs[focused])
    st.markdown(f"##### 📈 {focused_label} · 지수 추세 및 레짐 시계열")

    history = b.h_regime
    required = {"track", "as_of", "benchmark_close", "benchmark_sma50", "benchmark_sma200"}
    if history.empty or not required.issubset(history.columns) or history["as_of"].nunique() < 2:
        st.info("저장된 실제 레짐 이력이 2개 날짜 미만이어서 추이 차트를 표시할 수 없습니다.")
        return
    h = history.loc[history["track"].eq(focused)].copy()
    if "benchmark" in h and regs[focused].get("benchmark"):
        basis = price_benchmark(focused, regs[focused])
        h = h.loc[h.apply(lambda row: price_benchmark(focused, row) == basis, axis=1)] if not h.empty else h
    if h["as_of"].nunique() < 2:
        st.info(f"{focused_label}의 기록이 2개 날짜 미만입니다.")
        return
    h = num(h, "benchmark_close", "benchmark_sma50", "benchmark_sma200").sort_values("as_of").reset_index(drop=True)
    x = h["as_of"]
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.80, 0.20], vertical_spacing=0.05)
    fig.add_trace(go.Scatter(x=x, y=h["benchmark_close"], name="벤치마크 종가", mode="lines+markers",
                             line=dict(width=2.2, color=get_chart_theme()["line_primary"])), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=h["benchmark_sma50"], name="50일선", mode="lines+markers",
                             line=dict(dash="dot", color=PALETTE["amber"])), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=h["benchmark_sma200"], name="200일선", mode="lines+markers",
                             line=dict(dash="dash", color=PALETTE["purple"])), row=1, col=1)
    kinds = h.apply(compass_regime, axis=1)
    colors = [PALETTE["green"] if k == "ATTACK" else PALETTE["red"] if k == "RISK_OFF"
              else PALETTE["amber"] if k == "CAUTION" else PALETTE["slate"] for k in kinds]
    fig.add_trace(go.Bar(x=x, y=[1] * len(h), marker_color=colors,
                         hovertemplate="%{x}: %{customdata}<extra></extra>",
                         customdata=kinds, showlegend=False), row=2, col=1)
    fig.update_yaxes(visible=False, row=2, col=1)
    fig.update_xaxes(type="category", nticks=8)
    with st.container(border=True):
        show_plotly(clean_chart(fig, 345, title=f"{focused_label} 지수·50/200일선 & 실제 레짐 이력"),
                    key=f"plt_reg_hist_{focused}")
    st.caption("이력은 실제 게시된 날짜만 표시합니다. 가격 기준일이 다른 트랙의 이력을 혼합하지 않습니다.")



def render_changes(b: Bundle) -> None:
    mat, dates = list_matrix(b.h_cand)
    if len(dates) < 2:
        st.info(
            f"📈 **신규 진입 · 이탈 · 연속 체류 비교**는 후보 이력이 2일 이상 쌓이면 자동으로 활성화됩니다 (현재 **{len(dates)}일** 기록됨). "
            "오늘 선발된 리더 종목과 멀티 차트는 **🎯 3. 전술·멀티차트** 탭에서 바로 확인하실 수 있습니다."
        )
        return
    last, prev = dates[-1], dates[-2]
    cur, old = latest_rows(b.h_cand, last), latest_rows(b.h_cand, prev)
    in_last, in_prev = mat[last], mat[prev]
    streak = streak_ending_last(mat)
    new_keys = mat.index[in_last & ~in_prev]
    gone_keys = mat.index[~in_last & in_prev]
    persist = streak[in_last].sort_values(ascending=False)

    m = st.columns(3)
    m[0].metric(f"🆕 신규 진입 ({prev} → {last})", f"{len(new_keys)}종목", border=True)
    m[1].metric("👋 워치리스트 이탈", f"{len(gone_keys)}종목", border=True)
    m[2].metric("🔁 3일 이상 연속 체류", f"{int((persist >= 3).sum())}종목", border=True)

    def show(keys: Any, src: pd.DataFrame, title: str, extra_streak: bool = False) -> None:
        if len(keys) == 0:
            st.caption(f"{title}: 해당 종목이 없습니다.")
            return
        d = src.loc[[k for k in keys if k in src.index]].copy()
        if extra_streak:
            d["streak"] = [int(streak.get(k, 0)) for k in d.index]
        d = num(d, "rs_score", "topdown_score")
        cols = [c for c in ("name", "ticker", "sub_market", "sector", "setup_stage", "tier", "rs_score", "topdown_score", "streak") if c in d.columns]
        stock_dataframe(d[cols].rename(columns=LABELS), hide_index=True, width="stretch")

    t1, t2, t3, t4 = st.tabs(["🆕 신규 진입", "👋 이탈 종목", "🔁 연속 체류 상위", "🚀 RS 급등 (+5↑)"])
    with t1:
        show(new_keys, cur, "신규 진입 종목")
    with t2:
        show(gone_keys, old, "이탈 종목 (전일 기준 정보)")
    with t3:
        show(persist.head(15).index, cur, "연속 체류 상위", extra_streak=True)
    with t4:
        both = [k for k in cur.index if k in old.index]
        if both:
            d = cur.loc[both].copy()
            d["rs_prev"] = pd.to_numeric(old.loc[both, "rs_score"], errors="coerce")
            d["rs_now"] = pd.to_numeric(d["rs_score"], errors="coerce")
            d["rs_jump"] = d["rs_now"] - d["rs_prev"]
            jump = d[d["rs_jump"] >= 5].sort_values("rs_jump", ascending=False)
            if jump.empty:
                st.caption("직전 기록일 대비 RS +5 이상 급등한 종목이 없습니다.")
            else:
                stock_dataframe(
                    jump[["name", "ticker", "sub_market", "sector", "rs_prev", "rs_now", "rs_jump"]].rename(
                        columns={**LABELS, "rs_prev": "RS(전)", "rs_now": "RS(현)", "rs_jump": "Δ"}
                    ),
                    hide_index=True, width="stretch",
                )


def tab_compass(b: Bundle) -> None:
    summ = b.summ
    regs, port, cnt = summ.get("regimes") or {}, summ.get("portfolio") or {}, summ.get("counts") or {}
    top = summ.get("top_theme") or {}

    st.markdown("### 🧭 시장 국면 → 신규 배분 나침반")
    st.caption("지수별 국면·내부 체력·핵심 가격을 한눈에 비교하고 신규 배분·리스크 예산을 확인합니다.")
    render_regime_cards_and_breadth(regs, summ.get("metric_definitions"))
    render_market_snapshot_and_history(b)

    st.markdown("#### 💼 신규 배분 · 리스크 관리")
    with st.container(border=True):
        defaults = {"held_confirmed": False, "held_kr_pct": 0, "held_us_pct": 0}
        if "_held_inputs" not in st.session_state:
            # A distinct container survives cleanup of widgets from older versions.
            st.session_state["_held_inputs"] = {field: st.session_state.get(field, default) for field, default in defaults.items()}
        for field, default in defaults.items():
            st.session_state.setdefault("_" + field, st.session_state["_held_inputs"].get(field, default))
        held_on = st.toggle("💼 현재 보유 비중을 신규 한도 계산에 반영", key="_held_confirmed",
                            on_change=remember_held_input, args=("held_confirmed",),
                            help="활성화하면 KR·US 현재 보유 비중을 레짐 상한에서 차감합니다.")
        with st.expander("⚙️ 보유 비중 입력·한도 안내", expanded=held_on):
            hc1, hc2 = st.columns(2)
            hc1.slider("🇰🇷 KR 현재 보유 비중 (%)", 0, 100, step=5, key="_held_kr_pct",
                       disabled=not held_on, on_change=remember_held_input, args=("held_kr_pct",))
            hc2.slider("🇺🇸 US 현재 보유 비중 (%)", 0, 100, step=5, key="_held_us_pct",
                       disabled=not held_on, on_change=remember_held_input, args=("held_us_pct",))
            st.caption("보유 비중 반영 시 남은 신규 한도 = 레짐 노출 한도 − 현재 보유 비중. 개인 보유 정보는 이 세션의 표시·배분에만 반영됩니다.")
            guide = str(summ.get("exposure_guide") or "자료 없음").replace("신규 아이디어 한도", "전체 시장 참고 범위")
            st.caption(f"시장 대응 가이드 · {guide}")
        rem_info = compute_remaining_portfolio(b)
        if held_on:
            st.caption(
                f"💼 보유 KR {number_text(rem_info['held_kr'], 0)}% · US {number_text(rem_info['held_us'], 0)}%"
                f"  |  **신규 잔여 한도 KR {number_text(rem_info['rem_kr_cap'], 0)}% · US {number_text(rem_info['rem_us_cap'], 0)}%**"
                " · 현재 보유 비중 반영"
            )
        else:
            st.caption(
                f"📌 **신규 아이디어 한도 KR 최대 {number_text(rem_info['kr_cap'], 0)}% · US 최대 {number_text(rem_info['us_cap'], 0)}%**"
                " · 보유 비중 미반영 (가상 신규 배분 한도)"
            )

        st.caption(f"모델 합산 노출 상한 {number_text(port.get('exposure_cap_pct'), 0)}% · 국가별 한도와 함께 적용")
        st.caption(f"🔥 **최강 주도 테마** — 🇰🇷 KR: **{top.get('KR') or '-'}** | 🇺🇸 US: **{top.get('US') or '-'}**")
        alloc_pos_cnt = int((pd.to_numeric(allocation_view(b.watch, rem_info).get("portfolio_alloc_pct", pd.Series(dtype=float)), errors="coerce").fillna(0) > 0).sum()) if not b.watch.empty else 0
        c = st.columns(4)
        c[0].metric("🎯 셋업 적격", f"{cnt.get('actionable', 0)}종목", f"가상 배분 {alloc_pos_cnt}종목 · 전체 선정 {cnt.get('selected', 0)}", delta_color="off", border=True, help="정배열(Tier A)·바이존·손절폭(≤8%)·청산신호 정상·실적 D-3 초과를 모두 만족한 종목 수입니다.")
        c[1].metric("👑 주도섹터 액션", f"{cnt.get('leading_actionable', 0)}종목", "최우선 관찰군", delta_color="off", border=True, help=TOOLTIPS["sector_power"])
        c[2].metric("💼 신규 가상 배분", f"KR {number_text(rem_info['adj_kr_alloc'], 1)}% · US {number_text(rem_info['adj_us_alloc'], 1)}%", f"잔여 한도 KR {number_text(rem_info['rem_kr_cap'], 0)}% / US {number_text(rem_info['rem_us_cap'], 0)}%", delta_color="off", border=True, help=TOOLTIPS["portfolio_alloc_pct"])
        c[3].metric("🛡️ 신규 손절 리스크", f"{number_text(rem_info['adj_total_risk'], 2)}%", f"총 위험 예산 {number_text(port.get('risk_cap_pct'))}%", delta_color="off", border=True, help=TOOLTIPS["stop_dist_pct"])

    st.markdown("##### 📊 신규 배분 · 리스크 · 섹터 집중도")
    render_portfolio_visuals(b, rem_info)
    st.caption("배분·리스크는 게시본의 신규 후보에 대한 가상 수치이며, 보유 비중을 켠 경우에만 입력한 보유 비중을 차감해 종목별 신규 비중을 비례 조정합니다. 기존 보유 종목의 손절 위험은 미포함입니다.")

    st.divider()
    st.subheader("🔄 오늘의 워치리스트 변화")
    render_changes(b)


# ════════════════════════════════════════════════════════════════════
# 5. 탭 2: 🏭 주도 섹터
# ════════════════════════════════════════════════════════════════════
def sector_key(row: Any, lvl: str) -> tuple[str, str]:
    macro = row.get("macro_theme", "")
    sector = row.get("sector", "") if lvl == "sector" else ""
    return ("" if pd.isna(macro) else str(macro), "" if pd.isna(sector) else str(sector))


def sector_rotation_history(history: pd.DataFrame, current: pd.DataFrame, market: str,
                            lvl: str, analysis_date: str | None, periods: int = 5) -> pd.DataFrame:
    """Use recorded analysis dates, not reconstructed trading-day coordinates."""
    columns = ["date", "macro_theme", "sector", "median_rs", "rs_delta_4w", "_key"]
    required = {"date", "market", "level", "macro_theme", "median_rs", "rs_delta_4w"}
    if (history.empty or not required.issubset(history.columns)
            or (lvl == "sector" and "sector" not in history.columns)):
        return pd.DataFrame(columns=columns)
    if not isinstance(analysis_date, (str, datetime, pd.Timestamp)):
        return pd.DataFrame(columns=columns)
    cutoff = pd.to_datetime(analysis_date, errors="coerce", utc=True)
    if not isinstance(cutoff, pd.Timestamp) or pd.isna(cutoff):
        return pd.DataFrame(columns=columns)
    cutoff = cutoff.tz_convert(KST).tz_localize(None).normalize()
    h = history[(history["market"] == market) & (history["level"] == lvl)].copy()
    h["date"] = pd.to_datetime(h["date"], errors="coerce", utc=True).dt.tz_localize(None).dt.normalize()
    h = h[h["date"] <= cutoff]
    if "sector" not in h:
        h["sector"] = ""
    latest = current.copy()
    latest["date"] = cutoff
    if "sector" not in latest:
        latest["sector"] = ""
    h = pd.concat([h, latest], ignore_index=True)
    h = num(h, "median_rs", "rs_delta_4w")
    h = h[np.isfinite(h["median_rs"]) & np.isfinite(h["rs_delta_4w"])].copy()
    h["_key"] = [sector_key(row, lvl) for _, row in h.iterrows()]
    current_keys = {sector_key(row, lvl) for _, row in current.iterrows()}
    h = h[h["_key"].isin(current_keys)].drop_duplicates(["date", "_key"], keep="last")
    dates = sorted(h["date"].dropna().unique())[-max(1, int(periods)):]
    return h[h["date"].isin(dates)][columns].sort_values("date", kind="stable").reset_index(drop=True)


def sector_label_lines(value: Any, width: float = 10) -> list[str]:
    """Wrap full names at words/separators; never discard a sector's name."""
    def units(text):
        return sum(1.0 if unicodedata.east_asian_width(c) in ('W', 'F') else 0.58 for c in text)
    lines, line = [], ''
    for token in re.findall(r'[^·\s]+|[·\s]+', str(value).strip()):
        if line and units(line + token) > width:
            lines.append(line.strip())
            line = ''
        if units(token) > width:
            for char in token:
                if line and units(line + char) > width:
                    lines.append(line.strip())
                    line = ''
                line += char
        else:
            line += token
    if line.strip():
        lines.append(line.strip())
    return [line for line in lines if line] or ['-']


def sector_chart_height(count: int) -> int:
    return max(900, min(1400, 700 + 5 * count))


def _rotation_labels(fig: go.Figure, rows: pd.DataFrame, lbl_col: str,
                     x_range: tuple[float, float], y_range: tuple[float, float],
                     height: int, font_size: int = 10, numbered: bool = False) -> None:
    """Fit every label in two dimensions; focus selection only controls trails."""
    theme = get_chart_theme()
    width, height = 1112, height - 126
    occupied, cells = [], {}
    def cell_keys(box):
        return [(x, y) for x in range(int(box[0] // 64), int(box[2] // 64) + 1)
                for y in range(int(box[1] // 32), int(box[3] // 32) + 1)]
    def reserve(box):
        occupied.append(box)
        for key in cell_keys(box):
            cells.setdefault(key, set()).add(len(occupied) - 1)
    def overlap(box):
        nearby = set().union(*(cells.get(key, set()) for key in cell_keys(box)))
        return sum(max(0, min(box[2], occupied[i][2]) - max(box[0], occupied[i][0]))
                   * max(0, min(box[3], occupied[i][3]) - max(box[1], occupied[i][1])) for i in nearby)
    for x in (0, width - 140):
        for y in (0, height - 28):
            reserve((x, y, x + 140, y + 28))
    points = []
    for _, row in rows.iterrows():
        px = (float(row["median_rs"]) - x_range[0]) / (x_range[1] - x_range[0]) * width
        py = (y_range[1] - float(row["rs_delta_4w"])) / (y_range[1] - y_range[0]) * height
        points.append((px, py))
        reserve((px - 18, py - 18, px + 18, py + 18))
    for index, ((_, row), (px, py)) in enumerate(zip(rows.iterrows(), points), 1):
        parts = [str(index)] if numbered else sector_label_lines(row[lbl_col])
        text = "<br>".join(html.escape(p) for p in parts)
        half_w = max(10, max(sum(1.0 if unicodedata.east_asian_width(c) in ('W', 'F') else 0.58
                                for c in part) for part in parts) * font_size / 2 + 5)
        half_h = len(parts) * font_size * 0.65 + 5
        candidates = [(px + radius * math.cos(angle), py + radius * math.sin(angle))
                      for radius in (28, 48, 76, 112, 164, 232)
                      for angle in np.linspace(-math.pi / 2, 3 * math.pi / 2, 12, endpoint=False)]
        grid = [(x, y) for x in np.arange(half_w + 4, width - half_w, 2 * half_w + 6)
                for y in np.arange(half_h + 4, height - half_h, 2 * half_h + 6)]
        candidates += sorted(grid, key=lambda p: (p[0] - px) ** 2 + (p[1] - py) ** 2)
        best = None
        for cx, cy in candidates:
            cx = min(width - half_w - 3, max(half_w + 3, cx))
            cy = min(height - half_h - 3, max(half_h + 3, cy))
            box = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
            cost = overlap(box)
            candidate = (cost, (cx - px) ** 2 + (cy - py) ** 2, cx, cy, box)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
            if cost == 0:
                break
        _, _, cx, cy, box = best
        reserve(box)
        leader = math.hypot(cx-px, cy-py) > 40
        label_x = x_range[0] + cx / width * (x_range[1] - x_range[0])
        label_y = y_range[1] - cy / height * (y_range[1] - y_range[0])
        fig.add_annotation(name="sector_label", x=row["median_rs"] if leader else label_x,
                           y=row["rs_delta_4w"] if leader else label_y, showarrow=leader,
                           text=text, ax=x_range[0] + cx / width * (x_range[1] - x_range[0]),
                           ay=y_range[1] - cy / height * (y_range[1] - y_range[0]),
                           axref="x", ayref="y", xanchor="center", yanchor="middle",
                           arrowhead=0, arrowcolor=theme["subtext"], arrowwidth=0.8,
                           bgcolor="rgba(15,23,42,0.55)" if theme["mode"] == "dark" else "rgba(255,255,255,0.55)", borderpad=1,
                           font=dict(size=font_size, color=theme["text"]))


def sector_rotation_quadrant(d: pd.DataFrame, lvl: str, history: pd.DataFrame | None = None,
                             focus_keys: list[tuple[str, str]] | None = None,
                             font_size: int = 10, numbered: bool = False, size_mode: str = "pass_rate",
                             trail_mode: str = "simple", compact_labels: bool = False) -> go.Figure:
    required = {"median_rs", "rs_delta_4w"}
    if d is None or d.empty or not required.issubset(d.columns):
        return go.Figure()
    df = num(d, "median_rs", "rs_delta_4w", "sector_power", "passed_cnt", "pct_above_50dma")
    df = df[np.isfinite(df["median_rs"]) & np.isfinite(df["rs_delta_4w"])].copy()
    if df.empty:
        return go.Figure()
    for col, default in (("sector_power", np.nan), ("passed_cnt", np.nan), ("leader_name", "확인 불가")):
        if col not in df.columns:
            df[col] = default
    lbl_col = "sector" if lvl == "sector" and "sector" in df.columns else ("macro_theme" if "macro_theme" in df.columns else None)
    if lbl_col is None:
        df["_label"] = "-"
        lbl_col = "_label"
    df["_key"] = [sector_key(row, lvl) for _, row in df.iterrows()]
    focus_keys = list(df.sort_values("sector_power", ascending=False)["_key"].head(3)) if focus_keys is None else focus_keys
    focused = df[df["_key"].isin(focus_keys)].head(8)
    effective_trail_mode(trail_mode, len(focused))
    trails = history[history["_key"].isin(focused["_key"])] if history is not None and not history.empty else pd.DataFrame()
    bounds = pd.concat([df, trails], ignore_index=True) if not trails.empty else df
    x_min, x_max = min(40.0, float(bounds["median_rs"].min()) - 8), max(100.0, float(bounds["median_rs"].max()) + 8)
    y_abs = max(10.0, float(bounds["rs_delta_4w"].abs().max()) * 1.15 + 3)
    y_min, y_max = -y_abs, y_abs
    fig = go.Figure()

    fig.add_shape(type="rect", x0=70, x1=x_max, y0=0, y1=y_max, fillcolor=PALETTE["green"], opacity=0.07, line_width=0)
    fig.add_shape(type="rect", x0=x_min, x1=70, y0=0, y1=y_max, fillcolor=PALETTE["blue"], opacity=0.06, line_width=0)
    fig.add_shape(type="rect", x0=70, x1=x_max, y0=y_min, y1=0, fillcolor=PALETTE["amber"], opacity=0.07, line_width=0)
    fig.add_shape(type="rect", x0=x_min, x1=70, y0=y_min, y1=0, fillcolor=PALETTE["slate"], opacity=0.05, line_width=0)

    fig.add_vline(x=70, line_dash="dash", line_color="#64748B", line_width=1.2)
    fig.add_hline(y=0, line_dash="dash", line_color="#64748B", line_width=1.2)

    fig.add_annotation(x=x_max - 1, y=y_max - 1, text="<b>🚀 강세가속</b>", showarrow=False, xanchor="right", yanchor="top", font=dict(color=PALETTE["green"], size=12))
    fig.add_annotation(x=x_min + 1, y=y_max - 1, text="<b>🔥 신규부상</b>", showarrow=False, xanchor="left", yanchor="top", font=dict(color=PALETTE["blue"], size=12))
    fig.add_annotation(x=x_max - 1, y=y_min + 1, text="<b>⚠️ 탄력둔화</b>", showarrow=False, xanchor="right", yanchor="bottom", font=dict(color=PALETTE["amber"], size=12))
    fig.add_annotation(x=x_min + 1, y=y_min + 1, text="<b>🥶 소외·약세</b>", showarrow=False, xanchor="left", yanchor="bottom", font=dict(color=PALETTE["slate"], size=12))

    metrics = rotation_marker_metrics(df)
    sz = calculate_rotation_marker_sizes(df, size_mode)
    height = round(sector_chart_height(len(df)) * max(1, font_size / 10))
    theme = get_chart_theme()
    trail_meta = add_rotation_trails(fig, focused, trails, lbl_col, sz, theme, trail_mode,
                                     (x_min, x_max), (y_min, y_max), height)

    line_width, line_color = sample_outlines(metrics["sample_status"])
    def hover_num(value, suffix="", signed=False):
        value = pd.to_numeric(value, errors="coerce")
        return (f"{value:+.1f}" if signed else f"{value:g}") + suffix if pd.notna(value) and np.isfinite(value) else "확인 불가"
    custom = []
    for (_, row), (_, metric) in zip(df.iterrows(), metrics.iterrows()):
        leader = row.get("top_leader") if lvl == "macro" else row.get("leader_name")
        if pd.isna(leader) or not str(leader).strip(): leader = "확인 불가"
        custom.append([hover_num(row.get("sector_power")), hover_num(metric["univ_total"], "개"),
                       hover_num(metric["passed_cnt"], "개"), hover_num(metric["pass_rate_pct"], "%"),
                       hover_num(row.get("pct_above_50dma"), "%"), hover_num(row.get("actionable_cnt"), "개"),
                       html.escape(str(leader)), metric["sample_status"], metric["rate_source"]])
    fig.add_trace(go.Scatter(
        x=df["median_rs"], y=df["rs_delta_4w"], mode="markers", name="현재 섹터",
        text=df[lbl_col],
        marker=dict(
            size=sz, sizemode="diameter", color=df["sector_power"], colorscale=SOFT_POWER_SCALE, cmin=35, cmax=88,
            showscale=True, colorbar=dict(title=dict(text="파워", font=dict(color=theme["text"])), tickfont=dict(color=theme["axis"]), thickness=12, len=0.8),
            line=dict(width=line_width, color=line_color), opacity=0.92,
        ),
        customdata=custom,
        meta=dict(kind="rotation_points", identities=[list(key) for key in df["_key"]]),
        hovertemplate="<b>%{text}</b><br>중앙 RS: %{x:.1f}<br>RS 4주 변화: %{y:+.1f}점<br>섹터 Power: %{customdata[0]}<br>전체 종목: %{customdata[1]}<br>스크리닝 통과: %{customdata[2]}<br>통과 비율: %{customdata[3]}<br>50일선 상회율: %{customdata[4]}<br>Actionable: %{customdata[5]}<br>대표 종목: %{customdata[6]}<br>표본 상태: %{customdata[7]}<br>통과율 근거: %{customdata[8]}<extra></extra>",
        showlegend=False,
    ))
    label_rows = df
    if compact_labels and not numbered:
        top_keys = df.sort_values("sector_power", ascending=False)["_key"].head(5)
        label_rows = pd.concat([focused, df[df["_key"].isin(top_keys)]]).drop_duplicates("_key")
    _rotation_labels(fig, label_rows, lbl_col, (x_min, x_max), (y_min, y_max), height, font_size, numbered)
    clean_chart(fig, height, legend=False, title="섹터 로테이션")
    has_paths = trail_meta["rotation_path_count"] > 0 and trail_meta["trail_mode"] != "simple"
    fig.update_layout(margin=dict(l=68, r=60, t=62, b=150 if has_paths else 64),
                      showlegend=has_paths,
                      legend=dict(y=-0.16, yanchor="top", x=0, xanchor="left", orientation="h",
                                  title=dict(text="경로 식별 · 원 채움색은 Power"), font=dict(size=10)),
                      meta=dict(**trail_meta, sector_count=len(df), size_mode=size_mode,
                                sample_counts=metrics["sample_status"].value_counts().to_dict(),
                                rate_mismatch_count=int(metrics["rate_mismatch"].sum()),
                                rate_unknown_count=int(metrics["pass_rate_pct"].isna().sum())))
    fig.update_xaxes(title="중앙 RS 점수 (해당 시장 계산 모집단 백분위)", range=[x_min, x_max], nticks=6, automargin=True)
    fig.update_yaxes(title="RS 4주 변화량", range=[y_min, y_max], nticks=7, automargin=True)
    return fig



def rotation_mini_shared_bounds(selected: pd.DataFrame, history: pd.DataFrame | None
                                ) -> tuple[tuple[float, float], tuple[float, float]]:
    """One common, finite 4-quadrant scale for all selected mini charts."""
    coords = [selected[["median_rs", "rs_delta_4w"]]]
    if history is not None and not history.empty:
        coords.append(history[["median_rs", "rs_delta_4w"]])
    all_coords = pd.concat(coords, ignore_index=True)
    all_coords = num(all_coords, "median_rs", "rs_delta_4w")
    all_coords = all_coords[np.isfinite(all_coords["median_rs"]) & np.isfinite(all_coords["rs_delta_4w"])]
    if all_coords.empty:
        return (64., 76.), (-8., 8.)
    x_lo = min(70., float(all_coords["median_rs"].min())) - 6.
    x_hi = max(70., float(all_coords["median_rs"].max())) + 6.
    y_abs = max(8., float(all_coords["rs_delta_4w"].abs().max()) * 1.17 + 2.)
    return (x_lo, x_hi), (-y_abs, y_abs)


def sector_rotation_mini_figure(row: pd.Series, tail: pd.DataFrame,
                                x_range: tuple[float, float], y_range: tuple[float, float],
                                color: str, columns: int = 2) -> go.Figure:
    """Small-multiple of a single sector's real recorded observations.

    Source coordinates are never interpolated. A single or stationary
    observation displays the current dot, not a fabricated trajectory.
    """
    h = tail.copy()
    if not h.empty:
        h["date"] = pd.to_datetime(h["date"], errors="coerce")
        h = num(h, "median_rs", "rs_delta_4w")
        h = h.dropna(subset=["date", "median_rs", "rs_delta_4w"])
        h = h[np.isfinite(h["median_rs"]) & np.isfinite(h["rs_delta_4w"])]
        h = h.sort_values("date", kind="stable").drop_duplicates("date", keep="last")
    has_path = len(h) >= 2 and len(h[["median_rs", "rs_delta_4w"]].drop_duplicates()) >= 2
    fig = go.Figure()
    x0, x1 = x_range
    y0, y1 = y_range
    # Quadrant backgrounds extend beyond the nominal bounds. With a real
    # 1:1 pixel scale the view ranges can expand on narrow cards, but the
    # coloured quadrants should still cover the entire plotting area.
    for left, right, bottom, top, shade in (
        (70, 1000, 0, 1000, PALETTE["green"]),
        (-1000, 70, 0, 1000, PALETTE["blue"]),
        (70, 1000, -1000, 0, PALETTE["amber"]),
        (-1000, 70, -1000, 0, PALETTE["slate"]),
    ):
        if right > left and top > bottom:
            fig.add_shape(type="rect", x0=left, x1=right, y0=bottom, y1=top,
                          fillcolor=shade, opacity=.055, line_width=0)
    fig.add_vline(x=70, line_color="#64748B", line_dash="dot", line_width=1)
    fig.add_hline(y=0, line_color="#64748B", line_dash="dot", line_width=1)

    if has_path:
        history_dates = h["date"].dt.strftime("%Y-%m-%d").tolist()
        fig.add_trace(go.Scatter(
            x=h["median_rs"], y=h["rs_delta_4w"], mode="lines+markers",
            line=dict(color=color, width=1.9), marker=dict(color=color, size=5),
            customdata=history_dates, showlegend=False,
            meta=dict(kind="rotation_mini_history"),
            hovertemplate="분석일: %{customdata}<br>중앙 RS %{x:.1f} · 4주Δ %{y:+.1f}<extra></extra>",
        ))
        first = h.iloc[0]
        fig.add_trace(go.Scatter(
            x=[first["median_rs"]], y=[first["rs_delta_4w"]], mode="markers",
            marker=dict(symbol="circle-open", color=color, size=11, line=dict(width=2)),
            hovertemplate="첫 관측: " + history_dates[0] + "<extra></extra>",
            showlegend=False, meta=dict(kind="rotation_mini_start"),
        ))
        previous, latest = h.iloc[-2], h.iloc[-1]
        if (float(previous["median_rs"]) != float(latest["median_rs"])
                or float(previous["rs_delta_4w"]) != float(latest["rs_delta_4w"])):
            fig.add_trace(go.Scatter(
                x=[previous["median_rs"], latest["median_rs"]],
                y=[previous["rs_delta_4w"], latest["rs_delta_4w"]],
                mode="lines", line=dict(color=color, width=3.5),
                hoverinfo="skip", showlegend=False,
                meta=dict(kind="rotation_mini_recent"),
            ))

    current_x = pd.to_numeric(row.get("median_rs"), errors="coerce")
    current_y = pd.to_numeric(row.get("rs_delta_4w"), errors="coerce")
    phase = row.get("rotation_phase", "확인 불가")
    phase = "확인 불가" if pd.isna(phase) or not str(phase).strip() else str(phase)
    current_hover = f"<b>{html.escape(phase)}</b><br>현재 중앙 RS %{{x:.1f}}<br>RS 4주Δ %{{y:+.1f}}"
    if len(h) >= 2 and np.isfinite(current_x) and np.isfinite(current_y):
        previous = h.iloc[-2]
        current_hover += (
            f"<br>직전 관측 대비 RS {float(current_x) - float(previous['median_rs']):+.1f}"
            f" · 4주Δ {float(current_y) - float(previous['rs_delta_4w']):+.1f}"
        )
    current_hover += "<extra></extra>"
    if np.isfinite(current_x) and np.isfinite(current_y):
        fig.add_trace(go.Scatter(
            x=[float(current_x)], y=[float(current_y)],
            mode="markers+text" if columns <= 5 else "markers",
            text=["T0"] if columns <= 5 else None, textposition="top right",
            textfont=dict(size=10, color=color),
            marker=dict(color=color, size=11 if columns <= 5 else 8,
                        line=dict(width=1.2, color="#FFFFFF")),
            showlegend=False, meta=dict(kind="rotation_mini_current"),
            hovertemplate=current_hover,
        ))
    # Heights are fitted to the available column width; keep dense 6–8-column
    # cards readable by removing decorative axis titles and reducing margins.
    # The figure remains responsive via st.plotly_chart(width="stretch").
    heights = (660, 450, 345, 295, 258, 228, 207, 192)
    col_index = max(1, min(8, int(columns))) - 1
    narrow = columns >= 5
    tiny = columns >= 7
    clean_chart(fig, height=heights[col_index], legend=False)
    fig.update_layout(
        margin=dict(l=34 if columns <= 3 else (24 if columns <= 5 else 8),
                    r=5, t=4, b=30 if columns <= 3 else (22 if columns <= 5 else 7)),
        hovermode="closest",
        meta=dict(rotation_mini_history_points=len(h), rotation_mini_has_path=has_path,
                  rotation_mini_columns=int(columns)),
    )
    # Fill the plot domain instead of shrinking it. Plotly expands the numeric
    # range as required while still enforcing exact 1px:1px unit scaling.
    # Every card has identical nominal bounds and geometry for comparison.
    fig.update_xaxes(
        range=x_range, title="중앙 RS" if columns <= 2 else None,
        nticks=4 if narrow else 5, automargin=False,
        showticklabels=not tiny, constrain="range",
    )
    fig.update_yaxes(
        range=y_range, title="4주Δ" if columns <= 2 else None,
        nticks=4 if narrow else 5, automargin=False,
        showticklabels=not tiny, scaleanchor="x", scaleratio=1, constrain="range",
    )
    return fig


def sector_treemap(sec_df: pd.DataFrame, mk: str, details: bool = False, equal_size: bool = True) -> go.Figure | None:
    d = sec_df[(sec_df["market"] == mk) & (sec_df["level"] == "sector")].copy()
    if d.empty:
        return None
    d = num(d, "sector_power", "passed_cnt", "univ_total", "median_rs")
    d["size_val"] = d["passed_cnt"].fillna(0).clip(lower=1)
    d["macro_theme"] = d["macro_theme"].fillna("기타")
    d["sector"] = d["sector"].fillna("기타")
    d["leader_name"] = d["leader_name"].fillna("-")

    fig = px.treemap(
        d, path=[px.Constant(f"{mk} 전체 밸류체인"), "macro_theme", "sector"],
        values="size_val", color="sector_power",
        color_continuous_scale=SOFT_POWER_SCALE, range_color=[35, 88],
        custom_data=["sector_power", "median_rs", "passed_cnt", "leader_name"],
    )
    theme = get_chart_theme()
    trace = fig.data[0]
    parents = set(trace.parents)
    if equal_size:
        # Preserve existing power colors; only change the displayed cell areas.
        children = {}
        for node, parent in zip(trace.ids, trace.parents):
            children.setdefault(parent, []).append(node)
        def weight(node):
            return sum(weight(child) for child in children[node]) if node in children else 1
        trace.values = [weight(node) for node in trace.ids]
    colors = []
    for value in trace.marker.colors:
        fraction = float(np.clip((float(value) - 35) / 53, 0, 1)) if pd.notna(value) else 0.5
        rgb = [float(v) for v in re.findall(r"[\d.]+", px.colors.sample_colorscale(SOFT_POWER_SCALE, [fraction])[0])]
        colors.append("#0F172A" if sum(a * b for a, b in zip(rgb, (0.2126, 0.7152, 0.0722))) > 145 else "#FFFFFF")
    parent_hover = "<b>%{label}</b><br>클릭하면 하위 섹터를 확대합니다.<extra></extra>"
    leaf_hover = "<b>%{label}</b><br>섹터파워: %{customdata[0]:.1f}<br>중앙 RS: %{customdata[1]:.1f}<br>통과 종목: %{customdata[2]:.0f}개<br>대장주: %{customdata[3]}<extra></extra>"
    fig.update_traces(
        text=["<br>".join(html.escape(part) for part in sector_label_lines(label)) for label in trace.labels],
        texttemplate=["<b>%{text}</b><br>파워 %{customdata[0]:.0f} · 통과 %{customdata[2]:.0f}<br>%{customdata[3]}"
                      if details and node not in parents else
                      ("<b>%{text}</b><br>P %{customdata[0]:.0f} · %{customdata[2]:.0f}종목" if node not in parents else "<b>%{text}</b>")
                      for node in trace.ids],
        textfont=dict(size=12, color=colors), textposition="middle center",
        hovertemplate=[parent_hover if node in parents else leaf_hover for node in trace.ids],
        tiling=dict(pad=5), pathbar=dict(thickness=28),
        marker=dict(cornerradius=5, line=dict(width=1.5, color="#1E293B" if is_dark_mode() else "#FFFFFF")),
    )
    clean_chart(fig, sector_chart_height(len(d)), legend=False, title="대테마 · 세부 섹터")
    fig.update_layout(uniformtext=dict(mode=False), margin=dict(l=8, r=48, t=52, b=12),
                      coloraxis_colorbar=dict(title=dict(text="파워", font=dict(color=theme["text"])),
                                             tickfont=dict(color=theme["axis"]), thickness=12, len=0.8))
    return fig


def show_sector_plot(fig: go.Figure, key: str) -> None:
    """Native Streamlit Plotly charts with no custom iframe or hover overlays."""
    with st.container(horizontal_alignment="center"):
        with st.container(width=1240):
            show_plotly(fig, key)



def tab_sectors(b: Bundle) -> None:
    render_sector_review(b.sector_review,b.sector_review_source)
    st.caption("※ Leading은 상대강도 분류이지 매수 적격이 아닙니다. 표본 수·Breadth·기술적 적격을 함께 확인하세요.")
    sec = b.sectors
    if sec.empty:
        st.info("섹터 데이터가 없습니다.")
        return

    with st.container(border=True):
        c1, c2 = st.columns(2)
        mk = c1.radio("시장 선택", sorted(sec["market"].dropna().unique()), horizontal=True, key="sec_mk")
        lvl = c2.radio(
            "분류 단계", ["sector", "macro"], horizontal=True,
            format_func=lambda x: "🔬 세부 섹터 (밸류체인)" if x == "sector" else "🌐 매크로 대테마",
            key="sec_lvl",
        )

    d = num(
        sec[(sec["market"] == mk) & (sec["level"] == lvl)],
        "sector_power", "median_rs", "rs_delta_4w", "passed_cnt", "univ_total", "pass_rate_pct", "pct_above_50dma", "sub_sector_cnt", "actionable_cnt",
    ).sort_values("sector_power", ascending=False)
    if d.empty:
        st.info("해당 조건의 데이터가 없습니다.")
        return

    label_col = "sector" if lvl == "sector" else "macro_theme"

    st.markdown("##### 📋 섹터 현황 · 비교 기준표")
    st.caption("Power 순서로 정렬한 상세 목록입니다. 선택한 시장·분류의 모든 섹터를 위에서 먼저 확인하세요.")
    if lvl == "macro":
        if "top_leader" in d:
            d = d.copy()
            d["leader_ticker"] = d["top_leader"].astype(str).str.extract(r"\(([A-Za-z0-9.-]+)\)$", expand=False)
        table_cols = [c for c in ("macro_theme", "sub_sector_cnt", "sector_power", "rotation_phase", "pct_above_50dma", "median_rs", "rs_delta_4w", "passed_cnt", "actionable_cnt", "univ_total", "top_sub_sector", "top_leader", "leader_ticker") if c in d.columns]
    else:
        table_cols = [c for c in ("macro_theme", "sector", "sector_power", "rotation_phase", "pct_above_50dma", "median_rs", "rs_delta_4w", "passed_cnt", "actionable_cnt", "univ_total", "leader_name", "leader_ticker", "leader_setup") if c in d.columns]

    stock_dataframe(
        d[table_cols].rename(columns=LABELS), market=mk, hide_index=True, width="stretch", height=340,
        column_config={
            "섹터파워": st.column_config.NumberColumn("파워 점수", format="%.1f", help=TOOLTIPS["sector_power"]),
            "50일선상회": st.column_config.NumberColumn("50일선 상회", format="%.0f%%"),
            "중앙 RS": st.column_config.NumberColumn("중앙 RS (시장 계산 모집단)", format="%.1f", help=TOOLTIPS["median_rs"]),
            "RS 4주Δ": st.column_config.NumberColumn("RS 4주Δ", format="%+.1f", help=TOOLTIPS["rs_change_4w"]),
            "통과종목": st.column_config.NumberColumn("통과", format="%d종목"),
            "전체종목": st.column_config.NumberColumn("유니버스", format="%d종목"),
        },
    )

    v_tab1, v_tab2, v_tab3 = st.tabs(["🧭 4분면 로테이션 맵 (RRG)", "🗺️ 대테마▸세부섹터 트리맵", "📊 파워 랭킹 바 차트"])
    with v_tab1:
        st.caption("현재 위치와 이동 경로를 분리해 비교합니다.")
        options = [sector_key(row, lvl) for _, row in d.iterrows()
                   if np.isfinite(row["median_rs"]) and np.isfinite(row["rs_delta_4w"])]
        duplicate_labels = set(d.loc[d[label_col].duplicated(keep=False), label_col])
        st.markdown("##### ① 섹터 현재 위치 · 4분면")
        # A single label-mode control replaces the former number-only toggle.
        # Full names are an explicit option; the default stays easy to scan.
        with st.container(border=True):
            map_label, map_size = st.columns([3, 2], gap="small")
            with map_label:
                label_mode = st.radio(
                    "4분면 라벨", ["주요 라벨", "전체 라벨", "번호 표시"],
                    horizontal=True, key=f"sec_label_mode_{mk}_{lvl}",
                )
            with map_size:
                size_mode = st.radio(
                    "원 크기", ["pass_rate", "passed_count"],
                    format_func=lambda value: {
                        "pass_rate": "통과 비율 (%)", "passed_count": "통과 종목 수"
                    }[value],
                    horizontal=True, key=f"sec_size_{mk}_{lvl}",
                )
        numbered = label_mode == "번호 표시"
        compact_labels = label_mode == "주요 라벨"

        # Main map is deliberately a pure current-state view. The selected keys
        # affect only which names are shown, never add trails to this chart.
        fig = sector_rotation_quadrant(
            d, lvl, history=None,
            focus_keys=(st.session_state.get(f"sec_focus_{mk}_{lvl}") or options[:3])[:3],
            font_size=10,
            numbered=numbered, size_mode=size_mode, compact_labels=compact_labels,
        )
        show_sector_plot(fig, key=f"plt_sec_quad_{mk}_{lvl}")
        meta = fig.layout.meta or {}
        count = meta.get("sector_count", 0)
        samples = meta.get("sample_counts", {})
        size_label = "통과 비율 (%)" if size_mode == "pass_rate" else "통과 종목 수"
        st.caption(
            f"**{count}/{len(d)}개 현재 위치** · 색상: Power · 원 크기: {size_label} · "
            + ("라벨: 전체 섹터 이름 표시" if label_mode == "전체 라벨" else
             "라벨: 번호 표시 · 아래 번호 대조표 참조" if numbered else
             "라벨: Power 상위 5개 + 관심 섹터 · 나머지는 마우스를 올려 확인")
        )
        if meta.get("rate_mismatch_count") or meta.get("rate_unknown_count"):
            st.warning(
                f"통과율 자료 확인 필요: 엔진 값·건수 불일치 "
                f"{meta.get('rate_mismatch_count', 0)}개(표시용 건수로 보완), "
                f"확인 불가 {meta.get('rate_unknown_count', 0)}개. 원천 엔진 값은 유지합니다."
            )

        if numbered:
            legend = d[np.isfinite(d["median_rs"]) & np.isfinite(d["rs_delta_4w"])][
                [label_col, "sector_power", "median_rs", "rs_delta_4w"]].reset_index(drop=True)
            legend.insert(0, "번호", range(1, len(legend) + 1))
            stock_dataframe(legend.rename(columns=LABELS), hide_index=True, width="stretch")

        st.markdown("##### ② 선택 섹터 이동 경로 · 독립 비교")
        # Group the layout controls and trail selection next to the grid.
        with st.container(border=True):
            grid_controls, page_controls = st.columns([3, 1], gap="small")
            with grid_controls:
                n_cols = st.radio(
                    "한 줄당 차트 수", list(range(1, 9)), index=2,
                    horizontal=True, key=f"sec_path_cols_{mk}_{lvl}",
                )
            with page_controls:
                page_size = st.selectbox(
                    "페이지당 차트", [6, 12, 24, 48, "전체"],
                    index=1, key=f"sec_path_size_{mk}_{lvl}",
                    help="한 페이지에서 실제 렌더링할 경로 차트 개수",
                )
            pc1, pc2 = st.columns([3, 2], gap="small")
            with pc1:
                periods = st.radio(
                    "최근 관측일 수", [5, 10, 20], index=0, horizontal=True,
                    key=f"sec_tail_days_{mk}_{lvl}",
                )
            with pc2:
                all_paths = st.toggle(
                    "전체 섹터 경로 보기", value=False,
                    key=f"sec_all_paths_{mk}_{lvl}",
                )
        if all_paths:
            focus = options
            st.caption(f"전체 {len(options)}개 섹터의 경로를 표시합니다. 선택 보기로 바꾸려면 전체 보기를 해제하세요.")
        else:
            focus = st.multiselect(
                "이동 경로 비교 섹터", options, default=options[:3],
                format_func=lambda key: (
                    f"{key[1]} · {key[0]}" if key[1] in duplicate_labels else key[1]
                ) if lvl == "sector" else key[0],
                key=f"sec_focus_{mk}_{lvl}",
            )
        # Pagination limits only how many Plotly figures are constructed. The
        # full set remains selectable, and shared axes still use all chosen
        # sectors so a page change never silently rescales comparisons.
        per_page = len(focus) if page_size == "전체" else int(page_size)
        total_pages = max(1, (len(focus) + max(1, per_page) - 1) // max(1, per_page))
        # Match Multi-Chart Grid's native +/- number stepper. Keep a stable
        # per-market/level key, and clamp it when page count shrinks.
        page_key = f"sec_path_page_{mk}_{lvl}"
        previous_page = st.session_state.get(page_key, 1)
        try:
            previous_page = int(previous_page)
        except (TypeError, ValueError):
            previous_page = 1
        st.session_state[page_key] = min(total_pages, max(1, previous_page))
        if total_pages > 1:
            _, page_col = st.columns([3, 1], gap="small")
            page = page_col.number_input(
                f"페이지 이동 (1 ~ {total_pages})",
                min_value=1, max_value=total_pages, step=1,
                key=page_key,
            )
        else:
            page = 1
        start_at = (page - 1) * max(1, per_page)
        visible_focus = focus[start_at:start_at + max(1, per_page)]
        if n_cols >= 7:
            st.caption("7~8열에서는 축 눈금을 줄였습니다. 정확한 값은 마우스를 올려 확인하세요.")
        if not focus:
            st.caption("비교 섹터를 선택하면 실제 관측 경로를 별도 그래프에서 표시합니다.")
        else:
            raw_history = sector_rotation_history(
                b.h_sector, d, mk, lvl,
                b.man.get("d_str") or b.man.get("generated_at"), periods
            )
            selected = d.copy()
            selected["_key"] = [sector_key(row, lvl) for _, row in selected.iterrows()]
            selected = selected[selected["_key"].isin(focus)]
            selected_history = (raw_history[raw_history["_key"].isin(focus)]
                                if not raw_history.empty else raw_history)
            x_range, y_range = rotation_mini_shared_bounds(selected, selected_history)
            # The former trail_styles() accepts at most eight identities.
            # In the multi-chart grid any number can be selected, so assign
            # stable per-identity colors without the eight-trail restriction.
            palette = PALETTES[get_chart_theme()["mode"]]
            colors = {key: palette[int.from_bytes(
                hashlib.sha256(json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()).digest()[:4],
                "big") % len(palette)] for key in focus}
            st.caption(
                f"**{len(focus)}개 중 {start_at+1}–{start_at+len(visible_focus)}개 표시** "
                f"· {n_cols}열 · {page}/{total_pages}페이지 · 최근 {periods}개 관측일 · "
                "X:Y 1:1 · ◯ 첫 관측 / ● 현재(T0) / 굵은 선: 최근 구간"
            )
            for start in range(0, len(visible_focus), n_cols):
                cols = st.columns(n_cols, gap="small")
                for index_in_row, key in enumerate(visible_focus[start:start + n_cols]):
                    col = cols[index_in_row]
                    row = selected.loc[selected["_key"].map(lambda value: value == key)].iloc[0]
                    tail = (selected_history[selected_history["_key"].map(lambda value: value == key)]
                            if not selected_history.empty else selected_history)
                    color = colors[key]
                    name = str(row[label_col])
                    if lvl == "sector" and name in duplicate_labels:
                        name = f"{name} · {key[0]}"
                    # No nested bordered card: its default padding reduced
                    # plotting width and made the equal-scale domain shrink.
                    with col:
                        st.markdown(f"**{html.escape(name)}**")
                        phase = row.get("rotation_phase", "확인 불가")
                        if pd.isna(phase) or not str(phase).strip():
                            phase = "확인 불가"
                        phase_short = str(phase).split("(")[0].strip()
                        rs = float(row["median_rs"])
                        delta = float(row["rs_delta_4w"])
                        if n_cols <= 4:
                            st.caption(f"{phase_short} · RS {rs:.1f} · Δ {delta:+.1f}")
                        else:
                            st.caption(f"RS {rs:.1f} · Δ {delta:+.1f}")
                        mini = sector_rotation_mini_figure(
                            row, tail, x_range, y_range, color, columns=n_cols
                        )
                        show_plotly(mini, key=f"plt_sec_mini_{mk}_{lvl}_{focus.index(key)}")
                        if not mini.layout.meta["rotation_mini_has_path"]:
                            st.caption("이력 부족 · 현재만 표시")

        with st.expander("ⓘ 4분면·이동 경로 해석", expanded=False):
            st.markdown(
                "**① 현재 위치** · X=중앙 RS(기준 70), Y=RS 4주 변화(기준 0). "
                "오른쪽은 상대적으로 강하고 위쪽은 최근 강화 중입니다. "
                "점 색상은 Power, 크기는 통과율 또는 통과 종목 수입니다."
            )
            st.markdown(
                "**② 이동 경로** · 섹터별 개별 차트. ◯ 첫 관측 · ● 현재(T0) · 굵은 선 최근 이동. "
                "**X:Y 1:1**을 유지하면서 차트 영역을 채우도록 축 범위를 조정합니다. "
                "기록이 없거나 변동이 없으면 경로를 만들지 않습니다."
            )
            st.caption(
                f"표본 경고: 빨강 1~4개({samples.get('표본 매우 적음', 0)}), "
                f"주황 5~9개({samples.get('표본 주의', 0)}), "
                f"확인 불가({samples.get('표본 확인 불가', 0)}). "
                "관측 간격은 불규칙할 수 있으며 RS·Power는 매수 신호가 아닙니다."
            )
    with v_tab2:
        area_mode = st.radio("트리맵 박스 크기", ["균등 (전체 섹터 보기)", "통과 종목 수"], horizontal=True, key=f"sec_tree_area_{mk}")
        details = st.toggle("대장주도 표시", value=False, key=f"sec_tree_details_{mk}")
        tm = sector_treemap(sec, mk, details, area_mode == "균등 (전체 섹터 보기)")
        if tm is not None:
            show_sector_plot(tm, key=f"plt_sec_tree_{mk}")
            st.caption("P: 섹터 파워 · 숫자: 통과 종목 수 · 박스 클릭: 확대"
                       + (" · 균등 보기의 면적은 종목 수와 무관" if area_mode == "균등 (전체 섹터 보기)" else ""))
        else:
            st.caption("트리맵을 표시할 세부 섹터 데이터가 없습니다.")
    with v_tab3:
        top = d.head(15).iloc[::-1]
        bar_txt_colors = ["#FFFFFF" if (float(p) >= 75.0 or float(p) <= 44.0) else "#0F172A" for p in top["sector_power"]]
        fig = go.Figure(go.Bar(
            x=top["sector_power"], y=top[label_col], orientation="h",
            marker=dict(color=top["sector_power"], colorscale=SOFT_POWER_SCALE, cmin=35, cmax=88, cornerradius=4),
            text=[f"<b>{p:.1f}</b> (중앙RS {r:.0f} · 통과 {int(c)}종목)" for p, r, c in zip(top["sector_power"], top["median_rs"], top["passed_cnt"].fillna(0))],
            textposition="inside", insidetextanchor="middle", textfont=dict(color=bar_txt_colors, size=11),
        ))
        fig.update_layout(xaxis=dict(title="파워 점수 (0~100)", range=[0, 105]))
        show_plotly(clean_chart(fig, max(300, 26 * len(top) + 60), legend=False, title="상위 15개 섹터/테마 파워 랭킹"), key=f"plt_sec_rank_{mk}_{lvl}")



# ════════════════════════════════════════════════════════════════════
# 6. 탭 3: 🎯 전술 플레이북 & 멀티플 차트 그리드 & GARP 스캐너
# ════════════════════════════════════════════════════════════════════
@st.cache_data(ttl=MANIFEST_TTL_SEC, show_spinner=False, max_entries=16)
def _cached_prepare_candidates(run_id: str, _cand: pd.DataFrame, _watch: pd.DataFrame, _h_cand: pd.DataFrame) -> pd.DataFrame:
    del run_id
    df = _cand.copy()
    if df.empty:
        return df
    watch_keys = set(zip(_watch["market"].astype(str), _watch["ticker"].astype(str))) if not _watch.empty else set()
    df["actionable_now"] = [(str(m), str(t)) in watch_keys for m, t in zip(df["market"], df["ticker"])]
    if not _watch.empty:
        extra = [c for c in ("portfolio_alloc_pct", "portfolio_alloc_note") if c in _watch.columns and c not in df.columns]
        if extra:
            df = df.merge(_watch[["market", "ticker"] + extra], on=["market", "ticker"], how="left")
    mat, dates = list_matrix(_h_cand)
    if dates:
        st_ = streak_ending_last(mat)
        df["streak"] = [int(st_.get(f"{m}|{t}", 0)) for m, t in zip(df["market"], df["ticker"])]

    df = num(
        df, "rs_score", "rs_change_4w", "topdown_score", "sector_power", "last_close",
        "pivot_price", "base_pivot_price", "high_20d", "base_len_days",
        "dist_to_pivot_pct", "stop_price", "stop_dist_pct",
        "position_size_pct", "portfolio_alloc_pct", "volume_ratio", "vol_ratio_50d", "vol_dry_up_ratio",
        "range_10d_pct", "down_day_excess_pct", "distance_from_high_pct", "dist_sma20_pct",
        "dist_sma50_pct", "atr_from_50dma", "dist_days_25d", "peg", "eps_growth_pct",
    )
    # [P1-10] 복수 트랙/전체 보기 시 RS 옆에 소속 트랙을 고정 병기
    df["rs_with_track"] = [
        f"{float(rs):.1f} ({trk})" if pd.notna(rs) else f"- ({trk})"
        for rs, trk in zip(df.get("rs_score", pd.Series(np.nan, index=df.index)), df.get("sub_market", pd.Series("-", index=df.index)))
    ]
    df["fund_badge"] = [
        format_fund_badge(p, eg, es)
        for p, eg, es in zip(
            df.get("peg", pd.Series(np.nan, index=df.index)),
            df.get("eps_growth_pct", pd.Series(np.nan, index=df.index)),
            df.get("eps_status", pd.Series("", index=df.index)),
        )
    ]
    df["signal_badges"] = [
        format_signal_badges(bd, nh, dr, et, r4, d20, a50)
        for bd, nh, dr, et, r4, d20, a50 in zip(
            df.get("rs_blue_dot", pd.Series(False, index=df.index)),
            df.get("rs_line_new_high", pd.Series(False, index=df.index)),
            df.get("down_resilient", pd.Series(False, index=df.index)),
            df.get("earnings_tag", pd.Series("", index=df.index)),
            df.get("rs_change_4w", pd.Series(np.nan, index=df.index)),
            df.get("dist_sma20_pct", pd.Series(np.nan, index=df.index)),
            df.get("atr_from_50dma", pd.Series(np.nan, index=df.index)),
        )
    ]
    df["exclusion_reason"] = build_exclusion_reason(df)
    return df


def prepare_candidates(b: Bundle) -> pd.DataFrame:
    frame = _cached_prepare_candidates(str(b.man.get("run_id", "")), b.cand, b.watch, b.h_cand)
    return attach_base_counts(frame, b.base_observations, b.base_source)


def filter_by_tactical_mode(
    df: pd.DataFrame, mode: str, only_lead: bool, only_top20: bool,
    only_garp: bool, rs_min: float, sec_pick: list[str], q: str,
) -> pd.DataFrame:
    f = df.copy()
    if sec_pick:
        f = f[f["sector"].isin(sec_pick)]
    if rs_min > 0:
        f = f[f["rs_score"] >= rs_min]
    if only_lead and "is_leading_sector" in f.columns:
        f = f[tf(f["is_leading_sector"])]
    if only_top20 and "selected" in f.columns:
        f = f[tf(f["selected"])]
    if only_garp:
        f = f[is_garp_or_turnaround(f, max_peg=1.5)]
    if q.strip():
        qq = q.strip().lower()
        f = f[f["name"].astype(str).str.lower().str.contains(qq, regex=False) | f["ticker"].astype(str).str.lower().str.contains(qq, regex=False)]

    if mode == "ATTACK":
        in_bz = tf(f["in_buy_zone"]) if "in_buy_zone" in f.columns else f["dist_to_pivot_pct"].between(-0.025, 0.05)
        wide_act = tf(f["wide_stop_actionable"]) if "wide_stop_actionable" in f.columns else pd.Series(False, index=f.index)
        f = f[f["actionable_now"] | wide_act | in_bz]
        sort_cols = [c for c in ("actionable_now", "is_leading_sector", "topdown_score", "rs_score") if c in f.columns]
        f = f.copy()
        f["_table_group"] = np.where(tf(f["actionable_now"]), 2, np.where(wide_act.reindex(f.index), 1, 0))
        return tactical_sort(f, ["_table_group"] + sort_cols)

    if mode == "CAUTION":
        is_vcp = tf(f["is_true_vcp"]) if "is_true_vcp" in f.columns else pd.Series(False, index=f.index)
        is_res = tf(f["down_resilient"]) if "down_resilient" in f.columns else pd.Series(False, index=f.index)
        is_bd = tf(f["rs_blue_dot"]) if "rs_blue_dot" in f.columns else pd.Series(False, index=f.index)
        setup_ok = f["setup_stage"].astype(str).str.contains("VCP|수축|눌림목|포켓", regex=True)
        f = f[is_vcp | is_res | is_bd | setup_ok]
        sort_cols = [c for c in ("topdown_score", "rs_score") if c in f.columns]
        return tactical_sort(f, sort_cols)

    has_exit_warn = f["exit_signal"].astype(str).str.contains("🚨|⚠️|✂️", regex=True) if "exit_signal" in f.columns else pd.Series(False, index=f.index)
    is_turn = (f["tier"].astype(str) == "추세회복") if "tier" in f.columns else pd.Series(False, index=f.index)
    is_sel = tf(f["selected"]) if "selected" in f.columns else pd.Series(True, index=f.index)
    f = f[has_exit_warn | is_turn | is_sel]
    f = f.copy()
    f["_table_group"] = np.where(is_turn.reindex(f.index), 1, np.where(has_exit_warn.reindex(f.index), 2, 0))
    sort_cols = [c for c in ("topdown_score", "rs_score") if c in f.columns]
    return tactical_sort(f, ["_table_group"] + sort_cols)


def tactical_sort(frame: pd.DataFrame, score_columns: list[str]) -> pd.DataFrame:
    """Keep score priorities, resolve ties by identity, never by input order."""
    ties = [c for c in ("market", "ticker", "sub_market") if c in frame and c not in score_columns]
    return frame.sort_values(score_columns + ties, ascending=[False]*len(score_columns) + [True]*len(ties),
                             kind="stable", na_position="last")


def _common_column_config() -> dict[str, Any]:
    return {
        "RS": st.column_config.NumberColumn("RS", format="%.1f", help=TOOLTIPS["rs_score"]),
        "RS (트랙)": st.column_config.TextColumn("RS (트랙)", help=TOOLTIPS["rs_with_track"]),
        "RS 4주Δ": st.column_config.NumberColumn("RS 4주Δ", format="%+.0f", help=TOOLTIPS["rs_change_4w"]),
        "탑다운점수": st.column_config.NumberColumn("탑다운", format="%.1f", help=TOOLTIPS["topdown_score"]),
        "PEG": st.column_config.NumberColumn("PEG", format="%.2f", help=TOOLTIPS["peg"]),
        "EPS성장률": st.column_config.NumberColumn("EPS성장률", format="%+.1f%%", help=TOOLTIPS["eps_growth_pct"]),
        "현재가": st.column_config.NumberColumn("현재가", format="%.2f", help=TOOLTIPS["last_close"]),
        "베이스피벗": st.column_config.NumberColumn("베이스피벗", format="%.2f", help=TOOLTIPS["pivot_price"]),
        "베이스 순번": st.column_config.TextColumn("베이스 순번", help="현재 상승 사이클 안의 관측 순번과 상태입니다. 자료 부재·가격 기준 불명확·이력 부족은 미확인으로 표시합니다. 완료 베이스 수와 다르며 매매 적격·비중에는 반영하지 않습니다."),
        "20일고가": st.column_config.NumberColumn("20일고가", format="%.2f", help=TOOLTIPS["high_20d"]),
        "피벗괴리": st.column_config.NumberColumn("피벗괴리", format="%+.1f%%", help=TOOLTIPS["dist_to_pivot_pct"]),
        "손절가": st.column_config.NumberColumn("손절가", format="%.2f", help=TOOLTIPS["stop_price"]),
        "손절폭": st.column_config.NumberColumn("손절폭", format="%.1f%%", help=TOOLTIPS["stop_dist_pct"]),
        "권장비중": st.column_config.NumberColumn("권장비중", format="%.1f%%", help=TOOLTIPS["position_size_pct"]),
        "포트배분": st.column_config.NumberColumn("포트배분", format="%.1f%%", help=TOOLTIPS["portfolio_alloc_pct"]),
        "돌파거래량(50일)": st.column_config.NumberColumn("돌파거래량(50일)", format="%.2f배", help=TOOLTIPS["vol_ratio_50d"]),
        "거래량(20일)": st.column_config.NumberColumn("거래량(20일)", format="%.2f배", help=TOOLTIPS["volume_ratio"]),
        "거래량건조(5/50일)": st.column_config.NumberColumn("거래량건조", format="%.2f배", help=TOOLTIPS["vol_dry_up_ratio"]),
        "10일변동폭": st.column_config.NumberColumn("10일변동폭", format="%.1f%%", help=TOOLTIPS["range_10d_pct"]),
        "하락일초과%": st.column_config.NumberColumn("하락일초과", format="%+.2f%%", help=TOOLTIPS["down_day_excess_pct"]),
        "50일선이격": st.column_config.NumberColumn("50일선이격", format="%+.1f%%", help=TOOLTIPS["dist_sma50_pct"]),
        "52주고점괴리": st.column_config.NumberColumn("52주고점괴리", format="%+.1f%%", help=TOOLTIPS["distance_from_high_pct"]),
        "25일분배일": st.column_config.NumberColumn("25일분배일", format="%d일", help=TOOLTIPS["dist_days_25d"]),
        "체류일": st.column_config.NumberColumn("체류일", format="%d일", help=TOOLTIPS["streak"]),
        "청산신호": st.column_config.TextColumn("청산신호", help=TOOLTIPS["exit_signal"]),
        "제외 사유": st.column_config.TextColumn("제외 사유", help=TOOLTIPS["exclusion_reason"]),
    }


def render_mode_specific_table(f: pd.DataFrame, mode: str, dropped_df: pd.DataFrame, track_choice: str, rem_info: dict[str, Any] | None = None) -> None:
    show = allocation_view(f, rem_info)
    if "bc_display" not in show:
        show = attach_base_counts(show, pd.DataFrame(), {"status": "absent"})
    show["exclusion_reason"] = build_exclusion_reason(show)
    is_multi_track = track_choice not in TRACKS
    rs_col_name = "rs_with_track" if is_multi_track and "rs_with_track" in show.columns else "rs_score"

    for c in ("dist_to_pivot_pct", "stop_dist_pct", "distance_from_high_pct", "range_10d_pct", "dist_sma50_pct"):
        if c in show.columns:
            show[c] = pd.to_numeric(show[c], errors="coerce") * 100.0

    if mode == "ATTACK":
        act_mask = tf(show["actionable_now"]) if "actionable_now" in show.columns else pd.Series(False, index=show.index)
        wide_mask = (~act_mask) & (
            tf(show["wide_stop_actionable"]) if "wide_stop_actionable" in show.columns
            else ((show["tier"].astype(str) == "Tier A") & (show["stop_dist_pct"].abs() > 8.0) & (show["stop_dist_pct"].abs() <= 12.0) & (show["exit_signal"].astype(str) == "✅정상"))
        )
        act_df = show[act_mask].copy()
        wide_df = show[wide_mask].copy()
        excl_df = show[~act_mask & ~wide_mask].copy()

        st.markdown(f"##### 🟢 1. 게시 매수 검토 적격 — **{len(act_df)}종목**")
        st.caption("기술적 적격과 모델 배분은 별도입니다. 배분 0%는 시장 국면·예산·한도 등의 제약일 수 있으며, 종목별 모델 배분과 사유를 확인하세요.")
        act_cols = [c for c in (
            "name", "ticker", "sub_market", "sector", "setup_stage", "signal_badges", "bc_display", rs_col_name, "topdown_score",
            "fund_badge", "last_close", "pivot_price", "high_20d", "dist_to_pivot_pct", "stop_price", "stop_dist_pct",
            "position_size_pct", "portfolio_alloc_pct", "portfolio_alloc_note", "vol_ratio_50d", "volume_ratio"
        ) if c in act_df.columns]
        if act_df.empty:
            st.info("현재 필터 조건에서 게시 매수 검토 적격 종목이 없습니다. 아래 와이드 스톱 관찰군 또는 제외 목록을 확인하세요.")
        else:
            stock_dataframe(act_df[act_cols].rename(columns=LABELS), hide_index=True, width="stretch", height=min(380, 38 * (len(act_df) + 1)), column_config=_common_column_config())

        if not wide_df.empty:
            with st.expander(f"🟠 2. 와이드 스톱 관찰 — {len(wide_df)}종목", expanded=True):
                st.caption("정배열 주도주 중 손절폭 8~12% 관찰군 · 매수 권유 아님 · 리스크 예산(1% 또는 0.75%) 기준 축소 비중")
                w_cols = [c for c in (
                    "name", "ticker", "sub_market", "sector", "setup_stage", "signal_badges", "bc_display", rs_col_name, "topdown_score",
                    "last_close", "pivot_price", "dist_to_pivot_pct", "stop_price", "stop_dist_pct", "position_size_pct", "vol_ratio_50d"
                ) if c in wide_df.columns]
                stock_dataframe(wide_df[w_cols].rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())

        with st.expander(f"⚠️ 3. 바이존 내 배분 제외 — {len(excl_df)}종목", expanded=False):
            st.caption("바이존 -2.5%~+5% · 제외 검토 항목: 손절폭·청산신호·Tier·실적")
            if excl_df.empty:
                st.caption("바이존 내 적격 제외 종목이 없습니다.")
            else:
                excl_cols = [c for c in (
                    "name", "ticker", "sub_market", "sector", "exclusion_reason", "tier", "setup_stage", "exit_signal",
                    "signal_badges", "bc_display", rs_col_name, "topdown_score", "last_close", "pivot_price", "dist_to_pivot_pct", "stop_dist_pct", "vol_ratio_50d"
                ) if c in excl_df.columns]
                stock_dataframe(excl_df[excl_cols].rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())
        return

    if mode == "CAUTION":
        st.markdown("##### 🟡 조정장 관찰 워치리스트 — **VCP 수축 · 거래량 건조 · 조정장 방어력 & 저PEG/흑자전환**")
        cols = [c for c in (
            "name", "ticker", "sub_market", "sector", "setup_stage", "signal_badges", "bc_display", rs_col_name, "topdown_score", "rs_change_4w",
            "fund_badge", "peg", "eps_growth_pct", "range_10d_pct", "vol_dry_up_ratio", "down_day_excess_pct",
            "last_close", "pivot_price", "dist_to_pivot_pct", "position_size_pct", "streak"
        ) if c in show.columns]
        stock_dataframe(show[cols].rename(columns=LABELS), hide_index=True, width="stretch", height=min(420, 38 * (len(show) + 1)), column_config=_common_column_config())
        return

    st.markdown("##### 🔴 하락장 방어 & 모델 워치리스트 점검 — **모델 워치리스트 청산 경고 · 전일 Top20 이탈 · 추세회복 분리 점검**")
    has_warn = show["exit_signal"].astype(str).str.contains("🚨|⚠️|✂️", regex=True) if "exit_signal" in show.columns else pd.Series(False, index=show.index)
    is_turn = (show["tier"].astype(str) == "추세회복") if "tier" in show.columns else pd.Series(False, index=show.index)

    warn_df = show[has_warn & ~is_turn]
    turn_df = show[is_turn]
    hold_df = show[~has_warn & ~is_turn]

    r_tab1, r_tab2, r_tab3, r_tab4 = st.tabs([
        f"🚨 모델 워치리스트 청산 경고 ({len(warn_df)}종목)",
        f"📉 전일 Top20 이탈 ({len(dropped_df)}종목)",
        f"🌱 200일선 하회 추세회복 ({len(turn_df)}종목)",
        f"✅ 정상 유지 리더 ({len(hold_df)}종목)",
    ])
    def_cols = [c for c in (
        "name", "ticker", "sub_market", "sector", "exit_signal", "dist_days_25d",
        "bc_display",
        "fund_badge", "last_close", "stop_price", "stop_dist_pct", "dist_sma50_pct", "distance_from_high_pct", rs_col_name, "topdown_score"
    ) if c in show.columns]

    with r_tab1:
        st.caption("내 계좌 잔고가 아닌 **모델 워치리스트 편입 종목** 중 10/20/50일선 이탈 또는 분배일 과다 경고가 발생한 종목입니다.")
        if warn_df.empty:
            st.success("현재 모델 워치리스트 내 이평선 이탈이나 분배일 과다 경고가 발생한 종목이 없습니다.")
        else:
            stock_dataframe(warn_df[def_cols].rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())
    with r_tab2:
        if dropped_df.empty:
            st.caption("전일 대비 이탈한 종목이 없습니다.")
        else:
            d_sub = dropped_df.copy()
            for c in ("dist_to_pivot_pct", "stop_dist_pct", "distance_from_high_pct", "range_10d_pct", "dist_sma50_pct"):
                if c in d_sub.columns:
                    d_sub[c] = pd.to_numeric(d_sub[c], errors="coerce") * 100.0
            if track_choice in TRACKS:
                d_sub = d_sub[d_sub["sub_market"] == track_choice]
            elif track_choice.startswith("KR"):
                d_sub = d_sub[d_sub["sub_market"].isin(["KOSPI", "KOSDAQ"])]
            elif track_choice.startswith("US"):
                d_sub = d_sub[d_sub["sub_market"].isin(["NASDAQ100", "SP500"])]
            stock_dataframe(d_sub.rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())
    with r_tab3:
        if turn_df.empty:
            st.caption("현재 200일선 아래에서 올라오는 추세회복(턴어라운드) 종목이 없습니다.")
        else:
            stock_dataframe(turn_df[def_cols].rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())
    with r_tab4:
        if hold_df.empty:
            st.caption("해당 종목이 없습니다.")
        else:
            stock_dataframe(hold_df[def_cols].rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())


CHART_MISSING_REASONS = {
    "duplicate_identity": "동일 시장·티커 중복 행",
    "invalid_chart_payload": "가격 배열이 비었거나 서로 불일치",
    "chart_identity_mismatch": "차트 종목·기준일 불일치",
    "chart_line_mismatch": "게시 행과 피벗·손절선 불일치",
    "outside_chart_scope": "차트 생성 범위 밖",
    "limit_reached": "차트 생성 개수 제한",
    "cache_missing": "저장된 가격 이력 없음",
    "cache_read_error": "저장된 가격 이력 읽기 실패",
    "no_confirmed_bars": "확정된 가격 봉 없음",
    "no_bars_as_of": "기준일 이전 가격 봉 없음",
    "missing_ohlcv": "가격·거래량 항목 부족",
    "insufficient_bars": "유효 가격 이력 30봉 미만",
    "chart_build_error": "차트 데이터 생성 실패",
}


def tactical_chart_rows(f: pd.DataFrame, charts: dict) -> tuple[list, list, dict]:
    """Single ordered inventory used by the actual grid and audit evidence."""
    available, missing, reasons, seen = [], [], {}, set()
    for r in f.itertuples():
        mkt = str(getattr(r, "market", "")).strip().upper()
        tkr = str(r.ticker).strip().zfill(6) if mkt == "KR" else str(r.ticker).strip().upper()
        ck = f"{mkt}:{tkr}"
        ch = charts.get(ck)
        reason = None
        if ck in seen:
            # Duplicate accounting is separate from per-identity missing reasons.
            reasons.setdefault("duplicates", []).append(ck)
            continue
        seen.add(ck)
        if not isinstance(ch, dict):
            reason = "unavailable"
        elif not isinstance(ch.get("d"), list) or not ch["d"] or any(
            not isinstance(ch.get(k), list) or len(ch[k]) != len(ch["d"]) for k in ("o", "h", "l", "c", "v", "s20", "s50", "s200")
        ):
            reason = "invalid_chart_payload"
        elif ((ch.get("market", mkt), ch.get("ticker", tkr)) != (mkt, tkr)
              or (ch.get("snapshot_as_of") and str(getattr(r, "as_of", ch["snapshot_as_of"])) != ch["snapshot_as_of"])):
            reason = "chart_identity_mismatch"
        else:
            for line, field in (("pivot", "pivot_price"), ("stop", "stop_price")):
                value, expected = finite_number(ch.get(line)), finite_number(getattr(r, field, None))
                # Legacy chart JSON rounds lines to six decimals; CSV does not.
                if np.isfinite(value) and np.isfinite(expected) and not math.isclose(value, expected, rel_tol=1e-12, abs_tol=5.1e-7):
                    reason = "chart_line_mismatch"
                    break
        if reason:
            missing.append((mkt, tkr, str(getattr(r, "name", tkr))))
            if reason != "unavailable":
                reasons[ck] = reason
        else:
            available.append((r, ch, mkt, tkr, ck))
    return available, missing, reasons


def chart_missing_details(b: Bundle, rows: list[tuple[str, str, str]]) -> pd.DataFrame:
    coverage = b.summ.get("chart_coverage") or {}
    if not isinstance(coverage, dict):
        coverage = {}
    missing = coverage.get("missing") or {}
    if not isinstance(missing, dict):
        missing = {}
    records = []
    for market, ticker, name in rows:
        reason = missing.get(f"{market}:{ticker}")
        if not isinstance(reason, str):
            reason = None
        records.append({"market": market, "ticker": ticker, "name": name,
                        "reason": CHART_MISSING_REASONS.get(reason, "미제공 사유 정보 없음")})
    return pd.DataFrame(records, columns=["market", "ticker", "name", "reason"])


def render_chart_missing_details(b: Bundle, rows: list[tuple[str, str, str]]) -> None:
    if not rows:
        return
    details = chart_missing_details(b, rows)
    with st.expander(f"차트 데이터 미제공 {len(details)}종목 · 사유 보기"):
        counts = details["reason"].value_counts()
        st.caption(" · ".join(f"{reason} {count}종목" for reason, count in counts.items()))
        stock_dataframe(details.rename(columns={"market": "시장", "ticker": "티커", "name": "종목명", "reason": "미제공 사유"}),
                        hide_index=True, width="stretch")



def stock_grid_health_summary(row: dict[str, Any]) -> str:
    """Only confirmed inputs get positive or negative compact signals."""
    def numeric(key: str) -> float:
        value = pd.to_numeric(row.get(key), errors="coerce")
        return float(value) if pd.notna(value) else float("nan")

    strict = str(row.get("strict_minervini", "")).lower()
    trend = "✅" if strict == "true" else ("⚠️" if strict == "false" else "–")
    rs, delta = numeric("rs_score"), numeric("rs_change_4w")
    momentum = ("✅" if rs >= 80 and delta >= -2 else "⚠️") if np.isfinite(rs) and np.isfinite(delta) else "–"
    atr = numeric("atr_from_50dma")
    distance = ("✅" if atr <= 3.5 else "🚨") if np.isfinite(atr) else "–"
    dist = numeric("dist_days_25d")
    distribution = ("✅" if dist < 4 else "⚠️") if np.isfinite(dist) else "–"
    return f"정배열 {trend} · RS {momentum} · 이격 {distance} · 분배 {distribution}"


def choose_grid_stock(stock_key: str) -> None:
    """Only one chart-expanded stock is selected at once."""
    current = st.session_state.get("grid_detail_key")
    st.session_state["grid_detail_key"] = None if current == stock_key else stock_key
    if "grid_missing_pick" in st.session_state:
        st.session_state["grid_missing_pick"] = ""


def clear_grid_chart_selection() -> None:
    st.session_state["grid_detail_key"] = None


def render_grid_stock_diagnostics(b: Bundle, r: pd.Series, ch: dict | None) -> None:
    """On-demand expanded information: never duplicate the default grid charts."""
    mkt = str(r.get("market", ""))
    tkr = str(r.get("ticker", "")).zfill(6) if mkt == "KR" else str(r.get("ticker", "")).upper()
    key = f"{mkt}:{tkr}"
    with st.container(border=True):
        title_col, close_col = st.columns([5, 1])
        title_col.markdown(f"##### 🔍 {r.get('name', tkr)} ({tkr}) · 선택 종목 정밀 진단")
        close_col.button("닫기", key=f"grid_detail_close_{key}", on_click=choose_grid_stock, args=(key,))
        st.caption("차트·카드에서 확인한 요약은 반복하지 않습니다. 추가 진단을 선택하세요.")
        modes = ["7대 추세·배분", "손익비 1R·2R·3R", "베이스 관측", "RS·탑다운 이력", "차트 확대"]
        mode = st.radio("정밀 진단 항목", modes, horizontal=True, key=f"grid_detail_mode_{key}")
        if mode == modes[0]:
            render_stock_sepa_checklist(r)
        elif mode == modes[1]:
            values = [pd.to_numeric(r.get(col), errors="coerce") for col in ("last_close", "stop_price", "pivot_price")]
            rr_fig = risk_reward_bar(*(float(v) if pd.notna(v) else float("nan") for v in values), mkt)
            if rr_fig is not None:
                show_plotly(rr_fig, key=f"grid_rr_{key}")
            else:
                st.info("현재가·손절가 자료가 유효하지 않아 손익비 도식을 표시할 수 없습니다.")
            st.caption("손익비 도식은 위험·보상 범위 참고용이며 미래 수익을 보장하지 않습니다.")
        elif mode == modes[2]:
            if not render_base_panel(r):
                st.caption("베이스 정보가 미확인이면 값을 추정하지 않습니다.")
        elif mode == modes[3]:
            hist = b.h_cand
            if not hist.empty and {"market", "ticker", "date", "rs_score"}.issubset(hist.columns):
                ticker_column = hist["ticker"].astype(str).str.upper()
                if mkt == "KR":
                    ticker_column = ticker_column.str.zfill(6)
                mask = hist["market"].astype(str).eq(mkt) & ticker_column.eq(tkr)
                rows = num(hist.loc[mask], "rs_score", "topdown_score").sort_values("date")
            else:
                rows = pd.DataFrame()
            if len(rows) >= 2:
                fig = go.Figure()
                fig.add_scatter(x=rows["date"], y=rows["rs_score"], name="RS 점수", mode="lines+markers",
                                line=dict(color=PALETTE["indigo"], width=2.2))
                if "topdown_score" in rows.columns:
                    fig.add_scatter(x=rows["date"], y=rows["topdown_score"], name="탑다운 점수",
                                    mode="lines+markers", line=dict(color=PALETTE["green"], dash="dot"))
                fig.update_xaxes(type="category", nticks=8)
                show_plotly(clean_chart(fig, 250, title="RS · 탑다운 점수 누적 이력"), key=f"grid_rs_hist_{key}")
                st.caption("실제 저장된 분석일을 기준으로 표시하며, 기록이 없는 날짜는 추정하지 않습니다.")
            else:
                st.info("저장된 RS·탑다운 점수 이력이 2개 분석일 미만입니다.")
        else:
            if not ch or not ch.get("d"):
                st.info("게시된 가격 차트가 없어 확대 차트를 표시할 수 없습니다.")
            else:
                bars = st.radio("차트 확대 기간", CHART_PERIODS, index=4, horizontal=True,
                                format_func=chart_period_label, key=f"grid_zoom_period_{key}")
                count = min(bars, len(ch.get("d", [])))
                chart = candle_chart(ch, mkt, f"{r.get('name', tkr)} · {count}봉 확대", bars=bars, compact=False)
                valid_base = str(r.get("bc_status", "UNKNOWN")) not in ("UNKNOWN", "nan", "None") and str(r.get("bc_reason")) != "OBSERVATIONS_NOT_PUBLISHED"
                if valid_base and st.checkbox("베이스 관측 구간 표시", value=False, key=f"grid_base_overlay_{key}"):
                    chart = add_base_overlay(chart, ch, r, bars)
                show_plotly(chart, key=f"grid_zoom_{key}_{bars}")
                st.caption(f"실제 표시: {count}봉 · {ch['d'][-count]} ~ {ch['d'][-1]}")
                if count < bars:
                    st.info(f"요청한 {bars}봉 중 게시된 {count}봉만 표시합니다. 저장된 이력만 사용합니다.")


def render_missing_stock_analyzer(b: Bundle, f: pd.DataFrame, missing_rows: list[tuple[str, str, str]]) -> None:
    """Keep stock diagnostics accessible when the published price chart is absent."""
    if not missing_rows:
        return
    values = [f"{m}:{t}" for m, t, _ in missing_rows]
    labels = {f"{m}:{t}": f"{name} ({t}) · {m}" for m, t, name in missing_rows}
    with st.expander(f"🔍 차트 없는 종목 분석 · {len(values)}종목", expanded=False):
        selected = st.selectbox("종목 선택", [""] + values,
                                format_func=lambda key: labels.get(key, "선택 안 함"),
                                key="grid_missing_pick", on_change=clear_grid_chart_selection)
    if not selected:
        return
    keys = f["market"].astype(str) + ":" + f["ticker"].astype(str).str.upper().where(
        f["market"].astype(str) != "KR", f["ticker"].astype(str).str.zfill(6))
    matches = f.loc[keys.eq(selected)]
    if matches.empty:
        st.info("현재 필터에 선택한 종목이 없습니다.")
        return
    st.caption("가격 차트가 없지만 확인된 후보 지표와 베이스 정보는 분석할 수 있습니다.")
    render_grid_stock_diagnostics(b, matches.iloc[0], None)


# [P2-12] st.fragment로 멀티 차트 그리드를 격리하여 페이지/정렬 조작 시 상위 테이블 전체 재렌더링 방지
@st.fragment
def render_multi_chart_grid(b: Bundle, f: pd.DataFrame) -> None:
    st.markdown("#### 🖼️ 멀티플 차트 그리드 (Multi-Chart Grid)")
    run_id = str(b.man.get("run_id", ""))
    theme_mode = "dark" if is_dark_mode() else "light"

    chart_rows, missing_rows, invalid = tactical_chart_rows(f, b.charts)
    duplicates = invalid.pop("duplicates", [])
    if duplicates:
        st.caption(f"동일 시장·티커 중복 {len(duplicates)}행은 차트에서 한 번만 표시합니다: {', '.join(duplicates)}")
    if invalid:
        # Local view only: do not mutate verified publication diagnostics.
        from dataclasses import replace
        coverage = dict(b.summ.get("chart_coverage") or {})
        coverage["missing"] = {**(coverage.get("missing") or {}), **invalid}
        b = replace(b, summ={**b.summ, "chart_coverage": coverage})

    if not chart_rows:
        st.session_state["grid_page"] = 1
        st.session_state["grid_detail_key"] = None
        st.info("현재 필터링된 종목 중 사전 계산된 차트(`charts.json.gz`)가 있는 종목이 없습니다.")
        render_chart_missing_details(b, missing_rows)
        render_missing_stock_analyzer(b, f, missing_rows)
        return

    with st.container(border=True):
        c1, c3, c4 = st.columns([1.2, 1.6, 1.1])
        n_cols = c1.radio("한 줄당 차트 수 (열 지정)", [1, 2, 3, 4], index=1, horizontal=True, key="grid_cols")
        sort_by = c3.selectbox("차트 정렬", ["전술 기본 순서", "탑다운 점수 높은 순", "RS 높은 순", "피벗 근접 순", "저PEG 순", "섹터별 묶음"], key="grid_sort")
        page_sz = c4.selectbox("한 페이지 표시", [8, 12, 24, 60], index=1, format_func=lambda x: f"{x}개씩", key="grid_psz")
        bars_win = st.radio("표시 기간 (줌)", CHART_PERIODS, index=2, horizontal=True, format_func=chart_period_label, key="grid_bars")

    short_count = sum(len(ch.get("d", [])) < bars_win for _, ch, *_ in chart_rows)
    if short_count:
        st.caption(f"{short_count}개 종목은 요청한 {bars_win}봉보다 데이터가 짧아 저장된 범위만 표시합니다. 신규 상장 또는 이전 게시 데이터일 수 있습니다.")

    if sort_by == "탑다운 점수 높은 순":
        chart_rows.sort(key=lambda x: finite_number(getattr(x[0], "topdown_score", None), -float("inf")), reverse=True)
    elif sort_by == "RS 높은 순":
        chart_rows.sort(key=lambda x: finite_number(getattr(x[0], "rs_score", None), -float("inf")), reverse=True)
    elif sort_by == "피벗 근접 순":
        chart_rows.sort(key=lambda x: abs(finite_number(getattr(x[0], "dist_to_pivot_pct", None), float("inf"))))
    elif sort_by == "저PEG 순":
        def _peg_key(item: Any) -> float:
            pv = getattr(item[0], "peg", np.nan)
            try:
                fv = float(pv)
                return fv if (np.isfinite(fv) and fv > 0) else 999.0
            except Exception:
                return 999.0
        chart_rows.sort(key=_peg_key)
    elif sort_by == "섹터별 묶음":
        chart_rows.sort(key=lambda x: (str(getattr(x[0], "sector", "")), -finite_number(getattr(x[0], "topdown_score", None), -float("inf"))))

    total_charts = len(chart_rows)
    total_pages = max(1, math.ceil(total_charts / page_sz))

    if st.session_state.get("grid_page", 1) > total_pages:
        st.session_state["grid_page"] = 1

    if total_pages > 1:
        _, p_col2 = st.columns([3, 1])
        cur_page = p_col2.number_input(f"페이지 이동 (1 ~ {total_pages})", min_value=1, max_value=total_pages, step=1, key="grid_page")
    else:
        cur_page = 1

    start_i = (cur_page - 1) * page_sz
    end_i = min(total_charts, start_i + page_sz)
    visible = chart_rows[start_i:end_i]
    selected_key = st.session_state.get("grid_detail_key")
    if selected_key and selected_key not in {item[4] for item in visible}:
        st.session_state["grid_detail_key"] = None

    st.caption(
        f"**차트 {total_charts}개 중 {start_i + 1}~{end_i}번째 표시**"
        + (f" · 데이터 미제공 {len(missing_rows)}종목" if missing_rows else "")
    )
    st.caption("기본 순서는 전술 표의 그룹·우선순위와 같습니다. 표 머리글 클릭 정렬은 차트에 반영되지 않습니다. 가격이 없는 종목을 제외한 뒤 페이지를 채우며, 다른 페이지의 종목은 탈락이 아닙니다.")
    with st.expander("ⓘ 차트 색상·기준선 보기", expanded=False):
        st.markdown(
            "- **초록 점선:** 베이스 피벗\n"
            "- **초록 음영:** 바이존(-2.5%~+5%)\n"
            "- **빨강 점선:** 손절 기준가\n"
            "- **파랑/주황/보라:** 20/50/200일 이동평균선"
        )
    render_chart_missing_details(b, missing_rows)

    compact_mode = n_cols >= 2
    for i in range(0, len(visible), n_cols):
        cols = st.columns(n_cols)
        for offset, (col, (r, ch, mkt, tkr, ck)) in enumerate(zip(cols, visible[i:i + n_cols])):
            with col.container(border=True):
                tv_url, inf_url, inf_lbl = ext_links(mkt, tkr)
                dpiv = float(getattr(r, "dist_to_pivot_pct", 0) or 0) * 100.0
                stp = float(getattr(r, "stop_dist_pct", 0) or 0) * 100.0
                pos = float(getattr(r, "position_size_pct", 0) or 0)
                f_badge = getattr(r, "fund_badge", "-")
                sig_badge = getattr(r, "signal_badges", "-")
                sig_str = f" · {sig_badge}" if sig_badge != "-" else ""
                lead_mark = "👑" if str(getattr(r, "is_leading_sector", "")).lower() == "true" else ""
                p_alloc = finite_number(getattr(r, "portfolio_alloc_pct", None))
                ex_sig = str(getattr(r, "exit_signal", "✅정상"))
                rs_4w = float(getattr(r, "rs_change_4w", 0) or 0)
                sec_pwr = float(getattr(r, "sector_power", 50) or 50)
                if "🚨" in ex_sig or "⚠️" in ex_sig or "✂️" in ex_sig or "과열" in str(r.setup_stage) or "📉RS둔화" in sig_badge:
                    pri_badge = f"🔴 **[보유·추격 주의 · {ex_sig}]**"
                elif p_alloc > 0 and lead_mark:
                    pri_badge = f"🟢 **[최우선 공략 · 포트배분 {p_alloc:.1f}%]**"
                elif getattr(r, "actionable_now", False):
                    alloc_label = f"모델 배분 {p_alloc:.1f}%" if np.isfinite(p_alloc) and p_alloc > 0 else "모델 배분 0%" if p_alloc == 0 else "모델 배분 미확인"
                    pri_badge = f"🎯 **[게시 매수 검토 적격 · {alloc_label}]**"
                elif str(getattr(r, "wide_stop_actionable", "")).lower() == "true":
                    pri_badge = f"🟠 **[와이드 스톱 관찰 · 축소비중 {pos:.0f}%]**"
                elif "🔵블루닷" in sig_badge or "VCP" in str(r.setup_stage):
                    pri_badge = "🔵 **[선행 수축·블루닷 관찰]**"
                else:
                    pri_badge = "⚪ **[추세 추적]**"
                st.markdown(f"{pri_badge} &nbsp; **{r.name}** (`{tkr}` · {r.sub_market}){sig_str} &nbsp; [📈TV]({tv_url}) · [{inf_lbl}]({inf_url})")
                st.caption(
                    f"{lead_mark}{r.sector} P{sec_pwr:.0f} · 탑다운 **{getattr(r, 'topdown_score', 0):.1f}**"
                    f" · RS **{r.rs_score:.0f}** ({rs_4w:+.0f}) · {r.setup_stage}"
                )
                st.caption(f"실적 {f_badge} · 피벗 **{dpiv:+.1f}%** · 손절 **{stp:.1f}%**")
                st.caption(f"판정 기준일 {getattr(r, 'as_of', '미확인')} · 차트 마지막 봉 {ch.get('d', ['미확인'])[-1]} · 피벗·손절은 현재 게시 기준선입니다. 과거 최초 신호선이 아닙니다.")
                note = getattr(r, "portfolio_alloc_note", None)
                if isinstance(note, str) and note.strip():
                    st.caption(f"모델 배분 사유: {note}")
                fig_json = cached_candle_json(run_id, ck, mkt, bars_win, compact_mode, theme_mode, ch)
                fig = go.Figure(json.loads(fig_json))
                show_plotly(fig, key=f"mc_{mkt}_{tkr}_{start_i + i}_{n_cols}_{bars_win}")
                st.caption(stock_grid_health_summary(r._asdict()))
                selected = st.session_state.get("grid_detail_key") == ck
                st.button("▲ 진단 접기" if selected else "🔍 정밀 진단 보기",
                          key=f"grid_detail_btn_{mkt}_{tkr}_{start_i + i + offset}",
                          use_container_width=True, on_click=choose_grid_stock, args=(ck,))


        selected_key = st.session_state.get("grid_detail_key")
        expanded_item = next((item for item in visible[i:i + n_cols] if item[4] == selected_key), None)
        if expanded_item is not None:
            render_grid_stock_diagnostics(b, pd.Series(expanded_item[0]._asdict()), expanded_item[1])
    render_missing_stock_analyzer(b, f, missing_rows)


def render_garp_scanner(f: pd.DataFrame, track_choice: str, active_mode: str) -> None:
    st.markdown("#### 💎 GARP 스캐너 — 추세 모멘텀(RS) × 성장성 밸류에이션(PEG)")
    g_df = f.dropna(subset=["rs_score", "peg"]).copy()
    g_df = g_df[(g_df["peg"] > 0) & (g_df["peg"] <= 6.0)]

    turn_df = f[f.get("eps_status", pd.Series("", index=f.index)).astype(str).str.upper() == "TURNAROUND"].copy()

    if g_df.empty and turn_df.empty:
        st.info("현재 필터링된 종목 중 유효한 PEG(0~6) 또는 흑자전환 데이터가 있는 종목이 없습니다.")
        return

    if not g_df.empty:
        fig = go.Figure()
        x_min = min(68.0, float(g_df["rs_score"].min()) - 2)
        y_max = max(3.5, float(g_df["peg"].max()) + 0.3)

        fig.add_shape(type="rect", x0=80, x1=100, y0=0, y1=1.5, fillcolor=PALETTE["green"], opacity=0.10, line_width=0)
        fig.add_vline(x=80, line_dash="dash", line_color=PALETTE["green"], line_width=1.2)
        fig.add_hline(y=1.5, line_dash="dash", line_color=PALETTE["green"], line_width=1.2)
        fig.add_hline(y=1.0, line_dash="dot", line_color=PALETTE["indigo"], line_width=1.1, annotation_text="PEG 1.0 (초저평가 기준)")
        fig.add_annotation(
            x=99, y=0.15, text="<b>💎 GARP 스위트 스팟 (RS ≥ 80 & PEG ≤ 1.5)</b>",
            showarrow=False, xanchor="right", yanchor="bottom", font=dict(color=PALETTE["green"], size=12),
        )

        for trk, grp in g_df.groupby("sub_market"):
            sz = (grp["eps_growth_pct"].fillna(20).clip(10, 200) * 0.12 + 10).clip(10, 32)
            fig.add_trace(go.Scatter(
                x=grp["rs_score"], y=grp["peg"], mode="markers+text", name=str(trk),
                text=grp["name"], textposition="top center", textfont=dict(size=10),
                marker=dict(size=sz, opacity=0.85, line=dict(width=1, color="#1E293B")),
                customdata=np.stack([
                    grp["eps_growth_pct"].fillna(0),
                    grp["sector"].fillna("-"),
                    grp["setup_stage"].fillna("-"),
                ], axis=-1),
                hovertemplate="<b>%{text}</b><br>RS: %{x:.1f} · PEG: %{y:.2f}<br>EPS성장률: %{customdata[0]:+.1f}%<br>섹터: %{customdata[1]} · 셋업: %{customdata[2]}<extra></extra>",
            ))
        fig.update_layout(
            xaxis=dict(title="RS 상대강도 점수 (우측일수록 강한 추세)", range=[x_min, 100.5]),
            yaxis=dict(title="PEG 배수 (하단일수록 이익성장 대비 저평가)", range=[0, y_max]),
        )
        show_plotly(clean_chart(fig, 430, title="💎 RS × PEG GARP 사분면 (원 크기 = EPS 성장률)"), key=f"plt_garp_{track_choice}_{active_mode}")

    if not turn_df.empty:
        st.markdown(f"##### ✨ 흑자전환(TURNAROUND) 모멘텀 리더 ({len(turn_df)}종목)")
        t_cols = [c for c in ("name", "ticker", "sub_market", "sector", "setup_stage", "bc_display", "rs_with_track", "topdown_score", "last_close", "dist_to_pivot_pct", "position_size_pct") if c in turn_df.columns]
        t_show = turn_df[t_cols].copy()
        if "dist_to_pivot_pct" in t_show.columns:
            t_show["dist_to_pivot_pct"] = t_show["dist_to_pivot_pct"] * 100.0
        stock_dataframe(t_show.rename(columns=LABELS), hide_index=True, width="stretch", column_config=_common_column_config())


def risk_reward_bar(last: float, stop: float, pivot: float, mkt: str) -> go.Figure | None:
    if not (np.isfinite(last) and np.isfinite(stop) and last > 0 and stop > 0 and last > stop):
        return None
    entry = max(last, pivot) if (np.isfinite(pivot) and pivot > 0) else last
    risk = max(entry - stop, entry * 0.01)
    r2, r3 = entry + 2.0 * risk, entry + 3.0 * risk

    dark = is_dark_mode()
    c_loss = "rgba(225, 29, 72, 0.32)" if dark else PALETTE["red_bg"]
    c_r2 = "rgba(16, 185, 129, 0.28)" if dark else PALETTE["green_bg"]
    c_r3 = "rgba(5, 150, 105, 0.42)" if dark else "#A7F3D0"
    fig = go.Figure()
    fig.add_trace(go.Bar(y=["가격"], x=[entry - stop], base=[stop], orientation="h", name="1R 손절 구간", marker=dict(color=c_loss, cornerradius=4)))
    fig.add_trace(go.Bar(y=["가격"], x=[r2 - entry], base=[entry], orientation="h", name="+2R 목표 구간", marker=dict(color=c_r2, cornerradius=4)))
    fig.add_trace(go.Bar(y=["가격"], x=[r3 - r2], base=[r2], orientation="h", name="+3R 목표 구간", marker=dict(color=c_r3, cornerradius=4)))
    for label, val, col, sym in (
        ("손절가", stop, PALETTE["red"], "triangle-down"),
        ("현재가", last, PALETTE["blue"], "diamond"),
        ("베이스피벗", pivot if (np.isfinite(pivot) and pivot > 0) else entry, PALETTE["indigo"], "circle"),
        ("+2R", r2, PALETTE["green"], "star"),
        ("+3R", r3, "#047857", "star"),
    ):
        fig.add_trace(go.Scatter(
            x=[val], y=["가격"], mode="markers+text", name=label,
            text=[f"<b>{label}</b><br>{fmt_price(val, mkt)}"], textposition="top center",
            marker=dict(size=12, color=col, symbol=sym),
        ))
    pad = (r3 - stop) * 0.12
    fig.update_layout(barmode="overlay", xaxis=dict(range=[stop - pad, r3 + pad]), yaxis=dict(showticklabels=False))
    return clean_chart(fig, 160, legend=False, title="📐 손절가(1R) · 현재가 · 베이스피벗 · 목표가(+2R/+3R) 손익비 스케일")


def render_stock_sepa_checklist(r: pd.Series) -> None:
    """Shared SEPA diagnostics; existing eligibility and risk logic unchanged."""
    with st.container(border=True):
        st.markdown("##### 🩺 프로젝트 규칙 · 7대 추세 건강도 & 모델 배분 진단")
        is_min = str(r.get("strict_minervini", "")).lower() == "true"
        rs_v = float(r.get("rs_score", 0) or 0)
        rs_4w = float(r.get("rs_change_4w", 0) or 0)
        is_lead = str(r.get("is_leading_sector", "")).lower() == "true"
        sec_pwr = float(r.get("sector_power", 50) or 50)
        r10 = finite_number(r.get("range_10d_pct")) * 100.0
        vdry = float(r.get("vol_dry_up_ratio", np.nan))
        v50_r = float(r.get("vol_ratio_50d", np.nan))
        atr50 = finite_number(r.get("atr_from_50dma"))
        dd25 = finite_number(r.get("dist_days_25d"))
        d_res = str(r.get("down_resilient", "")).lower() == "true"
        act_now = bool(r.get("actionable_now", False))
        wide_act = str(r.get("wide_stop_actionable", "")).lower() == "true"
        p_alloc = float(r.get("portfolio_alloc_pct", 0) or 0)

        chk1 = f"{'✅' if is_min else '⚠️'} **정배열 구조**: {r.get('tier', '-')} ({'200일선 상승·정배열 완비' if is_min else '일부 이평선 조건 대기'})"
        chk2 = f"{'✅' if (rs_v >= 80 and rs_4w >= -2) else '⚠️'} **상대강도 모멘텀**: RS {rs_v:.1f} [{r.get('sub_market', '-')}] (4주 변화 {rs_4w:+.1f})"
        chk3 = f"{'✅' if is_lead else '⚪'} **섹터 위상**: {'👑 주도섹터' if is_lead else '일반섹터'} ({r.get('sector', '-')} · 파워 {sec_pwr:.0f})"
        vdry_s = f"{vdry:.2f}배" if np.isfinite(vdry) else "-"
        v50_s = f" · 50일거래량 {v50_r:.2f}배" if np.isfinite(v50_r) else ""
        chk4 = f"{'✅' if (r10 <= 10.0 or (np.isfinite(vdry) and vdry <= 0.75)) else '⚪'} **베이스 수축·거래량 건조**: 10일 변동폭 {r10:.1f}% · 거래량건조(5/50일) {vdry_s}{v50_s}"
        chk5 = (f"{'✅' if atr50 <= 3.5 else '🚨'} **이격도 안전성**: 50일선 대비 {atr50:.1f} ATR ({'안전 타점 영역' if atr50 <= 3.5 else '단기 과열 · 추격매수 금지'})"
                if np.isfinite(atr50) else "⚪ **이격도 안전성**: 자료 없음")
        chk6 = (f"{'✅' if dd25 < 4 else '⚠️'} **매물 소화·방어력**: 25일 분배일 {dd25:.0f}일 · 조정장 방어력 {'🛡️보유' if d_res else '보통'}"
                if np.isfinite(dd25) else "⚪ **매물 소화·방어력**: 분배일 자료 없음")
        if p_alloc > 0:
            chk7 = f"🟢 **실전 배분 판정**: **즉시 배분 가능** (셋업 적격 + 현재 한도 내 **포트 {p_alloc:.1f}%** 할당)"
        elif act_now:
            chk7 = f"🟡 **모델 배분 판정**: **게시 적격 · 배분 {number_text(p_alloc)}%** ({r.get('portfolio_alloc_note', '세부 배분 사유 미제공')})"
        elif wide_act:
            chk7 = f"🟠 **실전 배분 판정**: **와이드 스톱 관찰 등급** (손절폭 8~12% 고베타 주도주 · 리스크 비례 축소비중 {float(r.get('position_size_pct', 0)):.1f}%)"
        else:
            reason = build_exclusion_reason(pd.DataFrame([r])).iloc[0]
            chk7 = f"⚪ **실전 배분 판정**: **관찰/보유 영역** ({r.get('setup_stage', '-')} · {reason})"

        dc1, dc2 = st.columns(2)
        for item in (chk1, chk2, chk3, chk4):
            dc1.markdown(f"- {item}")
        for item in (chk5, chk6, chk7):
            dc2.markdown(f"- {item}")



def render_single_stock_deep_dive(b: Bundle, f: pd.DataFrame) -> None:
    labels = [f"{r.name} ({r.ticker}) · {r.sub_market} · 탑다운 {getattr(r, 'topdown_score', 0):.1f} (RS {r.rs_score:.0f}) · {r.fund_badge} · {r.setup_stage}" for r in f.itertuples()]
    idx = st.selectbox("정밀 분석할 종목 선택", range(len(labels)), format_func=lambda i: labels[i], key="stock_pick")
    r = f.iloc[idx]
    mkt, tkr = str(r["market"]), str(r["ticker"])

    m = st.columns(6)
    m[0].metric("현재가", fmt_price(r.get("last_close"), mkt), border=True, help=TOOLTIPS["last_close"])
    m[1].metric(f"RS ({r.get('sub_market', '-')})", f"{r.get('rs_score', 0):.1f}", f"{float(r['rs_change_4w']):+.1f}" if pd.notna(r.get("rs_change_4w")) else None, border=True, help=TOOLTIPS["rs_score"])
    m[2].metric("실적 · 밸류(PEG)", str(r.get("fund_badge", "-")), border=True, help=TOOLTIPS["peg"])
    m[3].metric("베이스피벗 (괴리율)", fmt_price(r.get("pivot_price"), mkt), pct(r.get("dist_to_pivot_pct")) if pd.notna(r.get("dist_to_pivot_pct")) else None, border=True, help=TOOLTIPS["dist_to_pivot_pct"])
    m[4].metric("권장 손절가 (손절폭)", fmt_price(r.get("stop_price"), mkt), pct(r.get("stop_dist_pct")) if pd.notna(r.get("stop_dist_pct")) else None, border=True, help=TOOLTIPS["stop_dist_pct"])
    alloc_v = r.get("portfolio_alloc_pct")
    m[5].metric("권장비중 (포트배분)", f"{float(r.get('position_size_pct', 0)):.1f}%", f"포트 {float(alloc_v):.1f}%" if pd.notna(alloc_v) else "대기", delta_color="off", border=True, help=TOOLTIPS["position_size_pct"])

    rr_fig = risk_reward_bar(
        float(pd.to_numeric(r.get("last_close"), errors="coerce")),
        float(pd.to_numeric(r.get("stop_price"), errors="coerce")),
        float(pd.to_numeric(r.get("pivot_price"), errors="coerce")),
        mkt,
    )
    if rr_fig is not None:
        show_plotly(rr_fig, key=f"plt_rr_{mkt}_{tkr}")

    render_stock_sepa_checklist(r)

    base_available = render_base_panel(r)
    key = f"{mkt}:{tkr.zfill(6) if mkt == 'KR' else tkr.upper()}"
    ch = b.charts.get(key)
    if ch:
        bars = st.radio("정밀 차트 표시 기간", CHART_PERIODS, index=4, horizontal=True, format_func=chart_period_label, key="deep_bars")
        count = min(bars, len(ch.get("d", [])))
        figure = candle_chart(ch, mkt, f"{r['name']} ({tkr}) · {count}봉 정밀 차트", bars=bars, compact=False)
        if base_available and st.checkbox("베이스 관측 구간 표시", value=False, key="bc_overlay"):
            figure = add_base_overlay(figure, ch, r, bars)
        show_plotly(figure, key=f"plt_deep_c_{mkt}_{tkr}_{bars}")
        if count:
            st.caption(f"실제 표시: {count}봉 · {ch['d'][-count]} ~ {ch['d'][-1]}")
        if count < bars:
            st.info(f"요청한 {bars}봉 중 게시된 {count}봉만 표시합니다. 새 게시 데이터 또는 종목별 수집 이력에 따라 범위가 달라집니다.")

    hist = b.h_cand[(b.h_cand["market"] == mkt) & (b.h_cand["ticker"] == tkr)] if not b.h_cand.empty else pd.DataFrame()
    if len(hist) >= 2:
        hist = num(hist, "rs_score", "topdown_score").sort_values("date")
        fig = go.Figure()
        fig.add_scatter(x=hist["date"], y=hist["rs_score"], name="RS 점수", mode="lines+markers", line=dict(color=PALETTE["indigo"], width=2.2))
        if "topdown_score" in hist.columns:
            fig.add_scatter(x=hist["date"], y=hist["topdown_score"], name="탑다운 점수", mode="lines+markers", line=dict(color=PALETTE["green"], dash="dot"))
        fig.update_xaxes(type="category", nticks=8)
        show_plotly(clean_chart(fig, 250, title="📈 RS · 탑다운 점수 누적 이력 추이"), key=f"plt_deep_rs_{mkt}_{tkr}")


def tab_playbook(b: Bundle) -> None:
    st.caption((b.summ.get("metric_definitions") or {}).get("actionable",
               "기술적 적격은 최신 시세 확보를 보증하지 않습니다. 종목·벤치마크 기준일과 가격 확인 경고를 별도로 확인하세요."))
    df = prepare_candidates(b)
    with st.expander("전술 판정과 날짜 읽는 법", expanded=False):
        st.markdown(
            "- **VCP:** 스윙 낙폭 또는 10일 구간 3개의 최대낙폭 감소와 ATR 수축을 결합한 규칙입니다. 정형 VCP 패턴 확정이나 거래량 감소의 필수 확인을 뜻하지 않습니다.\n"
            "- **피벗:** 당일을 제외한 가격·거래량으로 산출하며 매 거래일 다시 계산됩니다. 최초 피벗 확정일은 게시하지 않습니다.\n"
            "- **피벗돌파:** 기준일 종가가 피벗~+5%, 전일 대비 +1% 이상, 직전 50봉 평균 거래량의 1.5배 이상인 당일 조건입니다. 최초 돌파·유지·재돌파를 구별한 이벤트가 아닙니다.\n"
            "- **눌림목:** 정배열에서 20/50일선 근접 등으로 분류합니다. 이전 돌파나 지지 회복을 모두 검증했다는 뜻은 아닙니다.\n"
            "- **매수 구간:** 종가 기준 피벗 -2.5%~+5% 가격대이며 기술적 적격·시장별 모델 배분과는 별개입니다. 눌림목 적격은 피벗 -12%까지 허용할 수 있습니다.\n"
            "- **신규·유지:** 후보 편입 상태입니다. 신호 원장의 first_seen은 14일 재기록 제한이 있는 게시일이며 최초 기술적 돌파일이 아닙니다."
        )
    if df.empty:
        st.info("종목 데이터가 없습니다.")
        return
    regs = (b.summ or {}).get("regimes") or {}
    rem_info = compute_remaining_portfolio(b)

    with st.container(border=True):
        top_c1, top_c2 = st.columns([1.3, 1.7])
        track_opts = ["전체 (KR+US)", "KR (KOSPI+KOSDAQ)", "US (NASDAQ100+SP500)"] + [t for t in TRACKS if t in set(df["sub_market"])]
        if "pb_track" not in st.session_state and st.session_state.get("_saved_pb_track") in track_opts:
            st.session_state["pb_track"] = st.session_state["_saved_pb_track"]
        track_choice = top_c1.selectbox("① 대상 시장 / 트랙 선택", track_opts, key="pb_track", on_change=remember_playbook_input, args=("pb_track",))
        st.session_state["_saved_pb_track"] = track_choice

        rec_mode = detect_default_mode(regs, track_choice)
        mode_labels = {
            "ATTACK": "🟢 상승장 공략 (돌파·바이존)",
            "CAUTION": "🟡 조정장 관찰 (VCP·방어력)",
            "RISK_OFF": "🔴 하락장 방어 (이탈·청산점검)",
        }
        mode_order = ["ATTACK", "CAUTION", "RISK_OFF"]

        context = (track_choice, rec_mode)
        if st.session_state.get("_last_pb_context") != context:
            st.session_state["_last_pb_context"] = context
            st.session_state["pb_mode"] = rec_mode
        elif "pb_mode" not in st.session_state:
            saved_mode = st.session_state.get("_saved_pb_mode", rec_mode)
            st.session_state["pb_mode"] = saved_mode if saved_mode in mode_order else rec_mode

        active_mode = top_c2.radio(
            f"② 국면별 전술 모드 (선택 트랙 권장: {mode_labels[rec_mode]})",
            mode_order,
            format_func=lambda k: mode_labels[k],
            horizontal=True,
            key="pb_mode",
            on_change=remember_playbook_input,
            args=("pb_mode",),
            help="시장 레짐에 따라 자동으로 최적의 공략 모드가 세팅되며, 수동으로 전환하여 다른 국면의 종목을 탐색할 수 있습니다.",
        )
        st.session_state["_saved_pb_mode"] = active_mode

        f1, f2, f3, f4, f5, f6 = st.columns([1.3, 1.0, 0.85, 0.8, 1.05, 1.1])
        if track_choice == "KR (KOSPI+KOSDAQ)":
            sub_df = df[df["market"] == "KR"]
        elif track_choice == "US (NASDAQ100+SP500)":
            sub_df = df[df["market"] == "US"]
        elif track_choice in TRACKS:
            sub_df = df[df["sub_market"] == track_choice]
        else:
            sub_df = df

        sec_list = sorted(sub_df["sector"].dropna().unique()) if "sector" in sub_df.columns else []
        sec_pick = f1.multiselect("섹터 필터", sec_list, default=[], placeholder="전체 섹터")
        rs_lo = float(int(sub_df["rs_score"].min())) if not sub_df.empty else 70.0
        rs_min = f2.slider("RS 하한", min_value=rs_lo, max_value=99.0, value=rs_lo, step=1.0, help=TOOLTIPS["rs_score"])
        has_any_lead = bool(tf(sub_df["is_leading_sector"]).any()) if ("is_leading_sector" in sub_df.columns and not sub_df.empty) else False
        only_lead = f3.toggle("👑 주도섹터만", value=(active_mode == "ATTACK" and has_any_lead))
        only_top20 = f4.toggle("🏆 Top20만", value=False)
        only_garp = f5.toggle("💎 저PEG·흑자전환만", value=False, help="0 < PEG ≤ 1.5 또는 흑자전환(TURNAROUND) 종목만 필터링")
        q = f6.text_input("종목 검색", "", placeholder="이름 또는 티커", help=TOOLTIPS["name"])

    filtered = filter_by_tactical_mode(sub_df, active_mode, only_lead, only_top20, only_garp, rs_min, sec_pick, q)
    filtered = observation_filters(filtered, b.base_source)
    garp_cnt = int(is_garp_or_turnaround(filtered, 1.5).sum()) if not filtered.empty else 0
    st.caption(f"전술 필터 결과: **{len(filtered)}종목** (선택 트랙 통과 모수 {len(sub_df)}종목 · 이 중 💎저PEG/흑자전환 **{garp_cnt}종목** · 기본 정렬: 전술 그룹·우선순위 후 탑다운·RS, 동률은 시장·티커순)")

    if filtered.empty:
        st.session_state["grid_page"] = 1
        st.session_state["grid_detail_key"] = None
        st.warning("현재 전술 모드 및 필터 조건에 해당하는 종목이 없습니다. '👑 주도섹터만' 토글을 끄거나 다른 전술 모드를 눌러보세요.")
        return

    st.caption("베이스 순번은 관측용 참고 정보입니다. 자료가 없거나 이력이 부족하면 미확인으로 표시하며, 매매 적격·비중에는 반영하지 않습니다.")
    display_filtered = allocation_view(filtered, rem_info)
    render_mode_specific_table(display_filtered, active_mode, b.dropped, track_choice, rem_info)

    st.divider()
    sub_views = [
        "🖼️ 멀티차트 · 종목별 정밀 분석",
        "💎 GARP 스캐너 (RS × PEG · 흑자전환)",
    ]
    sel_sub_view = st.radio("하단 정밀 뷰 선택", sub_views, horizontal=True, label_visibility="collapsed", key="pb_sub_view")
    if sel_sub_view == sub_views[0]:
        render_multi_chart_grid(b, display_filtered)
    else:
        render_garp_scanner(filtered, track_choice, active_mode)


# ════════════════════════════════════════════════════════════════════
# 7. 탭 4: 🧪 실전 검증 (MAE 경로상 저점 안내 문구 명시)
# ════════════════════════════════════════════════════════════════════
def clean_validation_metrics(frame: pd.DataFrame) -> pd.DataFrame:
    """Keep legacy hold simulations visible, but never mix horizons or fill unknown quality."""
    out = frame.copy()
    def numeric(key):
        return pd.to_numeric(out.get(key, pd.Series(index=out.index, dtype=float)), errors="coerce").replace([np.inf,-np.inf],np.nan)
    complete = numeric("ret_10d").notna() & (numeric("bars") >= 10)
    complete &= tf(out.get("calendar_complete_10d",pd.Series(index=out.index,dtype=str)))
    out["ret_10d"] = numeric("ret_10d").where(complete)
    version = out.get("calculation_version", pd.Series(index=out.index,dtype=str)).eq("fixed_horizon_dates_v2")
    path = tf(out.get("path_ohlc_complete_10d",pd.Series(index=out.index,dtype=str)))
    stop = out.get("stop_price_basis",pd.Series(index=out.index,dtype=str)).eq("matched") & path
    stop &= (numeric("stop_price") > 0) & (numeric("entry_price") > numeric("stop_price"))
    out["ex_10d"] = numeric("ex_10d").where(complete & version)
    out["stop_aware_r_10d"] = numeric("stop_aware_r_10d").where(complete & version & stop)
    for key in ("mae_10d_pct","mfe_10d_pct"):
        out[key] = numeric(key).where(complete & version & path)
    raw = out.get("stop_hit_10d",pd.Series(index=out.index,dtype=str)).astype(str).str.lower().str.strip()
    valid_stop = raw.isin(("true","false","1","0","1.0","0.0")) & complete & version & stop
    out["stop_hit_10d"] = out.get("stop_hit_10d",pd.Series(index=out.index,dtype=object)).where(valid_stop)
    return out


def outcome_metrics(g: pd.DataFrame) -> pd.Series:
    g = clean_validation_metrics(g)
    def series(col: str) -> pd.Series:
        if col not in g.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(g[col], errors="coerce").dropna()

    r10, ex10 = series("ret_10d"), series("ex_10d")
    r_mult = series("stop_aware_r_10d")
    stop10_col = "stop_hit_10d"
    if stop10_col in g.columns:
        raw_stop = g[stop10_col]
        valid_stop = raw_stop.notna() & ~raw_stop.astype(str).str.strip().str.lower().isin(("", "nan", "none", "null"))
        stop = tf(raw_stop.loc[valid_stop])
    else:
        stop = pd.Series(dtype=bool)
    mae10 = series("mae_10d_pct")
    return pd.Series({
        "신호": len(g), "10일 평가": len(r10),
        "승률(10일)": float((r10 > 0).mean() * 100) if len(r10) else np.nan,
        "평균수익": float(r10.mean()) if len(r10) else np.nan,
        "중앙값": float(r10.median()) if len(r10) else np.nan,
        "평균 초과": float(ex10.mean()) if len(ex10) else np.nan,
        "초과 승률": float((ex10 > 0).mean() * 100) if len(ex10) else np.nan,
        "평균 R": float(r_mult.mean()) if len(r_mult) else np.nan,
        "손절 터치%": float(stop.mean() * 100) if len(stop) else np.nan,
        "평균 MAE": float(mae10.mean()) if len(mae10) else np.nan,
        "초과 평가": len(ex10), "손절R 평가": len(r_mult), "손절 평가": len(stop), "MAE 평가": len(mae10),
    })


def group_table(o: pd.DataFrame, by: str, label: str) -> pd.DataFrame:
    cols = ["신호", "10일 평가", "승률(10일)", "평균수익", "중앙값", "평균 초과", "초과 승률", "평균 R", "손절 터치%", "평균 MAE"]
    if o is None or o.empty or by not in o.columns:
        return pd.DataFrame(columns=cols).rename_axis(label)
    rows = []
    for k, g in o.dropna(subset=[by]).groupby(by, dropna=True):
        stats = outcome_metrics(g).to_dict()
        stats[label] = ("⚠️ " if stats["10일 평가"] < MIN_SAMPLE else "") + str(k)
        rows.append(stats)
    if not rows:
        return pd.DataFrame(columns=cols).rename_axis(label)
    return pd.DataFrame(rows).set_index(label).sort_values("평균 초과", ascending=False, na_position="last")


def render_live_signals_dashboard(live: pd.DataFrame) -> None:
    if live.empty:
        return
    st.markdown(f"#### ⏳ 현재 추적 중인 활성 신호 ({len(live)}건)")

    k = st.columns(4)
    status = live["status"].astype(str) if "status" in live.columns else pd.Series("미분류", index=live.index)
    wait_cnt = int((status == "대기").sum())
    prog_cnt = int((status == "진행").sum())
    cur_valid = pd.to_numeric(live["cur_ret"], errors="coerce").dropna() if "cur_ret" in live.columns else pd.Series(dtype=float)
    avg_cur = f"{cur_valid.mean():+.2f}%" if not cur_valid.empty else "집계 대기(익일 시가 진입)"
    stop_cnt = int(tf(live["stop_hit"]).sum()) if "stop_hit" in live.columns else 0

    k[0].metric("총 추적 신호", f"{len(live)}건", f"진행 {prog_cnt} · 진입대기 {wait_cnt}", delta_color="off", border=True)
    k[1].metric("진행 신호 평균 수익", avg_cur, border=True)
    k[2].metric("손절 터치 발생", f"{stop_cnt}건", border=True)
    setup_count = live["setup_stage"].nunique() if "setup_stage" in live.columns else 0
    k[3].metric("활성 셋업 유형", f"{setup_count}종류", border=True)

    c1, c2 = st.columns(2)
    with c1:
        with st.container(border=True):
            if {"setup_stage", "market"}.issubset(live.columns):
                st_cnt = live.groupby(["setup_stage", "market"], as_index=False).size()
            else:
                st_cnt = pd.DataFrame(columns=["setup_stage", "market", "size"])
            fig_s = px.bar(
                st_cnt, x="size", y="setup_stage", color="market", orientation="h",
                barmode="stack", color_discrete_map={"KR": PALETTE["indigo"], "US": PALETTE["green"]},
                labels={"size": "신호 건수", "setup_stage": "셋업 단계", "market": "시장"},
            )
            if not st_cnt.empty:
                show_plotly(clean_chart(fig_s, 250, title="📊 진행·대기 신호 셋업 단계별 분포"), key="plt_live_setups")
            else:
                st.caption("셋업/시장 컬럼이 없어 분포 차트를 생략했습니다.")

    with c2:
        with st.container(border=True):
            if not cur_valid.empty:
                prog_df = live.copy()
                prog_df["cur_ret"] = pd.to_numeric(prog_df["cur_ret"], errors="coerce")
                prog_df = prog_df.dropna(subset=["cur_ret"]).sort_values("cur_ret", ascending=True).tail(15)
                name_col = "name" if "name" in prog_df.columns else ("ticker" if "ticker" in prog_df.columns else None)
                if name_col is None:
                    prog_df["_display_name"] = prog_df.index.astype(str)
                    name_col = "_display_name"
                colors = [PALETTE["red"] if v >= 0 else PALETTE["green_soft"] for v in prog_df["cur_ret"]]
                fig_p = go.Figure(go.Bar(
                    x=prog_df["cur_ret"], y=prog_df[name_col], orientation="h",
                    marker=dict(color=colors, cornerradius=4),
                    text=[f"{v:+.1f}%" for v in prog_df["cur_ret"]], textposition="outside",
                ))
                fig_p.update_layout(xaxis_title="현재 수익률 (%)")
                show_plotly(clean_chart(fig_p, 250, legend=False, title="📈 진행 중 신호의 최신 모의 평가 수익률 (기존 20일 참고)"), key="plt_live_rets")
            elif "sector" in live.columns:
                sec_cnt = live["sector"].value_counts().head(10).iloc[::-1]
                fig_sec = go.Figure(go.Bar(
                    x=sec_cnt.values, y=sec_cnt.index, orientation="h",
                    marker=dict(color=PALETTE["blue_soft"], cornerradius=4),
                    text=sec_cnt.values, textposition="outside",
                ))
                fig_sec.update_layout(xaxis_title="신호 건수")
                show_plotly(clean_chart(fig_sec, 250, legend=False, title="🏭 신규 발생 신호 상위 섹터 분포 (익일 진입 대기)"), key="plt_live_secs")

    show_cols = [c for c in ("as_of", "market", "sub_market", "name", "ticker", "sector", "setup_stage", "tier", "status", "bars", "entry_price", "stop_price", "cur_ret", "mae_pct", "stop_hit") if c in live.columns]
    if not show_cols:
        st.info("활성 신호의 표시 가능한 컬럼이 없습니다.")
        return
    stock_dataframe(
        live[show_cols].rename(columns={
            "as_of": "신호일", "market": "시장", "sub_market": "트랙", "name": "종목명", "ticker": "티커",
            "sector": "섹터", "setup_stage": "셋업 단계", "tier": "티어", "status": "상태",
            "bars": "경과봉", "entry_price": "진입가", "stop_price": "손절가",
            "cur_ret": "현재수익(%)", "mae_pct": "MAE(%)", "stop_hit": "손절터치",
        }),
        hide_index=True, width="stretch",
    )


def render_validation_charts(done: pd.DataFrame) -> None:
    """Two distinct ten-day distributions; no synthetic equity curve."""
    if len(done) < MIN_SAMPLE:
        st.caption(f"유효한 10일 평가 {len(done)}건 · {MIN_SAMPLE}건 미만에서는 집단 분포 차트를 보류합니다.")
        return
    left, right = st.columns(2, gap="small")
    with left:
        values = pd.to_numeric(done.get("ret_10d", pd.Series(dtype=float)), errors="coerce").dropna()
        if not values.empty:
            fig = go.Figure(go.Histogram(
                x=values, nbinsx=min(30,max(6,int(np.sqrt(len(values))*2))),
                marker_color=PALETTE["indigo"], opacity=0.85,
            ))
            fig.add_vline(x=0, line_dash="dash", line_color="#64748B")
            fig.add_vline(x=float(values.median()), line_dash="dot",
                          line_color=PALETTE["amber"])
            fig.update_layout(xaxis_title="10일 수익률 (%)", yaxis_title="신호 건수")
            show_plotly(clean_chart(fig, 320, legend=False, title=f"10일 수익률 분포 · n={len(values)}"),
                        key="plt_val_hist")
        else:
            st.caption("유효한 10일 수익률이 없습니다.")
    with right:
        fields = {"mae_10d_pct", "mfe_10d_pct"}
        mm = done.dropna(subset=list(fields)).copy() if fields.issubset(done) else pd.DataFrame()
        if not mm.empty:
            if "stop_hit_10d" in mm:
                mm["hit_label"] = np.where(
                    mm["stop_hit_10d"].isna(), "손절 미확인",
                    np.where(tf(mm["stop_hit_10d"]), "손절 터치", "손절 미터치"),
                )
                color_arg = "hit_label"
            else:
                color_arg = None
            hover_cols = [c for c in ("name", "ticker", "as_of", "setup_stage", "ret_10d")
                          if c in mm]
            fig = px.scatter(
                mm, x="mae_10d_pct", y="mfe_10d_pct", color=color_arg,
                hover_data=hover_cols or None,
            )
            fig.add_vline(x=0, line_dash="dot", line_color="#64748B")
            fig.add_hline(y=0, line_dash="dot", line_color="#64748B")
            fig.update_layout(xaxis_title="10일 MAE (%)", yaxis_title="10일 MFE (%)")
            show_plotly(clean_chart(fig, 320, title=f"10일 MFE vs MAE · 유효 n={len(mm)}"),
                        key="plt_val_mm")
        else:
            st.caption("확인된 10일 고가·저가 경로(MFE/MAE)가 없습니다.")
    st.caption("두 그림은 10일 적격 표본의 분포이며, 일별 시계열이나 실제 포트폴리오 수익곡선이 아닙니다.")


def render_validation_group_comparison(done: pd.DataFrame, h_regime: pd.DataFrame,
                                       stock_dataframe_fn) -> None:
    """One selector + compact chart/table in place of four table-only tabs."""
    choices = {}
    if "setup_stage" in done:
        choices["셋업"] = ("setup_stage", done, "셋업")
    if (not h_regime.empty
            and {"track", "as_of", "signal_light"}.issubset(h_regime)
            and {"track", "as_of"}.issubset(done)):
        rg = h_regime[["track","as_of","signal_light"]].drop_duplicates(
            ["track","as_of"]
        ).copy()
        rg["레짐"] = rg["signal_light"].map(light_kind)
        linked = done.merge(rg[["track","as_of","레짐"]],
                            on=["track","as_of"], how="left")
        linked["레짐"] = linked["레짐"].fillna("알 수 없음")
        choices["진입 당시 레짐"] = ("레짐", linked, "레짐")
    if "as_of" in done:
        monthly = done.copy()
        monthly["신호 발생월"] = pd.to_datetime(
            monthly["as_of"], errors="coerce"
        ).dt.strftime("%Y-%m")
        choices["신호 발생월"] = ("신호 발생월", monthly, "월")
    if "sector" in done:
        choices["섹터"] = ("sector", done, "섹터")
    if not choices:
        return
    with st.expander("📋 집단 비교 · 셋업 / 레짐 / 발생월 / 섹터", expanded=False):
        kind = st.radio("비교 기준", list(choices), horizontal=True, key="v_group_mode")
        field, frame, label = choices[kind]
        gt = group_table(frame, field, label)
        if kind == "신호 발생월":
            gt = gt.sort_index()
        elif kind == "섹터":
            gt = gt.head(20)
        if gt.empty:
            st.caption("선택 범위에 비교할 집단이 없습니다.")
            return
        eligible = gt.loc[
            (pd.to_numeric(gt["10일 평가"], errors="coerce") >= MIN_SAMPLE) &
            pd.to_numeric(gt["중앙값"], errors="coerce").notna()
        ].reset_index()
        if not eligible.empty:
            eligible = eligible.sort_values("중앙값", ascending=True).tail(15)
            fig = go.Figure(go.Bar(
                x=eligible["중앙값"], y=eligible[label], orientation="h",
                marker_color=PALETTE["blue_soft"],
                text=[f"{v:+.1f}%" for v in eligible["중앙값"]],
                textposition="outside",
                customdata=eligible["10일 평가"],
                hovertemplate="%{y}<br>10일 수익률 중앙값 %{x:+.2f}%"
                              "<br>적격 신호 %{customdata}건<extra></extra>",
            ))
            fig.add_vline(x=0, line_dash="dash", line_color="#64748B")
            fig.update_layout(xaxis_title="10일 수익률 중앙값 (%)")
            show_plotly(clean_chart(fig, max(240,len(eligible)*31), legend=False,
                                  title=f"{kind} 비교 · 각 그룹 n≥{MIN_SAMPLE}"),
                        key="plt_val_group")
        else:
            st.caption(f"현재 모든 {kind} 그룹이 적격 {MIN_SAMPLE}건 미만입니다. 순위 판단은 보류하세요.")
        fmt = {
            "승률(10일)": "{:.0f}%", "평균수익": "{:+.2f}",
            "중앙값": "{:+.2f}", "평균 초과": "{:+.2f}",
            "초과 승률": "{:.0f}%", "평균 R": "{:+.2f}",
            "손절 터치%": "{:.0f}%", "평균 MAE": "{:.1f}",
        }
        stock_dataframe_fn(gt.style.format(fmt, na_rep="-"), width="stretch")
        st.caption("집단별 10일 평가 적격 수를 기준으로 판단합니다. 발생월은 신호 코호트이며 계좌의 월간 수익률이 아닙니다.")


def tab_validation(b: Bundle) -> None:
    st.subheader("🧪 실전 검증 · 적격성 → 성과 → 위험")
    st.warning(
        "이 화면은 참고용 모의 추적이며 공식 전략 성과가 아닙니다. "
        "게시 이전 진입·가격 근거가 미확인된 표본이 포함될 수 있고 실제 체결 수익이 아닙니다."
    )
    with st.expander("ⓘ 공식 검증과 다른 점", expanded=False):
        st.caption(
            "아래 신호 10일 모의 추적과 최대 120거래일 중장기 관찰은 계산 기간·적격 조건이 다릅니다. "
            "공식 검증 분석은 5. 데이터 탭의 published/analysis/latest.md에서 확인하세요."
        )
    o = num(b.outcomes, "ret_5d", "ret_10d", "ret_20d", "ex_5d", "ex_10d",
            "ex_20d", "mfe_10d_pct", "mae_10d_pct", "mfe_pct", "mae_pct",
            "r_10d", "stop_aware_ret_10d", "stop_aware_r_10d", "cur_ret",
            "bars", "entry_price", "stop_price")
    if o.empty:
        if not b.val.empty:
            with st.expander("기존 검증 원본 자료", expanded=False):
                stock_dataframe(b.val, hide_index=True, width="stretch")
        else:
            st.info("10일 모의 검증 자료가 없습니다.")
        render_tracking(b.trend_tracking, b.trend_daily, b.trend_source,
                        stock_dataframe, show_plotly, clean_chart)
        render_exit_experiments(b.exit_experiments,b.exit_experiment_source,"dark" if is_dark_mode() else "light")
        return
    o = clean_validation_metrics(o)
    if "market" not in o:
        st.warning("market 컬럼이 없어 시장을 구분할 수 없습니다.")
        o["market"] = "미분류"
    if "setup_stage" not in o:
        o["setup_stage"] = "미분류"
    if "status" not in o:
        o["status"] = "완료" if "ret_10d" in o else "미분류"
    with st.container(border=True):
        c = st.columns(3)
        markets = sorted(o["market"].dropna().unique())
        mk = c[0].multiselect("시장", markets, default=markets, key="v_mk")
        stages = sorted(o["setup_stage"].dropna().unique())
        stage = c[1].multiselect("셋업", stages, default=[], key="v_setup")
        since = c[2].selectbox("발생일 범위", ["전체","최근 90일","최근 30일"], key="v_since")

    filtered = o[o["market"].isin(mk)]
    if stage:
        filtered = filtered[filtered["setup_stage"].isin(stage)]
    if since != "전체" and "as_of" in filtered:
        days = 90 if "90" in since else 30
        filtered = filtered[pd.to_datetime(filtered["as_of"], errors="coerce")
                            >= pd.Timestamp.now() - pd.Timedelta(days=days)]
    done = filtered[filtered["ret_10d"].notna()] if "ret_10d" in filtered else filtered.iloc[0:0]
    live = filtered[filtered["status"].isin(["진행","대기"])] if "status" in filtered else filtered.iloc[0:0]

    st.markdown("##### ① 10거래일 신호별 사후 평가")
    st.caption(
        f"전체 {len(filtered)}건 · **10일 평가 적격 {len(done)}건** · "
        f"미평가 {len(filtered)-len(done)}건 · 진행·진입대기 {len(live)}건"
    )
    if done.empty:
        st.info("10일 평가 적격 표본이 없습니다. 미완료·근거 미확인은 수익률 0%로 계산하지 않습니다.")
    else:
        stats = outcome_metrics(done)
        k = st.columns(5)
        k[0].metric("10일 평가", f"{int(stats['10일 평가'])}건", border=True)
        k[1].metric(
            "승률", f"{stats['승률(10일)']:.0f}%" if pd.notna(stats["승률(10일)"]) else "—", border=True
        )
        k[2].metric(
            "평균 초과수익",
            f"{stats['평균 초과']:+.2f}%p" if pd.notna(stats["평균 초과"]) else "—",
            help=f"초과수익 유효 n={stats['초과 평가']}", border=True,
        )
        k[3].metric(
            "평균 모의 손절 R",
            f"{stats['평균 R']:+.2f}R" if pd.notna(stats["평균 R"]) else "—",
            help=f"손절 R 유효 n={stats['손절R 평가']} · 실제 체결 아님", border=True,
        )
        k[4].metric(
            "모의 손절 터치율",
            f"{stats['손절 터치%']:.0f}%" if pd.notna(stats["손절 터치%"]) else "—",
            help=f"손절 터치 유효 n={stats['손절 평가']}", border=True,
        )
        if int(stats["10일 평가"]) < MIN_SAMPLE:
            st.warning(f"적격 {len(done)}건 · 최소 {MIN_SAMPLE}건 미만으로 집단 간 우열 판단을 보류합니다.")
        render_validation_charts(done)
        render_validation_group_comparison(done, b.h_regime, stock_dataframe)

    if not live.empty:
        with st.expander(f"⏳ 10일 평가 미완료 신호 {len(live)}건 · 상세", expanded=False):
            render_live_signals_dashboard(live)
    st.divider()
    render_tracking(b.trend_tracking, b.trend_daily, b.trend_source,
                    stock_dataframe, show_plotly, clean_chart)
    render_exit_experiments(b.exit_experiments,b.exit_experiment_source,"dark" if is_dark_mode() else "light")


# ════════════════════════════════════════════════════════════════════
# 8. 탭 5: ℹ️ 데이터 (이력 현황 및 README.md 유저 매뉴얼 자동 렌더링)
# ════════════════════════════════════════════════════════════════════
def render_gpt_export(b: Bundle) -> None:
    st.subheader("🤖 ChatGPT 분석 파일")
    st.write("① 파일 받기 → ② ChatGPT에 첨부 → ③ ‘첨부된 요청문대로 분석해줘’ 입력")
    st.caption("추가 API 키·API 요금 없이 사용합니다. 앱은 외부 AI로 자동 전송하지 않으며, ChatGPT의 파일 업로드 한도는 이용 요금제에 따릅니다.")
    mode = st.selectbox("분석 유형", list(MODE_DETAILS),
                        format_func=lambda key: MODE_DETAILS[key][0], key="gpt_mode")
    _, purpose, timing = MODE_DETAILS[mode]
    st.caption(f"{purpose} · 권장 시점: {timing}. 예약 실행을 생성하거나 성공 여부를 확인하는 기능은 아닙니다.")
    st.caption(f"run_id: {b.man.get('run_id', '미확인')} · 생성: {b.man.get('generated_at', '미확인')}")
    st.caption("가격 기준일: " + str(b.man.get("as_of") or "미확인"))
    if b.export_snapshot_verified is False:
        st.warning("동일 게시본의 파일 무결성을 확인하지 못해 다운로드를 중단했습니다. 새로고침 후 다시 확인하세요. " + ", ".join(b.export_errors))
        return
    st.caption("거래소 캘린더·운영 실행 상태는 이 메뉴에서 검증하지 않습니다. 아침·저녁 선택만으로 당일 갱신을 보증하지 않습니다.")
    scope, limit = st.columns(2)
    market = scope.radio("분석할 시장", ["ALL", "KR", "US"], horizontal=True,
                         format_func=lambda key: {"ALL": "전체", "KR": "한국", "US": "미국"}[key], key="gpt_market")
    candidate_limit = limit.selectbox("시장별 후보 수", [20, 40, 80], key="gpt_candidate_limit")
    stock_key = None
    if mode == "stock_deep_dive":
        options = dict(stock_options(b.watch, b.cand, ("KR", "US") if market == "ALL" else (market,)))
        if not options:
            st.warning("현재 시장의 게시 데이터에 선택 가능한 종목이 없습니다.")
            return
        if "gpt_stock" in st.session_state and st.session_state["gpt_stock"] not in options:
            del st.session_state["gpt_stock"]
        stock_key = st.selectbox("심층 분석 종목 (종목명·티커 검색)", list(options),
                                 format_func=options.get, key="gpt_stock")
        st.caption("선택 종목은 후보 수 제한 밖에 있어도 1개 종목을 별도로 포함합니다. 기본 후보 표본과 중복될 수 있습니다.")
    if mode == "weekly":
        st.info("주간은 검증된 연구 집계로 부분 분석합니다. 이력 CSV는 manifest 해시 대상이 아니므로 추가 집계하지 않으며, all/recent30 통계를 주간 수익으로 바꾸지 않습니다.")
    research_raw = None
    src = get_source()
    if src:
        source_key = json.dumps({k: v for k, v in src.items() if k != "token"}, sort_keys=True)
        try:
            research_raw = fetch_file(source_key, str(b.man.get("run_id", "")), "analysis/latest.md", src)
        except Exception:
            st.info("실전 검증 자료를 읽지 못했습니다. 시장 분석 파일은 계속 사용할 수 있습니다.")
    try:
        export = build_analysis_export(b.man, b.summ, b.watch, b.cand, b.sectors,
                                       market=market, candidate_limit=candidate_limit, research_raw=research_raw,
                                       mode=mode, stock_key=stock_key, snapshot_verified=b.export_snapshot_verified,
                                       change_context=active_context(src, str(b.man.get("run_id", ""))))
    except (ValueError, TypeError, KeyError) as exc:
        st.warning(f"분석 파일을 만들 수 없습니다: {exc}")
        return
    if research_raw is None:
        st.caption("실전 검증 집계 자료가 아직 없습니다. 데이터 저장소 변경 병합 후 다음 Watchlist 실행부터 포함됩니다.")
    else:
        st.caption("현재 게시본과 일치하는 실전 검증 집계와 직전 게시본 대비 시장 변화를 함께 포함합니다. 불일치 자료는 제외합니다.")
    counts = export.counts
    compact_explainer(
        f"게시 적격 {counts['watchlist']}개 · 후보 {counts['candidates_included']}/{counts['candidates_available']}개"
        f" · 섹터·대테마 {counts['sectors']}개 · 파일 {len(export.text.encode('utf-8')) / 1024:.0f}KB",
        "분석 파일의 포함 범위",
        "게시 적격은 전체 선정 종목의 일부이며 후보 표본과 중복될 수 있습니다. "
        "개인 계좌·보유 비중·거래 내역과 시계열 차트는 포함하지 않습니다.",
    )
    if export.warnings:
        st.warning("자료 확인: " + " / ".join(export.warnings))
    market_label = {"ALL": "전체", "KR": "한국", "US": "미국"}[market]
    st.download_button(f"📥 {market_label} ChatGPT 분석 파일 받기", data=export.text,
                       file_name=export.filename, mime="text/markdown; charset=utf-8",
                       on_click="ignore", key=f"gpt_download_{mode}_{market}_{candidate_limit}",
                       disabled=not any(counts[key] for key in ("watchlist", "candidates_included", "sectors")))
    with st.expander("분석 요청문 보기·복사"):
        st.code(build_prompt(mode), language=None)
    with st.expander("예약 분석 설정 — 추가 API 요금 없음"):
        st.write("ChatGPT에서 GitHub를 연결하고 기존 아침 예약 작업의 지침을 수정하세요. 이 앱에서 예약 작업을 생성하거나 활성화하지는 않습니다.")
        st.markdown("[예약 설정·일일/주간 분석 지침](https://github.com/example-owner/trading-journal-data/blob/main/ops/CHATGPT_AUTOMATION.md)")
        st.caption("권장: 화~토 07:30 아침, 월~금 19:30 저녁, 일요일 07:30 주간 (한국 시간). 계정의 예약 작업·GitHub 연결 지원 및 이용 한도를 확인하세요.")


def tab_data(b: Bundle) -> None:
    render_actionable_changes(b, get_source(), stock_dataframe)
    st.divider()
    render_gpt_export(b)
    st.divider()
    st.subheader("📦 이력 축적 현황")
    rows = []
    for name, desc, df, dcol in (
        ("regime.csv", "4대 지수 레짐 및 내부 체력 이력", b.h_regime, "as_of"),
        ("sector_power.csv", "대테마 및 세부섹터 파워 이력", b.h_sector, "date"),
        ("candidates_daily.csv", "일별 상위 선정·매수검토 후보 이력", b.h_cand, "date"),
        ("signals.csv", "신규 액션 신호 원장 (쿨다운 적용)", b.ledger, "first_seen"),
        ("signal_outcomes.csv", "신호별 사후 수익률·MFE/MAE 추적", b.outcomes, "first_seen"),
    ):
        if df.empty or dcol not in df.columns:
            rows.append({"파일": name, "용도": desc, "누적 행 수": 0, "기록일 수": 0, "시작일": "-", "최근 기록일": "-"})
        else:
            rows.append({
                "파일": name, "용도": desc, "누적 행 수": len(df),
                "기록일 수": int(df[dcol].nunique()),
                "시작일": str(df[dcol].min()), "최근 기록일": str(df[dcol].max()),
            })
    stock_dataframe(
        pd.DataFrame(rows), hide_index=True, width="stretch",
        column_config={
            "누적 행 수": st.column_config.NumberColumn("누적 행 수", format="%d행"),
            "기록일 수": st.column_config.NumberColumn("기록일 수", format="%d일"),
        },
    )

    st.divider()
    st.subheader("📄 최신 게시 파일 목록 (latest/)")
    file_rows = []
    for k, v in (b.man.get("files") or {}).items():
        r_cnt = v.get("rows")
        file_rows.append({
            "파일명": k,
            "유형": "테이블 (CSV)" if k.endswith(".csv") else ("압축 차트 (GZIP)" if k.endswith(".gz") else "메타 (JSON)"),
            "데이터 행 수": f"{int(r_cnt):,}행" if r_cnt is not None else "- (비정형 JSON)",
            "파일 크기(KB)": round((v.get("bytes") or 0) / 1024, 1),
            "무결성 해시(SHA-256)": str(v.get("sha256", ""))[:12],
        })
    stock_dataframe(
        pd.DataFrame(file_rows), hide_index=True, width="stretch",
        column_config={"파일 크기(KB)": st.column_config.NumberColumn("크기 (KB)", format="%.1f KB")},
    )

    st.divider()
    st.subheader("📐 지표 단위 확인")
    compact_explainer(
        "일부 *_pct 필드는 소수 비율(0.05=5%)이고, 일부는 이미 % 값입니다. 혼용 환산에 주의하세요.",
        "필드별 저장 단위·환산 규칙",
        "- **소수 비율(0.05 = 5%)**: `dist_to_pivot_pct`, `stop_dist_pct`, "
        "`distance_from_high_pct`, `dist_sma50_pct`, `range_10d_pct`\n"
        "- **이미 % 단위(100을 추가로 곱하지 않음)**: `down_day_excess_pct`, "
        "`eps_growth_pct`, `position_size_pct`, `portfolio_alloc_pct`",
    )

    st.divider()
    st.subheader("📖 유저 사용 매뉴얼 (README.md)")
    readme_path = Path("README.md")
    if readme_path.exists():
        try:
            st.markdown(readme_path.read_text(encoding="utf-8"))
        except Exception as exc:
            st.warning(f"README.md를 읽지 못했습니다: {exc}")
    else:
        st.info("프로젝트 폴더에 `README.md` 파일이 존재하지 않습니다. 상단 매뉴얼 내용을 참고하세요.")


# ════════════════════════════════════════════════════════════════════
# 9. 진입점
# ════════════════════════════════════════════════════════════════════
def main() -> None:
    hdr_l, hdr_theme, hdr_r, hdr_pop = st.columns([4.4, 1.1, 0.9, 1.2])
    hdr_l.title("📈 Trend Leader Watchlist")
    hdr_theme.selectbox("차트 테마", ["자동", "라이트", "다크"], key="ui_theme_mode", label_visibility="collapsed", help="차트 레이블 테마 (자동 감지 또는 라이트/다크 수동 고정)")

    with hdr_pop.popover("📖 셋업·신호 치트시트"):
        st.markdown("""
        #### 🎯 주요 셋업 및 알파 신호 치트시트
        * **🔥 피벗돌파**: 당일 제외 피벗~+5%에 종가가 있고, 전일 대비 +1% 이상·직전 50봉 평균 거래량 1.5배 이상인 당일 조건입니다. 피벗과 같은 종가도 포함하며 최초 돌파·유지·재돌파 이벤트는 구분하지 않습니다.
        * **🎯 VCP수축**: 스윙 낙폭 수축 또는 10일 구간 3개의 최대낙폭 감소에 ATR 수축을 결합한 프로젝트 규칙입니다. 25봉 이상이면 VCP, 미만이면 단기수축으로 구분합니다. 거래량 감소는 필수 조건이 아니며 정형 패턴 확정을 뜻하지 않습니다.
        * **🧲 이평선눌림목**: 정배열에서 20/50일선 근접 등으로 분류한 관찰 상태입니다. 이전 돌파·거래량 건조·지지 회복을 모두 확인한 것은 아닙니다(메가캡 언더컷 회복 분기 포함).
        * **🔵 블루닷**: 주가가 신고가를 치기 전, 상대강도(RS) 라인이 먼저 52주 신고가를 경신한 최선행 리더 신호.
        * **🛡️ 조정장 방어력**: 시장 지수 하락일(D-Day)에 벤치마크 대비 초과 상승 성향을 보인 방어력 우수 종목.
        * **🟠 와이드 스톱 관찰**: 정배열·바이존을 충족했으나 권장 손절폭이 8~12%인 고베타 주도주 (리스크 비례 비중 자동 축소).
        """)

    if hdr_r.button("🔄 새로고침", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    page_labels = ["🧭 1. 국면·나침반", "🏭 2. 주도 섹터", "🎯 3. 전술·멀티차트", "🧪 4. 실전 검증", "ℹ️ 5. 데이터", MANUAL_PAGE]
    # Retire the private menu even when its old Cloud Secrets remain configured.
    # Reset sessions that were displaying it before this deployment.
    if st.session_state.get("main_page") == "💼 내 포트폴리오":
        st.session_state["main_page"] = page_labels[0]
    selected_page = st.radio("분석 화면", page_labels, horizontal=True, label_visibility="collapsed", key="main_page")

    if selected_page == MANUAL_PAGE:
        manual_src = get_source()
        if manual_src:
            manual_key = json.dumps({k: v for k, v in manual_src.items() if k != "token"}, sort_keys=True)
            manual_doc, manual_status = load_published_eod(manual_key, lambda rel: _read_raw(manual_src, rel))
        else:
            manual_doc, manual_status = {}, {"status": "unconfigured", "reason": "EOD 데이터 소스가 설정되지 않았습니다."}
        render_manual_watchlist(theme="dark" if is_dark_mode() else "light", snapshot=manual_doc, source_state=manual_status)
        return

    src = get_source()
    if not src:
        st.error("데이터 소스가 설정되지 않았습니다.")
        st.markdown("`DATA_DIR`(로컬) 또는 `GITHUB_TOKEN` + `DATA_REPO` (+ `DATA_ROOT`, `DATA_BRANCH`)를 Secrets/환경변수로 지정하세요.")
        st.stop()
    src_key = json.dumps({k: v for k, v in src.items() if k != "token"}, sort_keys=True)
    try:
        man = fetch_manifest(src_key, src)
    except Exception as exc:
        st.error(f"데이터를 불러오지 못했습니다: {exc}")
        st.stop()
    if not man:
        st.warning("아직 게시된 데이터가 없습니다 (latest/manifest.json 없음). 엔진을 한 번 실행하세요.")
        st.stop()

    b = load_bundle(src_key, src, man)
    render_banner(b)
    page_map = {"🧭 1. 국면·나침반": tab_compass, "🏭 2. 주도 섹터": tab_sectors,
                "🎯 3. 전술·멀티차트": tab_playbook, "🧪 4. 실전 검증": tab_validation,
                "ℹ️ 5. 데이터": tab_data}
    if selected_page == "🧪 4. 실전 검증":
        b.exit_experiments,b.exit_experiment_source=load_exit_publication(src_key,str(man.get("run_id","")),lambda rel:_read_raw(src,rel))
    page_map[selected_page](b)


if __name__ == "__main__":
    main()


