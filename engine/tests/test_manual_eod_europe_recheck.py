"""Offline assertions: EU recheck never alters published price snapshots."""
import copy
from datetime import datetime, timedelta, timezone

import pytest

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest
from watchlist.manual_eod_provider import Unavailable
from watchlist.manual_eod_publish import build_snapshot
from watchlist.manual_eod_europe_recheck import (
    eligible_stale, recheck_stale_europe, MAX_RECHECK_REQUESTS)
from test_manual_eod_pipeline import row


NOW=datetime(2026,11,19,4,28,tzinfo=timezone.utc)
SYMBOLS=[('XETR:ADS','EUR'),('XETR:BMW','EUR'),('SIX:UBSG','CHF'),
         ('BME:BBVA','EUR')]


def _rows():
    return [row(symbol=s,unit=u) for s,u in SYMBOLS]


class Provider:
    def __init__(self,latest='2026-11-17',reject=False):
        self.latest=latest
        self.reject=reject
        self.calls=0
        self.request_log=[]
    def fetch(self,spec,cutoff,**kwargs):
        self.calls+=1
        self.request_log.append(dict(symbol=spec['symbol'],market=spec['market'],
            operation='price',status=200,
            europe_source_days=dict(status='RECORDED',expected_session='2026-11-18',
                finding='EXPECTED_BAR_ELIGIBLE' if self.latest=='2026-11-18'
                        else 'EXPECTED_BAR_PRESENT_BUT_EXCLUDED',
                recent_source_days=[dict(utc_timestamp='2026-11-18T08:00:00+00:00',
                    local_session='2026-11-18',policy_verdict='OHLC_VALUE_MISSING')])))
        if self.reject:
            raise Unavailable('INVALID_REAL_OHLC')
        dates=['2026-11-17']+(['2026-11-18'] if self.latest=='2026-11-18' else [])
        bars=[dict(d=d,o=100.,h=105.,l=98.,c=102.,v=500.) for d in dates]
        identity=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],
            currency=spec['currency'],
            instrument_type='EQUITY',
            timezone=spec['provider_timezones'][0])
        return bars, identity, 1


def _prior():
    now=NOW
    rows=_rows()
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id='baseline',generated_at=now.isoformat()),
        core_commit='a'*40,workflow_run_id='123',cutoff=now,
        provider=Provider(),clock=lambda:now)


def test_recheck_requires_two_hours_before_same_session_probe():
    prior=_prior()
    assert len(eligible_stale(prior))==4
    p=Provider(latest='2026-11-18')
    got=recheck_stale_europe(prior,p,now=NOW+timedelta(minutes=45))
    assert p.calls==0 and got['attempted_direct_http']==0
    assert got['candidate_count']==4
    assert got['outcomes']=={'WAIT_FOR_COOLDOWN':4}
    assert all(i['next_eligible_utc'] for i in got['instruments'])


def test_recheck_detects_real_verified_final_bar_without_publishing_it():
    prior=_prior()
    frozen=copy.deepcopy(prior)
    p=Provider(latest='2026-11-18')
    result=recheck_stale_europe(prior,p,now=NOW+timedelta(hours=2,minutes=5))
    assert result['outcomes']=={'VALID_FINAL_BAR_NOW_AVAILABLE':4}
    assert result['attempted_direct_http']==4
    assert all(item['target_session']=='2026-11-18' for item in result['instruments'])
    assert all(item['latest_verified_session']=='2026-11-18' for item in result['instruments'])
    assert all(item['verified_listing'] is True for item in result['instruments'])
    assert result['published_prices_modified'] is False
    assert prior==frozen
    assert all(item['status']=='stale' for item in prior['instruments'].values())
    assert all('bars' not in item for item in result['instruments'])


def test_recheck_identifies_still_invalid_provider_and_preserves_prior():
    prior=_prior();previous=copy.deepcopy(prior)
    p=Provider(reject=True)
    got=recheck_stale_europe(prior,p,now=NOW+timedelta(hours=2,minutes=5))
    assert got['outcomes']=={'PROVIDER_REJECTED':4}
    assert got['attempted_direct_http']==4
    assert prior==previous
    assert all(entry['reason']=='INVALID_REAL_OHLC' for entry in got['instruments'])
    assert all(entry['source_day_evidence']['finding']=='EXPECTED_BAR_PRESENT_BUT_EXCLUDED'
               for entry in got['instruments'])


def test_read_only_http_cap_and_europe_scope():
    prior=_prior()
    p=Provider()
    got=recheck_stale_europe(prior,p,max_requests=2,
        now=NOW+timedelta(hours=2,minutes=5))
    assert p.calls==2 and got['attempted_direct_http']==2
    assert got['outcomes']=={
        'VALID_PRICES_STILL_BEHIND':2,
        'RECHECK_HTTP_BUDGET_DEFERRED':2}
    assert all(entry['market'] in ('CH','DE','ES') for entry in got['instruments'])


def test_bad_input_never_opens_network_or_extends_cap():
    doc=_prior();p=Provider()
    for limit in (-1,0,17,True):
        with pytest.raises(ValueError,match='RECHECK_REQUEST_CAP_INVALID'):
            recheck_stale_europe(doc,p,now=NOW+timedelta(hours=4),max_requests=limit)
    with pytest.raises(ValueError,match='AWARE_RECHECK_TIME_REQUIRED'):
        recheck_stale_europe(doc,p,now=datetime(2026,11,19,8))
    assert p.calls==0


def test_output_inside_published_is_rejected_before_any_read(monkeypatch,tmp_path):
    from watchlist import manual_eod_europe_recheck as module
    def never_read(_):
        raise AssertionError('should refuse invalid output location first')
    monkeypatch.setattr(module,'read_previous',never_read)
    with pytest.raises(ValueError,match='DIAGNOSTIC_MUST_NOT_TOUCH_PUBLISHED_NAMESPACE'):
        module.main(['--published',str(tmp_path/'published'),
                     '--output',str(tmp_path/'published'/'manual_eod'/'report.json')])

