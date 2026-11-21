"""Phase 2 synthetic observation only. No providers or operational artifacts."""
import numpy as np
import pandas as pd
import pytest
import exchange_calendars as xc
from dataclasses import asdict, FrozenInstanceError
import json

from watchlist.base_detector import BaseDetector, DetectorConfig, StateConflict


def config(**changes):
    # Synthetic fixture choices, NOT recommended market parameters.
    values=dict(min_length=6,max_length=100,min_depth=.03,max_depth=.40,
                stability_window=3,max_stability_range=.04,pullback_atr=1.,
                separation_atr=3.,pivot_buffer_atr=0.,failure_buffer_atr=.3,
                breakout_volume_ratio=1.2,hold_sessions=2,
                trend_sessions=3,reset_sessions=3)
    values.update(changes)
    return DetectorConfig(**values)


def frame(closes, market='US', volumes=None):
    cal=xc.get_calendar('XNYS' if market=='US' else 'XKRX')
    dates=cal.sessions_in_range('2022-01-04','2026-10-30')[:len(closes)]
    c=np.array(closes,dtype=float)
    return pd.DataFrame(dict(Open=c,High=c*1.002,Low=c*.998,Close=c,
        Volume=np.ones(len(c))*1e6 if volumes is None else volumes,
        _raw_open_valid=True,_raw_ohlc_valid=True,_session_confirmed=True),index=dates)


def cycle_prefix():
    # Observe an uptrend, a real reset and a new recovery; never seed Base 1.
    return list(np.linspace(60,120,230))+[55.]*80+list(np.linspace(55,200,260))


def first_base():
    c=cycle_prefix()+[190.,175.,165.,175.,184.,190.,195.,196.,196.,196.,202.,203.,204.]
    v=[1e6]*len(c);v[-3]=2e6
    return frame(c,volumes=v)


def detector(**kwargs):
    return BaseDetector(config(),market='US',ticker='SYNTH',price_basis='synthetic-adjusted',data_revision='v1',**kwargs)


def run(d,f):
    return d.update(f,observed_at='2026-11-09T23:00:00Z',mode='REPLAY')


def test_normal_first_base_requires_observed_cycle_and_later_hold():
    f=first_base();d=detector();result=run(d,f)
    assert result.issue is None
    assert result.latest.completed_base_count==1
    assert result.latest.current_base_ordinal==1
    assert result.latest.status=='BREAKOUT_CONFIRMED'
    assert result.latest.quality=='CONFIRMED'
    b=next(e for e in result.events if e.kind=='BREAKOUT')
    h=next(e for e in result.events if e.kind=='COMPLETE')
    assert b.date<h.date


def test_same_input_rerun_is_noop():
    d=detector();f=first_base();first=run(d,f);before=d.checkpoint()
    second=run(d,f)
    assert second.events==() and second.observations==()
    assert second.latest==first.latest and d.checkpoint()==before


def context(**changes):
    values=dict(config=config(),market='US',ticker='SYNTH',price_basis='synthetic-adjusted',data_revision='v1')
    values.update(changes)
    return values


def extend(f, closes, breakout_offset=-3):
    volumes=list(f.Volume)+[1e6]*len(closes)
    if breakout_offset is not None:
        volumes[breakout_offset]=2e6
    return frame(list(f.Close)+list(closes),volumes=volumes)


def three_bases():
    f=extend(first_base(),[210,220,240,230,220,215,224,231,235,236,236,236,243,245,246])
    return extend(f,[260,280,300,287,275,267,280,289,293,295,295,295,304,305,307])


@pytest.mark.parametrize('count',[1,2,3])
def test_independent_bases_have_separate_ids_and_exact_completion_dates(count):
    f=three_bases().iloc[:len(first_base())+(count-1)*15]
    result=run(detector(),f)
    done=[e for e in result.events if e.kind=='COMPLETE']
    assert [e.completed_base_count for e in done]==list(range(1,count+1))
    assert len({e.base_id for e in done})==count
    assert result.latest.completed_base_count==count
    assert done[-1].date==str(f.index[-1].date())
    for e in done:
        s=next(s for s in result.observations if s.as_of==e.date)
        assert s.base_first_observed_date>=s.base_start_date
        assert s.base_first_observed_date<=s.base_confirmed_date<s.base_breakout_date<s.base_breakout_confirmed_date


def test_three_inner_contractions_remain_one_base():
    closes=cycle_prefix()+[190,175,165,180,192,180,174,189,195,185,181,193,196,196,196,202,203,204]
    v=[1e6]*len(closes);v[-3]=2e6
    result=run(detector(),frame(closes,volumes=v))
    active=[e for e in result.events if e.ordinal==1 and e.kind=='FORMING']
    assert len(active)==1
    assert result.latest.completed_base_count==1


@pytest.mark.parametrize('completed',[False,True])
def test_failed_breakout_retest_keeps_original_identity_and_counts_once(completed):
    f=first_base() if completed else first_base().iloc[:-2]
    before=run(detector(),f).latest
    f=extend(f,[195,196,198,202,203,204])
    result=run(detector(),f)
    assert result.latest.base_id==before.base_id
    assert result.latest.completed_base_count==1
    assert result.latest.current_base_ordinal==1
    attempts=[e for e in result.events if e.kind=='BREAKOUT']
    assert len(attempts)==2 and attempts[0].attempt_id!=attempts[1].attempt_id
    assert len([e for e in result.events if e.kind=='COMPLETE'])==1
    assert any(e.kind=='RETEST' for e in result.events)
    if completed: assert result.latest.base_on_base


def test_base_on_base_merges_provisional_candidate_without_increment():
    f=first_base();before=run(detector(),f).latest
    f=extend(f,[220,210,199,198,199,202,203,204])
    result=run(detector(),f)
    assert any(e.kind=='MERGED' for e in result.events)
    assert result.latest.base_id==before.base_id
    assert result.latest.base_on_base is True
    assert result.latest.completed_base_count==1
    assert result.latest.status=='BREAKOUT_CONFIRMED'


@pytest.mark.parametrize('scale',[.01,1.,100.])
def test_relative_geometry_is_invariant_to_price_units(scale):
    f=first_base()
    f.loc[:,['Open','High','Low','Close']]*=scale
    result=run(detector(),f)
    assert result.issue is None and result.latest.completed_base_count==1
    assert [e.kind for e in result.events]==[e.kind for e in run(detector(),first_base()).events]


@pytest.mark.parametrize('prices',[
    [199.5]*40, # tiny range does not meet the minimum depth
    [190]*130, # long flat consolidation expires, never repeatedly counts
    [190,175,165,185,170,190,174,193,173,194]*10, # unstable range
])
def test_flat_or_unstable_ranges_do_not_manufacture_completed_bases(prices):
    result=run(detector(),frame(cycle_prefix()+prices))
    assert result.latest.completed_base_count==0
    assert not [e for e in result.events if e.kind=='COMPLETE']


def test_transient_template_lapse_does_not_reset_cycle():
    f=extend(first_base(),[190,195,196,203,205],breakout_offset=None)
    result=run(detector(),f)
    previous=run(detector(),first_base()).latest
    assert result.latest.cycle_id==previous.cycle_id
    assert result.latest.completed_base_count==1
    assert not [e for e in result.events if e.kind=='RESET' and e.date>previous.as_of]


def test_persistent_trend_and_structure_break_then_recovery_starts_new_zero_cycle():
    f=extend(first_base(),[80]*120+list(np.linspace(80,300,260)),breakout_offset=None)
    result=run(detector(),f)
    previous=run(detector(),first_base()).latest
    resets=[e for e in result.events if e.kind=='RESET' and e.date>previous.as_of]
    assert len(resets)==1
    assert result.latest.cycle_id!=previous.cycle_id
    assert result.latest.completed_base_count==0
    assert result.latest.quality=='CONFIRMED'


@pytest.mark.parametrize('prices',[list(np.linspace(60,120,50)),list(np.linspace(60,200,300))+[190,175,180,195]*10])
def test_ipo_or_left_censored_history_never_invents_absolute_base_one(prices):
    result=run(detector(),frame(prices))
    assert all(s.completed_base_count is None and s.current_base_ordinal is None for s in result.observations)
    assert result.latest.quality=='HISTORY_INSUFFICIENT'


@pytest.mark.parametrize('cut',[1,219,230,260,400,570,574,578,580,581,582])
def test_full_replay_and_truncated_history_agree_at_every_transition(cut):
    f=first_base();full=run(detector(),f);truncated=run(detector(),f.iloc[:cut])
    assert truncated.latest.logical()==full.observations[cut-1].logical()
    assert [e.logical() for e in truncated.events]==[e.logical() for e in full.events if e.date<=truncated.latest.as_of]


def test_future_branch_cannot_change_frozen_old_observations():
    f=first_base();d=detector();old=run(d,f.iloc[:-3]);frozen=[s.logical() for s in old.observations]
    run(d,f)
    bearish=f.copy();bearish.iloc[-3:,:5]=frame([150,140,130]).iloc[:,:5].to_numpy()
    alternate=run(detector(),bearish)
    assert frozen==[s.logical() for s in old.observations]
    assert frozen==[s.logical() for s in alternate.observations[:-3]]
    with pytest.raises(FrozenInstanceError): old.latest.status='BROKEN'


def test_incremental_rolling_checkpoint_restarts_equal_full_run():
    f=three_bases();full=run(detector(),f);d=detector();events=[];observations=[]
    for end in [230,310,570,579,581,583,590,599,len(f)]:
        last=d.checkpoint()['payload']['state']['last']
        start=max(0,f.index.get_loc(pd.Timestamp(last))-20) if last else 0
        part=run(d,f.iloc[start:end]);assert part.issue is None
        events.extend(part.events);observations.extend(part.observations)
        d=BaseDetector.restore(json.loads(json.dumps(d.checkpoint())),**context())
    assert [e.logical() for e in events]==[e.logical() for e in full.events]
    assert [s.logical() for s in observations]==[s.logical() for s in full.observations]
    assert d.checkpoint()==run_checkpoint(f)


def run_checkpoint(f):
    d=detector();run(d,f);return d.checkpoint()


@pytest.mark.parametrize('column,value,issue',[
    ('Close',np.nan,'INVALID_OR_HALTED_OHLCV'),('Volume',0,'INVALID_OR_HALTED_OHLCV'),
    ('High',1,'INVALID_OR_HALTED_OHLCV'),('Open','bad','INVALID_OHLCV_VALUE'),
    ('_raw_open_valid',False,'UNVERIFIED_OR_HALTED_BAR'),
    ('_raw_ohlc_valid',False,'UNVERIFIED_OR_HALTED_BAR'),
    ('_session_confirmed',False,'UNVERIFIED_OR_HALTED_BAR'),
    ('IsHalt',True,'UNVERIFIED_OR_HALTED_BAR'),('Stock Splits',2,'SPLIT_REQUIRES_NEW_PRICE_LINEAGE'),
])
def test_bad_bar_stops_without_advancing_pending_breakout(column,value,issue):
    f=first_base();d=detector();run(d,f.iloc[:-2]);before=d.checkpoint()
    if column not in f: f[column]=False if column=='IsHalt' else 0.
    if isinstance(value,str):f[column]=f[column].astype(object)
    f.loc[f.index[-2],column]=value
    result=run(d,f)
    assert result.issue==issue and result.latest.status=='UNKNOWN'
    assert result.latest.completed_base_count is None
    assert d.checkpoint()==before
    repaired=run(d,first_base())
    assert repaired.latest.completed_base_count==1


def test_duplicate_and_missing_sessions_are_not_silently_deduplicated_or_skipped():
    f=first_base();d=detector();run(d,f.iloc[:-2]);before=d.checkpoint()
    duplicate=pd.concat([f.iloc[:-2],f.iloc[-3:]])
    assert run(d,duplicate).issue=='DUPLICATE_SESSION'
    assert d.checkpoint()==before
    gap=f.drop(f.index[-2])
    assert run(d,gap).issue=='MISSING_EXCHANGE_SESSION'
    assert d.checkpoint()==before


@pytest.mark.parametrize('column',['Open','High','Low','Close','Volume'])
def test_revision_of_any_ohlcv_field_requires_explicit_rebuild(column):
    f=first_base();d=detector();run(d,f);before=d.checkpoint()
    # A valid small change cannot hide behind a Close-only checksum.
    if column=='High':f.iloc[0,f.columns.get_loc(column)]*=1.0001
    elif column=='Low':f.iloc[0,f.columns.get_loc(column)]*=.9999
    else:f.iloc[0,f.columns.get_loc(column)]*=1.0001
    assert run(d,f).issue=='HISTORY_REVISION_REQUIRES_REBUILD'
    assert d.checkpoint()==before


def test_lost_checkpoint_recovery_replays_only_available_information():
    f=first_base();d=detector();run(d,f)
    restored=BaseDetector.restore(d.checkpoint(),**context())
    assert restored.checkpoint()==d.checkpoint()
    assert run(detector(),f).latest==run(d,f).latest
    # If both durable checkpoint and old bars are gone, absolute count stays unknown.
    truncated=run(detector(),f.iloc[-230:])
    assert truncated.latest.completed_base_count is None
    assert truncated.latest.quality=='HISTORY_INSUFFICIENT'


@pytest.mark.parametrize('change',[
    {'config':config(hold_sessions=3)}, {'price_basis':'unadjusted'},
    {'data_revision':'v2'}, {'ticker':'OTHER'}, {'market':'KR'}])
def test_checkpoint_lineage_mismatch_is_rejected(change):
    d=detector();run(d,first_base())
    with pytest.raises(StateConflict):BaseDetector.restore(d.checkpoint(),**context(**change))


def test_algorithm_version_mismatch_is_rejected_even_with_valid_checksum():
    from watchlist.base_detector import _digest
    d=detector();run(d,first_base());e=d.checkpoint()
    e['payload']['context']['algorithm']='different-version';e['sha256']=_digest(e['payload'])
    with pytest.raises(StateConflict):BaseDetector.restore(e,**context())


@pytest.mark.parametrize('corruption',['checksum','negative','cursor','snapshot','base','empty'])
def test_corrupt_checkpoint_never_silently_resets_count(corruption):
    from watchlist.base_detector import _digest
    d=detector();run(d,first_base());e=d.checkpoint();s=e['payload']['state']
    if corruption=='checksum': e['sha256']='wrong'
    else:
        if corruption=='negative':s['completed']=-1
        if corruption=='cursor':s['last']='2020-01-01'
        if corruption=='snapshot':s['latest']['status']='NO_BASE'
        if corruption=='base':s['base']=None
        if corruption=='empty':s['recent']=[]
        e['sha256']=_digest(e['payload'])
    with pytest.raises(StateConflict):BaseDetector.restore(e,**context())


def test_checkpoint_atomic_cas_lock_and_interrupted_replace(tmp_path,monkeypatch):
    import watchlist.base_detector as module
    d=detector();run(d,first_base().iloc[:-2]);p=tmp_path/'research.json'
    digest=d.save(p);original=p.read_bytes()
    assert BaseDetector.load(p,**context()).checkpoint()==d.checkpoint()
    run(d,first_base())
    with pytest.raises(StateConflict):d.save(p)
    with pytest.raises(StateConflict):d.save(p,expected_digest='stale')
    assert p.read_bytes()==original
    lock=tmp_path/'research.json.lock';lock.write_text('existing writer')
    with pytest.raises(StateConflict):d.save(p,expected_digest=digest)
    assert lock.read_text()=='existing writer';lock.unlink()
    def crash(*args):raise OSError('simulated power loss before atomic replacement')
    with monkeypatch.context() as m:
        m.setattr(module.os,'replace',crash)
        with pytest.raises(OSError):d.save(p,expected_digest=digest)
    assert p.read_bytes()==original
    assert sorted(x.name for x in tmp_path.iterdir())==['research.json']
    d.save(p,expected_digest=digest)
    assert BaseDetector.load(p,**context()).checkpoint()==d.checkpoint()


def test_new_instance_requires_explicit_config_and_timezone():
    with pytest.raises(TypeError):DetectorConfig()
    with pytest.raises(ValueError):detector().update(first_base(),observed_at='2026-11-09',mode='REPLAY')
    with pytest.raises(ValueError):detector().update(first_base(),observed_at='2026-11-09T23:00Z',mode='AUTO')


@pytest.mark.parametrize('bad',[
    {'hold_sessions':0},{'min_length':2},{'min_depth':.9},{'max_depth':float('nan')},
    {'pullback_atr':-1},{'pivot_buffer_atr':-1},{'reset_sessions':True},
])
def test_invalid_parameters_are_rejected(bad):
    with pytest.raises(ValueError):config(**bad)


def test_session_finality_and_live_warmup_metadata():
    f=first_base();d=detector();last=f.index[-1]
    close=d.calendar.session_close(last)
    early=d.update(f,observed_at=close,mode='LIVE')
    assert early.issue=='SESSION_NOT_YET_FINAL'
    assert all(s.mode=='REPLAY' for s in early.observations)
    final=d.update(f,observed_at=close+pd.Timedelta(minutes=5),mode='LIVE')
    assert final.issue is None and final.latest.mode=='LIVE'
    assert len(final.observations)==1
    assert early.observations[-1].recorded_at!=final.latest.recorded_at


def test_korean_sessions_and_csat_delayed_close():
    f=frame([100]*220,market='KR');date=pd.Timestamp('2022-11-17')
    # Short local prefix beginning on a valid special session is valid warmup.
    one=f.iloc[:1].copy();one.index=pd.DatetimeIndex([date])
    d=BaseDetector(**context(market='KR',ticker='5930'))
    early=d.update(one,observed_at='2022-11-17T07:00Z',mode='LIVE')
    assert early.issue=='SESSION_NOT_YET_FINAL'
    final=d.update(one,observed_at='2022-11-17T07:35Z',mode='LIVE')
    assert final.issue is None and final.latest.ticker=='005930'


def test_calendar_out_of_bounds_is_quality_unknown_not_unhandled_exception():
    f=frame([100]);f.index=pd.DatetimeIndex(['1900-01-02'])
    assert run(detector(),f).issue=='CALENDAR_COVERAGE_UNKNOWN'


def test_checkpoint_overlap_is_required_when_cache_rolls():
    d=detector();f=first_base();run(d,f.iloc[:-3]);before=d.checkpoint()
    assert run(d,f.iloc[-3:]).issue=='CHECKPOINT_OVERLAP_MISSING'
    assert d.checkpoint()==before


def test_schema_missing_non_session_and_noncanonical_index_fail_safely():
    f=frame([100]);assert run(detector(),f.drop(columns='Volume')).issue=='REQUIRED_PRICE_PROVENANCE_MISSING'
    assert run(detector(),f.iloc[:0]).issue=='PRICE_WINDOW_EMPTY'
    f.index=pd.DatetimeIndex(['2022-01-08'])
    assert run(detector(),f).issue=='NON_EXCHANGE_SESSION'
    f.index=f.index.tz_localize('UTC')
    assert run(detector(),f).issue=='CANONICAL_SESSION_INDEX_REQUIRED'


@pytest.mark.parametrize('width',[.0002,.008])
def test_low_and_high_intraday_volatility_use_atr_without_extra_counts(width):
    f=first_base();f['High']=f.Close*(1+width);f['Low']=f.Close*(1-width)
    result=run(detector(),f)
    assert result.issue is None and result.latest.completed_base_count==1
    assert result.latest.status=='BREAKOUT_CONFIRMED'


def test_immature_rally_is_not_backdated_into_confirmed_base():
    f=frame(cycle_prefix()+[190,198,202])
    result=run(detector(),f)
    assert result.latest.completed_base_count==0
    assert result.latest.status=='BASE_FAILED'
    assert result.latest.base_confirmed_date is None
    assert result.latest.reason=='NO_BACKDATED_BREAKOUT'


def test_above_pivot_days_and_low_volume_cross_do_not_repeat_breakouts():
    f=extend(first_base(),[205,207,202,206,210],breakout_offset=None)
    result=run(detector(),f)
    assert len([e for e in result.events if e.kind=='BREAKOUT'])==1
    assert result.latest.completed_base_count==1
    f=first_base();f['Volume']=1e6
    result=run(detector(),f)
    assert not [e for e in result.events if e.kind=='BREAKOUT']
    assert result.latest.completed_base_count==0


def test_two_year_rolling_cache_without_original_anchor_preserves_durable_count():
    f=three_bases();d=detector();run(d,f.iloc[:-1])
    result=run(d,f.iloc[-504:])
    assert result.issue is None and result.latest.completed_base_count==3
    assert result.latest.logical()==run(detector(),f).latest.logical()


def test_partial_batch_reports_valid_prefix_and_resumes_at_first_bad_bar():
    f=first_base();bad=f.copy();bad.loc[bad.index[-2],'Volume']=0
    d=detector();partial=run(d,bad)
    assert partial.issue=='INVALID_OR_HALTED_OHLCV'
    assert partial.observations[-1].as_of==str(f.index[-3].date())
    assert d.checkpoint()['payload']['state']['phase']=='BREAKOUT_PENDING'
    final=run(d,f)
    assert len(final.observations)==2 and final.latest.completed_base_count==1
    assert [e.logical() for e in (*partial.events,*final.events)]==[e.logical() for e in run(detector(),f).events]


def test_revised_history_uses_new_lineage_and_cannot_overwrite_old_checkpoint(tmp_path):
    d=detector();run(d,first_base());p=tmp_path/'v1.json';digest=d.save(p)
    revised=BaseDetector(**context(data_revision='v2'));run(revised,first_base())
    assert revised.checkpoint()['payload']['context']!=d.checkpoint()['payload']['context']
    assert revised._state['base']['id']!=d._state['base']['id']
    with pytest.raises(StateConflict):revised.save(p,expected_digest=digest)
    assert BaseDetector.load(p,**context()).checkpoint()==d.checkpoint()


def test_kr_unreviewed_future_calendar_year_is_held_unknown():
    f=frame([100],market='KR');f.index=pd.DatetimeIndex(['2027-01-05'])
    d=BaseDetector(**context(market='KR'))
    assert d.update(f,observed_at='2027-01-05T23:00Z',mode='LIVE').issue=='UNREVIEWED_KR_SPECIAL_SESSION_YEAR'


@pytest.mark.parametrize('corruption',['recent','completed','flag'])
def test_semantically_corrupt_checkpoint_is_rejected_after_checksum_recalculation(corruption):
    from watchlist.base_detector import _digest
    d=detector();run(d,first_base());e=d.checkpoint();s=e['payload']['state']
    if corruption=='recent':s['recent'][-1]['Volume']*=2
    if corruption=='completed':s['completed']+=1
    if corruption=='flag':s['anchored']='false'
    e['sha256']=_digest(e['payload'])
    with pytest.raises(StateConflict):BaseDetector.restore(e,**context())


def test_empty_or_flat_checkpoint_roundtrip_and_invalid_json_do_not_reset(tmp_path):
    empty=detector();assert BaseDetector.restore(empty.checkpoint(),**context()).checkpoint()==empty.checkpoint()
    f=frame([100]*30);f['High']=f['Low']=f.Close
    flat=detector();run(flat,f)
    assert BaseDetector.restore(flat.checkpoint(),**context()).checkpoint()==flat.checkpoint()
    p=tmp_path/'broken.json';p.write_text('{incomplete')
    with pytest.raises(StateConflict):BaseDetector.load(p,**context())
    with pytest.raises(StateConflict):flat.save(p,expected_digest='anything')
    assert p.read_text()=='{incomplete' and not list(tmp_path.glob('*.lock'))


def test_no_provider_requests_or_automatic_files_are_needed(tmp_path,monkeypatch):
    import requests
    def forbidden(*args,**kwargs):raise AssertionError('external API forbidden')
    monkeypatch.setattr(requests.sessions.Session,'request',forbidden)
    monkeypatch.chdir(tmp_path)
    r=run(detector(),first_base())
    assert r.latest.completed_base_count==1
    assert list(tmp_path.iterdir())==[]


def test_daily_resume_from_every_checkpoint_equals_full_replay():
    f=three_bases();d=detector();observations=[];events=[];states=set()
    for i in range(len(f)):
        batch=run(d,f.iloc[max(0,i-1):i+1])
        assert batch.issue is None
        observations.extend(batch.observations);events.extend(batch.events)
        states.add(batch.latest.status)
        d=BaseDetector.restore(d.checkpoint(),**context())
    full=run(detector(),f)
    assert [s.logical() for s in observations]==[s.logical() for s in full.observations]
    assert [e.logical() for e in events]==[e.logical() for e in full.events]
    assert states=={'NO_BASE','BASE_FORMING','BASE_CONFIRMED','BREAKOUT_PENDING',
                    'BREAKOUT_CONFIRMED','BASE_FAILED','TREND_RESET'}


def test_checkpoint_after_ambiguous_post_replace_failure_can_be_loaded(tmp_path,monkeypatch):
    import watchlist.base_detector as module
    d=detector();run(d,first_base());p=tmp_path/'research.json'
    real_sync=module.os.fsync;calls=[]
    def crash_after_replace(fd):
        calls.append(fd)
        if len(calls)==2: raise OSError('directory sync failed after replacement')
        return real_sync(fd)
    with monkeypatch.context() as m:
        m.setattr(module.os,'fsync',crash_after_replace)
        with pytest.raises(OSError):d.save(p)
    # Outcome of a failed save may be ambiguous: reload, do not blindly overwrite.
    assert BaseDetector.load(p,**context()).checkpoint()==d.checkpoint()
    assert not list(tmp_path.glob('*.lock'))


def test_unseparated_pullback_above_old_pivot_is_extension_not_second_base():
    f=extend(first_base(),[210,203,204,205],breakout_offset=None)
    d=BaseDetector(**context(config=config(separation_atr=20)))
    before=run(d,first_base()).latest
    result=run(d,f)
    assert result.latest.base_id==before.base_id
    assert result.latest.base_on_base is True
    assert result.latest.completed_base_count==1
    assert result.latest.current_base_ordinal==1
    assert result.latest.status=='BREAKOUT_CONFIRMED'
    assert len([e for e in result.events if e.kind=='EXTENDED'])==1

