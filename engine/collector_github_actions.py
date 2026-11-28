"""
Trading Journal Collector (GitHub Actions)
- 매매 체결 동기화 (국내/미국, CMA/ISA) → data/trades_master.csv
- 종목별 매수/매도 일지 자동 작성 & 일봉 SVG 차트 (매도 일지는 원장 기반 추정 실현손익 포함)
- 야후 파이낸스 & Finviz 기반 모닝 시황 대시보드

실행 모드(--mode)            필요한 입력
  auto           시황(평일 06~10시 KST, 미작성 시) + KR/US 체결 동기화  ← 외부 스케줄러 기본값
  morning_prep   시황 일지만 강제 작성 (--date 로 과거 일자 재구성)
  hourly_check   체결 동기화만 (--market, --date)
  check          진단(원장·파일 변경 없음)       --target accounts | orders | candles | market
                   accounts 계좌/API 연동, orders 체결 조회 응답 구조(--market --date),
                   candles 종목 일봉 조회(--input 종목코드), market 시황 지표/기준일
  manual         API 가 못 주는 체결 직접 입력    --input "계좌,날짜,시장,코드,구분,수량,단가[,수수료[,세금[,종목명]]];..."
                   (--apply 없으면 검증만, --delete 면 같은 입력을 삭제)
  report         청산 사이클 성과 리포트(승률·손익비·기대값·R·조건별 분해) → reports/    --date ~ --date-end(기본: 이번 달), --apply 로 저장·알림
  backfill       기간(--date ~ --date-end, 종료일 생략 시 오늘) 누락 체결을 원장에 채움 (--apply 없으면 조회만, --overwrite 면 교체)
  rebuild        원장 기준 정리 (--apply 없으면 dry-run)   --target journals(기본: 중복 정리 + 일지/차트 재생성, --date~--date-end)
                   | layout(기존 시황/일지 메타데이터 위치를 journal_meta 설정에 맞춤)
  init           템플릿/설정 파일 생성 (--overwrite 시 기본값으로 덮어씀)
(구버전 이름 accounts/orders_check/candles_check/market_check/fold_meta/manual_trade/repair_ledger/init_templates 는 자동 변환)
"""
import argparse
import base64
import bisect
import math
import posixpath
from urllib.parse import quote
import hashlib
import html
import json
import logging
import os
import re
import statistics
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from io import StringIO
from zoneinfo import ZoneInfo

import portfolio_engine as portfolio

import pandas as pd
import requests
from github import Auth, Github, GithubException
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ==========================================
# 0. 기본 설정
# ==========================================
KST = timezone(timedelta(hours=9))
try:
    NY = ZoneInfo("America/New_York")
except Exception:  # tzdata 없음 → 고정 오프셋(차트 확정 판정에만 쓰이므로 치명적이지 않음)
    NY = timezone(timedelta(hours=-5))

BASE_URL = "https://api.nhplug.com:8443"
US_PATH = "/gbstock/inquiry/v1/unexecuted"
KR_PATH = "/krstock/inquiry/v1/dailyOrderExecution"
LEDGER_PATH = "data/trades_master.csv"
MARKET_DIR = "journals/market"
MARKET_CHART_DIR = f"{MARKET_DIR}/charts"
STOCK_DIR = "journals/stocks"
TEMPLATE_DIR = "templates"
CHART_DIR = f"{STOCK_DIR}/charts"
CHART_FETCH = 180
LEDGER_COLUMNS = ["date", "time", "tx_type", "code", "name", "qty", "unit_price",
                  "total_amt", "fee", "tax", "currency", "trade_id", "account", "order_no"]
NUM_COLS = ["qty", "unit_price", "total_amt", "fee", "tax"]
STR_COLS = [c for c in LEDGER_COLUMNS if c not in NUM_COLS]
WEEKDAYS_KO = "월화수목금토일"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
BACKFILL_ABORT_AFTER = 5  # 같은 시장에서 연속 N일 조회가 거부되면 더 과거 구간은 중단


def _digits(v): return re.sub(r"\D", "", v or "")


NH_APP_KEY = os.getenv("NH_APP_KEY")
NH_APP_SECRET = os.getenv("NH_APP_SECRET")
ACCOUNTS = {k: v for k, v in {
    "CMA": _digits(os.getenv("NH_ACCOUNT_CMA")),
    "ISA": _digits(os.getenv("NH_ACCOUNT_ISA")),
}.items() if v}

DATA_TOKEN = os.getenv("DATA_REPO_TOKEN") or os.getenv("GITHUB_TOKEN")
DATA_REPO_NAME = os.getenv("DATA_REPO_NAME") or "example-owner/trading-journal-data"
TG_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def _csv_env(name: str, default: str = "") -> set:
    return {x.strip().upper() for x in (os.getenv(name) or default).split(",") if x.strip()}


NO_US_ACCOUNTS = _csv_env("NH_NO_US_ACCOUNTS", "ISA")      # 미국주식 조회를 건너뛸 계좌
OPTIONAL_ACCOUNTS = _csv_env("NH_OPTIONAL_ACCOUNTS", "ISA")  # 실패/중복 응답을 조용히 넘길 계좌
RUN_WARNINGS = []

_SECRETS = [s for s in (NH_APP_KEY, NH_APP_SECRET, DATA_TOKEN, TG_BOT_TOKEN, *ACCOUNTS.values()) if s]


def mask(text) -> str:
    s = str(text)
    for secret in _SECRETS:
        s = s.replace(secret, "***MASKED***")
    return s


def add_warning(msg: str):
    msg = mask(msg)
    if msg not in RUN_WARNINGS:
        RUN_WARNINGS.append(msg)


class SecretMaskingFormatter(logging.Formatter):
    def format(self, record):
        return mask(super().format(record))


logger = logging.getLogger("GitHubActionCollector")
logger.setLevel(logging.INFO)
logger.propagate = False
_handler = logging.StreamHandler()
_handler.setFormatter(SecretMaskingFormatter("%(asctime)s - %(levelname)s - %(message)s"))
logger.addHandler(_handler)


def _build_session():
    s = requests.Session()
    # raise_on_status=False: 재시도 소진 후에도 예외 대신 마지막 응답을 돌려줘 호출부가 상태코드로 처리
    retry = Retry(total=3, connect=2, read=1, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET", "POST"]), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


HTTP = _build_session()
API_MIN_INTERVAL = float(os.getenv("NH_API_MIN_INTERVAL") or 0.22)
_LAST_CALL = [0.0]


def _throttle():
    wait = API_MIN_INTERVAL - (time.monotonic() - _LAST_CALL[0])
    if wait > 0:
        time.sleep(wait)
    _LAST_CALL[0] = time.monotonic()


CACHE_DIR = ".cache"


def _read_cache(key: str):
    path = os.path.join(CACHE_DIR, f"{key}.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _write_cache(key: str, data: dict):
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(os.path.join(CACHE_DIR, f"{key}.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


_TOKEN = {"v": None}


def get_nh_access_token(force: bool = False) -> str:
    if not force:
        if _TOKEN["v"]:
            return _TOKEN["v"]
        cached = _read_cache("nh_token")
        if cached and cached.get("token") and time.time() < cached.get("exp", 0):
            _TOKEN["v"] = cached["token"]
            return _TOKEN["v"]
    res = HTTP.post(f"{BASE_URL}/oauth2/token",
                    headers={"content-type": "application/x-www-form-urlencoded"},
                    data={"appkey": NH_APP_KEY, "appsecretkey": NH_APP_SECRET,
                          "grant_type": "client_credentials", "scope": "oob"}, timeout=10)
    res.raise_for_status()
    js = res.json()
    token = js.get("access_token")
    if not token:
        raise RuntimeError("NH 토큰 발급 응답에 access_token이 없습니다.")
    try:
        ttl = int(js.get("expires_in") or 3600)
    except (TypeError, ValueError):
        ttl = 3600
    _write_cache("nh_token", {"token": token, "exp": time.time() + max(ttl - 120, 60)})
    _TOKEN["v"] = token
    return token


def nh_post(path: str, extra_headers: dict, body: dict, timeout=(8, 30), _retried: bool = False):  # (연결, 읽기) 초
    _throttle()
    headers = {"content-type": "application/json; charset=UTF-8",
               "Authorization": f"Bearer {get_nh_access_token()}", **(extra_headers or {})}
    res = HTTP.post(f"{BASE_URL}{path}", headers=headers, json=body, timeout=timeout)
    if res.status_code == 401 and not _retried:  # 캐시된 토큰이 서버에서 먼저 만료/폐기된 경우
        logger.warning("401 응답 → 토큰 재발급 후 1회 재시도")
        get_nh_access_token(force=True)
        return nh_post(path, extra_headers, body, timeout, True)
    return res


def _hdr(res, name: str) -> str:
    try:
        return str(res.headers.get(name) or "")
    except Exception:
        return ""


def post_pages(path: str, body: dict, max_pages: int = 20) -> list:
    """cts/cts_flag 연속조회. 응답 객체 리스트를 반환."""
    seen, extra, resps = set(), {}, []
    for _ in range(max_pages):
        res = nh_post(path, extra, body)
        resps.append(res)
        if res.status_code != 200:
            break
        flag, cts = _hdr(res, "cts_flag").upper(), _hdr(res, "cts")
        if flag != "Y" or not cts or cts in seen:
            break
        seen.add(cts)
        extra = {"cts": cts, "cts_flag": flag}
    else:
        if max_pages > 1:
            add_warning(f"연속조회가 {max_pages}페이지 한도에 도달 — 일부 데이터가 누락됐을 수 있습니다 ({path})")
    return resps


def _as_list(raw) -> list:
    if isinstance(raw, dict):
        return [raw]
    return [x for x in (raw or []) if isinstance(x, dict)]


def to_float(v, default=0.0):
    try:
        s = str(v).replace(",", "").strip() if v is not None else ""
        return float(s) if s else default
    except ValueError:
        return default


def iso_date(d8: str) -> str: return f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"


def gh_url(path: str) -> str:
    """데이터 저장소 파일의 GitHub 웹 주소 (기본 브랜치로 연결되는 HEAD 사용)."""
    return f"https://github.com/{DATA_REPO_NAME}/blob/HEAD/{quote(path, safe='/')}"


def tg_link(label: str, path: str) -> str:
    return f'<a href="{gh_url(path)}">{html.escape(str(label))}</a>'


def stock_journal_path(date_iso: str, code: str, name: str, side: str) -> str:
    return f"{STOCK_DIR}/{date_iso}_{safe_filename(code)}_{safe_filename(name)}_{side}.md"


def journal_links_text(day_df: pd.DataFrame, rows, date_iso: str, limit: int = 8) -> str:
    """체결 행들에 해당하는 종목 일지 링크 한 줄. 파일명은 update_stock_journals 와 같은 규칙(그날 그 종목의 첫 행 이름)."""
    seen, out = set(), []
    for _, r in rows.iterrows():
        side = "매수" if "매수" in str(r["tx_type"]) else "매도" if "매도" in str(r["tx_type"]) else None
        if not side or (r["code"], side) in seen:
            continue
        seen.add((r["code"], side))
        g = day_df[day_df["code"] == r["code"]]
        name = str(g["name"].iloc[0]) if not g.empty else str(r["name"])
        out.append(tg_link(f"{name} {side} 일지", stock_journal_path(date_iso, r["code"], name, side)))
    if not out:
        return ""
    more = f" … 외 {len(out) - limit}건" if len(out) > limit else ""
    return "📒 " + " · ".join(out[:limit]) + more


def send_telegram_message(text: str) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return True
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    try:
        r = HTTP.post(url, json={"chat_id": TG_CHAT_ID, "text": text[:4000], "parse_mode": "HTML"}, timeout=10)
        if r.status_code == 200:
            return True
        if r.status_code == 400 and "parse" in str(r.text).lower():
            plain = html.unescape(re.sub(r"<[^>]+>", "", re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2 (\1)", text)))
            r = HTTP.post(url, json={"chat_id": TG_CHAT_ID, "text": plain[:4000]}, timeout=10)
            return r.status_code == 200
    except requests.RequestException:
        pass
    return False


def is_final(now_kst: datetime, market: str, date_iso: str) -> bool:
    """해당 거래일의 일봉이 확정됐는지(장 마감 + 여유). KR 16:00 KST, US 20:30 ET(애프터마켓 종료 후)."""
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    close = d.replace(hour=16, minute=0, tzinfo=KST) if market == "KR" else d.replace(hour=20, minute=30, tzinfo=NY)
    return now_kst >= close


# ==========================================
# 1. 매크로 및 시장 분석 로직
# ==========================================
YF_SYMBOLS = {
    "S&P 500": "^GSPC", "NASDAQ": "^IXIC", "DOW": "^DJI", "RUSSELL 2000": "^RUT",
    "PHLX Semi": "^SOX", "KOSPI": "^KS11", "KOSDAQ": "^KQ11",
    "USD/KRW": "KRW=X", "DXY": "DX-Y.NYB", "US 10Y": "^TNX", "US 30Y": "^TYX",
    "VIX": "^VIX", "HYG": "HYG", "WTI": "CL=F", "GOLD": "GC=F", "COPPER": "HG=F", "BTC": "BTC-USD"
}
YF_HOSTS = ("query2.finance.yahoo.com", "query1.finance.yahoo.com")
INDEX_NAMES = ["S&P 500", "NASDAQ", "DOW", "RUSSELL 2000", "PHLX Semi", "KOSPI", "KOSDAQ"]
US_INDEX_NAMES = {"S&P 500", "NASDAQ", "DOW", "RUSSELL 2000", "PHLX Semi"}
INDEX_CLOSE_HM = {"KOSPI": (15, 40), "KOSDAQ": (15, 40),
                  "S&P 500": (16, 15), "NASDAQ": (16, 15), "DOW": (16, 15),
                  "RUSSELL 2000": (16, 15), "PHLX Semi": (16, 15)}
# 지표 상태 판별 기준 기본값. 저장소의 config/limits.json 에 적은 항목만 덮어쓴다 (아래 load_limits).
# [상단(위험), 하단(안정)] 쌍은 상단 > 하단 이어야 한다. 시장폭은 [과열 초과, 침체 이하].
LIMITS_PATH = "config/limits.json"
DEFAULT_LIMITS = {
    "usdkrw": [1380, 1320], "dxy": [105, 100], "us10y": [4.5, 4.0], "us30y": [4.8, 4.3],
    "vix": [20, 15], "wti": [85, 75], "breadth": [75, 30],
    "dist_lookback": 25, "dist_warn": 3, "dist_danger": 5, "dist_invalidate_pct": 5.0,
    "dist_drop_pct": 0.2,              # 지수 분산일 하락 기준(%) 기본값 (IBD 표준 0.2, 미국 지수 기준)
    "dist_drop_pct_by_index": {},      # 지수별 개별 기준, 예: {"KOSPI": 0.3, "KOSDAQ": 0.4}
    "sigma_days": 60,                  # 일간 변동성(σ) 계산 기간(거래일)
    "journal_meta": "folded",          # 시황/일지 맨 위 YAML 메타데이터: "folded"(접이식) 또는 "top"(기존 --- 블록)
    "reconcile_enabled": True,         # 시황 작성 때 잔고와 원장 잔량을 대조해 누락 체결을 경고
    "reconcile_tol": 0.01,             # 대조 허용 오차(주)
    "reconcile_ignore": [],            # 대조에서 제외할 종목 코드 (원장 이전부터 보유한 종목 등)
    "default_stop_pct": 8.0,           # 손절가를 적지 않았을 때 평단 대비 기본 손절폭(%) (0 이면 기본 손절 없음)
    "stop_near_pct": 3.0,              # 현재가가 손절가 위로 이 % 이내면 '손절 임박'
    "max_heat_pct": 6.0,               # 포트폴리오 히트(현재가→손절 손실 합계 ÷ 총자산) 경고선(%)
    "equity_krw": 0,                   # 총자산(원). 0 이면 보유 평가금액 합계(원화 환산)를 사용
    "trail_line": "ma50",              # 트레일링 기준선: ema21 | ma50 | chandelier(최고가−N×ATR) | off
    "trail_confirm": 2,                # 기준선 아래 종가가 연속 몇 번이면 이탈로 볼지(ema21·ma50)
    "chandelier_atr": 3.0,             # chandelier 의 ATR 배수 (ATR 은 종가 기준 14일)
    "risk_alerts": True,               # 손절 임박·이탈, 트레일링 이탈 텔레그램 알림 (auto/hourly_check)
    "holdings_show": True,             # 시황에 보유 종목 섹션 표시
    "holdings_layout": "cards",        # 보유 종목 표시: cards(종목별 카드 목록, 모바일 권장) | table(5열 표, 넓은 화면용)
    "regime_show": True,               # 시황 맨 위에 규칙 기반 시장 신호 카드 표시
    "holdings_tech": True,             # 보유 종목에 추세/분산일 표시 (야후 호출이 종목 수만큼 늘어남)
    "holdings_rs": True,               # 보유 종목에 벤치마크 대비 상대수익률(RS) 표시 (holdings_tech 가 꺼져 있으면 함께 꺼짐)
    "rs_periods": [63, 126],           # RS 비교 기간(거래일): 단기, 장기 (약 3개월, 6개월)
    "rs_benchmark_us": "S&P 500",      # 미국 종목 벤치마크: "S&P 500", "NASDAQ" 등
    "rs_benchmark_kr": "auto",         # 국내 종목 벤치마크: "auto"(코스피 종목→KOSPI, 코스닥 종목→KOSDAQ), "KOSPI", "KOSDAQ"
    "stock_dist_mode": "sigma",        # 종목 분산일 하락 기준: "sigma"(k×σ) 또는 "fixed"(고정 %)
    "stock_dist_k": 1.0, "stock_dist_fixed_pct": 1.0,
}
LIMITS = dict(DEFAULT_LIMITS)
_PAIR_KEYS = ("usdkrw", "dxy", "us10y", "us30y", "vix", "wti", "breadth")


def parse_limits(raw: dict):
    """사용자 설정을 검증해 (적용값, 오류 메시지 리스트) 반환. 잘못된 항목은 기본값을 유지한다."""
    out, errs = dict(DEFAULT_LIMITS), []
    raw = dict(raw or {})
    if "holdings_stop_pct" in raw:  # 구버전 이름 → default_stop_pct (같은 의미: 평단 대비 손실 %)
        raw.setdefault("default_stop_pct", raw.pop("holdings_stop_pct"))
    for k, v in raw.items():
        if k.startswith("_"):
            continue
        if k not in DEFAULT_LIMITS:
            errs.append(f"알 수 없는 항목 '{k}'")
        elif k in _PAIR_KEYS:
            if isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v) and v[0] > v[1]:
                out[k] = [v[0], v[1]]
            else:
                errs.append(f"'{k}'는 [상단, 하단] 숫자 2개(상단 > 하단)여야 합니다")
        elif k == "reconcile_ignore":
            if isinstance(v, list) and all(isinstance(x, str) for x in v):
                out[k] = [x.strip().upper() for x in v]
            else:
                errs.append("'reconcile_ignore'는 종목 코드 문자열 목록이어야 합니다")
        elif k in ("default_stop_pct", "equity_krw"):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                out[k] = float(v) if k == "default_stop_pct" else v
            else:
                errs.append(f"'{k}'는 0 이상의 숫자여야 합니다")
        elif k in ("stop_near_pct", "max_heat_pct", "chandelier_atr"):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[k] = float(v)
            else:
                errs.append(f"'{k}'는 0보다 큰 숫자여야 합니다")
        elif k == "trail_line":
            if v in ("ema21", "ma50", "chandelier", "off"):
                out[k] = v
            else:
                errs.append("'trail_line'는 ema21 | ma50 | chandelier | off 중 하나여야 합니다")
        elif k == "trail_confirm":
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5:
                out[k] = v
            else:
                errs.append("'trail_confirm'는 1~5 정수여야 합니다")
        elif k in ("dist_drop_pct", "stock_dist_k", "stock_dist_fixed_pct", "reconcile_tol"):
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                out[k] = float(v)
            else:
                errs.append(f"'{k}'는 0보다 큰 숫자여야 합니다")
        elif k == "dist_drop_pct_by_index":
            if isinstance(v, dict) and all(n in INDEX_NAMES and isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for n, x in v.items()):
                out[k] = {n: float(x) for n, x in v.items()}
            else:
                errs.append("'dist_drop_pct_by_index'는 {지수명: 0보다 큰 숫자} 형태여야 하며 지수명은 " + ", ".join(INDEX_NAMES) + " 중 하나입니다")
        elif k == "rs_periods":
            if (isinstance(v, list) and len(v) == 2 and all(isinstance(x, int) and not isinstance(x, bool) and 5 <= x <= 250 for x in v) and v[0] < v[1]):
                out[k] = list(v)
            else:
                errs.append("'rs_periods'는 5~250 사이 정수 2개 [단기, 장기](단기 < 장기)여야 합니다")
        elif k == "rs_benchmark_us":
            if v in US_INDEX_NAMES:
                out[k] = v
            else:
                errs.append("'rs_benchmark_us'는 " + ", ".join(sorted(US_INDEX_NAMES)) + " 중 하나여야 합니다")
        elif k == "rs_benchmark_kr":
            if v in ("auto", "KOSPI", "KOSDAQ"):
                out[k] = v
            else:
                errs.append("'rs_benchmark_kr'는 \"auto\", \"KOSPI\", \"KOSDAQ\" 중 하나여야 합니다")
        elif k in ("holdings_show", "regime_show", "holdings_tech", "holdings_rs", "reconcile_enabled", "risk_alerts"):
            if isinstance(v, bool):
                out[k] = v
            else:
                errs.append(f"'{k}'는 true/false 여야 합니다")
        elif k == "holdings_layout":
            if v in ("cards", "table"):
                out[k] = v
            else:
                errs.append("'holdings_layout'는 cards 또는 table 이어야 합니다")
        elif k == "journal_meta":
            if v in ("folded", "top"):
                out[k] = v
            else:
                errs.append("'journal_meta'는 \"folded\" 또는 \"top\" 이어야 합니다")
        elif k == "stock_dist_mode":
            if v in ("sigma", "fixed"):
                out[k] = v
            else:
                errs.append("'stock_dist_mode'는 \"sigma\" 또는 \"fixed\" 여야 합니다")
        elif k == "dist_invalidate_pct":
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                out[k] = float(v)
            else:
                errs.append("'dist_invalidate_pct'는 0 이상의 숫자여야 합니다 (0 = 규칙 끄기)")
        elif isinstance(v, int) and not isinstance(v, bool) and v >= 1 and (k != "dist_lookback" or 5 <= v <= 100) and (k != "sigma_days" or 20 <= v <= 250):
            out[k] = v
        else:
            errs.append(f"'{k}' 값이 올바르지 않습니다")
    if out["dist_warn"] > out["dist_danger"]:
        errs.append("dist_warn 이 dist_danger 보다 클 수 없어 두 값을 기본값으로 되돌립니다")
        out["dist_warn"], out["dist_danger"] = DEFAULT_LIMITS["dist_warn"], DEFAULT_LIMITS["dist_danger"]
    return out, errs


_LIMITS_LOADED = [False]


def ensure_limits(repo):
    """설정은 실행당 한 번만 읽는다. (이전에는 시황 작성 때만 읽어서 체결 동기화·일지 작성에는 기본값이 쓰였음)"""
    if not _LIMITS_LOADED[0]:
        load_limits(repo)


def load_limits(repo):
    """config/limits.json 이 있으면 읽어 LIMITS 에 반영. 없거나 오류면 기본값으로 계속 진행한다."""
    global LIMITS
    _LIMITS_LOADED[0] = True
    LIMITS = dict(DEFAULT_LIMITS)
    try:
        text, _ = read_repo_text(repo, LIMITS_PATH)
        if not text:
            return
        LIMITS, errs = parse_limits(json.loads(text))
        if errs:
            add_warning(f"{LIMITS_PATH} 설정 오류(해당 항목은 기본값 사용): " + "; ".join(errs))
        logger.info("지표 기준 설정 적용: %s", {k: v for k, v in LIMITS.items() if v != DEFAULT_LIMITS[k]} or "기본값과 동일")
    except Exception as e:
        LIMITS = dict(DEFAULT_LIMITS)
        add_warning(f"{LIMITS_PATH} 읽기 실패 → 기본 기준 사용 ({type(e).__name__})")


def default_limits_json() -> str:
    body = {"_readme": "지표 상태 판별 기준. 바꿀 항목만 남기고 지워도 됩니다(없는 항목은 기본값). 쌍 값은 [상단(위험), 하단(안정)], breadth 는 [과열 초과, 침체 이하], "
                       "dist_invalidate_pct 는 분산일 이후 종가가 그만큼(%) 오르면 해당 분산일 제외(0이면 끔). "
                       "dist_drop_pct 는 지수 분산일 하락 기준(%)이며 dist_drop_pct_by_index 로 지수별 덮어쓰기. "
                       "보유 종목 분산일은 stock_dist_mode 가 sigma 면 '하락률 ≤ -(stock_dist_k × 최근 sigma_days 일 일간변동성)', fixed 면 stock_dist_fixed_pct(%) 기준. "
                       "reconcile_* 는 잔고-원장 대조(허용오차·제외 코드). 손절: 매수 일지 메타데이터의 stop/target 이 우선이고 없으면 default_stop_pct, "
                       "stop_near_pct 는 손절 임박 거리, max_heat_pct/equity_krw 는 포트폴리오 히트, trail_line/trail_confirm/chandelier_atr 는 트레일링 기준, risk_alerts 는 알림 on/off. "
                       "journal_meta 는 시황/일지 맨 위 메타데이터 표시 방식(folded=접이식, top=기존 YAML 블록)이며 rebuild(target=layout)로 기존 파일도 맞출 수 있음. holdings_show 는 시황의 보유 종목 섹션 표시 여부. "
                       "보유 종목 RS = 종목 수익률 − 벤치마크 수익률(%p), 기간은 rs_periods(거래일), 벤치마크는 rs_benchmark_us / rs_benchmark_kr."}
    body.update(DEFAULT_LIMITS)
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n"
MACRO_ORDER = ["USD/KRW", "DXY", "US 10Y", "US 30Y", "VIX", "HYG", "WTI", "GOLD", "COPPER", "BTC"]
MACRO_DESC = {"USD/KRW": " (환율)", "DXY": " (달러 인덱스)", "HYG": " (하이일드 채권)", "COPPER": " (구리)"}


YF_META = {}  # 심볼 → 야후 chart meta 일부(현재가/시각/거래량/시차). 일봉 배열이 최신 봉을 못 줄 때 보충용


def fetch_yf_history(symbol: str, range_str="1y", quiet: bool = False) -> tuple:
    log = logger.debug if quiet else logger.warning
    for host in YF_HOSTS:
        url = f"https://{host}/v8/finance/chart/{symbol}?interval=1d&range={range_str}"
        try:
            r = HTTP.get(url, headers={"User-Agent": BROWSER_UA}, timeout=10)
            if r.status_code != 200:
                log("Yahoo Finance 응답 코드 %s (%s @ %s)", r.status_code, symbol, host)
                continue
            data = r.json().get("chart", {}).get("result") or []
            if not data:
                continue
            res = data[0]
            ts = res.get("timestamp") or []
            meta = res.get("meta") or {}
            off = meta.get("gmtoffset") or 0
            YF_META[symbol] = {k: meta.get(k) for k in ("regularMarketPrice", "regularMarketTime", "regularMarketVolume", "gmtoffset", "chartPreviousClose",
                "symbol", "currency", "exchangeName", "exchangeTimezoneName", "instrumentType")}
            quote = (res.get("indicators", {}).get("quote") or [{}])[0]
            closes, volumes = quote.get("close") or [], quote.get("volume") or []
            n = min(len(ts), len(closes))
            vols = (list(volumes[:n]) + [None] * n)[:n] if volumes else [None] * n
            by_date = {}
            for i in range(n):
                if closes[i] is None:
                    continue
                d_str = datetime.fromtimestamp(ts[i] + off, timezone.utc).strftime("%Y-%m-%d")
                by_date[d_str] = (closes[i], vols[i])
            dates = sorted(by_date)
            return dates, [by_date[d][0] for d in dates], [by_date[d][1] for d in dates], off
        except Exception as e:
            log("Yahoo Finance 수집 실패 (%s @ %s): %s", symbol, host, e)
    return [], [], [], 0


def _drop_open_bar(name: str, dates, closes, volumes, off: int, now: datetime):
    """장중(미확정) 마지막 봉 제거. (dates, closes, volumes, dropped) 반환."""
    close_hm = INDEX_CLOSE_HM.get(name)
    if close_hm and dates:
        local = now.astimezone(timezone.utc) + timedelta(seconds=off)
        if dates[-1] == local.strftime("%Y-%m-%d") and (local.hour, local.minute) < close_hm:
            return dates[:-1], closes[:-1], volumes[:-1], True
    return dates, closes, volumes, False


_CONFIRM_HM = {"KOSPI": (15, 25), "KOSDAQ": (15, 25), "S&P 500": (15, 55), "NASDAQ": (15, 55), "DOW": (15, 55),
               "RUSSELL 2000": (15, 55), "PHLX Semi": (15, 55)}  # 마지막 체결 시각이 이 이후면 정규장 종가로 간주


def _patch_stale_bar(name: str, sym: str, dates, closes, volumes, now: datetime):
    """일봉 배열이 최신 확정 봉을 아직 못 줄 때(야후 지연) meta 의 마감 시세로 한 봉 보충. (dates, closes, volumes, patched)"""
    meta, hm, ok = YF_META.get(sym) or {}, INDEX_CLOSE_HM.get(name), _CONFIRM_HM.get(name)
    price, t = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
    if not (price and t and hm and ok and dates):
        return dates, closes, volumes, False
    off = meta.get("gmtoffset") or 0
    loc = datetime.fromtimestamp(t + off, timezone.utc)
    d = loc.strftime("%Y-%m-%d")
    now_loc = now.astimezone(timezone.utc) + timedelta(seconds=off)
    session_over = d < now_loc.strftime("%Y-%m-%d") or (now_loc.hour, now_loc.minute) >= hm
    if d > dates[-1] and (loc.hour, loc.minute) >= ok and session_over:
        return dates + [d], closes + [float(price)], volumes + [meta.get("regularMarketVolume")], True
    return dates, closes, volumes, False


try:
    import holidays as _holidays_lib  # 거래소 휴장일(XKRX, NYSE). 없으면 주말만 고려
except ImportError:  # pragma: no cover
    _holidays_lib = None
_HOLIDAY_CACHE = {}


def is_trading_day(market: str, d) -> bool:
    """주말과 거래소 휴장일이면 False. 라이브러리가 없거나 실패하면 주말만 판단한다(표시용 판단에만 사용)."""
    if d.weekday() >= 5:
        return False
    if _holidays_lib is None:
        return True
    key = (market, d.year)
    if key not in _HOLIDAY_CACHE:
        try:
            _HOLIDAY_CACHE[key] = _holidays_lib.financial_holidays("XKRX" if market == "KR" else "NYSE", years=[d.year])
        except Exception:
            _HOLIDAY_CACHE[key] = {}
    return d not in _HOLIDAY_CACHE[key]


def _expected_last_date(name: str, now: datetime) -> str:
    """지금 시점에 일봉이 있어야 할 가장 최근 거래일(주말·거래소 휴장일 제외)."""
    close = INDEX_CLOSE_HM[name]
    market = "KR" if name in ("KOSPI", "KOSDAQ") else "US"
    loc = now.astimezone(KST if market == "KR" else NY)
    d = loc.date() if (loc.hour, loc.minute) >= close else loc.date() - timedelta(days=1)
    for _ in range(15):
        if is_trading_day(market, d):
            break
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def _finviz_total(filters: str):
    url = f"https://finviz.com/screener.ashx?v=111&f={filters}"
    try:
        res = HTTP.get(url, headers={"User-Agent": BROWSER_UA}, timeout=10)
        if res.status_code != 200:
            logger.warning("Finviz 응답 코드 %s (%s)", res.status_code, filters)
            return None
        m = (re.search(r'Total:\s*<b>([0-9,]+)</b>', res.text)
             or re.search(r'#\s*\d+\s*/\s*([0-9,]+)\s*Total', res.text)
             or re.search(r'>\s*([0-9,]+)\s*Total\b', res.text))
        if m:
            return int(m.group(1).replace(",", ""))
    except Exception as e:
        logger.warning("Finviz 수집 실패: %s", e)
    return None


def get_sp500_breadth():
    # Finviz 지수 필터는 'idx_sp500' (기존 'sh_idx_sp500' 은 존재하지 않는 필터라 전체 종목 수가 나옴)
    above = _finviz_total("idx_sp500,ta_sma50_pa")
    time.sleep(1.0)
    total = _finviz_total("idx_sp500")
    if above is None or not total or total < 400 or above > total:
        return None
    return above / total * 100


def calc_ma_safe(data: list, period: int):
    valid = [x for x in data[-period:] if x is not None]
    return sum(valid) / len(valid) if len(valid) >= period * 0.8 else None


def calc_dist_days(closes, volumes, lookback=None, invalidate_pct=None, drop_pct=None):
    """IBD 방식 분산일 수: 최근 lookback 거래일 중 -0.2% 이하 하락 + 전일보다 큰 거래량.
    이후 종가가 해당 분산일 종가보다 invalidate_pct% 이상 오르면 그 분산일은 제외(0 이면 규칙 끔)."""
    lookback = lookback or LIMITS["dist_lookback"]
    pct = LIMITS["dist_invalidate_pct"] if invalidate_pct is None else invalidate_pct
    drop_th = LIMITS["dist_drop_pct"] if drop_pct is None else drop_pct
    if not closes or not volumes or len(closes) < lookback + 1:
        return 0
    c, v = closes[-(lookback + 1):], volumes[-(lookback + 1):]
    flagged = []
    for i in range(1, len(c)):
        if c[i] is None or c[i - 1] is None or v[i] is None or v[i - 1] is None or c[i - 1] == 0:
            continue
        drop = (c[i] - c[i - 1]) / c[i - 1] * 100
        if drop <= -drop_th and v[i] > v[i - 1]:
            flagged.append(i)
    if pct > 0:
        flagged = [i for i in flagged
                   if not any(c[j] is not None and c[j] >= c[i] * (1 + pct / 100) for j in range(i + 1, len(c)))]
    return len(flagged)


def calc_sigma(closes, days=None):
    """최근 days 거래일 일간 수익률(%)의 표준편차. 데이터가 부족하면 None."""
    days = days or LIMITS["sigma_days"]
    c = [x for x in closes if x]
    if len(c) < 21:
        return None
    rets = [(c[i] / c[i - 1] - 1) * 100 for i in range(max(1, len(c) - days), len(c))]
    return statistics.stdev(rets) if len(rets) >= 20 else None


def trend_status(closes):
    """종가 리스트로 추세 상태/배열/50일선 대비를 판정 (지수·종목 공통)."""
    cur = closes[-1]
    ma20, ma50, ma200 = calc_ma_safe(closes, 20), calc_ma_safe(closes, 50), calc_ma_safe(closes, 200)
    status, arr = "🟡 조정", "⚪ 혼조"
    if ma50 and ma200:
        arr = "🟢 정배열" if ma50 > ma200 else "🔴 역배열"
    if ma20 and ma50 and ma200:
        if cur > ma20 and ma50 > ma200:
            status = "🟢 상승"
        elif ma50 < ma200:
            status = "🔴 하락"
    vs50 = (f"{(cur / ma50 - 1) * 100:+.1f}% ({'위' if cur > ma50 else '아래'})") if ma50 else "-"
    return status, arr, vs50


def _level(cur, hi, lo, labels):
    return labels[0] if cur > hi else labels[1] if cur >= lo else labels[2]


def _signed(v: float, fmt: str, prefix: str = "", suffix: str = "") -> str:
    return f"{'-' if v < 0 else '+'}{prefix}{abs(v):{fmt}}{suffix}"


class _JournalDailyCloses:
    """One morning auto run, Yahoo chart/quote 1d/1y only. No disk or OHLC consumers."""

    def __init__(self, now):
        self.now = now
        self.sessions = {}
        self.series = {}

    def remember(self, market, symbol, dates, closes, volumes):
        meta = YF_META.get(symbol, {})
        kr = market == "KR"
        zone = "Asia/Seoul" if kr else "America/New_York"
        if (meta.get("symbol") != symbol or meta.get("currency") != ("KRW" if kr else "USD")
                or meta.get("exchangeTimezoneName") != zone or not meta.get("exchangeName")):
            return
        if symbol.startswith("^"):
            if meta.get("instrumentType") != "INDEX":
                return
        else:
            if kr:
                venues = {"KSC"} if symbol.endswith(".KS") else {"KOE"}
            else:
                venues = {"NYQ", "NMS", "NGM", "NCM", "ASE", "PCX", "BTS"}
            if str(meta.get("exchangeName")) not in venues or meta.get("instrumentType") not in ("EQUITY", "ETF"):
                return
        if len(dates) >= 240 and len(dates) == len(closes) == len(volumes):
            bars = (tuple(dates), tuple(closes), tuple(volumes))
            self.series[market, symbol] = (time.monotonic(), meta.get("regularMarketTime"), bars)

    def get(self, market, symbol):
        entry = self.series.get((market, symbol))
        if not entry or time.monotonic() - entry[0] >= 300:
            return None
        _, market_time, bars = entry
        dates, closes, volumes = bars
        try:
            # Check the calendar only when another consumer actually requests the series.
            if market not in self.sessions:
                from watchlist.session_calendar import get_calendar
                cal = get_calendar(market)
                zone = KST if market == "KR" else NY
                today = pd.Timestamp(self.now.astimezone(zone).date())
                last = cal.date_to_session(today, direction="previous")
                if cal.session_close(last) + timedelta(minutes=5) > self.now:
                    last = cal.previous_session(last)
                # This cache is used only in the Korean morning, before the KR session closes.
                sessions = cal.sessions_in_range(today - timedelta(days=370), last)
                required = sessions[sessions >= today - timedelta(days=364)]
                self.sessions[market] = (tuple(sessions.strftime("%Y-%m-%d")), len(required), cal.session_close(last).timestamp())
            sessions, required, close_time = self.sessions[market]
            if (close_time <= market_time <= self.now.timestamp() and len(dates) >= required and dates == sessions[-len(dates):]
                    and all(math.isfinite(x) and x > 0 for x in closes)
                    and all(math.isfinite(x) and x >= 0 for x in volumes)):
                return bars
        except Exception as exc:
            logger.debug("Daily close reuse unavailable (%s)", type(exc).__name__)
        self.series.pop((market, symbol), None)
        return None


def analyze_market_data(now: datetime = None, cutoff: str = None, daily=None):
    """(health_data, macro_data, missing, pending_us) 반환. pending_us = 장중이라 제외된 미국 지수.
    cutoff(YYYY-MM-DD) 지정 시 해당 일자 이전 봉만 사용 (과거 일자 시황 재작성용, 장중 봉 제거는 생략)."""
    now = now or datetime.now(KST)
    health_data, macro_data, missing, pending_us = {}, {}, [], []
    for name, sym in YF_SYMBOLS.items():
        dates, closes, volumes, off = fetch_yf_history(sym, "2y" if cutoff else "1y")
        time.sleep(0.2)
        if cutoff:
            keep = [i for i, d in enumerate(dates) if d < cutoff]
            dates, closes, volumes, dropped = [dates[i] for i in keep], [closes[i] for i in keep], [volumes[i] for i in keep], False
        else:
            dates, closes, volumes, dropped = _drop_open_bar(name, dates, closes, volumes, off, now)
        if daily is not None and not cutoff and name in INDEX_NAMES:
            daily.remember("US" if name in US_INDEX_NAMES else "KR", sym, dates, closes, volumes)
        patched = False
        if not cutoff and not dropped and name in INDEX_NAMES:
            dates, closes, volumes, patched = _patch_stale_bar(name, sym, dates, closes, volumes, now)
            if patched:
                logger.warning("%s 일봉 배열에 최신 종가가 없어 meta 시세로 보충 (%s)", name, dates[-1])
        if dropped and name in US_INDEX_NAMES:
            pending_us.append(name)
        valid = [(d, c) for d, c in zip(dates, closes) if c is not None]
        valid_closes = [c for _, c in valid]
        if len(valid_closes) < 2:
            missing.append(name)
            continue
        if name in ("US 10Y", "US 30Y") and valid_closes[-1] > 20:  # 일부 응답은 금리를 10배 값으로 줌
            valid = [(d, c / 10) for d, c in valid]
            valid_closes = [c for _, c in valid]
        cur, prev = valid_closes[-1], valid_closes[-2]
        chg = cur - prev
        chg_pct = (chg / prev) * 100 if prev != 0 else 0
        ma20 = calc_ma_safe(valid_closes, 20)
        ma50 = calc_ma_safe(valid_closes, 50)
        ma200 = calc_ma_safe(valid_closes, 200)
        if name in INDEX_NAMES:
            status, arr, vs50 = trend_status(valid_closes)
            cv = [(c, v) for c, v in zip(closes, volumes) if c is not None and v is not None]
            drop_th = LIMITS["dist_drop_pct_by_index"].get(name, LIMITS["dist_drop_pct"])
            dist_days = calc_dist_days([c for c, _ in cv], [v for _, v in cv], drop_pct=drop_th)
            health_data[name] = {
                "status": status,
                "cur": cur,
                "chg_pct": chg_pct,
                "vs50": vs50,
                "arr": arr,
                "dist": dist_days,
                "sigma": calc_sigma(valid_closes),
                "last_date": valid[-1][0],
                "patched": patched,
                "series": valid[-60:],
            }
        else:
            status = "⚪ 판단불가"
            fmt_cur, fmt_chg = f"{cur:,.2f}", _signed(chg, ".2f")
            if name == "USD/KRW":
                fmt_cur, fmt_chg = f"{cur:,.2f}원", _signed(chg, ".2f", suffix="원")
                status = _level(cur, *LIMITS["usdkrw"], ("🔴 위험", "🟡 주의", "🟢 안정"))
            elif name == "DXY":
                status = _level(cur, *LIMITS["dxy"], ("🔴 강달러/부담", "🟡 중립", "🟢 약달러/호조"))
            elif name in ("US 10Y", "US 30Y"):
                fmt_cur, fmt_chg = f"{cur:.3f}%", _signed(chg, ".3f", suffix="%p")
                status = _level(cur, *LIMITS["us10y" if name == "US 10Y" else "us30y"], ("🔴 위험/발작", "🟡 경계", "🟢 안정"))
            elif name == "VIX":
                status = _level(cur, *LIMITS["vix"], ("🔴 공포", "🟡 주의", "🟢 안정"))
            elif name == "WTI":
                fmt_cur, fmt_chg = f"${cur:,.2f}", _signed(chg, ",.2f", prefix="$")
                status = _level(cur, *LIMITS["wti"], ("🔴 인플레 우려", "🟡 중립", "🟢 안정"))
            elif name == "GOLD":
                fmt_cur, fmt_chg = f"${cur:,.2f}", _signed(chg, ",.2f", prefix="$")
                status = "🟢 강세" if cur > prev else "🟡 관망"
            elif name == "COPPER":
                fmt_cur, fmt_chg = f"${cur:,.2f}", _signed(chg, ",.2f", prefix="$")
                if ma20:
                    status = "🟢 경기회복(강세)" if cur > ma20 else "🔴 경기침체(약세)"
            elif name == "HYG":
                fmt_cur, fmt_chg = f"${cur:,.2f}", _signed(chg, ",.2f", prefix="$")
                if ma50:
                    status = "🟢 신용안정" if cur > ma50 else "🔴 신용경색(위험)"
            elif name == "BTC":
                fmt_cur, fmt_chg = f"${cur:,.0f}", _signed(chg, ",.0f", prefix="$")
                if ma20:
                    status = "🟢 Risk-On" if cur > ma20 else "🔴 Risk-Off"
            sign = "▲" if chg > 0 else "▼" if chg < 0 else "-"
            macro_data[name] = {"raw": cur, "cur": fmt_cur, "chg_str": f"{sign} {fmt_chg} ({chg_pct:+.2f}%)", "status": status}
    return health_data, macro_data, missing, pending_us


REGIME_BENCH = ["S&P 500", "NASDAQ", "KOSPI", "KOSDAQ"]
REGIME_LEVELS = {"🟢": ("상승 우위", "정상 비중 대응 — 신규 진입은 셋업이 충족될 때만", "TIP"),
                 "🟡": ("혼조 · 선별 대응", "비중 축소·선별 진입, 보유 종목 손절선 점검", "WARNING"),
                 "🔴": ("하락 압력 · 방어", "신규 진입 자제, 손절 엄수·현금 비중 우선", "CAUTION")}


def compute_regime(health: dict, macro: dict, breadth_pct) -> dict:
    """추세 매매용 시장 신호(규칙 기반 참고치). 기준 지수 추세 + 미국 분산일 + 시장 폭 + VIX 를 점수로 합산한다.
    ≥ +1.5 → 🟢, ≤ −2.0 → 🔴, 그 사이 → 🟡(방어 신호는 보수적으로). 지수 데이터가 없으면 None."""
    bench = [n for n in REGIME_BENCH if n in health]
    if not bench:
        return None
    up = [n for n in bench if health[n]["status"].startswith("🟢")]
    down = [n for n in bench if health[n]["status"].startswith("🔴")]
    score = (len(up) - len(down)) / len(bench) * 2
    lines = [f"- **추세:** 기준 지수 {len(bench)}개 중 상승 {len(up)} · 조정 {len(bench) - len(up) - len(down)} · 하락 {len(down)} "
             f"({', '.join(n + ' ' + health[n]['status'][:1] for n in bench)})"]
    us = [n for n in ("S&P 500", "NASDAQ") if n in health]
    if us:
        worst = max(health[n]["dist"] for n in us)
        score -= 2 if worst >= LIMITS["dist_danger"] else 1 if worst >= LIMITS["dist_warn"] else 0
        icon = lambda d: "🔴" if d >= LIMITS["dist_danger"] else "🟡" if d >= LIMITS["dist_warn"] else "🟢"
        lines.append("- **분산일:** " + " · ".join(f"{n} {health[n]['dist']}개 {icon(health[n]['dist'])}" for n in us))
    if breadth_pct is not None:
        hi, lo = LIMITS["breadth"]
        score += -0.5 if breadth_pct > hi else 1 if breadth_pct > lo else -1
        lines.append(f"- **시장 폭:** S&P 500 중 50일선 상회 {breadth_pct:.0f}% " + ("(🔴 과열)" if breadth_pct > hi else "(🟢 건강)" if breadth_pct > lo else "(🟡 약세)"))
    vix = (macro.get("VIX") or {}).get("raw")
    if vix:
        hi, lo = LIMITS["vix"]
        score += -1 if vix > hi else 0.5 if vix <= lo else 0
        lines.append(f"- **변동성:** VIX {vix:.1f} " + ("(🔴 공포)" if vix > hi else "(🟢 안정)" if vix <= lo else "(🟡 주의)"))
    weak = [n for n in bench if health[n]["vs50"].endswith("(아래)")]
    if weak:
        lines.append(f"- **주목:** 50일선 아래 — {', '.join(weak)}")
    level = "🟢" if score >= 1.5 else "🔴" if score <= -2.0 else "🟡"
    title, guide, kind = REGIME_LEVELS[level]
    return {"level": level, "title": title, "guide": guide, "kind": kind, "score": score, "lines": lines}


def regime_card(reg: dict, health: dict, macro: dict) -> str:
    """시황 맨 위 신호 카드(GitHub 콜아웃). 렌더러가 지원하지 않으면 일반 인용문으로 보인다."""
    snap = []
    for n in REGIME_BENCH:
        if n in health:
            d = health[n]
            arrow = "▲" if d["chg_pct"] > 0 else "▼" if d["chg_pct"] < 0 else "-"
            snap.append(f"{n} {d['cur']:,.0f} {arrow}{abs(d['chg_pct']):.1f}%")
    for k in ("USD/KRW", "US 10Y"):
        if k in macro:
            snap.append(f"{k} {macro[k]['cur']}")
    body = [f"[!{reg['kind']}]", f"**{reg['level']} {reg['title']}** — {reg['guide']}", "", " · ".join(snap), ""] + reg["lines"] + \
           ["", f"<sub>규칙 기반 참고용 판정 (점수 {reg['score']:+.1f}) · 기준은 아래 '지표별 상태 판별 기준' 참조</sub>"]
    return "\n".join(("> " + l).rstrip() for l in body)


def build_criteria_text() -> str:
    krw_hi, krw_lo = LIMITS["usdkrw"]
    dxy_hi, dxy_lo = LIMITS["dxy"]
    vix_hi, vix_lo = LIMITS["vix"]
    wti_hi, wti_lo = LIMITS["wti"]
    y10, y30 = LIMITS["us10y"], LIMITS["us30y"]
    br_hi, br_lo = LIMITS["breadth"]
    inv = LIMITS["dist_invalidate_pct"]
    lines = [
        f"- **USD/KRW (환율):** {krw_hi:,}원 초과(🔴위험), {krw_lo:,}~{krw_hi:,}원(🟡주의), {krw_lo:,}원 미만(🟢안정)",
        f"- **달러 인덱스 (DXY):** {dxy_hi} 초과(🔴강달러/부담), {dxy_lo}~{dxy_hi}(🟡중립), {dxy_lo} 미만(🟢약달러/호조)",
        f"- **미국 10년물:** {y10[0]}% 초과(🔴위험/발작), {y10[1]}~{y10[0]}%(🟡경계), {y10[1]}% 미만(🟢안정)",
        f"- **미국 30년물:** {y30[0]}% 초과(🔴위험/발작), {y30[1]}~{y30[0]}%(🟡경계), {y30[1]}% 미만(🟢안정)",
        f"- **VIX (공포지수):** {vix_hi} 초과(🔴공포), {vix_lo}~{vix_hi}(🟡주의), {vix_lo} 미만(🟢안정)",
        "- **HYG (하이일드 채권):** 50일선 상회(🟢신용안정), 하회(🔴신용경색/위험)",
        f"- **WTI 원유:** ${wti_hi} 초과(🔴인플레 우려), ${wti_lo}~${wti_hi}(🟡중립), ${wti_lo} 미만(🟢안정)",
        "- **금 (GOLD):** 전일 대비 상승(🟢강세), 하락/보합(🟡관망)",
        "- **구리 (COPPER):** 20일선 상회(🟢경기회복/강세), 하회(🔴경기침체/약세)",
        "- **비트코인 (BTC):** 20일선 상회(🟢Risk-On), 하회(🔴Risk-Off)",
        f"- **S&P 500 시장폭(50일선 상회 비율):** {br_hi}% 초과(🔴과열), {br_lo}~{br_hi}%(🟢상승장), {br_lo}% 이하(🟡침체)",
        "- **시장 신호(맨 위 카드):** 기준 지수(S&P 500·NASDAQ·KOSPI·KOSDAQ) 추세 −2~+2점, 미국 분산일 주의 −1·위험 −2, "
        "시장 폭 건강 +1·약세 −1·과열 −0.5, VIX 공포 −1·안정 +0.5 합산 → +1.5 이상 🟢, −2.0 이하 🔴, 그 사이 🟡",
        f"- **분산일:** 최근 {LIMITS['dist_lookback']}거래일 중 -0.2% 이하 하락 + 거래량 증가인 날. {LIMITS['dist_danger']}개 이상(🔴위험), {LIMITS['dist_warn']}개 이상(🟡주의)"
        + (f", 이후 종가가 {inv:g}% 이상 오르면 해당 분산일 제외" if inv > 0 else ""),
        f"- **지수 분산일 하락 기준:** 기본 -{LIMITS['dist_drop_pct']:g}%"
        + (" (지수별: " + ", ".join(f"{n} -{x:g}%" for n, x in LIMITS["dist_drop_pct_by_index"].items()) + ")" if LIMITS["dist_drop_pct_by_index"] else "")
        + f". σ는 최근 {LIMITS['sigma_days']}거래일 일간 변동성",
        "- **보유 종목 분산일:** 하락률이 "
        + (f"-{LIMITS['stock_dist_k']:g}×σ 이하" if LIMITS["stock_dist_mode"] == "sigma" else f"-{LIMITS['stock_dist_fixed_pct']:g}% 이하")
        + " + 거래량 증가, 개수 기준은 지수와 동일",
    ]
    return "<details>\n<summary>💡 지표별 상태 판별 기준 (클릭하여 펼치기)</summary>\n\n" + "\n".join(lines) + "\n\n</details>"


def _spread_labels(ys: list, min_gap: float = 14.0, upper: float = 40.0, lower: float = 294.0) -> list:
    if not ys:
        return []
    order = sorted(range(len(ys)), key=lambda i: ys[i])
    out = list(ys)
    for a, b in zip(order, order[1:]):
        if out[b] - out[a] < min_gap:
            out[b] = out[a] + min_gap
    if out[order[-1]] > lower:
        out[order[-1]] = lower
        for a, b in reversed(list(zip(order, order[1:]))):
            if out[b] - out[a] < min_gap:
                out[a] = max(upper, out[b] - min_gap)
    return out


def build_relative_chart_svg(title: str, health_data: dict, keys: list, colors: list) -> str:
    W, H, ml, mr, mt, mb = 840, 300, 50, 170, 40, 30
    plot_w, plot_h = W - ml - mr, H - mt - mb
    raw = []
    for key, color in zip(keys, colors):
        s = (health_data.get(key) or {}).get("series")
        if s:
            raw.append({"name": key, "color": color, "pts": s})
    if not raw:
        return ""
    common_start = max(r["pts"][0][0] for r in raw)
    series_data = []
    for r in raw:
        pts = [(d, c) for d, c in r["pts"] if d >= common_start and c]
        if len(pts) < 2:
            continue
        r["pts"] = pts
        series_data.append(r)
    if not series_data:
        return ""
    all_dates = sorted({d for r in series_data for d, _ in r["pts"]})
    idx = {d: i for i, d in enumerate(all_dates)}
    min_pct, max_pct = 0.0, 0.0
    for r in series_data:
        base = r["pts"][0][1]
        r["pcts"] = [(idx[d], (c / base - 1) * 100) for d, c in r["pts"]]
        vals = [p for _, p in r["pcts"]]
        min_pct, max_pct = min(min_pct, min(vals)), max(max_pct, max(vals))
    pad = (max_pct - min_pct) * 0.1 or 1
    lo, hi = min_pct - pad, max_pct + pad
    step = plot_w / max(len(all_dates) - 1, 1)
    X = lambda i: ml + i * step
    Y = lambda p: mt + (hi - p) / (hi - lo) * plot_h
    e = html.escape
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="sans-serif">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<text x="{ml}" y="24" font-size="14" font-weight="bold" fill="#222">{e(title)} (최근 3개월 상대 수익률)</text>']
    y0 = Y(0)
    o.append(f'<line x1="{ml}" y1="{y0:.1f}" x2="{W - mr}" y2="{y0:.1f}" stroke="#aaaaaa" stroke-dasharray="4 4"/>')
    o.append(f'<text x="{ml - 6}" y="{y0 + 4:.1f}" font-size="10" text-anchor="end" fill="#888">0%</text>')
    for p in [lo + (hi - lo) * 0.25, lo + (hi - lo) * 0.75]:
        y = Y(p)
        o.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{W - mr}" y2="{y:.1f}" stroke="#f0f0f0"/>')
        o.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" font-size="10" text-anchor="end" fill="#888">{p:+.0f}%</text>')
    # x축: 주 단위(해당 주 첫 거래일) 눈금 + 연한 세로선. 라벨이 너무 붙으면 건너뜀
    ticks, prev_wk = [], None
    for d in all_dates:
        wk = datetime.strptime(d, "%Y-%m-%d").isocalendar()[:2]
        if wk != prev_wk:
            ticks.append(d)
            prev_wk = wk
    min_px, prev_x, x_labels = 34.0, -1e9, []
    for d in ticks:
        x = X(idx[d])
        if x - prev_x >= min_px:
            x_labels.append(d)
            prev_x = x
    last_d = all_dates[-1]
    if X(idx[last_d]) - prev_x >= min_px + 6 and last_d not in x_labels:
        x_labels.append(last_d)
    for d in x_labels:
        x = X(idx[d])
        o.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{H - mb}" stroke="#f2f2f2"/>')
        o.append(f'<text x="{x:.1f}" y="{H - 10}" font-size="10" text-anchor="middle" fill="#888">{d[5:]}</text>')
    label_ys = _spread_labels([Y(s["pcts"][-1][1]) + 4 for s in series_data], lower=H - 6)
    for s, ly in zip(series_data, label_ys):
        pts = " ".join(f"{X(i):.1f},{Y(p):.1f}" for i, p in s["pcts"])
        o.append(f'<polyline points="{pts}" fill="none" stroke="{s["color"]}" stroke-width="2"/>')
        last = s["pcts"][-1][1]
        o.append(f'<text x="{W - mr + 5}" y="{ly:.1f}" font-size="12" font-weight="bold" fill="{s["color"]}">{e(s["name"])} ({last:+.1f}%)</text>')
    o.append(f'<text x="{W - mr}" y="{H - 22}" font-size="9" text-anchor="end" fill="#aaa">{all_dates[0]} ~ {all_dates[-1]} · 눈금=주 첫 거래일</text>')
    o.append("</svg>")
    return "\n".join(o)


# ==========================================
# 2. 마크다운 템플릿 및 파일 제어
# ==========================================
TRADES_RE = re.compile(r"<!-- TRADES_START -->.*?<!-- TRADES_END -->", re.S)
SUMMARY_RE = re.compile(r"<!-- SUMMARY_START -->.*?<!-- SUMMARY_END -->", re.S)
CHART_RE = re.compile(r"<!-- CHART_START -->.*?<!-- CHART_END -->", re.S)
REGIME_RE = re.compile(r"<!-- REGIME_START -->.*?<!-- REGIME_END -->", re.S)
CARD_RE = re.compile(r"<!-- CARD_START -->.*?<!-- CARD_END -->", re.S)
MARKET_CHART_RE = re.compile(r"<!-- MARKET_CHART_START -->.*?<!-- MARKET_CHART_END -->", re.S)
BREADTH_RE = re.compile(r"<!-- BREADTH_START -->.*?<!-- BREADTH_END -->", re.S)
HEALTH_RE = re.compile(r"<!-- HEALTH_START -->.*?<!-- HEALTH_END -->", re.S)
MACRO_RE = re.compile(r"<!-- MACRO_START -->.*?<!-- MACRO_END -->", re.S)
HOLDINGS_RE = re.compile(r"<!-- HOLDINGS_START -->.*?<!-- HOLDINGS_END -->", re.S)


def _fmt_qty(q): return f"{float(q):,.0f}" if float(q).is_integer() else f"{float(q):,.4f}".rstrip("0").rstrip(".")
def _fmt_money(v, currency): return f"{float(v):,.2f}" if currency == "USD" else f"{float(v):,.0f}"
def _fmt_signed_money(v, currency): return f"{float(v):+,.2f}" if currency == "USD" else f"{float(v):+,.0f}"
def safe_filename(name: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣ㄱ-ㅎ_\-().&]", "", re.sub(r"\s+", "_", str(name).strip())).strip("._")[:40] or "종목"


PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")
NOTE_RE = re.compile(r"<!--#.*?#-->\s*", re.S)
TEMPLATE_PATHS = {"market": f"{TEMPLATE_DIR}/market.md", "stock_buy": f"{TEMPLATE_DIR}/stock_buy.md",
                  "stock_sell": f"{TEMPLATE_DIR}/stock_sell.md"}
TEMPLATE_KIND = {"매수": "stock_buy", "매도": "stock_sell"}
_TEMPLATE_CACHE = {}

DEFAULT_MARKET_TEMPLATE = """---
date: {{date}}
weekday: {{weekday}}
regime: 
tags: [시황, 매크로]
---
# 📅 {{date}} ({{weekday}}) 시황

<!-- REGIME_START -->
<!-- REGIME_END -->

## 💼 내 포지션

<!-- HOLDINGS_START -->
<!-- HOLDINGS_END -->

## 🧠 오늘의 계획
> **오늘의 한 줄 요약:** 
>
> **나의 전략:** 

**관심종목**
- **종목 (시장):** 
  - **시나리오 (Setup):** 
  - **진입가 / 손절가:** 

**점검**
- [ ] 보유 종목 손절선·50일선 이탈 여부 확인
- [ ] 신규 진입은 시장 신호 확인 후, 사전에 정한 손절가·비중으로
- [ ] 오늘 예정된 이벤트·실적 확인

---
## 📊 시장 근거

### 🚦 지수 건강도 & 분산일

<!-- HEALTH_START -->
<!-- HEALTH_END -->

### 📈 지수 상대 성과

<!-- MARKET_CHART_START -->
<!-- MARKET_CHART_END -->

### 🌡️ 시장 폭 (Market Breadth)

<!-- BREADTH_START -->
<!-- BREADTH_END -->

### 🌍 글로벌 매크로 지표

<!-- MACRO_START -->
<!-- MACRO_END -->
"""

DEFAULT_STOCK_BUY_TEMPLATE = """---
date: {{date}}
market: {{market}}
code: "{{code}}"
name: "{{name_yaml}}"
currency: {{currency}}
accounts: []
side: 
setup: 
stop: 
target: 
rating: 
tags: []
---
# 📥 {{name}} ({{code}}) 매수 일지
> 📅 {{date}} · 🔗 [시황](../market/{{date}}.md)

<!-- CARD_START -->
<!-- CARD_END -->

## 📈 차트

<!-- CHART_START -->
<!-- CHART_END -->

## 🎯 셋업 & 계획
- **셋업:** (베이스 돌파 / 눌림목 / 추세 추종 …)
- **진입 근거:** (추세·RS·거래량·시장 신호)
- **차트 패턴 / 모멘텀:** 
- **초기 리스크 관리:** 손절가는 맨 위 메타데이터 `stop`, 목표가는 `target` 에 숫자로 적으세요(시스템이 자동 추적). 손익비 1 : ( ), 비중 ( )%

**진입 체크리스트**
- [ ] 시장 신호 우호적 (시황 확인)
- [ ] 정배열(20 > 50 > 200일선) 및 50일선 위
- [ ] 지수 대비 상대강도(RS) 우위
- [ ] 거래량 동반, 분산일 적음
- [ ] 손절가·비중을 진입 전에 확정

## 📝 복기
- **매수 타이밍은 적절했는가:** 
- **잘한 점:** 
- **아쉬운 점:** 
- **규칙 준수:** (지킴 / 어김 → 이유)
- **한 줄 교훈:** 

---
<details>
<summary>📒 상세 체결 데이터 (클릭)</summary>

**1. 체결 내역**

<!-- TRADES_START -->
<!-- TRADES_END -->

**2. 요약**

<!-- SUMMARY_START -->
<!-- SUMMARY_END -->

</details>"""

DEFAULT_STOCK_SELL_TEMPLATE = """---
date: {{date}}
market: {{market}}
code: "{{code}}"
name: "{{name_yaml}}"
currency: {{currency}}
accounts: []
side: 
pnl_amount: 
pnl_pct: 
r_multiple: 
setup: 
result: 
rating: 
tags: []
---
# 📤 {{name}} ({{code}}) 매도 일지
> 📅 {{date}} · 🔗 [시황](../market/{{date}}.md)

<!-- CARD_START -->
<!-- CARD_END -->

## 📈 차트

<!-- CHART_START -->
<!-- CHART_END -->

## 🎯 청산 복기
- **청산 유형:** (손절 / 목표 도달 / 추세 이탈 / 시간·기회비용 / 기타)
- **청산 근거:** 
- **계획 대비:** 손절가( ) · 목표가( ) · 실제 청산가( ) → R( )
- **매수 시나리오 평가:** 
- **잘한 점 / 개선할 점:** 
- **규칙 준수:** (지킴 / 어김 → 이유)
- **청산 후 추이 (5·10거래일):** 
- **한 줄 교훈:** 

**청산 체크리스트**
- [ ] 사전 계획(손절·목표·추세 이탈)대로 실행했는가
- [ ] 공포·욕심에 의한 청산은 아니었는가
- [ ] 재진입 조건 정리

---
<details>
<summary>📒 상세 체결 데이터 (클릭)</summary>

**1. 체결 내역**

<!-- TRADES_START -->
<!-- TRADES_END -->

**2. 요약**

<!-- SUMMARY_START -->
<!-- SUMMARY_END -->

</details>"""

DEFAULT_TEMPLATES = {"market": DEFAULT_MARKET_TEMPLATE, "stock_buy": DEFAULT_STOCK_BUY_TEMPLATE,
                     "stock_sell": DEFAULT_STOCK_SELL_TEMPLATE}


def render_template(text: str, values: dict) -> str:
    text = NOTE_RE.sub("", text.replace("\r\n", "\n")).lstrip("\n")
    return PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1), m.group(0))), text).rstrip("\n") + "\n"


def load_template(repo, kind: str) -> str:
    if kind in _TEMPLATE_CACHE:
        return _TEMPLATE_CACHE[kind]
    try:
        raw, _ = read_repo_text(repo, TEMPLATE_PATHS[kind])
    except Exception:
        raw = None
    _TEMPLATE_CACHE[kind] = raw if raw and NOTE_RE.sub("", raw).strip() else DEFAULT_TEMPLATES[kind]
    return _TEMPLATE_CACHE[kind]


def _replace_optional(text, regex, start, end, body):
    if not regex.search(text):
        return text
    # 주석 마커 위아래에 빈 줄을 둬서 표/이미지/인용문이 앞뒤 블록과 붙어 렌더링되는 것을 방지
    return regex.sub(lambda _: f"{start}\n\n{body.strip(chr(10))}\n\n{end}", text, count=1)


META_SUMMARY = "<summary>📋 메타데이터 (클릭)</summary>"
_FRONT_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*\n?", re.S)
_FOLDED_RE = re.compile(r"\A<details>\n" + re.escape(META_SUMMARY) + r"\n\n```yaml\n(.*?)\n```\n\n</details>\n?", re.S)


def fold_meta(text: str) -> str:
    """맨 위 YAML 프론트매터(--- … ---)를 접이식 블록으로 옮긴다. 키는 줄 맨 앞에 그대로 있어 기존 갱신 로직이 계속 동작한다."""
    m = _FRONT_RE.match(text)
    if not m:
        return text
    return f"<details>\n{META_SUMMARY}\n\n```yaml\n{m.group(1)}\n```\n\n</details>\n\n" + text[m.end():].lstrip("\n")


def unfold_meta(text: str) -> str:
    m = _FOLDED_RE.match(text)
    if not m:
        return text
    return f"---\n{m.group(1)}\n---\n" + text[m.end():].lstrip("\n")


def apply_meta_layout(text: str) -> str:
    """limits.json 의 journal_meta 설정("folded"/"top")에 맞춰 메타데이터 위치를 맞춘다(이미 맞으면 그대로)."""
    return fold_meta(text) if LIMITS["journal_meta"] == "folded" else unfold_meta(text)


def _set_frontmatter(text: str, key: str, value: str) -> str:
    return re.sub(rf"^{key}:.*$", lambda _: f"{key}: {value}", text, count=1, flags=re.M)


def _set_auto_frontmatter(text: str, key: str, value: str) -> str:
    """값이 비어 있거나 이전에 자동 기입한 값('# auto')일 때만 갱신 (사용자가 직접 쓴 값은 보존)."""
    m = re.search(rf"^{key}:[ \t]*(.*)$", text, flags=re.M)
    if not m:
        return text
    cur = m.group(1).strip()
    if cur and not cur.endswith("# auto"):
        return text
    return text[:m.start()] + f"{key}: {value} # auto" + text[m.end():]


# ==========================================
# 3. GitHub 저장소 I/O
# ==========================================
_REPO_CACHE = {}


def reset_run_caches():
    for cache in (_REPO_CACHE, _TEMPLATE_CACHE, _SIGNAL_CACHE, _FILES_CACHE):
        cache.clear()
    _LIMITS_LOADED[0] = False


def get_data_repo():
    if "repo" not in _REPO_CACHE:
        _REPO_CACHE["repo"] = Github(auth=Auth.Token(DATA_TOKEN), timeout=20).get_repo(DATA_REPO_NAME)
    return _REPO_CACHE["repo"]


def read_repo_text(repo, path, ref=None):
    try:
        c = repo.get_contents(path, **({"ref": ref} if ref else {}))
    except GithubException as e:
        if e.status == 404:
            return None, None
        raise
    data = c.decoded_content if c.encoding == "base64" else base64.b64decode(repo.get_git_blob(c.sha).content)
    return data.decode("utf-8"), c.sha


def repo_file_exists(repo, path) -> bool:
    try:
        repo.get_contents(path)
        return True
    except GithubException as e:
        if e.status == 404:
            return False
        raise


def upsert_file(repo, path, message, content):
    old, sha = read_repo_text(repo, path)
    if old is None:
        repo.create_file(path, message, content)
    elif old != content:
        repo.update_file(path, message, content, sha)


def load_ledger(repo):
    text, sha = read_repo_text(repo, LEDGER_PATH)
    if not text or not text.strip():
        return pd.DataFrame(columns=LEDGER_COLUMNS), sha
    df = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False).reindex(columns=LEDGER_COLUMNS)
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    for c in STR_COLS:
        df[c] = df[c].fillna("").astype(str)
    return df, sha


def save_ledger(repo, df, sha, message):
    df = df.sort_values("date", kind="stable")
    csv_text = df[LEDGER_COLUMNS].to_csv(index=False)
    res = repo.update_file(LEDGER_PATH, message, csv_text, sha) if sha else repo.create_file(LEDGER_PATH, message, csv_text)
    return res["content"].sha if res else None


# ==========================================
# 4. 시황 / 차트 / 종목 일지
# ==========================================
def ensure_market_journal(repo, date_iso: str, now: datetime, force: bool = False, daily=None) -> bool:
    past = date_iso < now.strftime("%Y-%m-%d")  # 과거 일자: 해당 일자 이전 종가 기준으로 재구성
    path = f"{MARKET_DIR}/{date_iso}.md"
    if repo_file_exists(repo, path):
        logger.info("시황 일지가 이미 존재합니다: %s (덮어쓰지 않음)", path)
        return False
    wd = WEEKDAYS_KO[datetime.strptime(date_iso, "%Y-%m-%d").weekday()]
    ensure_limits(repo)
    logger.info("야후 파이낸스 / Finviz 데이터 수집 중...")
    health_data, macro_data, missing, pending_us = analyze_market_data(now, date_iso if past else None, daily=daily)
    if pending_us and not force:
        # 겨울철(EST) 등 06:07 KST 에는 미국 정규장 마감 직후라 봉이 미확정 → 다음 실행에서 재시도
        logger.info("미국 지수 종가 미확정(%s) → 다음 실행에서 시황 작성", ", ".join(pending_us))
        return False
    if pending_us:
        add_warning("미국 지수 종가 미확정 상태로 시황 작성: " + ", ".join(pending_us))
    if not health_data and not macro_data:
        add_warning("시황 데이터 수집 실패")
        return False
    if missing:
        add_warning("Yahoo 수집 누락: " + ", ".join(missing))
    text = apply_meta_layout(render_template(load_template(repo, "market"), {"date": date_iso, "weekday": wd}))
    breadth_pct = None if past else get_sp500_breadth()  # Finviz 는 과거 시점 조회 불가
    regime = compute_regime(health_data, macro_data, breadth_pct) if LIMITS["regime_show"] else None
    if regime:
        text = _replace_optional(text, REGIME_RE, "<!-- REGIME_START -->", "<!-- REGIME_END -->", regime_card(regime, health_data, macro_data))
        text = _set_frontmatter(text, "regime", f'"{regime["level"]} {regime["title"]}"')
    chart_md_lines = []
    if health_data:
        us_svg = build_relative_chart_svg("🇺🇸 미국 주요 지수", health_data,
                                          ["S&P 500", "NASDAQ", "DOW", "RUSSELL 2000", "PHLX Semi"],
                                          ["#2f6fdb", "#d9363e", "#1a9850", "#e08a00", "#8e44ad"])
        if us_svg:
            upsert_file(repo, f"{MARKET_CHART_DIR}/{date_iso}_US_INDEX.svg", "Auto-chart", us_svg)
            chart_md_lines.append(f"![미국 지수 상대 비교](charts/{date_iso}_US_INDEX.svg)")
        kr_svg = build_relative_chart_svg("🇰🇷 국내 주요 지수", health_data, ["KOSPI", "KOSDAQ"], ["#2f6fdb", "#d9363e"])
        if kr_svg:
            upsert_file(repo, f"{MARKET_CHART_DIR}/{date_iso}_KR_INDEX.svg", "Auto-chart", kr_svg)
            chart_md_lines.append(f"![국내 지수 상대 비교](charts/{date_iso}_KR_INDEX.svg)")
    if chart_md_lines:
        text = _replace_optional(text, MARKET_CHART_RE, "<!-- MARKET_CHART_START -->", "<!-- MARKET_CHART_END -->", "\n\n".join(chart_md_lines))
    if breadth_pct is not None:
        b_stat = "🔴 과열 (조정 주의)" if breadth_pct > LIMITS["breadth"][0] else "🟢 상승장 (진입 유리)" if breadth_pct > LIMITS["breadth"][1] else "🟡 침체 (바닥권 대기)"
        b_lines = [f"- **S&P 500 50일선 상회 비율:** {breadth_pct:.1f}% → {b_stat}"]
    elif past:
        b_lines = ["- **S&P 500 50일선 상회 비율:** 과거 일자 조회 불가"]
    else:
        b_lines = ["- **S&P 500 50일선 상회 비율:** 조회 실패"]
        add_warning("Finviz 시장폭 조회 실패")
    text = _replace_optional(text, BREADTH_RE, "<!-- BREADTH_START -->", "<!-- BREADTH_END -->", "\n".join(b_lines))
    if health_data:
        present = [i for i in INDEX_NAMES if i in health_data]
        def _px(d):
            arrow = "▲" if d["chg_pct"] > 0 else "▼" if d["chg_pct"] < 0 else "-"
            return f"{d['cur']:,.2f} ({arrow}{abs(d['chg_pct']):.2f}%)"
        def _dist(d): return (f"**{d['dist']}개** ({'🔴위험' if d['dist'] >= LIMITS['dist_danger'] else '🟡주의' if d['dist'] >= LIMITS['dist_warn'] else '🟢안전'})"
                              + (f" · σ{d['sigma']:.2f}%" if d.get("sigma") else ""))
        if len(present) <= SMALL_TABLE_ROWS:
            h_lines = [f"- **{i}** {_px(health_data[i])} · {health_data[i]['status']} · 50일선 대비 {health_data[i]['vs50']} · {health_data[i]['arr']} · 분산일 {_dist(health_data[i])}"
                       for i in present]
        else:
            heads = ["지수", "현재가 (전일비)", "상태", "50일선比", "배열", f"분산일({LIMITS['dist_lookback']}일)"]
            h_lines = ["| " + " | ".join(_sm(h) for h in heads) + " |", "| :--- | ---: | :---: | :---: | :---: | :---: |"]
            for i in present:
                d = health_data[i]
                vals = [f"**{i}**", _px(d), d["status"], d["vs50"], d["arr"], _dist(d)]
                h_lines.append("| " + " | ".join(_sm(v) for v in vals) + " |")
        def _basis(names):
            out = []
            for i in names:
                if i not in health_data:
                    continue
                ld = health_data[i]["last_date"]
                stale = (not past) and ld < _expected_last_date(i, now)
                out.append(f"{i} {ld[5:]}" + (" ⚠️" if stale else "") + (" (보충)" if health_data[i].get("patched") else ""))
            return ", ".join(out)
        kr_b, us_b = _basis(["KOSPI", "KOSDAQ"]), _basis([i for i in INDEX_NAMES if i in US_INDEX_NAMES])
        basis = "> 기준 종가일(현지 거래일): " + " / ".join(x for x in (f"🇰🇷 {kr_b}" if kr_b else "", f"🇺🇸 {us_b}" if us_b else "") if x)
        if "⚠️" in basis:
            basis += "\n>\n> ⚠️ 최신 거래일 종가가 아직 반영되지 않았을 수 있습니다(휴장일이면 정상)."
        h_lines += ["", basis]
        text = _replace_optional(text, HEALTH_RE, "<!-- HEALTH_START -->", "<!-- HEALTH_END -->", "\n".join(h_lines))
    if macro_data:
        keys = [k for k in MACRO_ORDER if k in macro_data]
        if len(keys) <= SMALL_TABLE_ROWS:
            m_lines = [f"- **{k}{MACRO_DESC.get(k, '')}** {macro_data[k]['cur']} · {macro_data[k]['chg_str']} · {macro_data[k]['status']}" for k in keys]
        else:
            m_lines = ["| " + " | ".join(_sm(h) for h in ("지표", "현재", "전일 대비", "상태")) + " |", "| :--- | ---: | ---: | :--- |"]
            for k in keys:
                vals = [f"**{k}{MACRO_DESC.get(k, '')}**", macro_data[k]["cur"], macro_data[k]["chg_str"], macro_data[k]["status"]]
                m_lines.append("| " + " | ".join(_sm(v) for v in vals) + " |")
        body = "\n".join(m_lines) + "\n"
        body += "\n> ※ 환율·원자재·BTC 등 24시간 거래 자산은 작성 시점 값, 주식 지수는 직전 확정 종가 기준입니다.\n"
        if missing:
            body += f"\n> ⚠️ 수집 누락: {', '.join(missing)}\n"
        body += "\n" + build_criteria_text()
        text = _replace_optional(text, MACRO_RE, "<!-- MACRO_START -->", "<!-- MACRO_END -->", body)
    if LIMITS["holdings_show"]:
        if past:
            h_body = "- 과거 일자 재구성에서는 보유 현황을 조회하지 않습니다."
        elif not (NH_APP_KEY and NH_APP_SECRET and ACCOUNTS):
            h_body = "- NH 인증 정보가 없어 보유 현황을 조회하지 못했습니다."
        else:
            try:
                fx = (macro_data.get("USD/KRW") or {}).get("raw")
                hc = holdings_context(repo, now, daily=daily)
                h_rows, h_errs, recon = hc["rows"], hc["errs"], hc["recon"]
                if h_errs:
                    add_warning("보유 종목 조회 실패: " + "; ".join(h_errs))
                if recon:
                    sig = hashlib.md5(repr([(r["account"], r["code"], round(r["balance"] - r["ledger"], 3)) for r in recon]).encode()).hexdigest()[:12]
                    _notify_once(f"recon_{sig}", f"원장·잔고 불일치 {len(recon)}건: " + ", ".join(f"{r['account']} {r['name']} 원장 {_fmt_qty(r['ledger'])}/잔고 {_fmt_qty(r['balance'])}" for r in recon[:5])
                                 + " → manual 모드로 누락 체결을 입력하거나 reconcile_ignore 에 코드를 추가하세요.")
                h_body = holdings_block(h_rows, hc["tech"], hc["links"], recon, fx, hc["levels"]) if (h_rows or not h_errs) else "- 보유 종목 조회에 실패했습니다."
            except Exception as e:
                logger.exception(e)
                add_warning(f"보유 종목 조회 오류: {type(e).__name__}")
                h_body = "- 보유 종목 조회에 실패했습니다."
        text = _put_section(text, HOLDINGS_RE, "<!-- HOLDINGS_START -->", "<!-- HOLDINGS_END -->", "#### 💼 보유 종목", h_body)
    upsert_file(repo, path, "Auto-draft: Market context", text)
    logger.info("시황 일지 작성 완료: %s%s", path, " (과거 일자 재구성)" if past else "")
    send_telegram_message(f"🌅 <b>[시황 브리핑 완료]</b>\n{date_iso} 시황 업데이트가 완료되었습니다.\n📄 {tg_link(f'{date_iso} 시황 일지 보기', path)}")
    return True


LAST_CANDLE_ERR = {}  # (시장, 코드) → NH 일봉 조회 실패 사유 (계좌/금액 정보 없음)


def fetch_yf_candles(symbol: str, range_str: str = "1y"):
    """야후 일봉(OHLCV). 받지 못하면 빈 리스트. NH 일봉 조회 실패 시 폴백용."""
    for host in YF_HOSTS:
        try:
            r = HTTP.get(f"https://{host}/v8/finance/chart/{symbol}?interval=1d&range={range_str}",
                         headers={"User-Agent": BROWSER_UA}, timeout=10)
            if r.status_code != 200:
                continue
            data = r.json().get("chart", {}).get("result") or []
            if not data:
                continue
            res = data[0]
            ts = res.get("timestamp") or []
            off = (res.get("meta") or {}).get("gmtoffset") or 0
            q = (res.get("indicators", {}).get("quote") or [{}])[0]
            by_date = {}
            for i, t in enumerate(ts):
                try:
                    c = q["close"][i]
                    if c is None or c <= 0:
                        continue
                    o = q["open"][i] or c
                    h = q["high"][i] or max(o, c)
                    l = q["low"][i] or min(o, c)
                    v = (q.get("volume") or [None] * len(ts))[i] or 0
                except (KeyError, IndexError, TypeError):
                    continue
                d = datetime.fromtimestamp(t + off, timezone.utc).strftime("%Y%m%d")
                by_date[d] = {"date": d, "open": o, "high": max(h, o, c), "low": min(l, o, c), "close": c, "vol": float(v)}
            if by_date:
                return [by_date[k] for k in sorted(by_date)]
        except Exception as e:
            logger.debug("Yahoo 일봉 수집 실패 (%s @ %s): %s", symbol, host, mask(e))
    return []


def fetch_candles(market: str, code: str, end8: str, count: int = CHART_FETCH):
    """NH 일봉을 먼저 시도하고, 실패하면 사유를 기록한 뒤 야후 일봉으로 대체한다."""
    candles = _fetch_candles_nh(market, code, end8, count)
    if candles:
        return candles
    reason = LAST_CANDLE_ERR.get((market, code), "원인 미상")
    for sym in _yf_symbols(market, code):
        yc = [c for c in fetch_yf_candles(sym) if c["date"] <= end8][-count:]
        time.sleep(0.2)
        if len(yc) >= 5:
            logger.warning("NH 일봉 조회 실패 (%s %s: %s) → 야후 일봉으로 대체", market, code, reason)
            _notify_once(f"candle_fb_{market}_{end8}", f"{market} 일봉: NH 시세 조회 실패({reason}) → 야후 일봉으로 대체했습니다.")
            return yc
    return None


def _fetch_candles_nh(market: str, code: str, end8: str, count: int = CHART_FETCH, us_market_cls: str = "1"):
    LAST_CANDLE_ERR.pop((market, code), None)
    if market == "KR":
        path = "/krstock/quote/v1/period"
        body = {"Input_0": {"market_cd": "KRX", "iem_cd": code, "view_main_yn": "Y", "edate": end8,
                            "array_cnt": str(count), "gubun": "1"}}
    else:
        path = "/gbstock/quote/v1/period"
        body = {"Input_0": {"iem_cd": code, "end_dt": end8, "count": str(count), "maxavg": "060", "gubun": "3",
                            "xtick": "0001", "today_cls": "0", "market_cls": us_market_cls}}
    try:
        resps = post_pages(path, body, max_pages=1)
    except Exception as e:
        LAST_CANDLE_ERR[(market, code)] = f"요청 예외 {type(e).__name__}"
        return None
    if not resps or resps[0].status_code != 200:
        LAST_CANDLE_ERR[(market, code)] = _err_detail(resps[0]) if resps else "응답 없음"
        return None
    js = resps[0].json()
    blocks = {k: _as_list(v) for k, v in js.items() if str(k).startswith("Output")}
    best = []
    for name in sorted(blocks):  # 일봉 행이 Output_0 이 아닌 다른 블록에 있을 수 있어 모든 블록을 검사
        parsed = _parse_candle_rows(blocks[name])
        if len(parsed) > len(best):
            best = parsed
    if len(best) >= 5:
        return sorted(best, key=lambda x: x["date"])
    desc = []
    for name in sorted(blocks):
        rows = blocks[name]
        keys = ", ".join(sorted(rows[0].keys())) if rows and isinstance(rows[0], dict) else "-"
        desc.append(f"{name}: {len(rows)}행 [{keys}]")
    LAST_CANDLE_ERR[(market, code)] = ("일봉으로 해석 가능한 블록 없음 → " + " / ".join(desc))[:900] if desc else "응답에 Output 블록 없음"
    return None


# 일봉 행의 필드명 후보(시장별 명세 차이 흡수). 앞에 있는 이름을 우선 사용
_CANDLE_KEYS = {
    "date": ("trade_date", "bsop_date", "stck_bsop_date", "xymd", "date"),
    "open": ("open_prc", "hst_open_prc", "stck_oprc", "open"),
    "high": ("high", "hst_high", "high_prc", "stck_hgpr"),
    "low": ("low", "hst_low", "low_prc", "stck_lwpr"),
    "close": ("close_prc", "hst_close_prc", "clos_prc", "hst_clos_prc", "stck_clpr", "stck_prpr", "close", "last", "hst_last_prc"),
    "vol": ("movolume", "hst_acvol", "acml_vol", "acvol", "vol", "volume"),
}


def _parse_candle_rows(rows: list) -> list:
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        pick = {name: next((to_float(r[k]) if name != "date" else r[k] for k in keys if r.get(k) not in (None, "")), None)
                for name, keys in _CANDLE_KEYS.items()}
        d = re.sub(r"\D", "", str(pick["date"] or ""))[:8]
        c = pick["close"] or 0.0
        if len(d) != 8 or c <= 0:
            continue
        o = pick["open"] if pick["open"] and pick["open"] > 0 else c
        highs = [x for x in (pick["high"], o, c) if x and x > 0]
        lows = [x for x in (pick["low"], o, c) if x and x > 0]
        out.append({"date": d, "open": o, "high": max(highs), "low": min(lows), "close": c, "vol": pick["vol"] or 0.0,
                    "_t": str(r.get("bsop_time") or r.get("time") or "")})
    # 같은 날짜 행이 여럿(분봉 등)이면 일봉으로 합산: 시가=첫 행, 종가=마지막 행, 고/저=극값, 거래량=합
    by = {}
    for x in out:
        by.setdefault(x["date"], []).append(x)
    res = []
    for d, xs in by.items():
        if len(xs) == 1:
            xs[0].pop("_t", None)
            res.append(xs[0])
            continue
        xs.sort(key=lambda x: x["_t"])
        res.append({"date": d, "open": xs[0]["open"], "high": max(x["high"] for x in xs), "low": min(x["low"] for x in xs),
                    "close": xs[-1]["close"], "vol": sum(x["vol"] for x in xs)})
    return res


def build_cycle_chart_svg(title: str, candles: list, currency: str, trade8: str, sig: str, cycle: dict) -> str:
    """사이클 단위 일봉 차트: 매수 ▲(초록)/매도 ▼(검정) 마커(같은 날·같은 방향은 합산, 시간순 번호), 평단 계단선, 보유 구간 음영, 요약 한 줄, 번호 범례."""
    if not candles:
        return ""
    ds = [c["date"] for c in candles]
    first8 = cycle["start"].replace("-", "")
    first_i = min(bisect.bisect_left(ds, first8), len(ds) - 1)
    start = max(0, min(first_i - 10, len(candles) - CHART_MIN_BARS))
    view = candles[start:]
    vds = [c["date"] for c in view]
    closes = [c["close"] for c in candles]
    ma20 = [sum(closes[i - 19:i + 1]) / 20 if i >= 19 else None for i in range(len(closes))]
    ma60 = [sum(closes[i - 59:i + 1]) / 60 if i >= 59 else None for i in range(len(closes))]
    end_i = len(view) - 1
    if cycle["end"]:
        end_i = max(0, min(end_i, bisect.bisect_right(vds, cycle["end"].replace("-", "")) - 1))
    # 같은 날·같은 방향 체결 합산 → 마커
    groups = {}
    omitted = 0
    for f in cycle["fills"]:
        d8 = f["date"].replace("-", "")
        if d8 < vds[0]:
            omitted += 1
            continue
        g = groups.setdefault((f["date"], f["side"]), {"date": f["date"], "side": f["side"], "qty": 0.0, "amt": 0.0, "n": 0, "accs": set(), "avg": f["avg"], "qty_after": f["qty_after"]})
        g["qty"] += f["qty"]; g["amt"] += f["qty"] * f["price"]; g["n"] += 1; g["accs"].add(f["account"])
        g["avg"], g["qty_after"] = f["avg"], f["qty_after"]
    markers = sorted(groups.values(), key=lambda g: (g["date"], 0 if g["side"] == "매수" else 1))
    show_num = len(markers) <= 12
    legend = []
    for n, m in enumerate(markers, 1):
        m["no"] = n
        accs = "/".join(sorted(m["accs"]))
        legend.append(f"{'①②③④⑤⑥⑦⑧⑨⑩⑪⑫'[n - 1] if show_num else str(n) + '.'} {m['date'][5:]} {m['side']} {_fmt_qty(m['qty'])}주 @ {_fmt_money(m['amt'] / m['qty'], currency)}" + (f" ({accs})" if len(accs) < 14 else ""))
    cap = 16
    if len(legend) > cap:
        legend = legend[:cap // 2] + [f"… {len(legend) - cap}건 생략 …"] + legend[-cap // 2:]
    if omitted:
        legend.insert(0, f"◀ 차트 범위 이전 체결 {omitted}건 생략")
    rows = (len(legend) + 1) // 2
    ma_vals = [v for series in (ma20[start:], ma60[start:]) for v in series if v is not None] + [x for x in (cycle.get("stop"), cycle.get("target")) if x]
    lo = min([c["low"] for c in view] + ma_vals + [m["amt"] / m["qty"] for m in markers])
    hi = max([c["high"] for c in view] + ma_vals + [m["amt"] / m["qty"] for m in markers])
    llo, lhi = math.log(max(lo, 1e-9)), math.log(hi)  # 세로축은 로그 스케일
    lpad = (lhi - llo) * 0.13 or 0.02
    llo, lhi = llo - lpad, lhi + lpad
    W, ml, mr, mt, mb, vol_h, gap = 900, 74, 24, 76, 26, 70, 14
    price_h = 330
    vbot = mt + price_h + gap + vol_h
    H = vbot + 36 + rows * 15 + 10
    plot_w = W - ml - mr
    step = plot_w / len(view)
    X = lambda i: ml + (i + 0.5) * step
    Y = lambda p: mt + (lhi - math.log(max(p, 1e-9))) / (lhi - llo) * price_h
    vmax = max(c["vol"] for c in view) or 1.0
    UP, DOWN, BUY, SELL = "#d9363e", "#2f6fdb", "#16a34a", "#111827"
    e = html.escape
    first_view_i = max(0, bisect.bisect_left(vds, first8))
    hold_days = (len(ds) - 1 if not cycle["end"] else bisect.bisect_right(ds, cycle["end"].replace("-", "")) - 1) - bisect.bisect_left(ds, first8) + 1
    nb, ns = sum(1 for f in cycle["fills"] if f["side"] == "매수"), sum(1 for f in cycle["fills"] if f["side"] == "매도")
    state = "청산" if cycle["end"] else "진행 중"
    parts = [f"보유 {max(hold_days, 1)}거래일", f"매수 {nb} / 매도 {ns}"]
    if ns and cycle["cost"]:
        parts.append(f"실현손익 {_fmt_signed_money(cycle['pnl'], currency)} ({cycle['pnl'] / cycle['cost'] * 100:+.1f}%)")
    if not cycle["end"]:
        parts.append(f"평단 {_fmt_money(cycle['fills'][-1]['avg'], currency)} · 잔량 {_fmt_qty(cycle['fills'][-1]['qty_after'])}주")
    if cycle.get("orphan"):
        parts.append("⚠ 매수 이력 없음(원장 누락)")
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="sans-serif" data-sig="{sig}">',
         f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
         f'<text x="{ml}" y="24" font-size="16" font-weight="bold" fill="#222">{e(title)} · 사이클 #{cycle["no"]} ({state})</text>',
         f'<text x="{ml}" y="44" font-size="12" fill="#333">{e(" · ".join(parts))}</text>',
         f'<text x="{ml}" y="62" font-size="11" fill="#666">▲ 매수 · ▼ 매도 · 점선 = 평단 · 빨강/초록 점선 = 손절/목표 · 파란 음영 = 보유 구간 · 노란 배경 = 이 일지 날짜 · 주황 MA20 · 보라 MA60 · 세로축 로그</text>']
    for k in range(5):
        p = math.exp(llo + (lhi - llo) * k / 4)
        o.append(f'<line x1="{ml}" y1="{Y(p):.1f}" x2="{W - mr}" y2="{Y(p):.1f}" stroke="#e5e5e5"/>')
        o.append(f'<text x="{ml - 6}" y="{Y(p) + 4:.1f}" font-size="11" text-anchor="end" fill="#666">{_fmt_money(p, currency)}</text>')
    o.append(f'<rect x="{X(first_view_i) - step / 2:.1f}" y="{mt}" width="{X(end_i) - X(first_view_i) + step:.1f}" height="{vbot - mt}" fill="#e8f1ff" opacity="0.75"/>')
    for i, c in enumerate(view):
        if c["date"] == trade8:
            o.append(f'<rect x="{X(i) - step / 2:.1f}" y="{mt}" width="{step:.1f}" height="{vbot - mt}" fill="#fff4b3" opacity="0.9"/>')
    for i, c in enumerate(view):
        col = UP if c["close"] >= c["open"] else DOWN
        o.append(f'<rect x="{X(i) - step * 0.35:.1f}" y="{vbot - (c["vol"] / vmax * vol_h):.1f}" width="{max(step * 0.7, 1):.1f}" height="{(c["vol"] / vmax * vol_h):.1f}" fill="{col}" opacity="0.45"/>')
        top, bot = Y(max(c["open"], c["close"])), Y(min(c["open"], c["close"]))
        o.append(f'<line x1="{X(i):.1f}" y1="{Y(c["high"]):.1f}" x2="{X(i):.1f}" y2="{Y(c["low"]):.1f}" stroke="{col}"/>')
        o.append(f'<rect x="{X(i) - step * 0.3:.1f}" y="{top:.1f}" width="{max(step * 0.6, 1):.1f}" height="{max(bot - top, 1):.1f}" fill="{col}"/>')
    for series, col in ((ma20, "#e08a00"), (ma60, "#8e44ad")):
        pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(series[start:]) if v is not None)
        if pts.count(",") >= 2:
            o.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="1.2"/>')
    # 평단 계단선 (체결 때마다 평단이 바뀜. 청산되면 거기서 끝)
    seg = []
    for f in cycle["fills"]:
        i = max(0, bisect.bisect_right(vds, f["date"].replace("-", "")) - 1)
        seg.append((i, f["avg"], f["qty_after"]))
    for k, (i, av, qa) in enumerate(seg):
        nxt = seg[k + 1][0] if k + 1 < len(seg) else end_i
        if qa <= CYCLE_EPS or nxt < i:
            continue
        x1, x2 = X(i), X(nxt) + (step / 2 if k + 1 == len(seg) else 0)
        o.append(f'<line x1="{x1:.1f}" y1="{Y(av):.1f}" x2="{x2:.1f}" y2="{Y(av):.1f}" stroke="#555" stroke-width="1.4" stroke-dasharray="5 3"/>')
        if k + 1 == len(seg):
            o.append(f'<text x="{x1 + 4:.1f}" y="{Y(av) - 4:.1f}" font-size="10" text-anchor="start" fill="#555">평단 {_fmt_money(av, currency)}</text>')
    for key, label, col in (("stop", "손절", "#d9363e"), ("target", "목표", "#16a34a")):  # 손절·목표선 (매수 일지 메타데이터 stop/target)
        v = cycle.get(key)
        if v:
            o.append(f'<line x1="{X(first_view_i) - step / 2:.1f}" y1="{Y(v):.1f}" x2="{W - mr}" y2="{Y(v):.1f}" stroke="{col}" stroke-width="1.2" stroke-dasharray="2 3"/>')
            o.append(f'<text x="{W - mr - 2}" y="{Y(v) - 3:.1f}" font-size="10" text-anchor="end" fill="{col}">{label} {_fmt_money(v, currency)}</text>')
    # 마커: 매수는 저가 아래 ▲, 매도는 고가 위 ▼
    qmax = max((m["qty"] for m in markers), default=1.0) or 1.0
    for m in markers:
        i = max(0, bisect.bisect_right(vds, m["date"].replace("-", "")) - 1)
        c = view[i]
        sz = 5 + 4 * (m["qty"] / qmax) ** 0.5
        x = X(i)
        if m["side"] == "매수":
            y0 = Y(c["low"]) + 3
            o.append(f'<polygon points="{x:.1f},{y0:.1f} {x - sz:.1f},{y0 + sz * 1.6:.1f} {x + sz:.1f},{y0 + sz * 1.6:.1f}" fill="{BUY}" stroke="#fff" stroke-width="0.8"/>')
            ty = y0 + sz * 1.6 + 11
        else:
            y0 = Y(c["high"]) - 3
            o.append(f'<polygon points="{x:.1f},{y0:.1f} {x - sz:.1f},{y0 - sz * 1.6:.1f} {x + sz:.1f},{y0 - sz * 1.6:.1f}" fill="{SELL}" stroke="#fff" stroke-width="0.8"/>')
            ty = y0 - sz * 1.6 - 4
        if show_num:
            o.append(f'<text x="{x:.1f}" y="{ty:.1f}" font-size="11" font-weight="bold" text-anchor="middle" fill="{BUY if m["side"] == "매수" else SELL}">{m["no"]}</text>')
    last_i = len(view) - 1
    for i in sorted({0, last_i // 3, last_i * 2 // 3, last_i}):
        d = view[i]["date"]
        anchor = "start" if i == 0 else "end" if i == last_i else "middle"
        o.append(f'<text x="{X(i):.1f}" y="{vbot + 16:.1f}" font-size="10" text-anchor="{anchor}" fill="#666">{d[2:4]}-{d[4:6]}-{d[6:]}</text>')
    for k, line in enumerate(legend):
        col_x = ml if k % 2 == 0 else ml + plot_w // 2
        o.append(f'<text x="{col_x}" y="{vbot + 36 + (k // 2) * 15:.1f}" font-size="11" fill="#333">{e(line)}</text>')
    o.append("</svg>")
    return "\n".join(o)


_CANDLE_CACHE = {}


def ensure_chart(repo, date_iso: str, market: str, code: str, name: str, currency: str,
                 buys: pd.DataFrame, sells: pd.DataFrame, final: bool, cycle: dict = None):
    """사이클 차트 파일 생성/갱신. sig = (체결 구성, 일봉 확정 여부, 레이아웃 버전) → 체결이 바뀌거나 마감 후 확정되면 자동 교체."""
    if not cycle:
        logger.warning("%s(%s) %s: 사이클 정보가 없어 차트를 만들지 않습니다.", name, code, date_iso)
        return None
    fname = f"{date_iso}_{safe_filename(code)}.svg"
    path = f"{CHART_DIR}/{fname}"
    sig = hashlib.md5(repr((len(buys), len(sells), bool(final), cycle_sig(cycle), CHART_LAYOUT_VER)).encode()).hexdigest()[:12]
    existing, sha = read_repo_text(repo, path)
    link = f"![{name} 일봉 차트](charts/{fname})"
    if existing and f'data-sig="{sig}"' in existing:
        return link
    # 마지막 체결일 +3주(오늘까지)를 끝으로 일봉을 받아 사이클 전체를 보여준다
    last_d = datetime.strptime(cycle["fills"][-1]["date"], "%Y-%m-%d") + timedelta(days=32)  # 청산 후 20거래일 성과까지 포함
    end8 = max(min(datetime.now(KST).strftime("%Y%m%d"), last_d.strftime("%Y%m%d")), date_iso.replace("-", ""))
    ck = (market, code, end8)
    candles = _CANDLE_CACHE.get(ck)
    if not candles:
        candles = fetch_candles(market, code, end8)
        if candles:
            _CANDLE_CACHE[ck] = candles
    if not candles:
        add_warning(f"{name}({code}) 캔들 조회 실패 → 차트 생략 ({LAST_CANDLE_ERR.get((market, code), '원인 미상')})")
        return link if existing else None
    _CYCLE_STATS[(code, currency, cycle["no"])] = cycle_stats(cycle, candles)
    svg = build_cycle_chart_svg(f"{name} ({code}) · {date_iso}", candles, currency, date_iso.replace("-", ""), sig, cycle)
    if not svg:
        return None
    if existing is None:
        repo.create_file(path, "Auto-chart", svg)
    else:
        repo.update_file(path, "Auto-chart update", svg, sha)
    return link


def _split_sides(df: pd.DataFrame):
    tx = df["tx_type"].astype(str)
    return df[tx.str.contains("매수", na=False)], df[tx.str.contains("매도", na=False)]


def compute_realized(ledger: pd.DataFrame) -> dict:
    return portfolio.compute_realized(ledger)


MANUAL_PREFIX = "M-"  # 수동 입력 체결의 trade_id 접두사 (자동 동기화가 덮어쓰지 않도록 구분)


def is_manual(df: pd.DataFrame) -> pd.Series:
    return df["trade_id"].astype(str).str.startswith(MANUAL_PREFIX)


def _trade_key(r) -> tuple:
    return (r["account"], r["code"], "매수" if "매수" in str(r["tx_type"]) else "매도" if "매도" in str(r["tx_type"]) else str(r["tx_type"]),
            round(float(r["qty"]), 4), round(float(r["unit_price"]), 4))


def manual_covered(keep: pd.DataFrame, incoming: pd.DataFrame) -> list:
    """API 로 같은 체결(계좌·종목·구분·수량·단가, 날짜 ±1일)이 조회된 수동 행의 인덱스 → 자동 기록이 우선이므로 수동 행은 정리 대상."""
    if keep.empty or incoming.empty:
        return []
    man = keep[is_manual(keep)]
    if man.empty:
        return []
    inc = {}
    for _, r in incoming.iterrows():
        inc.setdefault(_trade_key(r), []).append(pd.to_datetime(r["date"], errors="coerce"))
    out = []
    for i, r in man.iterrows():
        d0 = pd.to_datetime(r["date"], errors="coerce")
        if any(pd.notna(d0) and pd.notna(d1) and abs((d1 - d0).days) <= 1 for d1 in inc.get(_trade_key(r), [])):
            out.append(i)
    return out


def dedupe_cross_date(df: pd.DataFrame):
    """같은 체결이 인접한 두 날짜로 중복 기록된 경우 이른 날짜만 남긴다. → (정리된 df, 제거된 행 df)
    같은 계좌·종목·구분·수량·단가이면서 날짜가 정확히 하루 차이인 행을 중복으로 본다.
    주문번호가 둘 다 있으면 같아야 하고, 한쪽이라도 비어 있으면 미국(USD)만 중복으로 인정한다."""
    if df is None or df.empty:
        return df, df.iloc[0:0] if df is not None else df
    d = df.assign(_q=df["qty"].astype(float).round(4), _p=df["unit_price"].astype(float).round(4),
                  _dt=pd.to_datetime(df["date"], errors="coerce"))
    drop = []
    for _, g in d.groupby(["account", "code", "currency", "tx_type", "_q", "_p"], sort=False):
        if len(g) < 2 or g["date"].nunique() < 2:
            continue
        kept = []  # (index, date, order_no) — 중복으로 제거되지 않은 행
        for idx, r in g.sort_values("_dt", kind="stable").iterrows():
            match = None
            for k in kept:
                if k[3] or pd.isna(r["_dt"]) or (r["_dt"] - k[1]).days != 1:
                    continue
                o1, o2 = str(k[2] or ""), str(r["order_no"] or "")
                if (o1 and o2 and o1 == o2) or ((not o1 or not o2) and r["currency"] == "USD"):
                    match = k
                    break
            if match is not None:
                match[3] = True  # 이 행으로 이미 한 건 상쇄됨
                drop.append(idx)
            else:
                kept.append([idx, r["_dt"], r["order_no"], False])
    if not drop:
        return df, df.iloc[0:0]
    return df.drop(index=drop), df.loc[drop]


CYCLE_EPS = 1e-4  # 이 이하의 잔량은 0(전량 청산)으로 봄 (소수점 잔량 오차 흡수)
CHART_LAYOUT_VER = "c5"
CHART_MIN_BARS = 63  # 사이클이 짧아도 차트는 최소 약 3개월(63거래일)을 보여줌 (부족분은 첫 매수 이전 구간으로 채움)


def compute_cycles(ledger: pd.DataFrame) -> dict:
    """종목별 보유 사이클(계좌 합산). 보유 수량이 0이 되면 사이클이 끝나고, 이후 첫 매수가 새 사이클의 시작이 된다.
    반환: {(code, currency): [{no, start, end(진행 중이면 None), fills:[{date, side, qty, price, account, qty_after, avg}], pnl, cost, orphan}]}
    평단은 이동평균법, 같은 날은 매수를 먼저 처리(compute_positions 와 동일), 수수료·세금 제외."""
    out = {}
    if ledger is None or ledger.empty:
        return out
    df = ledger.copy()
    tx = df["tx_type"].astype(str)
    df["is_buy"], df["is_sell"] = tx.str.contains("매수", na=False), tx.str.contains("매도", na=False)
    df = df[df["is_buy"] | df["is_sell"]]
    df = df.assign(_ord=df["is_sell"].astype(int)).sort_values(["date", "_ord"], kind="stable")
    for (code, cur), g in df.groupby(["code", "currency"], sort=False):
        cycles, c, qty, avg = [], None, 0.0, 0.0
        for r in g.itertuples(index=False):
            q, price = float(r.qty), float(r.unit_price)
            if r.is_buy:
                add_down = c is not None and qty > CYCLE_EPS and price < avg * 0.995
                avg_before = avg
                if c is None:
                    c = {"no": len(cycles) + 1, "start": r.date, "end": None, "fills": [], "pnl": 0.0, "cost": 0.0, "orphan": False}
                    cycles.append(c)
                avg = (avg * qty + price * q) / (qty + q) if (qty + q) else price
                qty += q
            else:
                if c is None:  # 원장에 매수 이력이 없는 매도 → 단독 사이클(복기 시 데이터 누락 표시)
                    c = {"no": len(cycles) + 1, "start": r.date, "end": None, "fills": [], "pnl": 0.0, "cost": 0.0, "orphan": True}
                    cycles.append(c)
                    avg, qty = price, q
                used = min(q, qty)
                c["pnl"] += (price - avg) * used
                c["cost"] += avg * used
                qty -= q
            c["fills"].append({"date": r.date, "side": "매수" if r.is_buy else "매도", "qty": q, "price": price, "account": r.account, "name": r.name,
                               "qty_after": max(qty, 0.0), "avg": avg,
                               "add_down": bool(r.is_buy and add_down), "avg_before": avg_before if r.is_buy else avg})
            if r.is_sell and qty <= CYCLE_EPS:
                c["end"], c, qty, avg = r.date, None, 0.0, 0.0
        out[(code, cur)] = cycles
    return out


def realized_r(cycle: dict, stop, date: str = None):
    """실현 R = 실현손익 ÷ 위험금액(Σ 매도수량 × (평단 − 손절가)). date 를 주면 그날 매도분만. 손절가가 없거나 평단 이하면 None."""
    if not stop:
        return None
    risk = pnl = 0.0
    for f in cycle["fills"]:
        if f["side"] != "매도" or (date and f["date"] != date):
            continue
        used = min(f["qty"], f["qty_after"] + f["qty"])
        unit = f["avg"] - stop
        if unit <= 0 or used <= 0:
            continue
        risk += used * unit
        pnl += used * (f["price"] - f["avg"])
    return pnl / risk if risk else None


_CYCLE_STATS = {}


def cycle_stats(cycle: dict, candles: list):
    """보유 중 최대 평가이익/손실(MFE/MAE, 매수 가중평균가 대비), 청산 효율(포착률), 청산 후 5·10·20거래일 수익률."""
    if not candles:
        return None
    buys = [f for f in cycle["fills"] if f["side"] == "매수"]
    if not buys:
        return None
    first8 = cycle["start"].replace("-", "")
    end8 = cycle["end"].replace("-", "") if cycle["end"] else candles[-1]["date"]
    win = [x for x in candles if first8 <= x["date"] <= end8]
    if not win:
        return None
    entry = sum(f["qty"] * f["price"] for f in buys) / sum(f["qty"] for f in buys)
    out = {"entry": entry, "mfe": (max(x["high"] for x in win) / entry - 1) * 100, "mae": min(0.0, (min(x["low"] for x in win) / entry - 1) * 100),
           "real": None, "capture": None, "after": {}}
    if cycle["end"] and cycle["cost"]:
        out["real"] = cycle["pnl"] / cycle["cost"] * 100
        if out["mfe"] > 0:
            out["capture"] = out["real"] / out["mfe"] * 100
        post = [x for x in candles if x["date"] > end8]
        for n in (5, 10, 20):
            if len(post) >= n:
                out["after"][n] = (post[n - 1]["close"] / cycle["fills"][-1]["price"] - 1) * 100
    return out


def pick_cycle(cycles: dict, code: str, currency: str, date_iso: str):
    """해당 일자에 체결이 있는 사이클(여러 개면 마지막). 없으면 None."""
    hits = [c for c in (cycles or {}).get((code, currency), []) if any(f["date"] == date_iso for f in c["fills"])]
    return hits[-1] if hits else None


def cycle_sig(cycle) -> str:
    key = [(f["date"], f["side"], round(f["qty"], 4), round(f["price"], 4)) for f in cycle["fills"]] + [cycle["end"], cycle.get("stop"), cycle.get("target"), CHART_LAYOUT_VER]
    return hashlib.md5(repr(key).encode()).hexdigest()[:12]


def compute_positions(ledger: pd.DataFrame) -> pd.DataFrame:
    pos = portfolio.compute_positions(ledger)
    pos.attrs["cycles"] = compute_cycles(ledger)
    return pos


SMALL_TABLE_ROWS = 3  # 행이 이 개수 이하이면 표 대신 목록으로 표시


def _pos_cells(r, pos, currency):
    """체결 1건의 (잔량, 평단, 실현손익) 문자열. 값이 없으면 '-'."""
    p = pos.loc[r.name] if (pos is not None and r.name in pos.index) else None
    if p is None or pd.isna(p["after_qty"]):
        return "-", "-", "-"
    qty = p["after_qty"]
    left = "0주 (전량 청산)" if qty < 1e-9 else f"{_fmt_qty(qty)}주"
    avg = "-" if qty < 1e-9 else _fmt_money(p["after_avg"], currency)
    pnl = "-"
    if not pd.isna(p["sell_pnl"]):
        pct = p["sell_pnl"] / p["sell_cost"] * 100 if p["sell_cost"] else 0.0
        pnl = f"{_fmt_signed_money(p['sell_pnl'], currency)} ({pct:+.2f}%)"
    return left, avg, pnl


def _before_cell(r, pos):
    """체결 직전 보유 수량(해당 계좌 기준). 값이 없으면 '-'."""
    p = pos.loc[r.name] if (pos is not None and r.name in pos.index) else None
    if p is None or pd.isna(p["after_qty"]):
        return "-"
    is_sell = "매도" in str(r["tx_type"])
    before = p["after_qty"] + float(r["qty"]) if is_sell else p["after_qty"] - float(r["qty"])
    return f"{_fmt_qty(max(before, 0.0))}주"


def _order_cell(r) -> str:
    return str(r.get("order_no") or "").strip() or "-"


def fills_table(df: pd.DataFrame, currency: str, pos: pd.DataFrame = None) -> str:
    if df.empty:
        return "*아직 체결 내역이 없습니다.*"
    show_pos = pos is not None
    if len(df) <= SMALL_TABLE_ROWS:
        lines = []
        for _, r in df.iterrows():
            line = f"- **{r['account']}** · {r['tx_type']} {_fmt_qty(r['qty'])}주 @ {_fmt_money(r['unit_price'], currency)} (합계 {_fmt_money(r['total_amt'], currency)})"
            if show_pos:
                left, avg, pnl = _pos_cells(r, pos, currency)
                before = _before_cell(r, pos)
                line += (f" · 직전 보유 {before}" if before != "-" else "") + f" → 잔량 {left}" + (f" · 평단 {avg}" if avg != "-" else "") + (f" · 실현손익 {pnl}" if pnl != "-" else "")
            if _order_cell(r) != "-":
                line += f" · 주문 {_order_cell(r)}"
            lines.append(line)
        return "\n".join(lines)
    head = "| 계좌 | 구분 | 수량 | 단가 | 금액 |" + (" 직전 보유 | 체결 후 잔량 | 평단 | 실현손익 |" if show_pos else "") + " 주문번호 |"
    rows = [head, "| --- | --- | --- | --- | --- |" + (" --- | --- | --- | --- |" if show_pos else "") + " --- |"]
    for _, r in df.iterrows():
        row = f"| {r['account']} | {r['tx_type']} | {_fmt_qty(r['qty'])} | {_fmt_money(r['unit_price'], currency)} | {_fmt_money(r['total_amt'], currency)} |"
        if show_pos:
            left, avg, pnl = _pos_cells(r, pos, currency)
            row += f" {_before_cell(r, pos)} | {left} | {avg} | {pnl} |"
        row += f" {_order_cell(r)} |"
        rows.append(row)
    return "\n".join(rows)


def summary_table(df: pd.DataFrame, currency: str, pos: pd.DataFrame = None) -> str:
    """요약은 최대 3줄(매수/매도/잔량)이라 항상 목록으로 표시."""
    buy, sell = _split_sides(df)
    lines = []
    for label, d in (("매수", buy), ("매도", sell)):
        if d.empty:
            continue
        q, a = d["qty"].sum(), d["total_amt"].sum()
        lines.append(f"- **{label}:** {_fmt_qty(q)}주 · 평균단가 {_fmt_money(a / q, currency)} · 합계 {_fmt_money(a, currency)}" if q
                     else f"- **{label}:** -")
    if pos is not None:  # 그날 마지막 체결 기준 계좌별 잔량 합계
        left = {}
        for _, r in df.iterrows():
            if r.name in pos.index and not pd.isna(pos.loc[r.name, "after_qty"]):
                left[r["account"]] = pos.loc[r.name, "after_qty"]
        if left:
            tot = sum(left.values())
            detail = ", ".join(f"{a} {_fmt_qty(q)}주" for a, q in left.items()) if len(left) > 1 else ""
            lines.append(f"- **체결 후 잔량:** {'0주 (전량 청산)' if tot < 1e-9 else _fmt_qty(tot) + '주'}" + (f" ({detail})" if detail and tot >= 1e-9 else ""))
    return "\n".join(lines) or "- -"


def pnl_note(pnl, currency: str) -> str:
    if not pnl:
        return ""
    if not pnl["known"]:
        return "\n\n> ℹ️ 원장에 매수 이력이 부족해 실현손익을 계산하지 않았습니다."
    pct = pnl["pnl"] / pnl["cost"] * 100 if pnl["cost"] else 0.0
    return (f"\n\n> 💰 **추정 실현손익:** {_fmt_signed_money(pnl['pnl'], currency)} {currency} ({pct:+.2f}%) "
            f"· 원장 이동평균 기준, 수수료·세금 제외")


def cycle_summary_line(cycle: dict, currency: str) -> str:
    """종목 일지 요약 맨 위 한 줄: 사이클 번호·기간·횟수·평단·잔량·실현손익."""
    fills, f0, fl = cycle["fills"], cycle["fills"][0], cycle["fills"][-1]
    nb, ns = sum(1 for f in fills if f["side"] == "매수"), sum(1 for f in fills if f["side"] == "매도")
    d0 = datetime.strptime(cycle["start"], "%Y-%m-%d")
    if cycle["end"]:
        span = f"{cycle['start'][5:]} → {cycle['end'][5:]} ({(datetime.strptime(cycle['end'], '%Y-%m-%d') - d0).days}일)"
        state = "청산"
    else:
        span = f"{cycle['start'][5:]}부터 (마지막 체결 {fl['date'][5:]}, {(datetime.strptime(fl['date'], '%Y-%m-%d') - d0).days}일)"
        state = "진행 중"
    parts = [f"{span}", f"매수 {nb} / 매도 {ns}"]
    if not cycle["end"]:
        parts.append(f"평단 {_fmt_money(fl['avg'], currency)} · 잔량 {_fmt_qty(fl['qty_after'])}주")
    if ns and cycle["cost"]:
        parts.append(f"실현손익 {_fmt_signed_money(cycle['pnl'], currency)} ({cycle['pnl'] / cycle['cost'] * 100:+.1f}%)")
    if cycle.get("orphan"):
        parts.append("⚠ 매수 이력 없음(원장 누락)")
    return f"- **사이클 #{cycle['no']} ({state}):** " + " · ".join(parts)


_SIGNAL_CACHE = {}


def market_signal(repo, date_iso: str):
    """그날 시황의 신호(frontmatter regime). 시황이 없거나 신호가 없으면 None."""
    if date_iso not in _SIGNAL_CACHE:
        txt, _ = read_repo_text(repo, f"{MARKET_DIR}/{date_iso}.md")
        m = re.search(r"^regime:[ \t]*\"?([^\"\n]+?)\"?[ \t]*$", txt or "", re.M)
        _SIGNAL_CACHE[date_iso] = m.group(1).strip() if m else None
    return _SIGNAL_CACHE[date_iso]


def is_add_down(cycles: dict, r) -> bool:
    """이 체결(행)이 평단 아래 추가매수(물타기)였는지."""
    for cyc in (cycles or {}).get((r["code"], r["currency"]), []):
        for f in cyc["fills"]:
            if (f["side"] == "매수" and f.get("add_down") and f["date"] == r["date"] and f["account"] == r["account"]
                    and abs(f["qty"] - float(r["qty"])) < 1e-6 and abs(f["price"] - float(r["unit_price"])) < 1e-6):
                return True
    return False


def stock_card(df: pd.DataFrame, currency: str, cycle, pnl, signal, levels: dict = None, stats: dict = None) -> str:
    """종목 일지 맨 위 상태 카드: 이 일지의 체결 요약 + 사이클 + (매도) 실현손익 + 당일 시장 신호."""
    buy, sell = _split_sides(df)
    heads = []
    for label, g in (("매수", buy), ("매도", sell)):
        if not g.empty:
            q, amt = float(g["qty"].sum()), float((g["qty"] * g["unit_price"]).sum())
            heads.append(f"**{label} {_fmt_qty(q)}주 @ {_fmt_money(amt / q, currency)}** (합계 {_fmt_money(amt, currency)})")
    kind = "NOTE"
    lines = []
    if cycle:
        lines.append(cycle_summary_line(cycle, currency))
    if not sell.empty and pnl and pnl.get("known") and pnl.get("cost"):
        kind = "TIP" if pnl["pnl"] >= 0 else "CAUTION"
        if not (cycle and cycle["end"] and abs(cycle["pnl"] - pnl["pnl"]) < 0.005):  # 사이클 손익과 같으면 중복이라 생략
            lines.append(f"- **이번 매도 실현손익:** {_fmt_signed_money(pnl['pnl'], currency)} ({pnl['pnl'] / pnl['cost'] * 100:+.1f}%)")
    stop = (levels or {}).get("stop")
    if cycle and (stop or (levels or {}).get("target")):
        avg = cycle["fills"][-1]["avg"] or None
        bits = ([f"손절 {_fmt_money(stop, currency)}" + (f" (평단 대비 {(stop / avg - 1) * 100:+.1f}%)" if avg else "")] if stop else []) + \
               ([f"목표 {_fmt_money(levels['target'], currency)}"] if levels.get("target") else [])
        lines.append("- **손절 · 목표:** " + " · ".join(bits))
    if cycle and not sell.empty:
        r_day = realized_r(cycle, stop, df["date"].iloc[0])
        r_all = realized_r(cycle, stop) if cycle["end"] else None
        if r_day is not None:
            lines.append(f"- **실현 R:** 이번 매도 {r_day:+.2f}R" + (f" · 사이클 전체 {r_all:+.2f}R" if r_all is not None and abs(r_all - r_day) > 0.005 else ""))
    if cycle and not buy.empty:
        for f in cycle["fills"]:
            if f["date"] == df["date"].iloc[0] and f["side"] == "매수" and f.get("add_down"):
                lines.append(f"- ⚠ **물타기:** 평단 {_fmt_money(f['avg_before'], currency)} 아래 {_fmt_money(f['price'], currency)}에서 추가 매수 — 이기는 종목에만 더한다는 원칙과 맞는지 점검")
                break
    if stats:
        if cycle and cycle["end"] and stats.get("real") is not None:
            cap = f" (포착률 {stats['capture']:.0f}%)" if stats.get("capture") is not None else ""
            lines.append(f"- **청산 효율:** 보유 중 최대 평가이익 {stats['mfe']:+.1f}% · 최대 평가손실 {stats['mae']:+.1f}% · 실현 {stats['real']:+.1f}%{cap}")
            after = stats.get("after") or {}
            lines.append("- **청산 후 성과:** " + (" · ".join(f"{n}일 {v:+.1f}%" for n, v in sorted(after.items())) if after else "집계 중(5거래일 이후부터 표시)"))
        else:
            lines.append(f"- **보유 중 최대 평가이익/손실:** {stats['mfe']:+.1f}% / {stats['mae']:+.1f}%")
    if signal:
        lines.append(f"- **당일 시장 신호:** {signal}")
    body = [f"[!{kind}]", " · ".join(heads) or "체결 내역"] + ([""] + lines if lines else [])
    return "\n".join(("> " + l).rstrip() for l in body)


def apply_trades_to_stock_journal(text: str, df: pd.DataFrame, currency: str, chart_md: str = None, pnl=None, pos=None, signal: str = None,
                                  levels: dict = None, stats: dict = None) -> str:
    buy, sell = _split_sides(df)
    side = "매수·매도" if not buy.empty and not sell.empty else "매수" if not buy.empty else "매도"
    accs = "[" + ", ".join(f'"{a}"' for a in dict.fromkeys(df["account"])) + "]"
    text = _set_frontmatter(text, "accounts", accs)
    text = _set_frontmatter(text, "side", side)
    summary = summary_table(df, currency, pos)
    cyc = None
    if pos is not None and not df.empty:
        cyc = pick_cycle(pos.attrs.get("cycles"), df["code"].iloc[0], currency, df["date"].iloc[0])
    if "<!-- CARD_START -->" in text:  # 새 템플릿: 상태 카드가 맨 위에 있으므로 사이클 줄은 카드에만 둔다
        text = _replace_optional(text, CARD_RE, "<!-- CARD_START -->", "<!-- CARD_END -->", stock_card(df, currency, cyc, pnl, signal, levels, stats))
    elif cyc:  # 카드가 없는 기존 일지: 요약 맨 위에 사이클 줄
        summary = cycle_summary_line(cyc, currency) + "\n" + summary
    if side == "매도" and pnl:
        summary += pnl_note(pnl, currency)
        if pnl["known"] and pnl["cost"]:
            text = _set_auto_frontmatter(text, "pnl_amount", f"{pnl['pnl']:.2f}" if currency == "USD" else f"{pnl['pnl']:.0f}")
            text = _set_auto_frontmatter(text, "pnl_pct", f"{pnl['pnl'] / pnl['cost'] * 100:.2f}")
            if cyc and (levels or {}).get("stop"):
                r_val = realized_r(cyc, levels["stop"], df["date"].iloc[0])
                if r_val is not None:
                    text = _set_auto_frontmatter(text, "r_multiple", f"{r_val:.2f}")
    text = _replace_optional(text, TRADES_RE, "<!-- TRADES_START -->", "<!-- TRADES_END -->", fills_table(df, currency, pos)
                            + ("\n\n<sub>잔량·평단·실현손익은 원장 기록 기준(이동평균법, 수수료·세금 제외)이며, 원장에 없는 이전 보유분은 반영되지 않습니다.</sub>" if pos is not None else ""))
    text = _replace_optional(text, SUMMARY_RE, "<!-- SUMMARY_START -->", "<!-- SUMMARY_END -->", summary)
    if chart_md:
        text = _replace_optional(text, CHART_RE, "<!-- CHART_START -->", "<!-- CHART_END -->", chart_md)
    return text


def update_stock_journals(repo, df_day: pd.DataFrame, date_iso: str, market: str, currency: str,
                          pnl_map: dict = None, final: bool = False, pos: pd.DataFrame = None):
    pnl_map = pnl_map or {}
    try:  # 그날 시황의 시장 신호(일지 카드에 표시). 실패해도 일지는 그대로 작성
        signal = market_signal(repo, date_iso)
    except Exception:
        signal = None
    d = df_day.copy()
    d["side_key"] = d["tx_type"].astype(str).apply(lambda x: "매수" if "매수" in x else "매도" if "매도" in x else None)
    errors = []
    for code, gc in d.dropna(subset=["side_key"]).groupby("code", sort=False):
        try:
            name = str(gc["name"].iloc[0])
            buys, sells = gc[gc["side_key"] == "매수"], gc[gc["side_key"] == "매도"]
            cyc = pick_cycle(pos.attrs.get("cycles") if pos is not None else None, code, currency, date_iso)
            levels = {}
            if cyc:
                try:  # 매수 일지 메타데이터의 손절/목표 (실패해도 일지는 작성)
                    levels = levels_for_cycle(repo, cyc, code)
                except Exception as e:
                    logger.warning("손절/목표 읽기 실패 (%s): %s", code, mask(e))
            try:
                chart_md = ensure_chart(repo, date_iso, market, code, name, currency, buys, sells, final,
                                        {**cyc, "stop": levels.get("stop"), "target": levels.get("target")} if cyc else None)
            except Exception as ce:
                logger.warning("차트 생성 실패 (%s): %s", code, ce)
                add_warning(f"{name}({code}) 차트 생성 오류 → 차트 생략")
                chart_md = None
            for side, g in (("매수", buys), ("매도", sells)):
                if g.empty:
                    continue
                path = f"{STOCK_DIR}/{date_iso}_{safe_filename(code)}_{safe_filename(name)}_{side}.md"
                text, sha = read_repo_text(repo, path)
                created = text is None
                if created:
                    text = apply_meta_layout(render_template(load_template(repo, TEMPLATE_KIND[side]),
                                           {"date": date_iso, "market": market, "code": code, "name": name,
                                            "name_yaml": str(name).replace('"', "'"), "currency": currency}))
                new_text = apply_trades_to_stock_journal(text, g, currency, chart_md, pnl_map.get((date_iso, code, currency)), pos, signal,
                                                       levels, _CYCLE_STATS.get((code, currency, cyc["no"])) if cyc else None)
                if created:
                    repo.create_file(path, "Auto-create", new_text)
                elif new_text != text:
                    repo.update_file(path, "Auto-update", new_text, sha)
        except Exception as e:
            logger.exception(e)
            errors.append(str(code))
    if errors:
        raise RuntimeError(f"일지 생성 실패: {errors}")


# ==========================================
# 5. 체결 조회 및 동기화
# ==========================================
def _norm_kr_code(raw) -> str:
    c = str(raw or "UNKNOWN").strip().upper()
    if len(c) == 7 and c.startswith("A"):
        c = c[1:]
    return c.zfill(6)


def _order_body(market: str, acc_no: str, target: str) -> dict:
    if market == "US":
        return {"Input_0": {"orr_dt": target, "act_no": acc_no, "oss_sby_dit_cd": "0", "sot_dit": "0", "ost_cns_dit": "1"}}
    return {"Input_0": {"orr_dt": target, "act_no": acc_no, "orr_mkt_cd": "00", "ost_cns_dit": "1"}}


# 주문번호 후보 필드명 (NH PLUG 응답 명세 확인 전까지의 추정 목록 — 위에서부터 첫 번째 비어있지 않은 값을 사용)
ORDER_NO_KEYS = ("orr_no", "itg_orr_no", "ord_no", "odno", "orgn_orr_no", "orr_num", "ordr_no")
_ORDER_KEY_HINT_SHOWN = [False]


def _pick_order_no(item: dict, market: str) -> str:
    for k in ORDER_NO_KEYS:
        v = str(item.get(k) or "").strip()
        if v:
            return v
    if not _ORDER_KEY_HINT_SHOWN[0]:  # 값은 찍지 않고 필드명만 1회 출력 → 정확한 필드명 확인용
        _ORDER_KEY_HINT_SHOWN[0] = True
        logger.warning("주문번호 필드를 찾지 못함(%s). 응답 필드명: %s", market, ", ".join(sorted(item.keys())))
    return ""


ORR_DT_MISMATCH = []  # (시장, 계좌, 조회일) — 응답 행의 주문일자가 조회일과 다른 체결(인접 날짜 중복의 원인 진단용)
ZERO_QTY_SUSPECTS = []  # (시장, 계좌, 조회일) — 체결가는 있는데 체결수량이 0으로 내려온 행


def _notify_once(key: str, msg: str):
    """같은 사유의 경고를 .cache 마커로 한 번만 보낸다(매시간 실행되므로 반복 방지)."""
    if _read_cache(key):
        return
    add_warning(msg)
    _write_cache(key, {"sent": True})


def _parse_fills(resps: list, market: str, acc_name: str, target: str) -> list:
    rows, seq = [], Counter()
    for r in resps:
        for item in _as_list(r.json().get("Output_0")):
            qty = to_float(item.get("cns_qty" if market == "US" else "tot_cns_qty"))
            if qty == 0:
                price0 = to_float(item.get("cns_pr" if market == "US" else "cns_avg_uit_pr"))
                if price0 > 0 or (market == "KR" and (to_float(item.get("cns_amt")) > 0 or to_float(item.get("cns_cnt")) > 0)):
                    ZERO_QTY_SUSPECTS.append((market, acc_name, target))
                continue
            if market == "US":
                code = str(item.get("tck_iem_cd") or item.get("tck_cd") or item.get("sym_cd") or item.get("iem_cd") or "UNKNOWN").strip().upper()
            else:
                code = _norm_kr_code(item.get("iem_cd"))
            row_dt = re.sub(r"\D", "", str(item.get("orr_dt") or ""))[:8]
            if len(row_dt) == 8 and row_dt != target:
                ORR_DT_MISMATCH.append((market, acc_name, target))
            tx_type = str(item.get("sby_dit_nm" if market == "US" else "sby_dit_cd_nm") or "체결").strip()
            name = str(item.get("iem_nm") or "종목명없음").strip()
            price = to_float(item.get("cns_pr" if market == "US" else "cns_avg_uit_pr"))
            total = qty * price if market == "US" else (to_float(item.get("cns_amt")) or qty * price)
            base = f"{acc_name}|{target}|{code}|{tx_type}|{qty:.4f}|{price:.4f}"
            seq[base] += 1
            tid_src = base if seq[base] == 1 else f"{base}|{seq[base]}"
            rows.append({
                "date": iso_date(target), "time": "00:00:00", "tx_type": tx_type, "code": code, "name": name,
                "qty": qty, "unit_price": price, "total_amt": total, "fee": 0.0, "tax": 0.0,
                "currency": "USD" if market == "US" else "KRW",
                "trade_id": hashlib.md5(tid_src.encode()).hexdigest(), "account": acc_name,
                "order_no": _pick_order_no(item, market)
            })
    return rows


def _err_detail(res) -> str:
    """NH 오류 응답에서 rsp_cd / rsp_msg 를 뽑아 짧게 요약 (계좌번호 등은 마스킹)."""
    try:
        js = res.json()
        txt = f"{js.get('rsp_cd', '')} {js.get('rsp_msg', '')}".strip() or (res.text or "")[:200]
    except Exception:
        txt = (res.text or "")[:200]
    return mask(f"HTTP {res.status_code} / {txt}")


def fetch_trades_for_market(market: str, target_date: str, errors: list = None):
    """계좌별 체결 스냅샷 {계좌: DataFrame} 과 통화를 반환. 체결이 없는 계좌는 포함하지 않는다.
    errors 리스트를 넘기면 필수 계좌의 조회 실패를 (계좌, 시장, 일자, 사유)로 모으고 개별 경고는 보내지 않는다."""

    def report(acc, detail, generic):
        if errors is not None:
            errors.append((acc, market, target_date, detail))
        else:
            add_warning(f"{acc} {market} {generic}")

    path = US_PATH if market == "US" else KR_PATH
    frames, queried, seen_sigs = {}, set(), {}
    required_ok = True  # 필수 계좌 조회가 모두 성공해야 선택 계좌(ISA)의 중복 응답 여부를 검증할 수 있음
    ordered = sorted(ACCOUNTS.items(), key=lambda kv: kv[0].upper() in OPTIONAL_ACCOUNTS)
    for acc_name, acc_no in ordered:
        if acc_no in queried or (market == "US" and acc_name.upper() in NO_US_ACCOUNTS):
            continue
        queried.add(acc_no)
        optional = acc_name.upper() in OPTIONAL_ACCOUNTS
        if optional and not required_ok:
            logger.warning("필수 계좌 조회 실패로 %s/%s 응답을 검증할 수 없어 건너뜀", acc_name, market)
            continue
        try:
            resps = post_pages(path, _order_body(market, acc_no, target_date))
        except Exception as e:
            logger.warning("체결 조회 실패 (%s/%s): %s", acc_name, market, e)
            if not optional:
                required_ok = False
                report(acc_name, mask(e), f"조회 실패 ({type(e).__name__})")
            continue
        if not resps or any(r.status_code != 200 for r in resps):
            bad = next((r for r in (resps or []) if r.status_code != 200), None)
            detail = _err_detail(bad) if bad is not None else "응답 없음"
            logger.warning("체결 조회 실패 (%s/%s, 조회일 %s) → 스킵: %s", acc_name, market, target_date, detail)
            if not optional:
                required_ok = False
                report(acc_name, detail, "페이지 오류")
            continue
        try:
            rows = _parse_fills(resps, market, acc_name, target_date)
        except Exception as e:  # JSON 아님 등 응답 형식 오류
            logger.warning("체결 응답 파싱 실패 (%s/%s): %s", acc_name, market, e)
            if not optional:
                required_ok = False
                report(acc_name, mask(e), f"응답 파싱 실패 ({type(e).__name__})")
            continue
        n_mis = sum(1 for k in ORR_DT_MISMATCH if k == (market, acc_name, target_date))
        if n_mis:
            ORR_DT_MISMATCH[:] = [k for k in ORR_DT_MISMATCH if k != (market, acc_name, target_date)]
            logger.warning("%s %s %s: 응답 주문일자(orr_dt)가 조회일과 다른 체결 %d건 (인접 날짜 중복 가능 → 중복 정리 로직 적용)", acc_name, market, target_date, n_mis)
        n_susp = sum(1 for k in ZERO_QTY_SUSPECTS if k == (market, acc_name, target_date))
        if n_susp:
            ZERO_QTY_SUSPECTS[:] = [k for k in ZERO_QTY_SUSPECTS if k != (market, acc_name, target_date)]
            _notify_once(f"zeroqty_{market}_{acc_name}_{target_date}_{n_susp}",
                         f"{acc_name} {market} {target_date}: 체결가는 있으나 체결수량이 0으로 조회된 주문 {n_susp}건 "
                         "(소수점 거래 등) → 자동 기록에서 제외됨. MTS 체결내역을 확인해 직접 기록해 주세요.")
        if not rows:
            continue
        sig = frozenset((x["code"], x["tx_type"], round(x["qty"], 4), round(x["unit_price"], 4)) for x in rows)
        if optional and sig in seen_sigs.values():
            logger.warning("%s/%s 응답이 다른 계좌와 동일 → 중복 응답으로 간주하고 제외", acc_name, market)
            continue
        seen_sigs[acc_name] = sig
        frames[acc_name] = rows
    snapshots = {a: pd.DataFrame(rows, columns=LEDGER_COLUMNS) for a, rows in frames.items()}
    return snapshots, ("USD" if market == "US" else "KRW")


# ---- 보유 종목 (잔고조회) ----
BALANCE_PATH = {"KR": "/krstock/inquiry/v1/balance", "US": "/gbstock/inquiry/v1/balance"}


def _balance_body(market: str, acc_no: str) -> dict:
    """국내: 현재가기준·상장종목·총자산·통합시세·정규장 / 해외: 정규장·미국·USD·비용 미포함 (openapi.json 기준)."""
    if market == "KR":
        return {"Input_0": {"act_no": acc_no, "bnc_bse_cd": "5", "ltg_aot_dit_cd": "1", "aet_bse": "2",
                            "qut_dit_cd": "UNT", "aly_qut_cd": "1"}}
    return {"Input_0": {"act_no": acc_no, "qut_iqr_dit_cd": "1", "fc_sec_trd_nat_cd": "200", "cur_cd": "USD", "xns_dit_cd": "0"}}


HOLDINGS_ZERO = []  # 평가금액·손익이 모두 0이라 제외한 잔량 (표시용 종목명)


def _parse_holdings(resps: list, market: str, acc_name: str) -> list:
    out = []
    for r in resps:
        for it in _as_list(r.json().get("Output_1")):
            if market == "KR":
                code, cur = _norm_kr_code(it.get("iem_cd")), "KRW"
                qty, value, pnl = to_float(it.get("itg_bnc_qty")), to_float(it.get("eal_amt")), to_float(it.get("eal_pls_amt"))
            else:
                code, cur = str(it.get("iem_cd") or "").strip().upper(), str(it.get("cur_cd") or "USD").strip() or "USD"
                qty, value, pnl = to_float(it.get("cns_bse_bnc_qty")), to_float(it.get("fc_eal_amt")), to_float(it.get("fc_eal_pls_amt"))
            if qty <= 0 or not code:
                continue
            if abs(value) < 0.005 and abs(pnl) < 0.005:
                # 전량 매도 후에도 소수점 잔량 행이 남고 금액이 0으로 내려오는 경우 → 보유로 보지 않음
                HOLDINGS_ZERO.append(str(it.get("iem_nm") or code).strip())
                continue
            out.append({"market": market, "code": code, "name": str(it.get("iem_nm") or code).strip(), "currency": cur,
                        "qty": qty, "value": value, "pnl": pnl, "cost": value - pnl, "account": acc_name})
    return out


HOLDINGS_OK = set()  # 잔고 조회에 성공한 (계좌, 시장) — 원장 대조 대상 범위


def reconcile_holdings(ledger: pd.DataFrame, rows: list, ok_pairs, tol: float = None, ignore=None) -> list:
    """계좌·종목별로 (잔고 수량 − 원장 잔량)이 허용오차를 넘는 항목. 잔고 조회에 성공한 (계좌, 시장)만 비교한다.
    반환: [{account, code, name, currency, ledger, balance}]"""
    tol = LIMITS["reconcile_tol"] if tol is None else tol
    ignore = {x.upper() for x in (LIMITS["reconcile_ignore"] if ignore is None else ignore)}
    led = {}
    if ledger is not None and not ledger.empty:
        tx = ledger["tx_type"].astype(str)
        sign = tx.str.contains("매수", na=False).astype(float) - tx.str.contains("매도", na=False).astype(float)
        for (acc, code, cur), q in (ledger["qty"].astype(float) * sign).groupby([ledger["account"], ledger["code"], ledger["currency"]]).sum().items():
            led[(acc, code, cur)] = q
    bal, names = {}, {}
    for r in rows:
        k = (r["account"], r["code"], r["currency"])
        bal[k] = bal.get(k, 0.0) + float(r["qty"])
        names[k] = r["name"]
    if ledger is not None and not ledger.empty:
        for _, r in ledger.drop_duplicates("code").iterrows():
            names.setdefault((r["account"], r["code"], r["currency"]), r["name"])
    out = []
    for k in sorted(set(led) | set(bal)):
        acc, code, cur = k
        if code.upper() in ignore or (acc, "US" if cur == "USD" else "KR") not in ok_pairs:
            continue
        b, l = bal.get(k, 0.0), max(led.get(k, 0.0), 0.0) if abs(led.get(k, 0.0)) > 1e-9 else 0.0
        if abs(b - l) > tol:
            nm = names.get(k) or next((v for kk, v in names.items() if kk[1] == code), code)
            out.append({"account": acc, "code": code, "name": nm, "currency": cur, "ledger": l, "balance": b})
    return out


def recon_lines(recon: list, limit: int = 6) -> list:
    lines = [f"> ⚠️ 원장 대조: 잔고와 원장 잔량이 다른 종목 {len(recon)}건 — API가 조회하지 못한 체결(소수점 등)이 있을 수 있습니다. `manual` 모드로 입력하거나 `reconcile_ignore`에 코드를 추가하세요."]
    for r in recon[:limit]:
        lines.append(f"> - {r['account']} {r['name']}({r['code']}): 원장 {_fmt_qty(r['ledger'])}주 / 잔고 {_fmt_qty(r['balance'])}주")
    if len(recon) > limit:
        lines.append(f"> - … 외 {len(recon) - limit}건")
    return lines


def fetch_holdings():
    """(보유 행 리스트, 오류 리스트) 반환. 필수 계좌 실패는 오류로, 선택 계좌(ISA 등)는 조용히 제외."""
    rows, errors = [], []
    HOLDINGS_OK.clear()
    seen = []
    required_ok = True
    HOLDINGS_ZERO.clear()
    for acc_name, acc_no in sorted(ACCOUNTS.items(), key=lambda kv: kv[0].upper() in OPTIONAL_ACCOUNTS):
        optional = acc_name.upper() in OPTIONAL_ACCOUNTS
        if optional and not required_ok:
            continue
        for market in ("KR", "US"):
            if market == "US" and acc_name.upper() in NO_US_ACCOUNTS:
                continue
            try:
                resps = post_pages(BALANCE_PATH[market], _balance_body(market, acc_no), max_pages=5)
                bad = next((r for r in resps if r.status_code != 200), None)
                if bad is not None:
                    raise RuntimeError(_err_detail(bad))
                part = _parse_holdings(resps, market, acc_name)
            except Exception as e:
                logger.warning("잔고 조회 실패 (%s/%s): %s", acc_name, market, mask(e))
                if not optional:
                    required_ok = False
                    errors.append(f"{acc_name}/{market}: {mask(e)}")
                continue
            sig = frozenset((r["market"], r["code"], round(r["qty"], 4), round(r["value"], 2)) for r in part)
            if optional and part and sig in seen:  # 선택 계좌가 다른 계좌와 동일 응답이면 중복으로 제외
                logger.warning("%s/%s 잔고가 다른 계좌와 동일 → 중복 응답으로 제외", acc_name, market)
                continue
            if part:
                seen.append(sig)
            HOLDINGS_OK.add((acc_name, market))
            rows.extend(part)
    return rows, errors


def _money_u(v: float, cur: str, signed: bool = False) -> str:
    sign = ("-" if v < 0 else "+" if signed else "")
    a = abs(v)
    return f"{sign}${a:,.2f}" if cur == "USD" else f"{sign}{a:,.0f}원"


def _yf_symbols(market: str, code: str) -> list:
    return [code.replace(".", "-")] if market == "US" else [f"{code}.KS", f"{code}.KQ"]


_BENCH_CACHE = {}


def _holding_closes(market, symbol, session_name, now, daily):
    if daily is not None:
        cached = daily.get(market, symbol)
        if cached is not None:
            logger.info("일지 확정 일봉 재사용")
            return cached
    dates, closes, volumes, off = fetch_yf_history(symbol, "1y", quiet=True)
    time.sleep(0.2)
    dates, closes, volumes, _ = _drop_open_bar(session_name, dates, closes, volumes, off, now)
    if daily is not None:
        daily.remember(market, symbol, dates, closes, volumes)
    return dates, closes, volumes


def _bench_series(name: str, now: datetime, daily=None):
    """Return the same date-aligned journal benchmark for all holdings in this pass."""
    if name not in _BENCH_CACHE:
        market = "US" if name in US_INDEX_NAMES else "KR"
        dates, closes, _ = _holding_closes(market, YF_SYMBOLS[name], name, now, daily)
        _BENCH_CACHE[name] = (dates, closes) if len(closes) >= 30 else None
    return _BENCH_CACHE[name]


def calc_rs(sdates, scloses, bench, n: int):
    """최근 n거래일 종목 수익률 − 벤치마크 수익률 (%p). 거래일이 달라도 날짜 기준(해당일 이전 마지막 종가)으로 맞춘다."""
    if not bench or len(scloses) < n + 1:
        return None
    bd, bc = bench
    d1, d0 = sdates[-1], sdates[-1 - n]
    if bd[0] > d0:
        return None
    b1, b0 = bc[bisect.bisect_right(bd, d1) - 1], bc[bisect.bisect_right(bd, d0) - 1]
    if not b0:
        return None
    return ((scloses[-1] / scloses[-1 - n]) - (b1 / b0)) * 100


def rs_icon(rs):
    """[단기, 장기] → 아이콘. 둘 중 하나라도 없으면 빈 문자열."""
    if not rs or rs[0] is None or rs[1] is None:
        return ""
    return {(True, True): "🟢", (True, False): "🟡", (False, True): "🟠", (False, False): "🔴"}[(rs[0] >= 0, rs[1] >= 0)]


def fetch_stock_tech(market: str, code: str, now: datetime, since: str = None, daily=None):
    """보유 종목의 추세 상태/분산일/RS. 야후에서 일봉을 받지 못하면 None."""
    used = None
    for sym in _yf_symbols(market, code):
        dates, closes, vols = _holding_closes(market, sym, "KOSPI" if market == "KR" else "S&P 500", now, daily)
        if len(closes) >= 30:
            used = sym
            break
    else:
        return None
    rs, bench_name = None, None
    if LIMITS["holdings_rs"]:
        if market == "US":
            bench_name = LIMITS["rs_benchmark_us"]
        else:
            bench_name = (("KOSDAQ" if used.endswith(".KQ") else "KOSPI") if LIMITS["rs_benchmark_kr"] == "auto" else LIMITS["rs_benchmark_kr"])
        bench = _bench_series(bench_name, now, daily)
        rs = [calc_rs(dates, closes, bench, n) for n in LIMITS["rs_periods"]]
    status, arr, vs50 = trend_status(closes)
    sigma = calc_sigma(closes)
    cv = [(c, v) for c, v in zip(closes, vols) if c is not None and v is not None]
    drop = (LIMITS["stock_dist_k"] * sigma if sigma else None) if LIMITS["stock_dist_mode"] == "sigma" else LIMITS["stock_dist_fixed_pct"]
    dist = None
    if drop and len(cv) >= LIMITS["dist_lookback"] + 1:
        dist = calc_dist_days([c for c, _ in cv], [v for _, v in cv], drop_pct=drop)
    hi52 = max(closes[-252:])
    return {"status": status, "arr": arr, "vs50": vs50, "dist": dist, "sigma": sigma, "drop": drop, "rs": rs, "rs_bench": bench_name,
            "off_high": (closes[-1] / hi52 - 1) * 100 if hi52 else None,
            "trail": trail_info(closes, dates, since.replace("-", "") if since else None),
            "as_of": dates[-1], "close": closes[-1], "source": "Yahoo daily"}


def holdings_context(repo, now: datetime, with_links: bool = True, with_recon: bool = True, daily=None) -> dict:
    """보유 종목 + 원장/사이클 + 손절·목표 + 추세지표를 한 번에 모은다(시황 작성과 위험 알림이 같이 사용).
    반환: {rows, errs, ledger, levels, tech, links, recon}"""
    rows, errs = fetch_holdings()
    ctx = {"rows": rows, "errs": errs, "ledger": None, "levels": {}, "tech": {}, "links": {}, "recon": []}
    since, cycles = {}, None
    try:
        ledger, _ = load_ledger(repo)
        ctx["ledger"] = ledger
        if rows and not ledger.empty:
            cycles = compute_cycles(ledger)
            for r in rows:
                cl = cycles.get((r["code"], r["currency"])) or []
                if cl:
                    since[(r["market"], r["code"])] = next((c for c in reversed(cl) if c["end"] is None), cl[-1])["start"]
    except Exception as e:
        logger.warning("원장 로드 실패: %s", mask(e))
    ledger = ctx["ledger"]
    if rows and ledger is not None and not ledger.empty:
        # A fill sync or manual edit may have changed a journal since the morning view.
        _FILES_CACHE.pop("files", None)
        steps = [("levels", lambda: holdings_levels(repo, rows, ledger, cycles=cycles), True),
                 ("links", lambda: holdings_journal_links(repo, rows, ledger, cycles=cycles), with_links),
                 ("recon", lambda: reconcile_holdings(ledger, rows, HOLDINGS_OK), with_recon and LIMITS["reconcile_enabled"] and not errs)]
        for name, fn, enabled in steps:  # 단계별로 격리: 하나가 실패해도 나머지는 계속
            if enabled:
                try:
                    ctx[name] = fn()
                except Exception as e:
                    logger.warning("보유 종목 %s 생성 실패: %s", name, mask(e))
    ctx["tech"] = enrich_holdings(rows, now, since, daily=daily) if rows else {}
    return ctx


def enrich_holdings(rows: list, now: datetime, since: dict = None, daily=None) -> dict:
    """{(시장, 코드): 추세/분산일/트레일링 정보}. since: {(시장, 코드): 진입일} (샹들리에 기준). holdings_tech=false 면 생략."""
    if not LIMITS["holdings_tech"]:
        return {}
    out = {}
    _BENCH_CACHE.clear()
    for key in dict.fromkeys((r["market"], r["code"]) for r in rows):
        try:
            out[key] = fetch_stock_tech(key[0], key[1], now, (since or {}).get(key), daily=daily)
        except Exception as e:
            logger.warning("종목 지표 계산 실패 (%s): %s", key[1], mask(e))
            out[key] = None
    return out


# ---- 손절가/목표가: 매수 일지 메타데이터(stop/target)에서 읽는다. 종목별(보유 사이클별)로 추적, 사이클 안에서는 가장 최근 일지 값이 우선 ----
_LEVEL_KEYS = ("stop", "target")
_FILES_CACHE = {}


def _num_or_none(raw: str):
    m = re.search(r"\d[\d,]*\.?\d*", raw or "")
    if not m:
        return None
    try:
        v = float(m.group().replace(",", ""))
    except ValueError:
        return None
    return v if v > 0 else None


def parse_levels(text: str) -> tuple:
    """일지 텍스트의 frontmatter(펼친/접힌 모두)에서 (stop, target). 비었거나 숫자가 아니면 None."""
    out = []
    for k in _LEVEL_KEYS:
        m = re.search(rf"^{k}:[ \t]*([^\n#]*)", text or "", re.M)
        out.append(_num_or_none(m.group(1)) if m else None)
    return tuple(out)


def _stock_files(repo) -> list:
    """종목 일지 폴더의 (이름, 경로, sha). 한 번 실행 동안 캐시."""
    if "files" not in _FILES_CACHE:
        _FILES_CACHE["files"] = [(it.name, it.path, getattr(it, "sha", None)) for it in repo.get_contents(STOCK_DIR)
                                 if getattr(it, "type", "file") == "file" and it.name.endswith(".md")]
    return _FILES_CACHE["files"]


def _read_levels(repo, path: str, sha) -> tuple:
    """파일 sha 가 같으면 디스크 캐시(.cache)의 값을 쓰고, 바뀐 파일만 다시 읽는다."""
    cache = _read_cache("levels_v1") or {}
    hit = cache.get(path)
    if hit and sha and hit.get("sha") == sha:
        return hit.get("stop"), hit.get("target")
    text, real_sha = read_repo_text(repo, path)
    stop, target = parse_levels(text or "")
    cache[path] = {"sha": sha or real_sha, "stop": stop, "target": target}
    _write_cache("levels_v1", cache)
    return stop, target


def levels_for_cycle(repo, cycle: dict, code: str) -> dict:
    """사이클의 매수 일지들을 날짜순으로 읽어 가장 최근에 적힌 손절가/목표가를 반환 {stop, target, src}."""
    out = {"stop": None, "target": None, "src": None}
    files = _stock_files(repo)
    for d in sorted({f["date"] for f in cycle["fills"] if f["side"] == "매수"}):
        pre = f"{d}_{safe_filename(code)}_"
        hit = next(((n, p, sh) for n, p, sh in files if n.startswith(pre) and n.endswith("_매수.md")), None)
        if not hit:
            continue
        stop, target = _read_levels(repo, hit[1], hit[2])
        if stop is not None:
            out["stop"], out["src"] = stop, d
        if target is not None:
            out["target"] = target
    return out


def holdings_levels(repo, rows: list, ledger: pd.DataFrame, *, cycles=None) -> dict:
    """{(코드, 통화): {stop, target, src}} — 보유 종목의 진행 중 사이클 기준."""
    if ledger is None or ledger.empty:
        return {}
    if cycles is None:
        cycles = compute_cycles(ledger)
    out = {}
    for key in dict.fromkeys((r["code"], r["currency"]) for r in rows):
        cl = cycles.get(key) or []
        if cl:
            cyc = next((c for c in reversed(cl) if c["end"] is None), cl[-1])
            out[key] = levels_for_cycle(repo, cyc, key[0])
    return out


def holding_risk(item: dict, level: dict = None) -> dict:
    return portfolio.holding_risk(item, level, limits=LIMITS)


def portfolio_heat(items: list, risks: dict, fx: float = None) -> dict:
    """현재가→손절가까지 손실 합계(원화)와 총자산 대비 %. 달러 자산은 환율 필요(없으면 계산 불가)."""
    def krw(v, cur):
        return v * (fx if cur == "USD" else 1.0) if (cur != "USD" or fx) else None
    risk_sum, equity_sum = 0.0, 0.0
    for i in items:
        rk, val = krw(risks[id(i)]["open_risk"] or 0.0, i["currency"]), krw(i["value"], i["currency"])
        if rk is None or val is None:
            return None
        risk_sum += rk
        equity_sum += val
    equity = float(LIMITS["equity_krw"]) or equity_sum
    return {"risk": risk_sum, "equity": equity, "pct": risk_sum / equity * 100 if equity else None}


def _ema_series(vals: list, span: int) -> list:
    k, e, out = 2 / (span + 1), vals[0], []
    for x in vals:
        e = x * k + e * (1 - k)
        out.append(e)
    return out


TRAIL_LABEL = {"ema21": "21EMA", "ma50": "50일선", "chandelier": "샹들리에"}


def trail_info(closes: list, dates: list = None, since8: str = None):
    """트레일링 기준선 대비 상태. 기준선 아래 연속 종가 수가 trail_confirm 이상이면 이탈. 샹들리에는 현재 종가 하나로 판정."""
    rule = LIMITS["trail_line"]
    if rule == "off" or len(closes) < 30:
        return None
    n = LIMITS["trail_confirm"]
    if rule == "chandelier":
        idx0 = next((i for i, d in enumerate(dates or []) if since8 and d.replace("-", "") >= since8), max(0, len(closes) - 22))
        atr = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - 14, len(closes))) / 14
        line = max(closes[idx0:]) - LIMITS["chandelier_atr"] * atr
        below = 1 if closes[-1] < line else 0
        return {"rule": rule, "line": line, "below": below, "broken": below >= 1}
    series = _ema_series(closes, 21) if rule == "ema21" else [None] * 49 + [sum(closes[i - 49:i + 1]) / 50 for i in range(49, len(closes))]
    below = 0
    for i in range(len(closes) - 1, -1, -1):
        if series[i] is not None and closes[i] < series[i]:
            below += 1
        else:
            break
    return {"rule": rule, "line": series[-1], "below": below, "broken": below >= n}


def holding_flags(item: dict, info: dict, risk: dict) -> list:
    """점검 사유 [(종류, 문구)]. 종류 breach·near·trail 은 알림 대상, 나머지는 시황 카드에만 표시."""
    out = []
    if risk["status"] == "breach":
        out.append(("breach", f"손절가 이탈({_fmt_money(risk['stop'], item['currency'])}, {risk['dist']:+.1f}%)"))
    elif risk["status"] == "near":
        out.append(("near", f"손절 임박(+{risk['dist']:.1f}%)"))
    if info:
        tr = info.get("trail")
        if tr and tr["broken"]:
            out.append(("trail", f"트레일링 이탈({TRAIL_LABEL[tr['rule']]} 아래" + (f" {tr['below']}일" if tr["rule"] != "chandelier" else "") + ")"))
        if str(info["vs50"]).endswith("(아래)") and not (tr and tr["rule"] == "ma50" and tr["broken"]):
            out.append(("ma50", "50일선 이탈"))
        if info.get("dist") is not None and info["dist"] >= LIMITS["dist_warn"]:
            out.append(("dist", f"분산일 {info['dist']}개"))
        rs = info.get("rs")
        if rs and None not in rs and rs[0] < 0 and rs[1] < 0:
            out.append(("rs", "상대강도 약세"))
    return out


def _sm(x: str) -> str:
    return f"<small>{x}</small>"


def _short(name: str, n: int = 10) -> str:
    return name if len(name) <= n else name[:n - 1] + "…"


def _rs_label() -> str:
    return "/".join(f"{round(n / 21)}M" for n in LIMITS["rs_periods"])


def _tech_parts(info):
    """(추세 문자열, 분산일, RS) 압축 표기. 계산 불가면 '-'. 예: ('🟢 상승 +4.5% · 고점 -8%', '1🟢', '+8.2/+15.1 🟢')"""
    if not info:
        return "-", "-", "-"
    d = info["dist"]
    dist = "-" if d is None else f"{d}{'🔴' if d >= LIMITS['dist_danger'] else '🟡' if d >= LIMITS['dist_warn'] else '🟢'}"
    rs = info.get("rs")
    rs_s = "-" if not rs or all(x is None for x in rs) else " ".join(x for x in ("/".join("-" if v is None else f"{v:+.1f}" for v in rs), rs_icon(rs)) if x)
    oh = info.get("off_high")
    tr = f"{info['status']} {info['vs50'].split(' ')[0]}" + (f" · 고점 {oh:+.0f}%" if oh is not None else "")
    t = info.get("trail")
    if t and t["broken"]:
        tr += f" · ⛔{TRAIL_LABEL[t['rule']]} 이탈"
    elif t and t["below"]:
        tr += f" · ⚠{TRAIL_LABEL[t['rule']]} 아래 {t['below']}일"
    return tr, dist, rs_s


def holdings_journal_links(repo, rows: list, ledger: pd.DataFrame = None, *, cycles=None) -> dict:
    """{(코드, 통화): [(구분, 날짜, 상대경로)]} — 보유 종목의 진행 중(없으면 마지막) 사이클 체결일 일지 중 실제 존재하는 파일."""
    if ledger is None:
        ledger, _ = load_ledger(repo)
    if ledger.empty:
        return {}
    if cycles is None:
        _FILES_CACHE.pop("files", None)
        cycles = compute_cycles(ledger)
    files = [name for name, _, _ in _stock_files(repo)]
    out = {}
    for key in dict.fromkeys((r["code"], r["currency"]) for r in rows):
        cyc_list = cycles.get(key) or []
        if not cyc_list:
            continue
        cyc = next((c for c in reversed(cyc_list) if c["end"] is None), cyc_list[-1])
        links = []
        for d, side in dict.fromkeys((f["date"], f["side"]) for f in cyc["fills"]):
            pre = f"{d}_{safe_filename(key[0])}_"
            fn = next((f for f in files if f.startswith(pre) and f.endswith(f"_{side}.md")), None)
            if fn:
                links.append((side, d, quote(posixpath.relpath(f"{STOCK_DIR}/{fn}", MARKET_DIR))))
        if links:
            out[key] = links
    return out


def _journal_cell(item: dict, links: dict, limit: int = 4) -> str:
    items = (links or {}).get((item["code"], item["currency"]))
    if not items:
        return "-"
    txt = " ".join(f"[{side} {d[5:].replace('-', '/')}]({url})" for side, d, url in items[-limit:])
    return ("… " if len(items) > limit else "") + txt


def aggregate_holdings(rows: list) -> list:
    """계좌별 행을 종목(통화·시장·코드) 단위로 합산. 원화 먼저, 평가금액 큰 순."""
    agg = {}
    for r in rows:
        a = agg.setdefault((r["currency"], r["market"], r["code"]), {**r, "qty": 0.0, "value": 0.0, "pnl": 0.0, "cost": 0.0})
        for k in ("qty", "value", "pnl", "cost"):
            a[k] += r[k]
    return sorted(agg.values(), key=lambda x: (x["currency"] != "KRW", -x["value"]))


def _alert_once(key: str, text: str) -> bool:
    """같은 알림은 3일간 한 번만 텔레그램으로 보낸다(실행 경고가 아닌 일반 알림). 발송했으면 True."""
    sent = _read_cache("alerts_v1") or {}
    today = datetime.now(KST).strftime("%Y-%m-%d")
    if key in sent:
        return False
    ok = send_telegram_message(text)
    if ok:
        keep = {k: d for k, d in sent.items() if d >= (datetime.now(KST) - timedelta(days=3)).strftime("%Y-%m-%d")}
        keep[key] = today
        _write_cache("alerts_v1", keep)
    return bool(ok)


RISK_TITLE = {"breach": "🛑 손절가 이탈", "near": "⚠️ 손절 임박", "trail": "⛔ 트레일링 이탈"}


def risk_check(repo, now: datetime, context=None, daily=None):
    """보유 종목의 손절 임박·이탈, 트레일링 이탈, 히트 초과를 텔레그램으로 알린다(같은 상태는 한 번). 실시간이 아니라 매시 실행 기준이며,
    실제 손절은 증권사 조건(스탑) 주문으로 거는 것을 전제로 한 보조 알림이다."""
    if not LIMITS["risk_alerts"] or not (NH_APP_KEY and NH_APP_SECRET and ACCOUNTS):
        return
    hc = context if context is not None else holdings_context(repo, now, with_links=False, with_recon=False, daily=daily)
    if hc["errs"] or not hc["rows"]:
        return
    items = aggregate_holdings(hc["rows"])
    risks = {id(i): holding_risk(i, hc["levels"].get((i["code"], i["currency"]))) for i in items}
    day = now.strftime("%Y%m%d")
    for i in items:
        rk, info = risks[id(i)], hc["tech"].get((i["market"], i["code"]))
        for kind, text in holding_flags(i, info, rk):
            if kind not in RISK_TITLE:
                continue
            cur = i["currency"]
            body = (f"{RISK_TITLE[kind]} <b>{html.escape(i['name'])}</b> ({i['code']})\n{html.escape(text)}\n"
                    f"현재 {_fmt_money(rk['price'], cur)} · 평단 {_fmt_money(rk['avg'], cur)} ({(rk['price'] / rk['avg'] - 1) * 100:+.1f}%)"
                    + (f" · {rk['R']:+.1f}R" if rk["R"] is not None else ""))
            _alert_once(f"risk_{kind}_{i['code']}_{day}_{round(rk['stop'] or 0, 2)}", body)
    fx = None
    if any(i["currency"] == "USD" for i in items):  # 달러 자산이 있으면 히트 환산용 환율 조회(야후 KRW=X)
        _, fxc, _, _ = fetch_yf_history("KRW=X", "5d", quiet=True)
        fx = fxc[-1] if fxc else None
    heat = portfolio_heat(items, risks, fx)
    if heat and heat["pct"] is not None and heat["pct"] > LIMITS["max_heat_pct"]:
        _alert_once(f"heat_{day}", f"🔥 <b>포트폴리오 히트 {heat['pct']:.1f}%</b> — 한도 {LIMITS['max_heat_pct']:g}% 초과\n전 종목 손절가 도달 시 -{heat['risk']:,.0f}")


def _risk_cell(item: dict, rk: dict, compact: bool = False) -> str:
    """손절·R 칸(2줄): 손절가와 현재가 거리, 현재 R 배수·목표가. compact 면 줄바꿈 없는 덩어리로 만든다."""
    cur = item["currency"]
    if rk["stop"] is None:
        return "손절 미설정"
    nb = (lambda t: t.replace(" ", "&nbsp;")) if compact else (lambda t: t)
    mark = {"breach": "🛑", "near": "⚠️"}.get(rk["status"], "")
    line1 = nb(f"{mark}{'' if compact else '손절 '}{_fmt_money(rk['stop'], cur)}" + ("(기본)" if rk["default"] else "") + f" ({rk['dist']:+.1f}%)")
    bits = ([f"{rk['R']:+.1f}R"] if rk["R"] is not None else []) + ([f"목표 {_fmt_money(rk['target'], cur)}"] if rk["target"] else [])
    return ("<br>".join([line1, nb(" · ".join(bits))]) if bits else line1) if compact else line1 + (" · " + " · ".join(bits) if bits else "")


def holdings_block(rows: list, tech: dict = None, links: dict = None, recon: list = None, fx: float = None, levels: dict = None) -> str:
    """종목 단위(계좌 합산)로 집계해 접이식(<details>)으로 보여준다. 4종목 이상은 5열 표(종목·평가손익·추세·손절R·일지, 칸마다 2줄)로
    가로 폭을 줄이고, 3종목 이하는 목록. 펼치지 않아도 점검이 필요한 종목은 카드로 먼저 보여준다."""
    items = aggregate_holdings(rows)
    if not items:
        return "- 보유 종목이 없습니다."
    pct = lambda pnl, cost: (pnl / cost * 100) if cost else 0.0
    tech, levels = tech or {}, levels or {}
    parts = {id(i): _tech_parts(tech.get((i["market"], i["code"]))) for i in items}
    risks = {id(i): holding_risk(i, levels.get((i["code"], i["currency"]))) for i in items}
    show_rs = bool(tech) and LIMITS["holdings_rs"]
    show_links = bool(links)
    kr_val = lambda i: i["value"] * (fx if i["currency"] == "USD" else 1.0)
    weights = {}
    if fx or len({i["currency"] for i in items}) == 1:
        tot_v = sum(kr_val(i) for i in items)
        weights = {id(i): kr_val(i) / tot_v * 100 for i in items} if tot_v else {}
    flags = {id(i): holding_flags(i, tech.get((i["market"], i["code"])), risks[id(i)]) for i in items}
    heat = portfolio_heat(items, risks, fx)
    avg_s = lambda i: _fmt_money(risks[id(i)]["avg"], i["currency"]) if risks[id(i)]["avg"] else "-"
    pnl_s = lambda i: f"{_money_u(i['pnl'], i['currency'], True)} ({pct(i['pnl'], i['cost']):+.1f}%)"
    if LIMITS["holdings_layout"] == "cards" or len(items) <= SMALL_TABLE_ROWS:
        # 종목별 카드(중첩 목록): 칸 폭에 제한받지 않아 모바일에서 줄바꿈이 자연스럽다
        lines = []
        for i in items:
            tr, ds, rs_s = parts[id(i)]
            w = f" · 비중 {weights[id(i)]:.0f}%" if id(i) in weights else ""
            lines.append(f"- **{i['name']}** `{i['code']}`{w}")
            sub = [f"💰 {_fmt_qty(i['qty'])}주 · 평단 {avg_s(i)} → 평가 {_money_u(i['value'], i['currency'])} · **{pnl_s(i)}**"]
            if tech:
                sub.append(f"📈 {tr} · 분산 {ds}" + (f" · RS {rs_s}" if show_rs else ""))
            sub.append("🛡️ " + _risk_cell(i, risks[id(i)]))
            if show_links and _journal_cell(i, links) != "-":
                sub.append("📒 " + _journal_cell(i, links))
            lines += [f"  - {_sm(t)}" for t in sub]
    else:
        heads = ["종목", "평가 · 손익"] + (["추세 · 분산 · RS" if show_rs else "추세 · 분산"] if tech else []) + ["손절 · R"] + (["일지"] if show_links else [])
        lines = ["| " + " | ".join(_sm(h) for h in heads) + " |",
                 "| :--- | ---: |" + (" :--- |" if tech else "") + " :--- |" + (" :--- |" if show_links else "")]
        for i in items:
            tr, ds, rs_s = parts[id(i)]
            w = f" · 비중 {weights[id(i)]:.0f}%" if id(i) in weights else ""
            nb = lambda t: str(t).replace(" ", "&nbsp;")          # 숫자·금액은 칸 안에서 중간에 끊어지지 않게
            cells = [f"**{_short(i['name'], 8)}**<br>{nb(_fmt_qty(i['qty']) + '주·평단 ' + avg_s(i))}" + (f"<br>비중 {weights[id(i)]:.0f}%" if id(i) in weights else ""),
                     f"{nb(_money_u(i['value'], i['currency']))}<br>{nb(pnl_s(i))}"]
            if tech:
                cells.append(f"{tr}<br>분산 {ds}" + (f" · RS {rs_s}" if show_rs else ""))
            cells.append(_risk_cell(i, risks[id(i)], True))
            if show_links:
                jl = _journal_cell(i, links, 3)
                cells.append(jl.replace(" [", "<br>[") if jl != "-" else "-")
            lines.append("| " + " | ".join(_sm(v) for v in cells) + " |")
    lines.append("")
    summ = []
    for cur, label in (("KRW", "원화"), ("USD", "달러")):
        sub = [i for i in items if i["currency"] == cur]
        if sub:
            cost, value, pnl = (sum(i[k] for i in sub) for k in ("cost", "value", "pnl"))
            lines.append("- " + _sm(f"**합계 ({label}, {len(sub)}종목):** 투자 {_money_u(cost, cur)} → 평가 {_money_u(value, cur)} · **{_money_u(pnl, cur, True)} ({pct(pnl, cost):+.2f}%)**"))
            summ.append(f"{label} {pct(pnl, cost):+.1f}%")
    if fx and len({i["currency"] for i in items}) > 1:
        tc, tv = sum(i["cost"] * (fx if i["currency"] == "USD" else 1.0) for i in items), sum(kr_val(i) for i in items)
        lines.append("- " + _sm(f"**합계(원화 환산, USD/KRW {fx:,.0f}):** 투자 {tc:,.0f}원 → 평가 {tv:,.0f}원 · **{tv - tc:+,.0f}원 ({pct(tv - tc, tc):+.2f}%)**"))
        summ.append(f"원화환산 {pct(tv - tc, tc):+.1f}%")
    if heat and heat["pct"] is not None:
        over = heat["pct"] > LIMITS["max_heat_pct"]
        lines.append("- " + _sm(f"**포트폴리오 히트:** 전 종목 손절가 도달 시 -{heat['risk']:,.0f}원 (자산의 **{heat['pct']:.1f}%**) · 한도 {LIMITS['max_heat_pct']:g}% {'🔴 초과' if over else '🟢'}"))
        summ.append(f"히트 {heat['pct']:.1f}%" + ("🔴" if over else ""))
    lines.append("")
    note = "> 증권사 잔고조회 기준(정규장 종가). 일부 종목만 보이면 ISA 등 미지원 계좌는 제외된 것입니다."
    if HOLDINGS_ZERO:
        note += f"\n>\n> 평가금액이 0인 잔량 {len(set(HOLDINGS_ZERO))}종목은 제외했습니다(전량 매도 후 남은 소수점 잔량 등)."
    note += ("\n>\n> **보는 법:** `평단`은 평균 매수가, `비중`은 포트폴리오 내 비중(달러 자산은 USD/KRW로 환산)입니다. "
             "`손절`은 매수 일지 메타데이터의 `stop`(없으면 평단 −" + f"{LIMITS['default_stop_pct']:g}%" + " 기본값 `(기본)`), 괄호는 현재가가 손절가보다 몇 % 위인지, "
             "`R`은 (현재가−평단)÷(평단−손절가)입니다. 🛑 이탈 · ⚠️ 임박(" + f"{LIMITS['stop_near_pct']:g}%" + " 이내).")
    if tech:
        note += (f"\n>\n> 추세 옆 수치는 50일선 대비, '고점'은 52주 고점 대비 낙폭이고 분산일은 야후 일봉 기준(하락 기준 "
                 + (f"-{LIMITS['stock_dist_k']:g}×σ" if LIMITS["stock_dist_mode"] == "sigma" else f"-{LIMITS['stock_dist_fixed_pct']:g}%")
                 + f", 최근 {LIMITS['dist_lookback']}거래일)입니다. 트레일링 기준선: {TRAIL_LABEL.get(LIMITS['trail_line'], '끔')}"
                 + (f"(아래 종가 {LIMITS['trail_confirm']}회 연속 시 이탈)" if LIMITS["trail_line"] in ("ema21", "ma50") else "") + ".")
    if show_rs:
        note += (f"\n>\n> RS = 종목 수익률 − 벤치마크 수익률(%p, 최근 {'/'.join(str(n) for n in LIMITS['rs_periods'])}거래일). "
                 "🟢 단기·장기 모두 초과 · 🟡 단기만 초과 · 🟠 장기만 초과 · 🔴 모두 미달")
    if recon:
        note += "\n>\n" + "\n".join(recon_lines(recon))
    lines.append(note)
    summary = f"💼 보유 {len(items)}종목 · " + " · ".join(summ) + (" · ⚠️ 원장 불일치" if recon else "") + " (클릭하여 펼치기)"
    attn = [f"> - **{i['name']}** — " + " · ".join(t for _, t in flags[id(i)]) for i in items if flags[id(i)]]
    head = []
    if heat and heat["pct"] is not None and heat["pct"] > LIMITS["max_heat_pct"]:
        head.append(f"> - **포트폴리오 히트 {heat['pct']:.1f}%** — 한도 {LIMITS['max_heat_pct']:g}% 초과: 신규 진입 전에 손절 상향·비중 축소를 점검하세요")
    attn = head + attn
    card = ("> [!WARNING]\n> **오늘 점검할 항목 %d개**\n" % len(attn) + "\n".join(attn[:7]) + (f"\n> - … 외 {len(attn) - 7}개" if len(attn) > 7 else "") + "\n\n") if attn else ""
    return card + f"<details>\n<summary>{summary}</summary>\n\n" + "\n".join(lines) + "\n\n</details>"


def _put_section(text: str, regex, start: str, end: str, title: str, body: str) -> str:
    """마커가 없는(이전 버전) 템플릿에도 '전략' 구분선 앞에 섹션을 자동 삽입한 뒤 본문을 채운다."""
    if not regex.search(text):
        skeleton = f"{title}\n\n{start}\n{end}\n\n"
        m = re.search(r"\n---\n(?=#### 🧠)", text)
        text = text[:m.start() + 1] + skeleton + text[m.start() + 1:] if m else text.rstrip("\n") + "\n\n" + skeleton
    return _replace_optional(text, regex, start, end, body)


def resolve_target_date(now_kst: datetime, market: str, date=None) -> str:
    if date:
        t = _digits(date)
        if len(t) != 8:
            raise ValueError(f"날짜 형식 오류(YYYYMMDD): {date}")
        return t
    if market == "US" and now_kst.hour < 12:  # 미국장은 KST 오전까지 전날 거래일
        return (now_kst - timedelta(days=1)).strftime("%Y%m%d")
    return now_kst.strftime("%Y%m%d")


def refresh_cycle_journals(repo, ledger: pd.DataFrame, codes: list, skip_date: str, now_kst: datetime, market: str):
    """새 체결이 속한 사이클의 이전 일지들을 다시 만들어 차트·잔량이 전체 사이클을 반영하게 한다. codes: [(코드, 통화)]"""
    pos, pnl = compute_positions(ledger), None
    pnl = compute_realized(ledger)
    cycles = pos.attrs.get("cycles") or {}
    for code, cur in dict.fromkeys(codes):
        cyc = pick_cycle(cycles, code, cur, skip_date)
        if not cyc:
            continue
        for d in sorted({f["date"] for f in cyc["fills"]} - {skip_date}):
            g = ledger[(ledger["date"] == d) & (ledger["code"] == code) & (ledger["currency"] == cur)]
            if g.empty:
                continue
            try:
                update_stock_journals(repo, g, d, market, cur, pnl, is_final(now_kst, market, d), pos)
            except Exception as e:
                logger.warning("사이클 이전 일지 갱신 실패 (%s %s): %s", d, code, mask(e))


def check_and_update_trades(now_kst: datetime, market: str, date=None, shared: dict = None):
    shared = shared if shared is not None else {}
    target_date = resolve_target_date(now_kst, market, date)
    if datetime.strptime(target_date, "%Y%m%d").weekday() >= 5:
        return
    if not (NH_APP_KEY and NH_APP_SECRET and ACCOUNTS):
        add_warning("NH 인증 정보/계좌 시크릿이 설정되지 않았습니다.")
        return
    date_iso = iso_date(target_date)
    snapshots, currency = fetch_trades_for_market(market, target_date)
    if not snapshots:
        return
    repo = shared.get("repo") or get_data_repo()
    shared["repo"] = repo
    ledger, sha = (shared["ledger"], shared["sha"]) if "ledger" in shared else load_ledger(repo)
    # 이번에 조회에 성공한 계좌의 해당 일자 구간만 API 값으로 교체 (부분체결로 trade_id 가 바뀌는 경우 대응)
    in_slice = ((ledger["date"] == date_iso) & (ledger["currency"] == currency) & ledger["account"].isin(list(snapshots))
                & ~is_manual(ledger))  # 수동 입력 행은 API 값으로 덮어쓰지 않음
    old = ledger[in_slice]
    incoming = pd.concat([f for f in snapshots.values() if not f.empty], ignore_index=True)
    keep = ledger[~in_slice]
    # 같은 체결이 인접 날짜로 중복 조회되는 경우(같은 계좌·종목·구분·수량·단가) 이른 날짜만 남김
    both = pd.concat([keep.assign(_src=0), incoming.assign(_src=1)], ignore_index=True)
    both, dups = dedupe_cross_date(both)
    removed_keep = int((dups["_src"] == 0).sum()) if not dups.empty else 0
    incoming = both[both["_src"] == 1].drop(columns="_src")
    keep = both[both["_src"] == 0].drop(columns="_src")
    if not dups.empty:
        logger.warning("인접 날짜 중복 체결 %d건 제외 (원장 기존 행 %d건 정리)", len(dups), removed_keep)
        if removed_keep:
            _notify_once(f"dedupe_{market}_{target_date}_{removed_keep}",
                         f"{market} 원장에서 중복 체결 {removed_keep}건을 정리했습니다. 남은 중복 일지 파일은 rebuild(apply)로 정리하세요.")
    cov = manual_covered(keep, incoming)
    moved = set()
    if cov:  # 수동으로 넣어둔 체결이 이제 API 로도 조회됨 → 수동 행 제거(알림은 이미 나갔으므로 재발송 안 함)
        moved |= {_trade_key(r) for _, r in keep.loc[cov].iterrows()}
        keep = keep.drop(index=cov)
        logger.info("수동 입력 %d건이 API 로 확인되어 정리", len(cov))
    merged = pd.concat([keep, incoming], ignore_index=True) if not keep.empty else incoming.copy()
    new_rows = incoming[~incoming["trade_id"].isin(old["trade_id"])]
    if removed_keep:  # 이미 알림이 나간 체결이 이른 날짜로 옮겨진 것뿐이면 '신규 체결' 알림을 다시 보내지 않음
        moved |= {_trade_key(r) for _, r in dups[dups["_src"] == 0].iterrows()}
    if moved:
        new_rows = new_rows[[_trade_key(r) not in moved for _, r in new_rows.iterrows()]]
    changed = (set(zip(incoming["trade_id"], incoming["order_no"])) != set(zip(old["trade_id"], old["order_no"]))) or removed_keep > 0 or bool(cov)

    # 장 마감 후 일봉 확정본으로 차트를 한 번 더 교체 (체결 변동 없어도 1회 수행; .cache 마커로 중복 방지)
    final = is_final(now_kst, market, date_iso)
    tid_sig = hashlib.md5("|".join(sorted(incoming["trade_id"])).encode()).hexdigest()[:12]
    marker = f"final_{market}_{target_date}"
    pending_key = f"journal_pending_{market}_{target_date}"
    pending = bool((_read_cache(pending_key) or {}).get("pending"))  # 이전 실행에서 일지 작성이 실패해 재시도가 필요한 상태
    refresh = (not changed) and (pending or (final and (_read_cache(marker) or {}).get("sig") != tid_sig))
    if not changed and not refresh:
        return

    # 원장 저장과 체결 알림을 먼저 처리: 일지/차트 작성이 실패해도 체결 기록과 알림은 막히지 않도록 함
    if changed:
        sha = save_ledger(repo, merged, sha, f"Auto-sync: {market}")
        shared["ledger"], shared["sha"] = merged, sha
        if not new_rows.empty:
            cyc_all = compute_cycles(merged)
            lines = [f"• {html.escape(str(r['name']))} {html.escape(str(r['tx_type']))} {_fmt_qty(r['qty'])}주 @ {_fmt_money(r['unit_price'], currency)} ({r['account']})"
                     + (" ⚠️물타기(평단 아래 추가매수)" if is_add_down(cyc_all, r) else "") for _, r in new_rows.head(10).iterrows()]
            more = f"\n… 외 {len(new_rows) - 10}건" if len(new_rows) > 10 else ""
            day_df0 = merged[(merged["date"] == date_iso) & (merged["currency"] == currency)]
            send_telegram_message(f"🚨 <b>[체결 알림 - {market}]</b>\n신규 체결: 총 {len(new_rows)}건\n" + "\n".join(lines) + more
                                  + "\n" + journal_links_text(day_df0, new_rows, date_iso))

    day_df = merged[(merged["date"] == date_iso) & (merged["currency"] == currency)]
    try:
        update_stock_journals(repo, day_df, date_iso, market, currency, compute_realized(merged), final, compute_positions(merged))
    except Exception:
        _write_cache(pending_key, {"pending": True})  # 다음 실행에서 일지만 다시 시도
        if changed:
            add_warning(f"{market} 체결은 원장에 저장·알림 완료, 종목 일지 작성만 실패 → 다음 실행에서 재시도합니다.")
        raise
    if pending:
        _write_cache(pending_key, {"pending": False})
    if changed and not new_rows.empty:
        refresh_cycle_journals(repo, merged, list(zip(new_rows["code"], new_rows["currency"])), date_iso, now_kst, market)
    if final:
        _write_cache(marker, {"sig": tid_sig})


# ==========================================
# 6. 운영/점검 모드
# ==========================================
def run_accounts(args):
    print("=== 연동 계좌 점검 ===")
    if not (NH_APP_KEY and NH_APP_SECRET):
        print("NH_APP_KEY / NH_APP_SECRET 미설정")
        return
    get_nh_access_token(force=True)
    print("NH Access Token 발급: 정상")
    if not ACCOUNTS:
        print("설정된 계좌 시크릿(NH_ACCOUNT_CMA / NH_ACCOUNT_ISA)이 없습니다.")
        return
    target = datetime.now(KST).strftime("%Y%m%d")
    for name, acc_no in ACCOUNTS.items():
        disp = "********"
        opt = " (선택 계좌)" if name.upper() in OPTIONAL_ACCOUNTS else ""
        print(f"- {name} 계좌 {disp}{opt}")
        for market in ("KR", "US"):
            if market == "US" and name.upper() in NO_US_ACCOUNTS:
                print(f"    {market}: 조회 제외 (NH_NO_US_ACCOUNTS)")
                continue
            try:
                resps = post_pages(US_PATH if market == "US" else KR_PATH, _order_body(market, acc_no, target), max_pages=1)
                res = resps[0]
                n = len(_as_list(res.json().get("Output_0"))) if res.status_code == 200 else "-"
                print(f"    {market}: HTTP {res.status_code} / 오늘 체결 행 {n}")
            except Exception as e:
                print(f"    {market}: 실패 ({mask(e)})")
            try:
                bres = post_pages(BALANCE_PATH[market], _balance_body(market, acc_no), max_pages=1)[0]
                if bres.status_code == 200:
                    print(f"    {market} 잔고: HTTP 200 / 보유 {len(_as_list(bres.json().get('Output_1')))}종목")
                else:
                    print(f"    {market} 잔고: {_err_detail(bres)}")
            except Exception as e:
                print(f"    {market} 잔고: 실패 ({mask(e)})")


def _date_range(args, now_kst):
    """--date 생략 시 오늘, --date-end 생략 시 오늘까지. 기간 제한 없음(평일만)."""
    today = now_kst.strftime("%Y%m%d")
    start = _digits(args.date) or today
    end = _digits(args.date_end) or today
    s, e = datetime.strptime(start, "%Y%m%d"), datetime.strptime(end, "%Y%m%d")
    if e < s:
        raise ValueError("--date-end 가 --date 보다 앞입니다.")
    return [(s + timedelta(days=i)).strftime("%Y%m%d") for i in range((e - s).days + 1)
            if (s + timedelta(days=i)).weekday() < 5]


def run_backfill(args, now_kst):
    """기간 내 체결을 조회해 원장에만 반영(일지 생성 없음).
    기본: 원장에 (일자, 통화, 계좌) 구간이 아예 없는 곳만 채움 / --overwrite: 조회에 성공한 계좌 구간을 API 값으로 교체.
    최신 → 과거 순으로 조회하며, 한 시장에서 연속 5일 조회가 거부되면(예: 조회 가능 기간 초과) 더 과거는 중단하고 알린다."""
    dates = _date_range(args, now_kst)
    if not dates:
        logger.info("조회 대상 평일이 없습니다.")
        return
    markets = [args.market] if args.market else ["KR", "US"]
    repo = get_data_repo()
    ledger, sha = load_ledger(repo)
    added, replaced = 0, 0
    failures, aborted = [], []
    logger.info("backfill 기간: %s ~ %s (평일 %d일) 시장=%s", dates[0], dates[-1], len(dates), "/".join(markets))
    for market in markets:
        streak = 0
        for target in reversed(dates):
            errs = []
            snapshots, currency = fetch_trades_for_market(market, target, errs)
            if errs:
                failures.extend(errs)
                streak += 1
                if streak >= BACKFILL_ABORT_AFTER:
                    aborted.append((market, target, errs[0][3]))
                    logger.error("[%s] 연속 %d일 조회 거부 → %s 이전 구간 중단: %s", market, streak, target, errs[0][3])
                    break
                continue
            streak = 0
            date_iso = iso_date(target)
            if not snapshots:
                logger.info("[%s] %s: API 체결 0건 (응답은 정상)", market, date_iso)
                continue
            for acc, df in snapshots.items():
                slice_mask = (ledger["date"] == date_iso) & (ledger["currency"] == currency) & (ledger["account"] == acc) & ~is_manual(ledger)
                exists = bool(slice_mask.any())
                if exists and not args.overwrite:
                    logger.info("[%s] %s %s: 원장에 이미 있음(%d건) → 건너뜀 (API %d건)", market, date_iso, acc, int(slice_mask.sum()), len(df))
                    continue
                if exists:
                    replaced += int(slice_mask.sum())
                    ledger = ledger[~slice_mask]
                ledger = pd.concat([ledger, df], ignore_index=True) if not ledger.empty else df.copy()
                added += len(df)
                logger.info("[%s] %s %s: %d건 %s", market, date_iso, acc, len(df), "교체" if exists else "추가")
    logger.info("backfill 결과: 추가/교체 %d건 (기존 삭제 %d건) 조회 실패 %d건 apply=%s", added, replaced, len(failures), args.apply)

    if failures:  # 텔레그램으로 한 번에 요약 (실행 종료 시 RUN_WARNINGS 로 전송)
        sample = failures[0]
        msg = (f"backfill 조회 실패 {len(failures)}건 — 예: {sample[0]}/{sample[1]} {iso_date(sample[2])} → {sample[3]}")
        for market, target, detail in aborted:
            msg += f"\n{market}: {iso_date(target)} 이전은 연속 {BACKFILL_ABORT_AFTER}일 거부되어 중단 ({detail})"
        add_warning(msg)
    if not args.apply or added == 0:
        if added and not args.apply:
            logger.info("[조회만] 저장하려면 apply 를 켜고 다시 실행하세요.")
        return
    ledger, dups = dedupe_cross_date(ledger.reset_index(drop=True))
    if not dups.empty:
        logger.warning("인접 날짜 중복 체결 %d건 제외", len(dups))
    save_ledger(repo, ledger, sha, f"Auto-backfill: {dates[0]}~{dates[-1]}")
    logger.info("원장 저장 완료. 일지가 필요하면 rebuild 모드(apply)로 재생성하세요.")


_SIDE_MAP = {"매수": "매수", "buy": "매수", "b": "매수", "매도": "매도", "sell": "매도", "s": "매도"}


def parse_manual_trades(raw: str, accounts: dict, ledger: pd.DataFrame, today_iso: str):
    """'계좌,날짜,시장,코드,구분,수량,단가[,수수료[,세금[,종목명]]]' 를 ';' 또는 줄바꿈으로 이어 입력. → (행 목록, 오류 목록)"""
    rows, errs, seen = [], [], Counter()
    entries = [e.strip() for e in re.split(r"[;\n]+", raw or "") if e.strip()]
    if not entries:
        return [], ["입력된 체결이 없습니다. 형식: 계좌,날짜,시장,코드,구분,수량,단가  (예: CMA,2026-10-19,KR,123456,매도,0.37,72000)"]
    acc_map = {k.upper(): k for k in (accounts or {})}
    for n, e in enumerate(entries, 1):
        f = [x.strip() for x in re.split(r"[,\t]", e)]
        if len(f) < 7:
            errs.append(f"{n}번째 입력 '{e}': 항목이 7개 이상 필요합니다 (계좌,날짜,시장,코드,구분,수량,단가). 숫자에 쉼표(,)는 쓸 수 없습니다.")
            continue
        acc_s, d_s, mk, code, side_s, q_s, p_s = f[:7]
        fee_s, tax_s, name = (f[7] if len(f) > 7 else ""), (f[8] if len(f) > 8 else ""), (f[9] if len(f) > 9 else "")
        bad = []
        if len(f) >= 8 and re.fullmatch(r"\d{3}", f[7]) and re.fullmatch(r"\d{1,3}", p_s):
            bad.append("숫자에 천 단위 쉼표(예: 72,000)가 있는 것 같습니다. 쉼표 없이 72000 으로 입력하세요")
        acc = acc_map.get(acc_s.upper()) if acc_map else acc_s.upper()
        if not acc:
            bad.append(f"계좌 '{acc_s}' 없음 (사용 가능: {', '.join(acc_map.values())})")
        dd = re.sub(r"\D", "", d_s)
        try:
            d_iso = iso_date(dd) if len(dd) == 8 and datetime.strptime(dd, "%Y%m%d") else ""
        except ValueError:
            d_iso = ""
        if not d_iso:
            bad.append(f"날짜 '{d_s}' 형식 오류 (YYYY-MM-DD)")
        elif d_iso > today_iso:
            bad.append(f"날짜 '{d_s}' 가 미래입니다")
        mk = mk.upper()
        if mk not in ("KR", "US"):
            bad.append(f"시장 '{mk}' 는 KR 또는 US")
        code = code.upper()
        if mk == "KR" and not re.fullmatch(r"[0-9A-Z]{6}", code):
            bad.append(f"국내 코드 '{code}' 는 6자리여야 합니다")
        if mk == "US" and not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", code):
            bad.append(f"미국 티커 '{code}' 형식 오류")
        side = _SIDE_MAP.get(side_s.lower())
        if not side:
            bad.append(f"구분 '{side_s}' 는 매수/매도")
        try:
            qty, price = float(q_s), float(p_s)
            fee, tax = float(fee_s or 0), float(tax_s or 0)
            if not (qty > 0 and price > 0 and fee >= 0 and tax >= 0) or any(x != x or x == float("inf") for x in (qty, price, fee, tax)):
                raise ValueError
        except ValueError:
            bad.append("수량·단가는 0보다 큰 숫자, 수수료·세금은 0 이상의 숫자여야 합니다")
        if bad:
            errs.append(f"{n}번째 입력 '{e}': " + " / ".join(bad))
            continue
        if not name and not ledger.empty:
            hit = ledger[ledger["code"] == code]
            name = str(hit["name"].iloc[-1]) if not hit.empty else ""
        name = name or code
        curr = "KRW" if mk == "KR" else "USD"
        key = f"{d_iso}|{acc}|{code}|{side}|{qty:.6f}|{price:.6f}"
        seen[key] += 1
        tid = MANUAL_PREFIX + hashlib.md5(f"{key}|{seen[key]}".encode()).hexdigest()[:12]
        rows.append({"date": d_iso, "time": "00:00:00", "tx_type": side, "code": code, "name": name, "qty": qty, "unit_price": price,
                     "total_amt": round(qty * price, 0 if curr == "KRW" else 2), "fee": fee, "tax": tax, "currency": curr,
                     "trade_id": tid, "account": acc, "order_no": ""})
    return rows, errs


def run_manual(args, now_kst):
    """API 가 돌려주지 않는 체결(소수점, 조회 불가 계좌 등)을 직접 입력해 원장에 추가/삭제하고 관련 일지(잔량·평단 포함)를 갱신.
    --apply 없으면 입력 검증과 반영 예정 내용만 출력(dry-run). --delete 면 같은 입력을 삭제."""
    repo = get_data_repo()
    ledger, sha = load_ledger(repo)
    rows, errs = parse_manual_trades(args.input, ACCOUNTS, ledger, now_kst.strftime("%Y-%m-%d"))
    if errs:
        for e in errs:
            logger.error(e)
        raise ValueError(f"수동 입력 오류 {len(errs)}건 — 위 로그를 확인하세요.")
    cur, added, deleted, skipped = ledger.copy(), [], [], []
    for r in rows:
        label = f"{r['date']} {r['account']} {r['code']} {r['name']} {r['tx_type']} {_fmt_qty(r['qty'])}주 @ {r['unit_price']:g}"
        hit = cur["trade_id"] == r["trade_id"]
        if args.delete:
            if hit.any():
                deleted.append(cur[hit].iloc[0].to_dict())
                cur = cur[~hit]
            else:
                skipped.append(f"{label}: 삭제할 수동 기록이 없습니다")
            continue
        if hit.any():
            skipped.append(f"{label}: 이미 입력되어 있습니다")
            continue
        auto = cur[~is_manual(cur)]
        dup = manual_covered(pd.DataFrame([r], columns=LEDGER_COLUMNS), auto) if not auto.empty else []
        if dup or any(_trade_key(r) == _trade_key(a) and abs((pd.to_datetime(a["date"]) - pd.to_datetime(r["date"])).days) <= 1 for _, a in auto.iterrows()):
            skipped.append(f"{label}: 자동 기록에 이미 같은 체결이 있어 건너뜁니다")
            continue
        added.append(r)
        cur = pd.concat([cur, pd.DataFrame([r], columns=LEDGER_COLUMNS)], ignore_index=True)
    for r in added:
        logger.info("[%s] %s %s %s %s %s %s주 @ %g", "추가 예정" if not args.apply else "추가", r["date"], r["account"], r["code"], r["name"], r["tx_type"], _fmt_qty(r["qty"]), r["unit_price"])
    for r in deleted:
        logger.info("[%s] %s %s %s %s %s %s주 @ %g", "삭제 예정" if not args.apply else "삭제", r["date"], r["account"], r["code"], r["name"], r["tx_type"], _fmt_qty(r["qty"]), r["unit_price"])
    for m in skipped:
        logger.warning("건너뜀 — %s", m)
    if not added and not deleted:
        logger.info("반영할 변경이 없습니다.")
        return
    if not args.apply:
        logger.info("[DRY-RUN] 추가 %d건 / 삭제 %d건. 실제로 반영하려면 apply 를 켜고 다시 실행하세요.", len(added), len(deleted))
        return

    sha = save_ledger(repo, cur, sha, f"Manual: +{len(added)} -{len(deleted)}")
    logger.info("원장 저장 완료 (추가 %d건, 삭제 %d건)", len(added), len(deleted))
    if added:
        cyc_all = compute_cycles(cur)
        lines = [f"• {html.escape(str(r['name']))} {r['tx_type']} {_fmt_qty(r['qty'])}주 @ {_fmt_money(r['unit_price'], r['currency'])} ({r['account']}, {r['date']})"
                 + (" ⚠️물타기(평단 아래 추가매수)" if is_add_down(cyc_all, r) else "") for r in added[:10]]
        links = []
        for d_iso in dict.fromkeys(r["date"] for r in added):
            rows_d = pd.DataFrame([r for r in added if r["date"] == d_iso])
            links.append(journal_links_text(cur[cur["date"] == d_iso], rows_d, d_iso))
        send_telegram_message(f"✍️ <b>[수동 입력 체결]</b> 총 {len(added)}건\n" + "\n".join(lines) + (f"\n… 외 {len(added) - 10}건" if len(added) > 10 else "")
                              + "\n" + "\n".join(x for x in links if x))
    # 거래가 사라진 일지/차트 파일 정리 (삭제 시) — git 이력으로 복구 가능
    for r in deleted:
        rem = cur[(cur["date"] == r["date"]) & (cur["code"] == r["code"]) & (cur["currency"] == r["currency"])]
        if not rem["tx_type"].astype(str).str.contains(r["tx_type"], na=False).any():
            path = f"{STOCK_DIR}/{r['date']}_{safe_filename(r['code'])}_{safe_filename(r['name'])}_{r['tx_type']}.md"
            _, fsha = read_repo_text(repo, path)
            if fsha:
                repo.delete_file(path, "Manual: remove journal", fsha)
                logger.info("삭제: %s", path)
        if rem.empty:
            path = f"{CHART_DIR}/{r['date']}_{safe_filename(r['code'])}.svg"
            _, fsha = read_repo_text(repo, path)
            if fsha:
                repo.delete_file(path, "Manual: remove chart", fsha)
                logger.info("삭제: %s", path)
    # 영향받는 종목의 해당 날짜 이후 일지를 다시 만들어 잔량·평단·실현손익을 맞춤
    affected = {}
    for r in added + deleted:
        k = (r["code"], r["currency"])
        affected[k] = min(affected.get(k, r["date"]), r["date"])
    pnl_map, pos_all = compute_realized(cur), compute_positions(cur)
    failed = []
    for (code, curr), dmin in affected.items():
        for cyc in (pos_all.attrs.get("cycles") or {}).get((code, curr), []):  # 해당 날짜를 포함한 사이클의 첫 일지부터 갱신
            if cyc["start"] <= dmin <= (cyc["end"] or "9999-12-31"):
                dmin = cyc["start"]
        sub = cur[(cur["code"] == code) & (cur["currency"] == curr) & (cur["date"] >= dmin)]
        market = "US" if curr == "USD" else "KR"
        for d_iso, g in sub.groupby("date"):
            try:
                update_stock_journals(repo, g, d_iso, market, curr, pnl_map, is_final(now_kst, market, d_iso), pos_all)
            except Exception as e:
                logger.error("일지 갱신 실패 %s %s: %s", d_iso, code, mask(e))
                failed.append(f"{d_iso} {code}")
    if failed:
        raise RuntimeError("원장은 저장됐지만 일부 일지 갱신 실패: " + ", ".join(failed) + " → rebuild(apply)로 재생성하세요.")


def trade_stats(recs: list) -> dict:
    """청산된 사이클 기록 [{pct, days, R, end}] 의 추세추종 핵심 통계."""
    n = len(recs)
    if not n:
        return {"n": 0}
    pcts = [r["pct"] for r in recs]
    wins, losses = [r for r in recs if r["pct"] > 0], [r for r in recs if r["pct"] <= 0]
    mean = lambda xs: sum(xs) / len(xs) if xs else None
    avg_win, avg_loss = mean([r["pct"] for r in wins]), mean([r["pct"] for r in losses])
    loss_sum = sum(r["pct"] for r in losses)
    cum, peak, mdd = 0.0, 0.0, 0.0
    for r in sorted(recs, key=lambda x: x["end"]):
        cum += r["pct"]
        peak = max(peak, cum)
        mdd = max(mdd, peak - cum)
    rs = [r["R"] for r in recs if r.get("R") is not None]
    return {"n": n, "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / n * 100, "avg_win": avg_win, "avg_loss": avg_loss,
            "payoff": (avg_win / abs(avg_loss)) if (avg_win is not None and avg_loss) else None, "expectancy": sum(pcts) / n,
            "profit_factor": (sum(r["pct"] for r in wins) / abs(loss_sum)) if loss_sum < 0 else None,
            "days_win": mean([r["days"] for r in wins]), "days_loss": mean([r["days"] for r in losses]),
            "avg_r": mean(rs), "n_r": len(rs), "mdd": mdd, "total_pct": sum(pcts)}


def group_rows(recs: list, keyfn) -> list:
    groups = {}
    for r in recs:
        groups.setdefault(keyfn(r), []).append(r)
    out = []
    for k in sorted(groups, key=str):
        st = trade_stats(groups[k])
        out.append((k, st["n"], st["win_rate"], st["expectancy"], st["avg_r"]))
    return out


def _f(v, fmt="{:.1f}", dash="-"):
    return dash if v is None else fmt.format(v)


def cycle_record(repo, code: str, cur: str, cyc: dict, files_ok: bool = True) -> dict:
    """리포트용 사이클 기록: 손익%, 보유일, R(손절가 있을 때), 진입 당시 시장 신호, setup, 진입 체크리스트 이행률."""
    rec = {"code": code, "name": cyc["fills"][-1].get("name") or code, "cur": cur, "start": cyc["start"], "end": cyc["end"],
           "pnl": cyc["pnl"], "pct": cyc["pnl"] / cyc["cost"] * 100 if cyc["cost"] else 0.0,
           "days": (datetime.strptime(cyc["end"], "%Y-%m-%d") - datetime.strptime(cyc["start"], "%Y-%m-%d")).days,
           "market": "US" if cur == "USD" else "KR", "R": None, "signal": None, "setup": None, "adherence": None,
           "add_down": any(f.get("add_down") for f in cyc["fills"])}
    if not files_ok:
        return rec
    try:
        lv = levels_for_cycle(repo, cyc, code)
        rec["R"] = realized_r(cyc, lv["stop"]) if lv["stop"] else None
        first = cyc["start"]
        hit = next(((n, p) for n, p, _ in _stock_files(repo) if n.startswith(f"{first}_{safe_filename(code)}_") and n.endswith("_매수.md")), None)
        if hit:
            text, _ = read_repo_text(repo, hit[1])
            m = re.search(r"^setup:[ \t]*([^\n#]*)", text or "", re.M)
            rec["setup"] = (m.group(1).strip() or None) if m else None
            done, todo = len(re.findall(r"^- \[[xX]\]", text or "", re.M)), len(re.findall(r"^- \[ \]", text or "", re.M))
            rec["adherence"] = done / (done + todo) * 100 if (done + todo) else None
        rec["signal"] = market_signal(repo, first)
    except Exception as e:
        logger.warning("리포트 보강 정보 읽기 실패 (%s): %s", code, mask(e))
    return rec


def report_equity_svg(recs: list, title: str) -> str:
    """청산 사이클 손익%의 누적 곡선(거래 종료일 순)."""
    pts, cum = [], 0.0
    for r in sorted(recs, key=lambda x: x["end"]):
        cum += r["pct"]
        pts.append((r["end"], cum))
    if not pts:
        return ""
    W, H, ml, mr, mt, mb = 640, 220, 56, 16, 34, 28
    ys = [0.0] + [v for _, v in pts]
    lo, hi = min(ys), max(ys)
    pad = (hi - lo) * 0.1 or 1.0
    lo, hi = lo - pad, hi + pad
    X = lambda i: ml + (W - ml - mr) * (i / max(len(pts) - 1, 1) if len(pts) > 1 else 0.5)
    Y = lambda v: mt + (hi - v) / (hi - lo) * (H - mt - mb)
    poly = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, (_, v) in enumerate(pts))
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="sans-serif"><rect width="{W}" height="{H}" fill="#fff"/>',
         f'<text x="{ml}" y="20" font-size="14" font-weight="bold" fill="#222">{html.escape(title)}</text>',
         f'<line x1="{ml}" y1="{Y(0):.1f}" x2="{W - mr}" y2="{Y(0):.1f}" stroke="#aaa" stroke-dasharray="3 3"/>',
         f'<text x="{ml - 6}" y="{Y(0) + 4:.1f}" font-size="10" text-anchor="end" fill="#666">0%</text>',
         f'<text x="{ml - 6}" y="{Y(hi - pad) + 4:.1f}" font-size="10" text-anchor="end" fill="#666">{hi - pad:+.0f}%</text>',
         f'<text x="{ml - 6}" y="{Y(lo + pad) + 4:.1f}" font-size="10" text-anchor="end" fill="#666">{lo + pad:+.0f}%</text>',
         f'<polyline points="{poly}" fill="none" stroke="#2f6fdb" stroke-width="2"/>']
    o += [f'<circle cx="{X(i):.1f}" cy="{Y(v):.1f}" r="3" fill="{"#d9363e" if v >= (pts[i - 1][1] if i else 0) else "#2f6fdb"}"/>' for i, (_, v) in enumerate(pts)]
    o += [f'<text x="{ml}" y="{H - 8}" font-size="10" fill="#666">{pts[0][0]}</text>', f'<text x="{W - mr}" y="{H - 8}" font-size="10" text-anchor="end" fill="#666">{pts[-1][0]}</text></svg>']
    return "\n".join(o)


def build_report(recs: list, start: str, end: str, chart_md: str = "") -> str:
    st = trade_stats(recs)
    L = [f"# 📊 성과 리포트 {start} ~ {end}", ""]
    if not st["n"]:
        return "\n".join(L + ["> 이 기간에 청산된 사이클이 없습니다."]) + "\n"
    kind = "TIP" if st["expectancy"] > 0 else "CAUTION"
    L += [f"> [!{kind}]", f"> **청산 {st['n']}건 · 승률 {st['win_rate']:.0f}% · 손익비 {_f(st['payoff'], '{:.2f}')} · 거래당 기대값 {st['expectancy']:+.2f}%**", ">",
          f"> - 평균 이익 {_f(st['avg_win'], '{:+.1f}%')} / 평균 손실 {_f(st['avg_loss'], '{:+.1f}%')} · 프로핏 팩터 {_f(st['profit_factor'], '{:.2f}')}",
          f"> - 평균 보유일 이긴 거래 {_f(st['days_win'], '{:.0f}일')} / 진 거래 {_f(st['days_loss'], '{:.0f}일')} (추세추종은 이긴 거래를 더 오래 들고 가는 것이 이상적)",
          f"> - 누적 수익률(사이클 손익% 합) {st['total_pct']:+.1f}% · 최대 낙폭 -{st['mdd']:.1f}%p"
          + (f" · 평균 R {st['avg_r']:+.2f} (손절가 입력 {st['n_r']}건)" if st["avg_r"] is not None else " · R 은 매수 일지에 `stop` 을 적으면 계산됩니다"), ""]
    if chart_md:
        L += [chart_md, ""]
    for cur, label in (("KRW", "원화"), ("USD", "달러")):
        sub = [r for r in recs if r["cur"] == cur]
        if sub:
            gp, gl = sum(r["pnl"] for r in sub if r["pnl"] > 0), sum(r["pnl"] for r in sub if r["pnl"] <= 0)
            L.append(f"- **{label} 실현손익 합계:** {_fmt_signed_money(gp + gl, cur)}{'원' if cur == 'KRW' else ''} (이익 {_fmt_signed_money(gp, cur)} / 손실 {_fmt_signed_money(gl, cur)})")
    L += ["", "## 조건별 분해", ""]
    def table(title, rows):
        out = [f"### {title}", "", "| 구분 | 건수 | 승률 | 거래당 기대값 | 평균 R |", "| :--- | ---: | ---: | ---: | ---: |"]
        out += [f"| {k} | {n} | {wr:.0f}% | {ex:+.2f}% | {_f(ar, '{:+.2f}')} |" for k, n, wr, ex, ar in rows]
        return out + [""]
    L += table("시장별", group_rows(recs, lambda r: r["market"]))
    L += table("진입 당시 시장 신호", group_rows(recs, lambda r: r["signal"] or "(기록 없음)"))
    L += table("셋업(setup)", group_rows(recs, lambda r: r["setup"] or "(미기입)"))
    L += table("보유 기간", group_rows(recs, lambda r: "10일 이하" if r["days"] <= 10 else "11~30일" if r["days"] <= 30 else "31일 이상"))
    L += table("진입 체크리스트 이행", group_rows(recs, lambda r: "미기록" if r["adherence"] is None else "80% 이상" if r["adherence"] >= 80 else "80% 미만"))
    L += table("물타기 여부", group_rows(recs, lambda r: "물타기 있음" if r["add_down"] else "없음"))
    L += ["## 사이클 목록", "", "| 종목 | 기간 | 보유 | 손익 | R | 진입 신호 |", "| :--- | :--- | ---: | ---: | ---: | :--- |"]
    for r in sorted(recs, key=lambda x: x["end"], reverse=True):
        L.append(f"| **{r['name']}** ({r['code']}) | {r['start'][5:]} → {r['end'][5:]} | {r['days']}일 | {r['pct']:+.1f}% | {_f(r['R'], '{:+.2f}')} | {r['signal'] or '-'} |")
    L += ["", "> 사이클 = 첫 매수부터 전량 매도까지(계좌 합산). 수수료·세금 제외. 손익%는 사이클 평균 매수가 대비 실현손익률입니다."]
    return "\n".join(L) + "\n"


def run_report(args, now_kst):
    """청산된 사이클 성과 리포트를 reports/ 에 저장하고 텔레그램으로 요약한다. 기간: --date ~ --date-end (기본: 이번 달 1일 ~ 오늘)."""
    repo = get_data_repo()
    end = iso_date(_digits(args.date_end)) if args.date_end else now_kst.strftime("%Y-%m-%d")
    start = iso_date(_digits(args.date)) if args.date else end[:8] + "01"
    ledger, _ = load_ledger(repo)
    recs = []
    for (code, cur), cycles in compute_cycles(ledger).items():
        for cyc in cycles:
            if cyc["end"] and start <= cyc["end"] <= end and not cyc.get("orphan"):
                recs.append(cycle_record(repo, code, cur, cyc))
    name = start[:7] if start[:7] == end[:7] else f"{start}_{end}"
    chart_md = ""
    if recs:
        svg = report_equity_svg(recs, f"누적 수익률(사이클 손익% 합) {start} ~ {end}")
        if svg:
            if args.apply:  # dry-run 에서는 파일을 만들지 않는다
                upsert_file(repo, f"reports/charts/{name}.svg", f"Report chart {name}", svg)
            chart_md = f"![누적 수익률](charts/{name}.svg)"
    text = build_report(recs, start, end, chart_md)
    if args.apply:
        upsert_file(repo, f"reports/{name}.md", f"Report {name}", text)
        st = trade_stats(recs)
        if st["n"]:
            send_telegram_message(f"📊 <b>[성과 리포트 {html.escape(name)}]</b>\n청산 {st['n']}건 · 승률 {st['win_rate']:.0f}% · 손익비 {_f(st['payoff'], '{:.2f}')} · 기대값 {st['expectancy']:+.2f}%/거래\n"
                                  f"최대 낙폭 -{st['mdd']:.1f}%p" + (f" · 평균 R {st['avg_r']:+.2f}" if st["avg_r"] is not None else "") + f"\n📄 {tg_link('리포트 보기', f'reports/{name}.md')}")
        logger.info("성과 리포트 저장: reports/%s.md (청산 %d건)", name, st["n"])
    else:
        logger.info("[미리보기] 청산 %d건. 저장·알림하려면 apply 를 켜세요.\n%s", len(recs), text[:1500])


def run_rebuild(args, now_kst):
    repo = get_data_repo()
    ledger, sha = load_ledger(repo)
    if ledger.empty:
        logger.info("원장이 비어있습니다.")
        return
    # 0) 인접 날짜로 중복 기록된 체결 정리 (이른 날짜만 남김) + 거래가 사라진 일지/차트 파일 삭제 (git 이력으로 복구 가능)
    cleaned, dups = dedupe_cross_date(ledger)
    if not dups.empty:
        logger.warning("중복 체결 %d건 발견 (apply=%s)", len(dups), args.apply)
        stale = {}
        for _, r in dups.iterrows():
            side = "매수" if "매수" in str(r["tx_type"]) else "매도" if "매도" in str(r["tx_type"]) else None
            same_day = cleaned[(cleaned["date"] == r["date"]) & (cleaned["code"] == r["code"]) & (cleaned["currency"] == r["currency"])]
            if side and not same_day["tx_type"].astype(str).str.contains(side, na=False).any():
                stale[f"{STOCK_DIR}/{r['date']}_{safe_filename(r['code'])}_{safe_filename(r['name'])}_{side}.md"] = None
            if same_day.empty:
                stale[f"{CHART_DIR}/{r['date']}_{safe_filename(r['code'])}.svg"] = None
            logger.warning("  중복: %s %s %s %s %s주 @ %s (%s)", r["date"], r["account"], r["code"], r["tx_type"], _fmt_qty(r["qty"]), r["unit_price"], "제거 예정" if not args.apply else "제거")
        for path in stale:
            if not args.apply:
                logger.info("[DRY-RUN] 삭제 대상 파일 확인: %s", path)
                continue
            _, fsha = read_repo_text(repo, path)
            if fsha:
                repo.delete_file(path, "Auto-repair: remove duplicate journal", fsha)
                logger.info("삭제: %s", path)
        if args.apply:
            sha = save_ledger(repo, cleaned, sha, "Auto-repair: remove duplicate fills")
            logger.info("원장 중복 %d건 제거 저장 완료", len(dups))
        ledger = cleaned
    pnl_map = compute_realized(ledger)
    pos_all = compute_positions(ledger)
    view = ledger
    if args.date:
        view = view[view["date"] >= iso_date(_digits(args.date))]
    if args.date_end:
        view = view[view["date"] <= iso_date(_digits(args.date_end))]
    if args.market:
        view = view[view["currency"] == ("USD" if args.market == "US" else "KRW")]
    logger.info("원장 기반 일지 재생성 (대상 %d건, 전체 %d건). apply=%s", len(view), len(ledger), args.apply)
    failed = []
    for (d_iso, curr), group in view.groupby(["date", "currency"]):
        market = "US" if curr == "USD" else "KR"
        if not args.apply:
            logger.info("[DRY-RUN] 재생성 대상: %s %s (%d건)", d_iso, market, len(group))
            continue
        logger.info("재생성 중: %s %s", d_iso, market)
        try:
            update_stock_journals(repo, group, d_iso, market, curr, pnl_map, is_final(now_kst, market, d_iso), pos_all)
        except Exception as e:
            logger.error("재생성 실패 %s %s: %s", d_iso, market, mask(e))
            failed.append(f"{d_iso} {market}")
    if failed:
        raise RuntimeError("일부 날짜 재생성 실패: " + ", ".join(failed))


def run_init(repo, overwrite: bool):
    """기본 템플릿 생성. 파일별로 생성/덮어씀/동일/건너뜀 결과를 로그로 남긴다."""
    for kind, path in TEMPLATE_PATHS.items():
        content = DEFAULT_TEMPLATES[kind]
        old, sha = read_repo_text(repo, path)
        if old is None:
            repo.create_file(path, "init templates", content)
            logger.info("[템플릿] %s: 새로 생성", path)
        elif old.replace("\r\n", "\n") == content:
            logger.info("[템플릿] %s: 기본값과 이미 동일 → 변경 없음", path)
        elif overwrite:
            repo.update_file(path, "init templates (overwrite)", content, sha)
            logger.info("[템플릿] %s: 기본값으로 덮어씀", path)
        else:
            logger.info("[템플릿] %s: 이미 존재 → 건너뜀 (덮어쓰려면 overwrite=true)", path)
    # 지표 기준 설정 파일: 사용자 설정 보호를 위해 overwrite 여도 덮어쓰지 않고, 없을 때만 생성
    if read_repo_text(repo, LIMITS_PATH)[0] is None:
        repo.create_file(LIMITS_PATH, "init limits config", default_limits_json())
        logger.info("[설정] %s: 기본값으로 새로 생성", LIMITS_PATH)
    else:
        logger.info("[설정] %s: 이미 존재 → 유지 (덮어쓰지 않음)", LIMITS_PATH)


def run_candles_check(args, now_kst):
    """일봉 조회 점검: 지정 종목에 대해 NH 시세와 야후 일봉을 바로 시험하고 결과를 요약한다(공개 시세 + 오류 코드만 출력).
    미국은 시장구분(market_cls) 값을 바꿔가며 어떤 값에서 일봉이 오는지도 확인한다."""
    code = (args.input or "").strip().upper()
    if not code:
        print("종목코드/티커가 필요합니다. 워크플로 입력 'input' 에 입력하세요. (예: CRWD, 005930)")
        return
    market = args.market or ("KR" if code.isdigit() or (len(code) == 6 and code[:1].isdigit()) else "US")
    end8 = args.date or now_kst.strftime("%Y%m%d")
    print(f"=== 일봉 조회 점검: {market} {code} (기준일 {end8}) ===")
    if not (NH_APP_KEY and NH_APP_SECRET):
        print("- NH 인증 정보가 없어 NH 조회는 건너뜁니다.")
    best, nh_cd = None, None
    variants = [None] if market == "KR" else ["1", "2", "3"]
    for v in (variants if (NH_APP_KEY and NH_APP_SECRET) else []):
        tag = "NH" + (f" market_cls={v}" if v else "")
        try:
            cd = _fetch_candles_nh(market, code, end8, CHART_FETCH, **({"us_market_cls": v} if v else {}))
        except Exception as e:
            print(f"- {tag}: 예외 {type(e).__name__}: {mask(e)}")
            continue
        if cd:
            print(f"- {tag}: 성공 — {len(cd)}봉, 마지막 {cd[-1]['date']} 종가 {cd[-1]['close']:g}")
            best = best or cd
            nh_cd = nh_cd or cd
        else:
            print(f"- {tag}: 실패 — {LAST_CANDLE_ERR.get((market, code), '원인 미상')}")
    for sym in _yf_symbols(market, code):
        yc = [c for c in fetch_yf_candles(sym) if c["date"] <= end8][-CHART_FETCH:]
        time.sleep(0.2)
        print(f"- 야후 {sym}: " + (f"성공 — {len(yc)}봉, 마지막 {yc[-1]['date']} 종가 {yc[-1]['close']:g}" if yc else "데이터 없음"))
        if yc and nh_cd:  # NH 일봉과 야후 일봉의 종가 교차 비교(단위·수정주가 차이 확인)
            ref = {c["date"]: c["close"] for c in yc}
            both = [(c["date"], c["close"], ref[c["date"]]) for c in nh_cd if c["date"] in ref]
            diff = [b for b in both if abs(b[1] / b[2] - 1) > 0.005]
            print(f"    NH·야후 겹치는 {len(both)}일 중 종가 0.5% 이상 차이 {len(diff)}일" + (f" (예: {diff[-1][0]} NH {diff[-1][1]:g} / 야후 {diff[-1][2]:g})" if diff else ""))
        if yc and not best and len(yc) >= 5:
            best = yc
    if best:
        px = best[-1]["close"]
        demo = {"no": 1, "start": iso_date(best[-1]["date"]), "end": None, "pnl": 0.0, "cost": 0.0, "orphan": False,
                "fills": [{"date": iso_date(best[-1]["date"]), "side": "매수", "qty": 1.0, "price": px, "account": "-", "name": code, "qty_after": 1.0, "avg": px}]}
        svg = build_cycle_chart_svg(f"{code} 점검", best, "KRW" if market == "KR" else "USD", end8, "check", demo)
        print(f"- 차트 SVG 생성: {'성공' if svg.startswith('<svg') else '실패'}")
    else:
        print("- 사용 가능한 일봉이 없어 차트를 만들 수 없습니다. 종목코드/기준일을 확인하세요.")


def run_fold_meta(args, now_kst):
    """기존 시황/일지 파일의 맨 위 메타데이터를 설정(journal_meta)에 맞게 일괄 변환. --apply 없으면 대상 건수만 출력."""
    repo = get_data_repo()
    todo = []
    for d in (MARKET_DIR, STOCK_DIR):
        try:
            items = repo.get_contents(d)
        except Exception as e:
            logger.warning("%s 목록 조회 실패: %s", d, mask(e))
            continue
        todo += [it.path for it in items if getattr(it, "type", "file") == "file" and it.path.endswith(".md")]
    changed = 0
    for path in sorted(todo):
        text, sha = read_repo_text(repo, path)
        if text is None:
            continue
        new = apply_meta_layout(text)
        if new == text:
            continue
        changed += 1
        if args.apply:
            repo.update_file(path, f"Meta layout: {LIMITS['journal_meta']}", new, sha)
            time.sleep(0.2)
    logger.info("메타데이터 레이아웃(%s): 전체 %d개 중 변환 %s %d개", LIMITS["journal_meta"], len(todo), "완료" if args.apply else "대상", changed)
    if not args.apply and changed:
        logger.info("실제로 변환하려면 apply 를 켜고 다시 실행하세요.")


def run_market_check(args, now_kst):
    """시황 지표 점검: 심볼별 최근 일봉·meta·보충/제외 여부·기대 거래일을 출력(공개 시세만)."""
    print(f"=== 시황 지표 점검 (현재 {now_kst.strftime('%Y-%m-%d %H:%M')} KST) ===")
    for name, sym in YF_SYMBOLS.items():
        dates, closes, vols, off = fetch_yf_history(sym, "1y", quiet=True)
        time.sleep(0.2)
        if not dates:
            print(f"- {name} ({sym}): 데이터 없음")
            continue
        raw_last = dates[-1]
        dates2, closes2, vols2, dropped = _drop_open_bar(name, dates, closes, vols, off, now_kst)
        patched = False
        if not dropped and name in INDEX_NAMES:
            dates2, closes2, vols2, patched = _patch_stale_bar(name, sym, dates2, closes2, vols2, now_kst)
        meta = YF_META.get(sym) or {}
        mt = meta.get("regularMarketTime")
        mt_s = datetime.fromtimestamp(mt + (meta.get("gmtoffset") or 0), timezone.utc).strftime("%m-%d %H:%M") if mt else "-"
        last3 = ", ".join(f"{d[5:]}={c:,.2f}" for d, c in list(zip(dates, closes))[-3:])
        exp = f" / 기대 {_expected_last_date(name, now_kst)[5:]}" if name in INDEX_NAMES else ""
        print(f"- {name} ({sym}): 최근 {last3} | meta {mt_s} {meta.get('regularMarketPrice')} | 사용 마지막 {dates2[-1][5:]}"
              f"{' (장중 봉 제외)' if dropped else ''}{' (meta 보충)' if patched else ''}{exp} | 원본 마지막 {raw_last[5:]}")


def run_orders_check(args, now_kst):
    """체결 조회 응답 구조 점검. 금액/수량/계좌번호 값은 출력하지 않고 필드명·건수·플래그만 보여준다.
    미국은 해외주식수량구분코드(ove_qty_dit_cd)를 바꿔가며 건수가 달라지는지도 확인한다(명세상 값 설명 없음)."""
    if not (NH_APP_KEY and NH_APP_SECRET and ACCOUNTS):
        print("NH 인증 정보/계좌 시크릿이 없습니다.")
        return
    for market in ([args.market] if args.market else ["KR", "US"]):
        target = resolve_target_date(now_kst, market, args.date)
        qk, pk = ("cns_qty", "cns_pr") if market == "US" else ("tot_cns_qty", "cns_avg_uit_pr")
        print(f"\n=== {market} 주문/체결 구조 점검 (조회일 {target}) ===")
        for acc_name, acc_no in ACCOUNTS.items():
            if market == "US" and acc_name.upper() in NO_US_ACCOUNTS:
                continue
            for variant in ([None, "0", "1", "2"] if market == "US" else [None]):
                body = _order_body(market, acc_no, target)
                body["Input_0"]["ost_cns_dit"] = "0"  # 체결+미체결 전체
                if variant is not None:
                    body["Input_0"]["ove_qty_dit_cd"] = variant
                tag = f"{acc_name} {market}" + (f" 수량구분={variant}" if variant is not None else "")
                try:
                    resps = post_pages(US_PATH if market == "US" else KR_PATH, body, max_pages=3)
                except Exception as e:
                    print(f"- {tag}: 실패 ({mask(e)})")
                    continue
                bad = next((r for r in resps if r.status_code != 200), None)
                if bad is not None:
                    print(f"- {tag}: {_err_detail(bad)}")
                    continue
                items = [it for r in resps for it in _as_list(r.json().get("Output_0"))]
                filled = [it for it in items if to_float(it.get(qk)) > 0]
                frac = [it for it in filled if to_float(it.get(qk)) % 1 != 0]
                zero_px = [it for it in items if to_float(it.get(qk)) == 0 and to_float(it.get(pk)) > 0]
                mism = sum(1 for it in items if len(re.sub(r"\D", "", str(it.get("orr_dt") or ""))[:8]) == 8 and re.sub(r"\D", "", str(it.get("orr_dt")))[:8] != target)
                print(f"- {tag}: 행 {len(items)} / 체결수량>0 {len(filled)} / 그중 소수 {len(frac)} / 수량0·체결가>0 {len(zero_px)}"
                      + (f" / 주문일자≠조회일 {mism}" if market == "US" else ""))
                if items and variant in (None, "0"):
                    print("    필드:", ", ".join(sorted(items[0].keys())))
                    dkeys = sorted(k for k in items[0] if re.search(r"(dt|date)$", k))
                    for k in dkeys:
                        print(f"    {k} 분포:", dict(Counter(str(it.get(k) or "-") for it in items)))
                    if market == "US":
                        names = Counter(str(it.get("orr_sts_nm") or "-") for it in items)
                        print("    주문상태 분포:", dict(names))


PORTFOLIO_PATH = "private/portfolio/latest.json"


def portfolio_enabled():
    return os.getenv("PORTFOLIO_SNAPSHOT_ENABLED", "").lower() == "true"


def load_portfolio_market(repo, commit):
    """Read immutable market artifacts, verifying latest files before use."""
    root = "watchlist/published/"
    raw, _ = read_repo_text(repo, root + "latest/manifest.json", ref=commit)
    manifest = json.loads(raw or "{}")
    result = {"manifest": manifest, "summary": {}, "candidates": pd.DataFrame(), "watch": pd.DataFrame(), "events": []}
    if str(manifest.get("schema_version", "")).split(".")[0] != "1":
        return result
    for filename, key in (("summary.json", "summary"), ("candidates.csv", "candidates"), ("watchlist.csv", "watch")):
        raw, _ = read_repo_text(repo, root + "latest/" + filename, ref=commit)
        expected = (manifest.get("files", {}).get(filename) or {}).get("sha256")
        if not raw or not expected or hashlib.sha256(raw.encode("utf-8")).hexdigest() != expected:
            raise ValueError("시장 데이터 무결성 오류")
        result[key] = json.loads(raw) if filename.endswith(".json") else pd.read_csv(StringIO(raw), dtype={"ticker": str})
    raw, _ = read_repo_text(repo, root + "history/signals.csv", ref=commit)
    if raw:
        events = pd.read_csv(StringIO(raw), dtype=str, keep_default_na=False).to_dict("records")
        runs = {}
        events.sort(key=lambda e: int(e["workflow_run_id"]) if str(e.get("workflow_run_id", "")).isdigit() else 0, reverse=True)
        for event in events:
            if event.get("published_at"):
                continue  # Explicitly recorded publication metadata, not a guessed date.
            run_id = event.get("workflow_run_id", "")
            if not str(run_id).isdigit():
                continue
            if run_id not in runs:
                if len(runs) >= 100:
                    continue  # Bound API work; unresolved signals stay unmatchable.
                runs[run_id] = None
                try:
                    run = repo.get_workflow_run(int(run_id))
                    started = portfolio._utc_timestamp(run.created_at)
                    completed = portfolio._utc_timestamp(run.updated_at)
                    if (run.status == "completed" and run.conclusion == "success"
                            and run.path.split("@")[0] == ".github/workflows/watchlist.yml"
                            and started is not None and completed is not None and started <= completed):
                        runs[run_id] = (started, completed)
                except Exception:
                    pass  # Missing Actions read access cannot establish publication.
            generated = portfolio._utc_timestamp(event.get("generated_at"))
            bounds = runs.get(run_id)
            if bounds and generated is not None and bounds[0] <= generated <= bounds[1]:
                event["published_at"] = bounds[1].isoformat()
                event["availability_source"] = "successful_watchlist_workflow_completed"
        result["events"] = events
    return result


def private_allocations(snapshot, market, cfg, watchlist, now):
    budget = portfolio.held_budget(snapshot)
    manifest, summary = market["manifest"], market["summary"]
    try:
        published = portfolio._utc_timestamp(manifest.get("generated_at"))
        if published is None or not 0 <= (pd.Timestamp(now).tz_convert("UTC") - published).total_seconds() <= 86400:
            budget["reasons"].append("market_snapshot_stale")
        if manifest.get("config_hash") != watchlist.get_config_version_hash(cfg):
            budget["reasons"].append("market_policy_mismatch")
        for regime in summary.get("regimes", {}).values():
            age = (now.date() - pd.Timestamp(regime["as_of"]).date()).days
            if not 0 <= age <= 4:
                budget["reasons"].append("regime_stale")
    except (ValueError, TypeError, KeyError):
        budget["reasons"].append("market_unconfirmed")
    budget["complete"] = budget["complete"] and not budget["reasons"]
    actions = market["watch"].copy()
    if actions.empty:
        return {"status": "no_candidates" if budget["complete"] else "blocked",
                "reasons": budget["reasons"], "rows": [], "total_alloc_pct": 0.0}
    # Published stop_dist_pct is the same unclipped risk distance used by features.
    actions["raw_stop_dist_pct"] = actions["stop_dist_pct"]
    allocated, meta = watchlist.allocate_portfolio_risk_budget(actions, summary.get("regimes", {}), cfg, held=budget)
    columns = [c for c in ("market", "ticker", "sector", "macro_theme", "portfolio_alloc_pct",
                           "portfolio_risk_contrib_pct", "portfolio_alloc_note") if c in allocated]
    return {**meta, "rows": allocated[columns].to_dict("records")}


def save_portfolio_snapshot(repo, now, context=None, *, apply=True):
    """Opt-in, private repositories only. Never write the ledger or publish account data."""
    if repo.private is not True:
        raise ValueError("포트폴리오는 비공개 저장소에서만 저장할 수 있습니다")
    commit = repo.get_branch(repo.default_branch).commit.sha
    text, ledger_sha = read_repo_text(repo, LEDGER_PATH, ref=commit)
    if not text:
        raise ValueError("원장이 없어 포트폴리오를 생성할 수 없습니다")
    ledger = pd.read_csv(StringIO(text), dtype=str, keep_default_na=False)
    hc = context if context is not None else holdings_context(repo, now, with_links=False)
    quotes, diagnostics = {}, {}
    names = {}
    for row in hc['rows']:
        key = (row['market'], row['code'])
        names[key] = portfolio.security_name(row['code'], row.get('name'), names.get(key))
    # All ledger holdings remain visible, even when absent from candidate lists.
    for (market, code), info in hc["tech"].items():
        if info:
            quotes[(market, code)] = {"price": info.get("close"), "as_of": info.get("as_of"), "source": info.get("source")}
            diagnostics[(market, code)] = {k: info.get(k) for k in ("status", "vs50", "dist", "rs", "rs_bench", "trail")}
    # Backend-only reuse: diagnosis of held symbols is independent of selection.
    from watchlist import trend_watchlist as watchlist
    cfg = watchlist.load_config()
    history = portfolio.trade_analytics(portfolio.normalize_ledger(ledger), [])
    closed = [c for c in history["cycles"] if c["end"] and not c["quality"]
              and 0 <= (now.date() - pd.Timestamp(c["end"]).date()).days <= 90]
    closed.sort(key=lambda c: c["end"], reverse=True)
    # Limit API work; rows beyond the cap still appear as data_unknown.
    for c in closed[:20]:
        diagnostics.setdefault((c["market"], c["code"]), {})
    bench_cache = {}
    def frame(rows, market):
        if not rows:
            return pd.DataFrame()
        f = pd.DataFrame(rows).rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close", "vol": "Volume"})
        f.index = pd.to_datetime(f.pop("date"))
        return watchlist.trim_incomplete_bar(f, market, True)
    for market, code in list(diagnostics):
        try:
            hist = pd.DataFrame()
            benchmark = "SPY"
            for symbol in _yf_symbols(market, code):
                hist = frame(fetch_yf_candles(symbol), market)
                if not hist.empty:
                    if market == "KR":
                        benchmark = "^KQ11" if symbol.endswith(".KQ") else "^KS11"
                    break
            if benchmark not in bench_cache:
                bench_cache[benchmark] = frame(fetch_yf_candles(benchmark), market)
            features = watchlist.compute_features(hist, bench_cache[benchmark], cfg) if not hist.empty and not bench_cache[benchmark].empty else None
            if features:
                diagnostics[(market, code)].update({k: features.get(k) for k in ("exit_signal", "sma10", "sma20", "sma50", "sma200", "is_stale_price", "as_of", "is_actionable", "is_breakout", "base_len_days", "setup_stage")})
                d = diagnostics[(market, code)]
                n = int(features.get("base_len_days", 0))
                d["setup_started_at"] = hist.index[-n].strftime("%Y-%m-%d") if 0 < n <= len(hist) else None
                previous = watchlist.compute_features(hist.iloc[:-1], bench_cache[benchmark], cfg)
                d["new_breakout"] = bool(features.get("is_breakout") and previous is not None and not previous.get("is_breakout"))
                # Closed symbols also need a dated quote for reentry checks.
                quotes[(market, code)] = {"price": float(hist["Close"].iloc[-1]),
                                          "as_of": features["as_of"], "source": "Yahoo daily"}
                diagnostics[(market, code)]["rs_score"] = None
                diagnostics[(market, code)]["advanced_status"] = "stale" if features.get("is_stale_price") else "computed"
            else:
                diagnostics[(market, code)]["advanced_status"] = "insufficient_history"
        except Exception:
            diagnostics[(market, code)]["advanced_status"] = "unavailable"

    market_source = {"commit": commit}
    market = {"manifest": {}, "summary": {}, "candidates": pd.DataFrame(), "watch": pd.DataFrame(), "events": []}
    try:
        market = load_portfolio_market(repo, commit)
        manifest = market["manifest"]
        market_source.update(run_id=manifest.get("run_id"), as_of=manifest.get("as_of"))
        for row in market["candidates"].to_dict("records"):
            key = (str(row.get("market")), str(row.get("ticker")))
            names[key] = portfolio.security_name(key[1], names.get(key), row.get('name'))
            if key in diagnostics:
                fields = ("rs_score", "sector", "macro_theme", "is_leading_sector") if str(row.get("as_of")) == diagnostics[key].get("as_of") else ("sector", "macro_theme")
                diagnostics[key].update({k: (None if pd.isna(row.get(k)) else row.get(k)) for k in fields})
                diagnostics[key]["published_as_of"] = row.get("as_of")
    except Exception:
        market_source["status"] = "unavailable"
    fxd, fxc, _, _ = fetch_yf_history("KRW=X", "5d", quiet=True)
    recon = {"status": "unavailable" if hc["errs"] else "checked", "checked_at": now.isoformat(),
             "mismatch_count": len(reconcile_holdings(ledger, hc["rows"], HOLDINGS_OK)) if not hc["errs"] else None}
    if not LIMITS["reconcile_enabled"] or hc["ledger"] is None or not HOLDINGS_OK:
        recon["status"] = "not_checked"
    snapshot = portfolio.build_snapshot(ledger, generated_at=now.isoformat(), ledger_sha=ledger_sha,
        quotes=quotes, levels=hc["levels"], diagnostics=diagnostics,
        fx=fxc[-1] if fxc else None, fx_as_of=fxd[-1] if fxd else None,
        equity_krw=LIMITS["equity_krw"], market_source=market_source,
        reconciliation=recon, limits=LIMITS, signal_events=market["events"],
        equity_as_of=os.getenv("PORTFOLIO_EQUITY_AS_OF"), names=names)
    snapshot["reentry"] = portfolio.reentry_watch(snapshot, diagnostics, market["summary"].get("regimes", {}))
    snapshot["allocation"] = private_allocations(snapshot, market, cfg, watchlist, now)
    # Refuse to replace the snapshot if another writer changed the input meanwhile.
    _, current_sha = read_repo_text(repo, LEDGER_PATH)
    if current_sha != ledger_sha:
        raise ValueError("원장 버전 변경: 다음 실행에서 포트폴리오 재생성")
    if apply:
        upsert_file(repo, PORTFOLIO_PATH, "Update private portfolio snapshot",
                    json.dumps(watchlist._json_safe(snapshot), ensure_ascii=False, allow_nan=False))
    return snapshot


def run(args) -> int:
    failed = False
    portfolio_context = None
    now = datetime.now(KST)
    reset_run_caches()
    try:
        normalize_args(args)
    except ValueError as e:
        logger.error(str(e))
        return 1
    if args.mode != "check" and not DATA_TOKEN:
        logger.error("DATA_REPO_TOKEN / GITHUB_TOKEN 이 설정되지 않았습니다.")
        return 1
    try:
        if args.mode not in ("check", "init", "portfolio"):
            ensure_limits(get_data_repo())
        if args.mode == "init":
            run_init(get_data_repo(), args.overwrite)
        elif args.mode == "check":
            run_check(args, now)
        elif args.mode == "portfolio":
            repo = get_data_repo()
            load_limits(repo)
            snapshot = save_portfolio_snapshot(repo, now, apply=args.apply)
            logger.info("포트폴리오 %s: 보유 그룹 %d건, 신규 배분 상태 %s",
                        "저장" if args.apply else "조회만(원장·스냅샷 변경 없음)",
                        len(snapshot["positions"]), snapshot["allocation"]["status"])
        elif args.mode == "manual":
            run_manual(args, now)
        elif args.mode == "backfill":
            run_backfill(args, now)
        elif args.mode == "rebuild":
            (run_fold_meta if args.target == "layout" else run_rebuild)(args, now)
        elif args.mode == "report":
            run_report(args, now)
        elif args.mode == "morning_prep":
            d_iso = iso_date(_digits(args.date)) if args.date else now.strftime("%Y-%m-%d")
            ensure_market_journal(get_data_repo(), d_iso, now, force=True)
        elif args.mode in ("hourly_check", "auto"):
            daily = None
            if args.mode == "auto" and now.weekday() < 5 and 6 <= now.hour <= 10:
                try:
                    daily = _JournalDailyCloses(now)
                    ensure_market_journal(get_data_repo(), now.strftime("%Y-%m-%d"), now, daily=daily)
                except Exception as e:  # 시황 실패가 체결 동기화를 막지 않도록 분리
                    logger.exception(e)
                    add_warning(f"시황 작성 오류: {type(e).__name__}")
            shared = {}
            for m in ([args.market] if args.market else ["KR", "US"]):
                try:
                    check_and_update_trades(now, m, args.date, shared)
                except Exception as e:  # 한 시장 실패가 다른 시장 동기화를 막지 않도록 분리
                    logger.exception(e)
                    add_warning(f"{m} 동기화 오류: {type(e).__name__}")
                    failed = True
            try:  # 손절·트레일링 알림은 부가 기능: 실패해도 동기화 결과에 영향 없음
                if portfolio_enabled():
                    portfolio_context = holdings_context(get_data_repo(), now, with_links=False)
                risk_check(get_data_repo(), now, context=portfolio_context, daily=daily if daily and daily.series else None)
            except Exception as e:
                logger.warning("위험 알림 확인 실패: %s", mask(e))
    except Exception as e:
        logger.exception(e)
        add_warning(f"실행 오류({args.mode}): {type(e).__name__}: {e}")
        failed = True

    eligible = args.mode in ("auto", "hourly_check") or (args.mode in ("manual", "backfill", "rebuild") and args.apply)
    if portfolio_enabled() and eligible and not failed:
        try:
            save_portfolio_snapshot(get_data_repo(), now, context=portfolio_context)
        except Exception:
            # Never log account data/remote response bodies or roll back saved trades.
            logger.warning("비공개 포트폴리오 생성 실패: 이전 스냅샷 유지")
            failed = True
    if RUN_WARNINGS and not (args.mode == "portfolio" and not args.apply):
        send_telegram_message("⚠️ <b>[실행 경고]</b>\n" + "\n".join(html.escape(w) for w in RUN_WARNINGS))
    return 1 if failed else 0


def run_check(args, now_kst):
    """진단 모드 분기. 원장/일지 파일은 바꾸지 않는다."""
    if args.target == "accounts":
        run_accounts(args)
    elif args.target == "orders":
        run_orders_check(args, now_kst)
    elif args.target == "candles":
        run_candles_check(args, now_kst)
    else:
        run_market_check(args, now_kst)


MODES = ["portfolio", "auto", "morning_prep", "hourly_check", "check", "manual", "backfill", "rebuild", "report", "init"]
CHECK_TARGETS = ("accounts", "orders", "candles", "market")
REBUILD_TARGETS = ("journals", "layout")
LEGACY_MODES = {"accounts": ("check", "accounts"), "orders_check": ("check", "orders"), "candles_check": ("check", "candles"),
                "market_check": ("check", "market"), "fold_meta": ("rebuild", "layout"), "repair_ledger": ("rebuild", "journals"),
                "manual_trade": ("manual", None), "init_templates": ("init", None)}


def normalize_args(args):
    """구버전 모드 이름을 새 이름으로 바꾸고, 모드별 필수 입력을 검증한다(잘못되면 ValueError)."""
    for name, default in (("market", None), ("date", None), ("date_end", None), ("target", None), ("input", None),
                          ("apply", False), ("overwrite", False), ("delete", False)):
        if not hasattr(args, name):
            setattr(args, name, default)
    if args.mode in LEGACY_MODES:
        new, target = LEGACY_MODES[args.mode]
        logger.warning("모드 '%s' 는 '%s'%s 로 바뀌었습니다.", args.mode, new, f" (target={target})" if target else "")
        args.mode, args.target = new, (args.target or target)
    if args.mode == "check":
        args.target = (args.target or "").strip().lower()
        if args.target not in CHECK_TARGETS:
            raise ValueError("check 모드는 target 이 필요합니다: " + " | ".join(CHECK_TARGETS))
    elif args.mode == "rebuild":
        args.target = (args.target or "journals").strip().lower()
        if args.target not in REBUILD_TARGETS:
            raise ValueError("rebuild 모드의 target 은 " + " | ".join(REBUILD_TARGETS) + " 중 하나입니다")
    return args


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Trading Journal Collector")
    p.add_argument("--mode", default="auto", choices=MODES + list(LEGACY_MODES), metavar="MODE")
    p.add_argument("--market", type=lambda s: s.strip().upper() or None, choices=["KR", "US", None])
    p.add_argument("--date")
    p.add_argument("--date-end", dest="date_end")
    p.add_argument("--target")
    p.add_argument("--input", "--code", "--trades", dest="input")   # --code/--trades 는 구버전 이름
    p.add_argument("--apply", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--delete", "--manual-delete", dest="delete", action="store_true")
    return p


if __name__ == "__main__":
    sys.exit(run(build_parser().parse_args()))

