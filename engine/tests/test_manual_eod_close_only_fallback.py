"""Offline regression of verified close-only fallback and historical gap census."""
from copy import deepcopy
from datetime import datetime, timezone

import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest, validate_snapshot
from watchlist.manual_eod_history_coverage import session_coverage
from watchlist.manual_eod_provider import (
    specification, result_of, close_rows_of, bars_of, Unavailable)
from watchlist.manual_eod_publish import build_snapshot
from watchlist.manual_eod_source_diagnostics import europe_source_day_evidence
from test_manual_eod_pipeline import row


CUTOFF=datetime(2026,11,19,4,28,tzinfo=timezone.utc)
EUROW=row(symbol='XETR:ADS',unit='EUR',watch_region='overseas')


def price(day, close=105.):
    return dict(d=day,o=100.,h=106.,l=98.,c=close,v=2000.)


def provider_doc(*,venue='GER',close_new=105.,high_new=None):
    spec=specification(EUROW)
    times=[int(datetime(y,m,d,8,tzinfo=timezone.utc).timestamp())
        for y,m,d in ((2026,11,16),(2026,11,17),(2026,11,18))]
    quote=dict(open=[100.,100.,None],high=[106.,106.,high_new],
        low=[98.,98.,None],close=[102.,103.,close_new],
        volume=[300.,300.,None])
    return dict(meta=dict(symbol='ADS.DE',exchangeName=venue,
        exchangeTimezoneName='Europe/Berlin',instrumentType='EQUITY',
        currency='EUR',dataGranularity='1d'),
        timestamp=times,indicators=dict(quote=[quote]))


def test_close_only_independent_from_incomplete_candles():
    spec=specification(EUROW)
    raw=provider_doc()
    result,identity=result_of(dict(chart=dict(result=[raw],error=None)),
        'ADS.DE',spec)
    # The last incomplete O/H/L candle is still excluded from full OHLC.
    full,_=bars_of(result,spec,CUTOFF)
    assert full[-1]['d']=='2026-11-17'
    close,missing=close_rows_of(result,spec,CUTOFF)
    assert close[-1]==dict(d='2026-11-18',c=105.)
    assert len(close)==3
    assert identity['provider_exchange']=='GER'
    assert missing==0
    evidence=europe_source_day_evidence(result,spec,CUTOFF)
    availability=evidence['recent_source_days'][-1]['field_availability']
    assert availability['open_present'] is False
    assert availability['high_present'] is False
    assert availability['low_present'] is False
    assert availability['close_present'] is True
    assert availability['close_valid'] is True
    assert '105.0' not in str(availability)


def test_missing_close_cannot_be_created_from_other_fields():
    spec=specification(EUROW)
    raw=provider_doc(close_new=None)
    rows,skipped=close_rows_of(raw,spec,CUTOFF)
    assert skipped==1 and rows[-1]['d']=='2026-11-17'
    evidence=europe_source_day_evidence(raw,spec,CUTOFF)
    assert evidence['recent_source_days'][-1]['field_availability']['close_present'] is False


def test_verified_listing_is_required_even_for_close_only():
    spec=specification(EUROW)
    raw=provider_doc(venue='NYQ')
    with pytest.raises(Unavailable,match='PROVIDER_EXCHANGE_MISMATCH'):
        result_of(dict(chart=dict(result=[raw],error=None)),'ADS.DE',spec)


def test_calendar_gaps_are_not_weekend_gaps():
    coverage=session_coverage('DE',['2026-11-16','2026-11-18'],
        expected_session='2026-11-18')
    assert coverage['missing_between_first_last']==1
    assert coverage['recent_missing_examples']==['2026-11-17']
    assert coverage['trailing_missing_sessions']==0
    coverage=session_coverage('DE',['2026-11-13','2026-11-16'],
        expected_session='2026-11-18')
    assert coverage['missing_between_first_last']==0
    assert coverage['trailing_missing_sessions']==2


class Existing:
    def __init__(self,valid_close=True):
        self.calls=0;self.max_calls=64;self.valid_close=valid_close
        self.request_log=[]
    def fetch(self,spec,cutoff,*,close_only=False,**kwargs):
        self.calls+=1
        listing=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],
            currency=spec['currency'],
            timezone=spec['provider_timezones'][0],
            instrument_type='EQUITY')
        if close_only:
            if not self.valid_close:
                raise Unavailable('NO_VALID_FINAL_CLOSE_SERIES')
            return [dict(d='2026-11-16',c=102.),
                    dict(d='2026-11-17',c=103.),
                    dict(d='2026-11-18',c=105.)],listing,0
        return [price('2026-11-16'),price('2026-11-17')],listing,1


def _build(provider):
    return build_snapshot([EUROW],catalog_sha256=digest(csv_bytes([EUROW])),
        core=dict(run_id='20261119T042657Z-test1',
            generated_at='2026-11-19T04:27:00+00:00'),
        core_commit='a'*40,workflow_run_id='1',cutoff=CUTOFF,
        provider=provider,close_fallback=True,clock=lambda:CUTOFF)


def test_publisher_keeps_stale_candle_and_separate_newer_close_series():
    p=Existing();doc=_build(p);entry=doc['instruments']['XETR:ADS']
    assert p.calls==2
    assert entry['status']=='stale'
    assert entry['last_final_session']=='2026-11-17'
    assert entry['bars']['d']==['2026-11-16','2026-11-17']
    assert entry['close_only']['last_final_session']=='2026-11-18'
    assert entry['close_only']['d']==['2026-11-16','2026-11-17','2026-11-18']
    assert entry['history_coverage']['trailing_missing_sessions']==1
    assert entry['close_only']['history_coverage']['trailing_missing_sessions']==0
    assert doc['collection_metrics']['close_only']['direct_http_requests']==1
    assert validate_snapshot(doc) is doc
    assert not any(k in entry['close_only'] for k in ('o','h','l','v'))


def test_failed_close_only_does_not_promote_or_fabricate_last_day():
    p=Existing(valid_close=False);doc=_build(p);entry=doc['instruments']['XETR:ADS']
    assert entry['status']=='stale'
    assert 'close_only' not in entry
    assert entry['last_final_session']=='2026-11-17'
    assert entry['close_only_probe']=='NO_VALID_FINAL_CLOSE_SERIES'
    assert validate_snapshot(doc)


def test_invalid_ohlc_remains_invalid_even_if_standalone_close_accepted():
    p=Existing();doc=_build(p);bad=deepcopy(doc)
    item=bad['instruments']['XETR:ADS']
    item['bars']['h'][-1]=1.
    with pytest.raises(ValueError,match='invalid real OHLC'):
        validate_snapshot(bad)


def test_close_only_cannot_claim_unconfirmed_date_or_change_basis():
    doc=_build(Existing())
    item=doc['instruments']['XETR:ADS']
    item['close_only']['d'][-1]='2026-11-19'
    item['close_only']['last_final_session']='2026-11-19'
    with pytest.raises(ValueError,match='unconfirmed'):
        validate_snapshot(doc)
    doc=_build(Existing())
    item=doc['instruments']['XETR:ADS']
    item['close_only']['adjustment']='unknown'
    with pytest.raises(ValueError,match='source/basis'):
        validate_snapshot(doc)


def test_missing_full_candle_chart_may_have_verified_close_only_history():
    class BrokenOHLC(Existing):
        def fetch(self,spec,cutoff,*,close_only=False,**kwargs):
            if not close_only:
                self.calls+=1
                raise Unavailable('INVALID_REAL_OHLC')
            return super().fetch(spec,cutoff,close_only=True,**kwargs)
    p=BrokenOHLC();doc=_build(p)
    item=doc['instruments']['XETR:ADS']
    assert item['status']=='missing'
    assert item['reason']=='INVALID_REAL_OHLC'
    assert item['bars']=={}
    assert item['last_final_session'] is None
    assert item['close_only']['last_final_session']=='2026-11-18'
    assert item['close_only']['d']==['2026-11-16','2026-11-17','2026-11-18']
    assert item['close_only']['source_symbol']=='ADS.DE'
    assert validate_snapshot(doc) is doc

