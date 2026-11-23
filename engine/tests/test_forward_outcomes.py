"""Fixed-cache tests: no broker, price download, or GitHub calls."""
from copy import deepcopy
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd
import pytest

from watchlist import trend_watchlist as engine
from watchlist.analysis_report import CALCULATION_VERSION, _availability


def prices(dates, start=100.):
    d=pd.DataFrame(index=pd.to_datetime(dates))
    d['Open']=start;d['Close']=start+np.arange(len(d));d['High']=d['Close']+2;d['Low']=start-2
    d['_base_price_basis']='yfinance:auto_adjust=True'
    d['_session_finality_version']=2
    d['_session_confirmed']=True
    d['_raw_open_valid']=True;d['_raw_ohlc_valid']=True
    return d


@pytest.fixture
def cached(tmp_path,monkeypatch):
    (tmp_path/'history').mkdir();(tmp_path/'prices').mkdir()
    monkeypatch.setattr(engine,'US_HISTORY_CACHE_DIR',tmp_path/'prices')
    monkeypatch.setattr(engine,'KR_HISTORY_CACHE_DIR',tmp_path/'prices')
    class DefaultClock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,11,4,12,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(engine,'datetime',DefaultClock)
    signal=dict(first_seen='2026-10-01',as_of='2026-10-01',market='US',sub_market='SP500',ticker='EX',
                setup_stage='VCP',stop_price=90.,signal_close=100.)
    def run(stock,benchmark=None,**changes):
        row={**signal,**changes}
        pd.DataFrame([row]).to_csv(tmp_path/'history/signals.csv',index=False)
        if stock is not None:stock.to_pickle(tmp_path/'prices'/f"{str(row['ticker']).zfill(6) if row['market']=='KR' else row['ticker']}.pkl")
        monkeypatch.setattr(engine,'_make_bench_getter',lambda cfg:lambda *args: stock if benchmark is None else benchmark)
        engine.update_signal_outcomes(tmp_path,deepcopy(engine.DEFAULT_CONFIG))
        return pd.read_csv(tmp_path/'history/signal_outcomes.csv').iloc[0]
    return tmp_path,run


@pytest.mark.parametrize('bars',[0,5,20])
def test_signal_day_next_cached_session_and_distinct_horizon_maturity(cached,bars):
    _,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=bars+1))
    r=run(stock)
    if not bars:assert r['status']=='대기' and r['bars']==0;return
    assert r['entry_date']=='2026-10-02' and r['bars']==bars
    assert r['calculation_version']==CALCULATION_VERSION and r['entry_price_source']=='open'
    assert r['ret_5d']==pytest.approx(5.-.3)
    assert pd.isna(r['ret_20d']) if bars==5 else r['ret_20d']==pytest.approx(20.-.3)
    assert r['stop_price_basis']=='matched'


def test_benchmark_uses_the_stocks_actual_exit_date_when_sessions_are_missing(cached):
    _,run=cached
    stock=prices(['2026-10-01','2026-10-02','2026-10-05','2026-10-07','2026-10-08','2026-10-09'])
    benchmark=prices(pd.bdate_range('2026-10-01',periods=7))
    benchmark['Close']=[100,101,102,103,104,105,125]
    r=run(stock,benchmark)
    assert r['bars']==2 and pd.isna(r['eval_date_5d'])
    assert pd.isna(r['ex_5d'])
    assert not bool(r['calendar_complete_5d'])  # Missing stock bars cannot certify five market sessions.


@pytest.mark.parametrize('missing',['exit_date','open'])
def test_missing_benchmark_date_or_entry_open_stays_unknown(cached,missing):
    _,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=6));benchmark=stock.copy()
    if missing=='exit_date':benchmark=benchmark.iloc[:-1]
    else:benchmark=benchmark.drop(columns='Open')
    r=run(stock,benchmark)
    assert pd.notna(r['ret_5d']) and pd.isna(r['ex_5d'])


@pytest.mark.parametrize('gap,expected',[(True,-15.3),(False,-10.3)])
def test_gap_and_intraday_stop_use_existing_fill_rules_and_costs(cached,gap,expected):
    _,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=11))
    stock.iloc[2,stock.columns.get_loc('Low')]=80 if gap else 89
    if gap:stock.iloc[2,stock.columns.get_loc('Open')]=85
    r=run(stock)
    assert r['stop_aware_ret_10d']==pytest.approx(expected)
    assert r['stop_aware_r_10d']==pytest.approx(expected/10.)
    assert r['stop_exit_date']=='2026-10-05'


def test_holiday_uses_actual_cached_sessions_without_inventing_thanksgiving_prices(cached,monkeypatch):
    _,run=cached
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,12,4,12,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(engine,'datetime',Clock)
    stock=prices(['2026-11-25','2026-11-27','2026-11-30','2026-12-01','2026-12-02','2026-12-03'])
    r=run(stock,as_of='2026-11-25',first_seen='2026-11-25')
    assert r['entry_date']=='2026-11-27' and r['eval_date_5d']=='2026-12-03'


@pytest.mark.parametrize('market,date,utc_hour,utc_minute,closed',[
    ('US','2026-07-01',20,4,False),('US','2026-07-01',20,6,True),
    ('US','2026-11-03',21,4,False),('US','2026-11-03',21,6,True),
    ('KR','2026-11-03',6,34,False),('KR','2026-11-03',6,36,True)])
def test_only_confirmed_daily_bars_are_evaluated_in_each_timezone(cached,monkeypatch,market,date,utc_hour,utc_minute,closed):
    _,run=cached;day=datetime.strptime(date,'%Y-%m-%d')
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(day.year,day.month,day.day,utc_hour,utc_minute,tzinfo=timezone.utc).astimezone(tz)
    monkeypatch.setattr(engine,'datetime',Clock)
    stock=prices([day-timedelta(days=1),day,day+timedelta(days=1)])
    stock["_base_price_basis"] = engine.OBSERVATION_BASIS[market]
    r=run(stock,market=market,ticker='123456' if market=='KR' else 'EX',
          sub_market='KOSPI' if market=='KR' else 'SP500',as_of=(day-timedelta(days=1)).strftime('%Y-%m-%d'))
    assert r['bars']==int(closed)  # Both an unfinished bar and a future cached bar are excluded.


@pytest.mark.parametrize('day,before,after',[('2026-03-09','13:29','13:31'),('2026-11-02','14:29','14:31')])
def test_publication_bound_respects_the_us_dst_session_open(day,before,after):
    row=dict(market='US',first_seen=day,entry_date=day)
    assert _availability({**row,'published_at':f'{day}T{before}:00Z'})[1] is True
    assert _availability({**row,'published_at':f'{day}T{after}:00Z'})[0]=='entry_before_signal'


def test_same_day_repeat_missing_cache_and_completed_twenty_day_rows_are_preserved(cached,monkeypatch):
    root,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=21))
    run(stock);path=root/'history/signal_outcomes.csv';original=path.read_bytes()
    def no_price_io(*a,**k):raise AssertionError('completed events must not reload price caches')
    monkeypatch.setattr(pd,'read_pickle',no_price_io)
    run(None)
    assert path.read_bytes()==original
    assert len(pd.read_csv(path))==1


def test_repeat_of_a_live_signal_updates_one_row_without_creating_another_entry(cached):
    root,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=6))
    run(stock);path=root/'history/signal_outcomes.csv';original=path.read_bytes()
    run(stock)
    assert path.read_bytes()==original and len(pd.read_csv(path))==1


@pytest.mark.parametrize('track,market,cache_name,value',[
    ('KOSPI','KR','idx_1001.pkl',101.),('KOSDAQ','KR','idx_2001.pkl',102.),
    ('SP500','US','SPY.pkl',103.),('NASDAQ100','US','QQQ.pkl',104.)])
def test_existing_market_benchmark_mapping_is_reused(cached,track,market,cache_name,value):
    root,_=cached;b=prices(['2026-10-01']);b['Close']=value;b.to_pickle(root/'prices'/cache_name)
    got=engine._make_bench_getter(deepcopy(engine.DEFAULT_CONFIG))(track,market)
    assert got['Close'].iloc[0]==value


def test_multiple_events_for_one_symbol_share_one_price_cache_read(cached,monkeypatch):
    root,_=cached;stock=prices(pd.bdate_range('2026-10-01',periods=21));stock.to_pickle(root/'prices/EX.pkl')
    pd.DataFrame([dict(first_seen=date,as_of=date,market='US',sub_market='SP500',ticker='EX',stop_price=90.)
                  for date in ('2026-10-01','2026-10-20')]).to_csv(root/'history/signals.csv',index=False)
    actual=pd.read_pickle;reads=[]
    def read(path):reads.append(path);return actual(path)
    monkeypatch.setattr(pd,'read_pickle',read)
    monkeypatch.setattr(engine,'_make_bench_getter',lambda cfg:lambda *args:stock)
    engine.update_signal_outcomes(root,deepcopy(engine.DEFAULT_CONFIG))
    assert len(reads)==1 and len(pd.read_csv(root/'history/signal_outcomes.csv'))==2


def test_changed_adjustment_basis_is_flagged_without_rebasing_the_historical_stop(cached):
    _,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=11),start=99.)
    r=run(stock,signal_close=100.)
    assert r['stop_price_basis']=='changed' and r['stop_price']==90.


def test_missing_open_fallback_is_disclosed_and_missing_cache_does_not_erase_prior_progress(cached):
    root,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=6)).drop(columns='Open')
    r=run(stock)
    assert pd.isna(r['entry_price']) and r['status']=='진입일 원본 시가 미확보'
    original=(root/'history/signal_outcomes.csv').read_bytes()
    (root/'prices/EX.pkl').unlink();run(None)
    assert (root/'history/signal_outcomes.csv').read_bytes()==original


def test_corrupt_outcome_history_is_preserved_instead_of_overwritten(cached):
    root,_=cached;path=root/'history/signal_outcomes.csv';path.write_bytes(b'\xff\xfeexisting-history')
    original=path.read_bytes()
    # Exercise the updater directly because reading a deliberately corrupt result is also invalid.
    pd.DataFrame([dict(first_seen='2026-10-01',market='US',ticker='EX')]).to_csv(root/'history/signals.csv',index=False)
    assert engine.update_signal_outcomes(root,deepcopy(engine.DEFAULT_CONFIG))==0
    assert path.read_bytes()==original


@pytest.mark.parametrize('contents',[b'\xffbad-history',b'wrong,column\nkeep,original\n'])
def test_history_upsert_fails_closed_on_unreadable_or_keyless_files(tmp_path,contents):
    path=tmp_path/'history.csv';path.write_bytes(contents)
    with pytest.raises(ValueError,match='원본을 보존'):
        engine._upsert_csv(path,pd.DataFrame([dict(date='2026-11-03',ticker='EX')]),['date','ticker'])
    assert path.read_bytes()==contents


def test_missing_original_open_cannot_become_verified_after_normalization(cached):
    from watchlist.analysis_report import tag_raw_price_quality
    _,run=cached;stock=prices(pd.bdate_range('2026-10-01',periods=11))
    stock.loc[stock.index[1],'Open']=np.nan
    before=stock.copy(deep=True)
    tagged=tag_raw_price_quality(stock)
    pd.testing.assert_frame_equal(stock,before)
    normalized=engine.normalize_index(engine.normalize_index(tagged))
    assert not bool(normalized.iloc[1]['_raw_open_valid'])
    r=run(normalized)
    assert r['entry_price_source']!='open' and not bool(r['path_ohlc_complete_10d'])


def test_price_quality_metadata_does_not_change_existing_strategy_features():
    import json
    from watchlist.analysis_report import tag_raw_price_quality
    h=prices(pd.bdate_range(end='2026-10-30',periods=300))
    h=h.drop(columns=['_raw_open_valid','_raw_ohlc_valid']);h['Volume']=1_000_000.
    h['Low']=h['Close']*.98;h['Open']=h['Close']*.99
    tagged=tag_raw_price_quality(h)
    for key in ('_raw_open_valid','_raw_ohlc_valid'):
        tagged[key]=tagged[key].astype(object);tagged.loc[tagged.index[:150],key]=None
    cfg=deepcopy(engine.DEFAULT_CONFIG)
    before=engine.compute_features(h,h,cfg);after=engine.compute_features(tagged,h,cfg)
    assert before is not None and after is not None
    assert json.dumps(engine._json_safe(before),sort_keys=True)==json.dumps(engine._json_safe(after),sort_keys=True)


def test_missing_first_session_cannot_turn_a_later_price_into_next_session_entry(cached):
    _,run=cached;b=prices(pd.bdate_range('2026-10-01',periods=8));stock=b.drop(index=b.index[1])
    r=run(stock,b)
    assert pd.isna(r['entry_date']) and r['bars']==0
    assert not bool(r['entry_session_verified'])



def test_recovered_published_signal_matches_daily_runs_through_entry_stop_and_maturity(tmp_path):
    from watchlist import trend_tracking
    from test_exit_policy_core import fixture
    from watchlist.exit_policy_experiments import prepare, simulate

    signal, full, cutoff = fixture(length=20)
    days = full.index[full.index > pd.Timestamp(signal["as_of"])]
    full.loc[days[2], ["Open", "Low"]] = [85., 80.]
    full.loc[days[3]:, ["High", "Close"]] = [125., 120.]
    cal = engine.get_calendar("US")
    def execute(root, bars, clock):
        hist = root / "history"
        hist.mkdir(parents=True, exist_ok=True)
        if not (hist / "signals.csv").exists():
            pd.DataFrame([signal]).to_csv(hist / "signals.csv", index=False)
        cache = root / "cache"
        cache.mkdir(exist_ok=True)
        bars.to_pickle(cache / "SYNTH.pkl")
        full.to_pickle(cache / "SPY.pkl")
        engine.update_signal_outcomes(root, engine.DEFAULT_CONFIG, evaluated_at=clock, price_dirs={"US":cache,"KR":cache})
        trend_tracking.update_tracking(root, lambda *args:bars, clock, run_id=clock.isoformat(), generated_at=clock.isoformat())
        return pd.read_csv(hist / "signal_outcomes.csv")

    daily, recovery = tmp_path / "daily", tmp_path / "recovery"
    for day in days:
        reference = execute(daily, full.loc[:day], cal.session_close(day) + pd.Timedelta(hours=1))
    # No publication during the gap. A partial provider response lacks the stop day.
    assert execute(recovery, full.drop(days[0]), cutoff).iloc[0].bars == 0
    partial = execute(recovery, full.drop(days[2]), cutoff).iloc[0]
    assert partial.bars == 2 and pd.isna(partial.stop_exit_date)
    recovered = execute(recovery, full, cutoff)
    pd.testing.assert_frame_equal(reference, recovered)
    assert recovered.iloc[0].stop_exit_date == days[2].date().isoformat()
    assert recovered.iloc[0].stop_aware_ret_10d == pytest.approx(-15.3)
    assert recovered.iloc[0].ret_20d == pytest.approx(19.7)
    for file in ("signals.csv", "signal_outcomes.csv"):
        assert (recovery/"history"/file).read_bytes() == (daily/"history"/file).read_bytes()
    def daily_rows(root):
        return pd.read_csv(root/"history/trend_tracking_daily.csv.gz")
    a, b = daily_rows(daily), daily_rows(recovery)
    assert len(b) == 20 and b.recorded_at.nunique() == 1 and a.recorded_at.nunique() == 20
    pd.testing.assert_frame_equal(a.drop(columns=["recorded_at","tracking_run_id"]), b.drop(columns=["recorded_at","tracking_run_id"]))
    research = simulate(prepare(signal, full, cutoff), "FIXED", 20)
    assert research["exit_fill_date"] == recovered.iloc[0].stop_exit_date
    assert research["net_return_pct"] == pytest.approx(recovered.iloc[0].stop_aware_ret_10d)
    pd.testing.assert_frame_equal(execute(recovery, full, cutoff), recovered)


def test_live_price_revision_preserves_published_facts_and_withholds_statistics(cached):
    from watchlist.analysis_report import validation_statistics
    _, run = cached
    bars = prices(pd.bdate_range("2026-10-01", periods=16))
    before = run(bars.iloc[:11])
    bars.loc[bars.index[3], "Low"] = 85.
    held = run(bars)
    for field in ("entry_price", "ret_10d", "stop_aware_ret_10d", "stop_exit_date"):
        assert held[field] == before[field]
    assert held["review_reason"] and held.review_required and not held.entry_session_verified
    bars.loc[bars.index[3], "Low"] = 98.
    assert run(bars).review_required  # Only explicit review can release a recorded revision.
    signal = {**held.to_dict(), "published_at":"2026-10-01T22:00Z"}
    report = validation_statistics(pd.DataFrame([signal]), held.to_frame().T,
                                   {"as_of":{"SP500":"2026-10-30"}, "d_str":"2026-10-30"})
    assert report["quality"]["entry_eligible_rows"] == 0


def test_close_only_middle_bar_cannot_prove_a_stop_and_can_later_be_completed(cached):
    _, run = cached
    stock = prices(pd.bdate_range("2026-10-01", periods=21))
    stock.loc[stock.index[2], "Low"] = 85
    partial = stock.copy()
    partial.loc[stock.index[2], "_raw_ohlc_valid"] = False
    r = run(partial)
    assert pd.isna(r.stop_aware_ret_10d) and pd.isna(r.stop_hit_10d)
    assert pd.isna(r.mfe_10d_pct) and pd.notna(r.ret_20d) and r.status != "종료"
    r = run(stock)
    assert r.status == "종료" and r.stop_aware_ret_10d == pytest.approx(-10.3)


def test_price_only_subscribers_survive_a_pause_across_the_observation_limit(tmp_path, monkeypatch):
    import json
    cfg = deepcopy(engine.DEFAULT_CONFIG)
    monkeypatch.setattr(engine, "ROOT", tmp_path)
    hist = tmp_path / "published/history"
    latest = tmp_path / "published/latest"
    hist.mkdir(parents=True)
    latest.mkdir()
    (latest/"manifest.json").write_text(json.dumps({"as_of":{"SP500":"2026-03-03"}}))
    pd.DataFrame([{"market":"US","first_seen":d,"ticker":t} for d,t in (
        ("2026-02-27","REMOVED"), ("2026-03-02","REMOVED"), ("2023-01-03","OLD"))]
        ).to_csv(hist/"signals.csv",index=False)
    assert engine.observation_tickers(cfg, "US") == {"REMOVED"}


def test_offline_review_rechecks_sealed_row_and_rejects_mixed_history(tmp_path, monkeypatch):
    import hashlib
    import json
    from watchlist import outcome_review
    from test_analysis_report import write_market

    manifest = write_market(tmp_path)
    (tmp_path/"latest/manifest.json").write_text(json.dumps({**manifest, "config_hash":"synthetic"}))
    dates = pd.bdate_range("2026-10-01", periods=21)
    cache = tmp_path / "data/us_history_cache"
    cache.mkdir(parents=True)
    stock = prices(dates)
    stock.to_pickle(cache/"EXA.pkl")
    stock.to_pickle(cache/"SPY.pkl")
    hist = tmp_path/"history"
    signals = pd.read_csv(hist/"signals.csv").iloc[:1].assign(as_of="2026-10-01", sub_market="SP500", signal_close=100., stop_price=90.)
    signals.to_csv(hist/"signals.csv", index=False)
    (hist/"signal_outcomes.csv").unlink()
    engine.update_signal_outcomes(tmp_path, engine.DEFAULT_CONFIG, price_dirs={"US":cache, "KR":cache},
                                 evaluated_at=pd.Timestamp("2026-11-03T20:00Z"))
    outcome = pd.read_csv(hist/"signal_outcomes.csv").iloc[0]
    assert outcome.status == "종료"
    from watchlist.analysis_report import publish_analysis_report
    publish_analysis_report(tmp_path, round_trip_cost_pct=.3)
    stock.loc[dates[2],"Low"] = 85
    stock.to_pickle(cache/"EXA.pkl")
    def hashes():
        return {p:hashlib.sha256(p.read_bytes()).digest() for p in tmp_path.rglob("*") if p.is_file()}
    original = hashes()
    review = outcome_review.review(tmp_path,tmp_path/"data",dates[1])
    assert len(review["records"]) == 1
    row = review["records"][0]
    assert row["cache_proposal"]["stop_exit_date"] == str(dates[2].date())
    assert "stop_aware_ret_10d" in row["conflicting_fields"] and row["stored"]["status"] == "종료"
    assert original == hashes()
    with (hist/"signals.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="hash mismatch"):
        outcome_review.review(tmp_path,tmp_path/"data",dates[1])


def test_unknown_price_basis_in_prefix_cannot_invent_a_split_day_stop(cached):
    _, run = cached
    stock = prices(pd.bdate_range("2026-10-01", periods=11))
    stock.loc[stock.index[3], ["Open","High","Low","Close"]] = [50.,52.,49.,51.]
    partial = stock.copy()
    partial.loc[stock.index[3], "_base_price_basis"] = None
    held = run(partial)
    assert held.bars == 2 and pd.isna(held.stop_exit_date) and pd.isna(held.ret_5d)


def test_split_after_a_known_exit_keeps_the_earlier_event(cached):
    _, run = cached
    stock = prices(pd.bdate_range("2026-10-01", periods=16))
    stock.loc[stock.index[3], "Low"] = 85.
    stock["Stock Splits"] = 0.
    stock.loc[stock.index[12], "Stock Splits"] = 2.
    r = run(stock)
    assert r.bars == 11 and r.stop_exit_date == str(stock.index[3].date())
    assert pd.notna(r.ret_10d) and pd.isna(r.ret_20d)
