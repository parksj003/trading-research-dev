"""Official closure regressions; synthetic prices and temporary histories only."""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json

import exchange_calendars as xc
import pandas as pd
import pytest

from test_base_detector import config, first_base
from test_base_count_publish import commit
from test_forward_outcomes import prices
from watchlist import base_count_publish as publish
from watchlist import base_detector as bd
from watchlist import trend_watchlist as engine
from watchlist import trend_tracking as tracking
from research.base_count import study

NOW = '2026-11-09T23:00:00Z'
CLOSED = ('2026-06-03', '2026-07-17')
PAIRS = [('2026-06-02', '2026-06-04'), ('2026-07-16', '2026-07-20')]
LEGACY_POLICY = 'reviewed-finality-through-2026-v1'


def bars(dates):
    f = prices(dates)
    f['_base_price_basis'] = 'pykrx:date:adjusted=True'
    f['High'] = f[['Open', 'Close']].max(axis=1) + 2
    f['Low'] = f[['Open', 'Close']].min(axis=1) - 2
    f['Volume'] = 1000000.
    return f


def context(market='KR'):
    return dict(config=config(), market=market, ticker='123456' if market=='KR' else 'SYNTH',
                price_basis='synthetic-adjusted', data_revision='v1')


def official_sessions(start, end):
    # Explicit fixture exclusions from official KRX notices, not code under test.
    dates = xc.get_calendar('XKRX').sessions_in_range(start, end)
    return dates[~dates.isin(pd.to_datetime(CLOSED))]


@pytest.mark.parametrize('market', ['KR', 'KOSPI', 'KOSDAQ'])
@pytest.mark.parametrize('day', CLOSED)
def test_official_closed_date_is_never_attested(day, market):
    f = bars([day]); before = f.copy(deep=True)
    assert pd.isna(engine.session_ready_times(f.index, market).iloc[0])
    assert not engine.tag_completed_sessions(f, market)['_session_confirmed'].iloc[0]
    pd.testing.assert_frame_equal(f, before)


@pytest.mark.parametrize('day', CLOSED)
@pytest.mark.parametrize('version', [0, 2, 3, 999])
def test_old_true_flags_cannot_certify_closed_rows_and_cache_is_immutable(tmp_path, day, version):
    f = bars(['2026-05-29', day, '2026-07-20'])
    f['_session_finality_version'] = version
    path = tmp_path/'fixture.pkl'; f.to_pickle(path); original = path.read_bytes()
    out = engine.kr_completed_cached_bars(path, f)
    assert list(out.index) == list(pd.to_datetime(['2026-05-29', '2026-07-20']))
    assert path.read_bytes() == original and bool(f.loc[day, '_session_confirmed'])


@pytest.mark.parametrize('previous,following', PAIRS)
def test_detector_contiguous_across_closure_full_incremental_and_restore(previous, following):
    f = bars([previous, following]); d = bd.BaseDetector(**context())
    full = d.update(f, observed_at=NOW, mode='REPLAY')
    assert full.issue is None and len(full.observations) == 2
    partial = bd.BaseDetector(**context()); first = partial.update(f.iloc[:1], observed_at=NOW, mode='REPLAY')
    partial = bd.BaseDetector.restore(partial.checkpoint(), **context())
    second = partial.update(f, observed_at=NOW, mode='REPLAY')
    assert second.issue is None
    assert [s.logical() for s in first.observations + second.observations] == [s.logical() for s in full.observations]
    assert partial.checkpoint() == d.checkpoint()


@pytest.mark.parametrize('day', CLOSED)
def test_detector_rejects_phantom_closed_bar_without_advancing(day):
    d = bd.BaseDetector(**context()); original = d.checkpoint()
    out = d.update(bars([day]), observed_at=NOW, mode='REPLAY')
    assert out.issue == 'NON_EXCHANGE_SESSION' and out.latest.status == 'UNKNOWN'
    assert d.checkpoint() == original


def old_detector(monkeypatch):
    # Build an actual v1-context synthetic envelope, with its correct checksum.
    d = bd.BaseDetector(**context())
    d._context['calendar_policy'] = LEGACY_POLICY
    d.update(bars(['2026-06-02']), observed_at=NOW, mode='REPLAY')
    return d


def test_old_kr_checkpoint_is_not_silently_relabelled(monkeypatch):
    envelope = old_detector(monkeypatch).checkpoint(); before = deepcopy(envelope)
    with pytest.raises(bd.StateConflict, match='lineage'):
        bd.BaseDetector.restore(envelope, **context())
    assert envelope == before
    us = bd.BaseDetector(**context('US'))
    assert us.checkpoint()['payload']['context']['calendar_policy'] == LEGACY_POLICY
    assert bd.BaseDetector.restore(us.checkpoint(), **context('US')).checkpoint() == us.checkpoint()


def test_publisher_preserves_old_objects_and_blocks_legacy_kr_lineage(tmp_path, monkeypatch):
    f = bars(['2026-06-02']); f['_base_price_basis'] = 'synthetic-adjusted'
    candidates = pd.DataFrame([dict(market='KR', ticker='123456', as_of='2026-06-02')])
    cfg = dict(enabled=True, markets={'KR': dict(parameters=asdict(config()), price_basis='synthetic-adjusted', data_revision='v1')})
    # Create an old publication using the genuine old calendar context.
    real = bd.BaseDetector.__init__
    def legacy(self, *a, **kw):
        real(self, *a, **kw); self._context['calendar_policy'] = LEGACY_POLICY
    with monkeypatch.context() as patch:
        patch.setattr(bd.BaseDetector, '__init__', legacy)
        old = publish.prepare_publication(tmp_path, candidates, cfg, lambda *a:f, run_id='old', generated_at=NOW)
    commit(tmp_path, old)
    original = {p:p.read_bytes() for p in (tmp_path/'history/base_count/objects').iterdir()}
    def prepare(run):
        return publish.prepare_publication(tmp_path, candidates, cfg, lambda *a:f, run_id=run, generated_at=NOW)
    result = prepare('new')
    assert result['observations'][0]['reason'] == 'CHECKPOINT_LINEAGE_CONFLICT'
    assert result['observations'][0]['completed_base_count'] is None
    assert publish.read_object(tmp_path, result['generation'])['instruments'] == publish.read_object(tmp_path, old['generation'])['instruments']
    assert all(p.read_bytes() == raw for p,raw in original.items())
    assert publish.read_previous(tmp_path)['generation'] == old['generation']
    retry = prepare('retry')
    assert retry['observations'] == result['observations']


@pytest.mark.parametrize('asof', ['2026-06-02', '2026-07-16'])
def test_outcomes_certify_real_entry_and_exact_five_ten_twenty_sessions(tmp_path, monkeypatch, asof):
    (tmp_path/'history').mkdir(); (tmp_path/'prices').mkdir()
    f = bars(official_sessions(asof, '2026-09-01')[:21])
    f.to_pickle(tmp_path/'prices/123456.pkl')
    monkeypatch.setattr(engine, 'KR_HISTORY_CACHE_DIR', tmp_path/'prices')
    monkeypatch.setattr(engine, '_make_bench_getter', lambda cfg:lambda *a:f)
    pd.DataFrame([dict(first_seen=asof, as_of=asof, market='KR', sub_market='KOSPI', ticker='123456',
                       stop_price=90., signal_close=100.)]).to_csv(tmp_path/'history/signals.csv', index=False)
    engine.update_signal_outcomes(tmp_path, deepcopy(engine.DEFAULT_CONFIG))
    r = pd.read_csv(tmp_path/'history/signal_outcomes.csv').iloc[0]
    assert r.entry_date == str(f.index[1].date()) and r.entry_session_verified
    for n in (5,10,20):
        assert r[f'eval_date_{n}d'] == str(f.index[n].date())
        assert r[f'calendar_complete_{n}d']
        assert r[f'ret_{n}d'] == pytest.approx((f.Close.iloc[n]/f.Open.iloc[1]-1)*100-.3)
    original = (tmp_path/'history/signal_outcomes.csv').read_bytes()
    monkeypatch.setattr(pd, 'read_pickle', lambda *a:pytest.fail('completed outcome must not be recomputed'))
    engine.update_signal_outcomes(tmp_path, deepcopy(engine.DEFAULT_CONFIG))
    assert (tmp_path/'history/signal_outcomes.csv').read_bytes() == original


@pytest.mark.parametrize('previous,following', PAIRS)
def test_long_tracking_uses_next_real_session(previous, following):
    f = bars([previous,following])
    s = dict(first_seen=previous, as_of=previous, market='KR', sub_market='KOSPI', ticker='123456',
             signal_close=100., stop_price=90., published_at=previous+'T10:00:00Z', generated_at=previous+'T09:00:00Z')
    r,path = tracking.observe(s,f,pd.Timestamp(following+'T12:00:00Z'))
    assert r.get('entry_date') == following and r['bars'] == 1 and len(path) == 1


@pytest.mark.parametrize('previous,following', PAIRS)
def test_research_uses_same_sessions_without_rewriting_old_runs(previous, following):
    f = bars([previous,following]); before = f.copy(deep=True)
    runs, quality = study.segments(f,'KR')
    assert quality['missing_sessions'] == [] and len(runs) == 1 and len(runs[0]) == 2
    pd.testing.assert_frame_equal(f,before)


def test_us_ordinary_csat_and_unreviewed_kr_year_policies_remain():
    assert pd.notna(engine.session_ready_times(['2026-06-03'],'US').iloc[0])
    assert engine.session_ready_times(['2026-06-04'],'KR').iloc[0] == pd.Timestamp('2026-06-04T06:35Z')
    assert engine.session_ready_times(['2026-11-19'],'KR').iloc[0] == pd.Timestamp('2026-11-19T07:35Z')
    assert pd.isna(engine.session_ready_times(['2027-01-05'],'KR').iloc[0])


@pytest.mark.parametrize('previous,following', PAIRS)
def test_structural_base_length_counts_only_real_sessions(previous, following):
    f = first_base()
    # The complete synthetic cycle ends just after the closure. No thresholds change.
    dates = official_sessions('2020-01-02', following)[-len(f):]
    f.index = dates
    assert previous in f.index and following in f.index
    full = bd.BaseDetector(**context()).update(f, observed_at=NOW, mode='REPLAY')
    assert full.issue is None and full.latest.completed_base_count == 1
    start = pd.Timestamp(full.latest.base_start_date)
    assert full.latest.base_length_days == len(dates[(dates>=start)&(dates<=f.index[-1])])


def test_calendar_adapter_is_consistent_bounded_and_does_not_mutate_vendor_cache():
    from watchlist.session_calendar import get_calendar,ReviewedKRCalendar
    raw = xc.get_calendar('XKRX'); original = raw.schedule.copy(deep=True)
    cal = get_calendar('KR')
    for previous,following in PAIRS:
        closed = CLOSED[PAIRS.index((previous,following))]
        assert cal.next_session(previous) == pd.Timestamp(following)
        assert cal.previous_session(following) == pd.Timestamp(previous)
        assert cal.session_offset(previous,1) == pd.Timestamp(following)
        assert cal.date_to_session(closed,direction='previous') == pd.Timestamp(previous)
        assert cal.date_to_session(closed,direction='next') == pd.Timestamp(following)
        assert pd.Timestamp(closed) not in cal.schedule.index
        for method in (cal.session_open,cal.session_close,cal.next_session,cal.previous_session):
            with pytest.raises(ValueError):method(closed)
        with pytest.raises(ValueError):cal.date_to_session(closed)
    assert set(cal.sessions) == set(raw.sessions) - set(pd.to_datetime(CLOSED))
    # A future upstream version may already include these holidays.
    corrected_again=ReviewedKRCalendar(cal)
    pd.testing.assert_index_equal(corrected_again.sessions,cal.sessions)
    pd.testing.assert_frame_equal(corrected_again.schedule,cal.schedule)
    with pytest.raises(ValueError):cal.session_offset(cal.first_session,-1)
    with pytest.raises(ValueError):cal.session_offset(cal.last_session,1)
    pd.testing.assert_frame_equal(raw.schedule, original)
    assert get_calendar('US') is xc.get_calendar('XNYS')


def setup_outcome(tmp_path, monkeypatch, asof='2026-06-02'):
    (tmp_path/'history').mkdir();(tmp_path/'prices').mkdir()
    f = bars(official_sessions(asof,'2026-11-06')[:21])
    monkeypatch.setattr(engine,'KR_HISTORY_CACHE_DIR',tmp_path/'prices')
    monkeypatch.setattr(engine,'_make_bench_getter',lambda cfg:lambda *a:f)
    pd.DataFrame([dict(first_seen=asof,as_of=asof,market='KR',sub_market='KOSPI',ticker='123456',
        stop_price=90.,signal_close=100.)]).to_csv(tmp_path/'history/signals.csv',index=False)
    return f,tmp_path/'prices/123456.pkl',tmp_path/'history/signal_outcomes.csv'


def test_new_policy_partial_outcome_can_progress_without_becoming_legacy(tmp_path,monkeypatch):
    from watchlist.session_calendar import calendar_policy
    f,cache,path=setup_outcome(tmp_path,monkeypatch)
    for n in (5,10,20):
        f.iloc[:n+1].to_pickle(cache)
        engine.update_signal_outcomes(tmp_path,deepcopy(engine.DEFAULT_CONFIG))
        result=pd.read_csv(path)
        assert len(result)==1 and result.iloc[0].bars==n
        assert result.iloc[0].calendar_policy==calendar_policy('KR')


@pytest.mark.parametrize('bars_count',[5,20])
@pytest.mark.parametrize('missing_asof',[False,True])
def test_legacy_kr_outcome_is_not_migrated_or_relabelled(tmp_path,monkeypatch,bars_count,missing_asof):
    f,cache,path=setup_outcome(tmp_path,monkeypatch)
    f.iloc[:bars_count+1].to_pickle(cache)
    engine.update_signal_outcomes(tmp_path,deepcopy(engine.DEFAULT_CONFIG))
    legacy=pd.read_csv(path,dtype=str).drop(columns='calendar_policy')
    legacy.to_csv(path,index=False)
    if missing_asof:
        sig=pd.read_csv(tmp_path/'history/signals.csv',dtype=str);sig['as_of']=None
        sig.to_csv(tmp_path/'history/signals.csv',index=False)
    original=path.read_bytes()
    monkeypatch.setattr(pd,'read_pickle',lambda *a:pytest.fail('legacy outcome must remain frozen'))
    assert engine.update_signal_outcomes(tmp_path,deepcopy(engine.DEFAULT_CONFIG))==1
    assert path.read_bytes()==original


@pytest.mark.parametrize('terminal',[False,True])
def test_legacy_kr_tracking_preserves_numeric_and_daily_history(tmp_path,monkeypatch,terminal):
    from test_trend_tracking import fixture
    s,f,now=fixture(120 if terminal else 5,'KR')
    hist=tmp_path/'history';hist.mkdir()
    pd.DataFrame([s]).to_csv(hist/'signals.csv',index=False)
    tracking.update_tracking(tmp_path,lambda *a:f,now,run_id='old',generated_at=now.isoformat())
    old=pd.read_csv(hist/'trend_tracking.csv',dtype={'ticker':str}).drop(columns='calendar_policy')
    old.to_csv(hist/'trend_tracking.csv',index=False)
    daily=(hist/'trend_tracking_daily.csv.gz').read_bytes()
    tracking.update_tracking(tmp_path,lambda *a:pytest.fail('old KR policy cannot be resumed'),now,
                             run_id='new',generated_at=now.isoformat())
    new=pd.read_csv(hist/'trend_tracking.csv',dtype={'ticker':str})
    for column in ('bars','evaluation_date','entry_price','hold_return_pct','tracking_version'):
        assert old.iloc[0][column]==new.iloc[0][column]
    assert (hist/'trend_tracking_daily.csv.gz').read_bytes()==daily
    assert new.iloc[0].status==('최대 관찰 기간 도달' if terminal else '데이터 부족/검증 불가')
    assert 'calendar_policy' not in new or pd.isna(new.iloc[0]['calendar_policy'])


def test_closed_cache_rows_do_not_trigger_impossible_reacquisition(tmp_path):
    f=bars(['2026-06-02','2026-06-03','2026-06-04'])
    assert not engine.kr_cached_intraday_bar(tmp_path/'not-read.pkl',f)
    f.loc['2026-06-04','_session_confirmed']=False
    assert engine.kr_cached_intraday_bar(tmp_path/'not-read.pkl',f)


@pytest.mark.parametrize('stage',['', 'VCP'])
def test_outcome_empty_stage_roundtrip_does_not_duplicate_signal(tmp_path,monkeypatch,stage):
    f,cache,path=setup_outcome(tmp_path,monkeypatch,asof='2026-10-01')
    # Keep the independent reproduction after the reviewed closures as well.
    f=bars(official_sessions('2026-10-01','2026-11-03')[:11])
    sig=pd.read_csv(tmp_path/'history/signals.csv',dtype=str);sig['setup_stage']=stage
    sig.to_csv(tmp_path/'history/signals.csv',index=False)
    for n in (5,10):
        f.iloc[:n+1].to_pickle(cache)
        engine.update_signal_outcomes(tmp_path,deepcopy(engine.DEFAULT_CONFIG))
        result=pd.read_csv(path)
        assert len(result)==1 and result.iloc[0].bars==n

