"""Offline journal price and ledger replay. All identities and fills are synthetic."""
from collections import Counter
from datetime import datetime
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import Mock

import pytest

import collector_github_actions as journal
from watchlist.session_calendar import get_calendar

NOW = datetime(2026, 11, 3, 8, tzinfo=journal.KST)
SYMBOL = "SYNTH"


def response(symbol, market="US"):
    kr = market == "KR"
    sessions = get_calendar(market).sessions_in_range("2025-11-03", "2026-11-02")
    meta = dict(symbol=symbol, exchangeName="KSC" if kr else "NMS",
                instrumentType="INDEX" if symbol.startswith("^") else "EQUITY",
                currency="KRW" if kr else "USD", gmtoffset=32400 if kr else -18000,
                exchangeTimezoneName="Asia/Seoul" if kr else "America/New_York")
    meta["regularMarketTime"] = int(get_calendar(market).session_close(sessions[-1]).timestamp())
    quote = {"close": [100 + n / 10 for n in range(len(sessions))], "volume": [10] * len(sessions)}
    return {"meta": meta, "timestamp": [int(d.timestamp()) + (0 if kr else 15 * 3600) for d in sessions],
            "indicators": {"quote": [quote]}}


def serve(monkeypatch, docs):
    calls = Counter()
    def get(url, **kwargs):
        symbol = urlsplit(url).path.rsplit("/", 1)[-1]
        calls[symbol] += 1
        return SimpleNamespace(status_code=200, json=lambda: {"chart": {"result": [docs[symbol]]}})
    monkeypatch.setattr(journal.HTTP, "get", get)
    monkeypatch.setattr(journal.time, "sleep", lambda _: None)
    monkeypatch.setattr(journal, "YF_META", {})
    monkeypatch.setattr(journal, "_BENCH_CACHE", {})
    return calls


def test_morning_and_post_sync_risk_replay(monkeypatch):
    name = journal.LIMITS["rs_benchmark_us"]
    benchmark = journal.YF_SYMBOLS[name]
    docs = {symbol: response(symbol) for symbol in (SYMBOL, benchmark)}
    calls = serve(monkeypatch, docs)
    monkeypatch.setattr(journal, "YF_SYMBOLS", {name: benchmark})
    def replay(daily):
        health = journal.analyze_market_data(NOW, daily=daily)
        rows = [{"market": "US", "code": SYMBOL}]
        return (health, journal.enrich_holdings(rows, NOW, daily=daily),
                journal.enrich_holdings(rows, NOW, since={("US", SYMBOL): "2026-10-01"}, daily=daily))
    original = replay(None)
    assert calls == {benchmark: 3, SYMBOL: 2}
    calls.clear()
    assert replay(journal._JournalDailyCloses(NOW)) == original
    assert calls == {benchmark: 1, SYMBOL: 1}


@pytest.mark.parametrize("bad", ["delay", "hole", "short", "price", "volume", "currency", "identity", "venue", "zone", "intraday_time"])
def test_ineligible_series_refetched(monkeypatch, bad):
    doc = response(SYMBOL)
    if bad in ("delay", "hole", "short"):
        cut = {"delay": -1, "hole": 50, "short": slice(0, 100)}[bad]
        for field in (doc["timestamp"], *doc["indicators"]["quote"][0].values()):
            del field[cut]
    elif bad in ("price", "volume"):
        doc["indicators"]["quote"][0]["close" if bad == "price" else "volume"][40] = None
    elif bad == "intraday_time":
        doc["meta"]["regularMarketTime"] -= 3600
    else:
        doc["meta"][{"currency": "currency", "identity": "symbol", "venue": "exchangeName", "zone": "exchangeTimezoneName"}[bad]] = "unknown"
    docs = {SYMBOL: doc}
    calls = serve(monkeypatch, docs)
    daily = journal._JournalDailyCloses(NOW)
    read = lambda: journal._holding_closes("US", SYMBOL, "S&P 500", NOW, daily)
    read()
    docs[SYMBOL] = response(SYMBOL)
    good = read()
    assert tuple(map(tuple, good)) == read()
    assert calls[SYMBOL] == 2


def test_run_isolation_expiry_and_ohlc_boundary(monkeypatch):
    doc = response(SYMBOL)
    calls = serve(monkeypatch, {SYMBOL: doc})
    clock = Mock(return_value=10)
    monkeypatch.setattr(journal.time, "monotonic", clock)
    first, concurrent = (journal._JournalDailyCloses(NOW) for _ in range(2))
    def read(scope):
        return journal._holding_closes("US", SYMBOL, "S&P 500", NOW, scope)
    read(first)
    assert first.get("KR", SYMBOL) is None
    assert concurrent.get("US", SYMBOL) is None
    # Close-only is never substituted for chart OHLC, including on a cache hit.
    assert journal.fetch_yf_candles(SYMBOL) == []
    assert calls[SYMBOL] == 1 + len(journal.YF_HOSTS)
    doc["indicators"]["quote"][0]["close"][-1] = 7
    assert read(concurrent)[1][-1] == 7
    assert read(first)[1][-1] != 7
    clock.return_value += 300
    assert read(first)[1][-1] == 7
    assert calls[SYMBOL] == 3 + len(journal.YF_HOSTS)


def test_updated_ledger_and_stop_are_read_again_after_sync(monkeypatch, tmp_path):
    import pandas as pd

    ledger = pd.DataFrame([dict(account="fixture", code=SYMBOL, currency="USD", date="2026-10-01",
                                tx_type="매수", qty=1, unit_price=100, name=SYMBOL)])
    rows = [dict(account="fixture", code=SYMBOL, currency="USD", market="US", name=SYMBOL, qty=1)]
    file = SimpleNamespace(name="2026-10-01_SYNTH_SYNTH_매수.md", path="synthetic.md", sha="one")
    repo = Mock()
    repo.get_contents.return_value = [file]
    balances = Mock(return_value=(rows, []))
    cycles = Mock(wraps=journal.compute_cycles)
    text = Mock(return_value=("stop: 91\n", "one"))
    monkeypatch.setattr(journal, "_FILES_CACHE", {})
    monkeypatch.setattr(journal, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "read_repo_text", text)
    monkeypatch.setattr(journal, "fetch_holdings", balances)
    monkeypatch.setattr(journal, "compute_cycles", cycles)
    monkeypatch.setattr(journal, "load_ledger", lambda repo: (ledger.copy(), "head"))
    monkeypatch.setattr(journal, "enrich_holdings", lambda *a, **kw: {})
    for expected in (91, 95):
        ctx = journal.holdings_context(repo, NOW)
        assert ctx["levels"][SYMBOL, "USD"]["stop"] == expected
        assert ctx["links"][SYMBOL, "USD"]
        file.sha = "two"
        text.return_value = ("stop: 95\n", "two")
    assert cycles.call_count == balances.call_count == repo.get_contents.call_count == 2


def test_calendar_failure_and_provider_outage_do_not_reuse_prices(monkeypatch):
    from watchlist import session_calendar
    doc = response(SYMBOL)
    calls = serve(monkeypatch, {SYMBOL: doc})
    daily = journal._JournalDailyCloses(NOW)
    read = lambda: journal._holding_closes("US", SYMBOL, "S&P 500", NOW, daily)
    initial = read()
    monkeypatch.setattr(session_calendar, "get_calendar", Mock(side_effect=ValueError("coverage")))
    assert read() == initial and calls[SYMBOL] == 2
    broken = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(journal.HTTP, "get", broken)
    assert read() == ([], [], [])
    assert broken.call_count == len(journal.YF_HOSTS)


def test_chart_failure_after_fill_save_is_retryable(monkeypatch, tmp_path):
    import pandas as pd

    row = dict(account="fixture", code=SYMBOL, currency="USD", date="2026-11-02", time="10:00",
               tx_type="매수", qty=1, unit_price=100, total_amt=100, name=SYMBOL,
               fee=0, tax=0, trade_id="synthetic-fill", order_no="o1")
    fill = pd.DataFrame([row], columns=journal.LEDGER_COLUMNS)
    repo = object()
    monkeypatch.setattr(journal, "CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(journal, "RUN_WARNINGS", [])
    for key in ("NH_APP_KEY", "NH_APP_SECRET"):
        monkeypatch.setattr(journal, key, "fixture")
    monkeypatch.setattr(journal, "ACCOUNTS", {"fixture": "synthetic"})
    monkeypatch.setattr(journal, "fetch_trades_for_market", lambda *args: ({"fixture": fill}, "USD"))
    state = {"repo": repo, "ledger": fill.iloc[:0], "sha": "before"}
    save = Mock(return_value="after")
    monkeypatch.setattr(journal, "save_ledger", save)
    notify = Mock()
    monkeypatch.setattr(journal, "send_telegram_message", notify)
    render = Mock(side_effect=[RuntimeError("offline"), None])
    monkeypatch.setattr(journal, "update_stock_journals", render)
    with pytest.raises(RuntimeError, match="offline"):
        journal.check_and_update_trades(NOW, "US", shared=state)
    assert state["sha"] == "after"
    assert state["ledger"].trade_id.tolist() == ["synthetic-fill"]
    assert journal._read_cache("journal_pending_US_20261102") == {"pending": True}
    for _ in range(2):
        journal.check_and_update_trades(NOW, "US", shared=state)
    assert save.call_count == notify.call_count == 1
    assert render.call_count == 2
    assert journal._read_cache("journal_pending_US_20261102") == {"pending": False}
