"""Protect normal EOD refresh while reserving measured HTTP for real OHLC failures.

All tests are offline; no supplier requests or account data.
"""
from copy import deepcopy
from datetime import datetime, timezone

from watchlist.manual_catalog import csv_bytes
from watchlist.manual_eod_contract import digest, validate_snapshot
from watchlist.manual_eod_close_fallback import planned_fallback_reserve
from watchlist.manual_eod_gap_audit import audit_missing_history
from watchlist.manual_eod_publish import build_snapshot
from watchlist.manual_eod_provider import Unavailable
from test_manual_eod_yfinance import foreign


EARLY=datetime(2026,11,18,1,tzinfo=timezone.utc)
LATE=datetime(2026,11,19,2,tzinfo=timezone.utc)


def _prices(days=('2026-11-17',)):
    return [dict(d=date,o=100.,h=105.,l=98.,c=102.,v=100.)
            for date in days]


class Provider:
    def __init__(self,*,invalid=(),full_days=('2026-11-17','2026-11-18')):
        self.calls=0
        self.max_calls=64
        self.request_log=[]
        self.invalid=set(invalid)
        self.full_days=full_days
    def fetch(self,spec,cutoff,*,close_only=False,metadata_only=False,start=None):
        if self.calls>=self.max_calls:
            raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
        self.calls+=1
        self.request_log.append(dict(market=spec['market'],symbol=spec['symbol'],
            operation='identity' if metadata_only else 'price',status=200,
            close_only_test=close_only))
        identity=dict(provider_symbol=spec['candidates'][0],
            provider_exchange=spec['provider_exchanges'][0],
            currency=spec['currency'],timezone=spec['provider_timezones'][0],
            instrument_type='EQUITY')
        if metadata_only:return [],identity,0
        if close_only:
            return [dict(d=d,c=102.) for d in ('2026-11-17','2026-11-18')],identity,0
        if spec['symbol'] in self.invalid:
            raise Unavailable('INVALID_REAL_OHLC')
        return _prices(self.full_days),identity,0


def _build(rows,p,*,cutoff,run,previous=None,close_fallback=False):
    return build_snapshot(rows,catalog_sha256=digest(csv_bytes(rows)),
        core=dict(run_id=run,generated_at=cutoff.isoformat()),
        core_commit='a'*40,workflow_run_id='1',cutoff=cutoff,provider=p,
        clock=lambda:cutoff,previous=previous,close_fallback=close_fallback)


def test_eight_proven_failures_get_eight_calls_within_original_64_cap():
    errors=[foreign(f'NYSE:ERR{i}','USD') for i in range(8)]
    others=[foreign(f'NYSE:OTHER{i}','USD') for i in range(100)]
    first=_build(errors,Provider(full_days=('2026-11-17',)),
        cutoff=EARLY,run='prior')
    for i,r in enumerate(errors):
        item=first['instruments'][r['symbol']]
        item['reason']='INVALID_REAL_OHLC'
        item['status']='retained'
        if i<2:
            # Missing OHLC can still have a separate valid close-only chart.
            item.update(bars={},last_final_session=None,
                last_final_close_at=None,status='missing')
    validate_snapshot(first)
    frozen=deepcopy(first)
    assert planned_fallback_reserve(errors,first['instruments'],64,enabled=True)==8
    p=Provider(invalid=[r['symbol'] for r in errors])
    actual=_build(errors+others,p,cutoff=LATE,run='new',
        previous=first,close_fallback=True)
    assert p.calls==64
    metrics=actual['collection_metrics']
    assert metrics['http_requests']==64
    assert metrics['primary_direct_http_cap']==56
    assert metrics['close_only_reserved_requests']==8
    assert metrics['direct_requests_by_lane']['close_only']==8
    assert sum(metrics['direct_requests_by_lane'].values())==64
    assert metrics['close_only']['candidate_symbols']==8
    assert metrics['close_only']['attempted_symbols']==8
    assert metrics['close_only']['published_series']==8
    assert metrics['close_only']['direct_http_requests']==8
    for r in errors:
        item=actual['instruments'][r['symbol']]
        assert item['status'] in ('retained','missing')
        # Under the 56-call primary cap, some known-bad OHLC symbols may
        # be deferred in that lane. Their existing integrity evidence still
        # justifies an independent, verified close-only probe.
        assert item['reason'] in ('INVALID_REAL_OHLC',
            'PROVIDER_REQUEST_BUDGET_DEFERRED')
        assert item['close_only']['d'][-1]=='2026-11-18'
        assert set(item['close_only'])=={
            'd','c','source','source_symbol','adjustment','identity_evidence',
            'last_final_session','verified_at','history_coverage'}
    assert all('close_only' not in actual['instruments'][r['symbol']]
        for r in others)
    assert first==frozen
    validate_snapshot(actual)


def test_only_budget_deferred_never_reserves_or_attempts_close_only():
    rows=[foreign(f'NYSE:PREV{i}','USD') for i in range(2)]
    earlier=_build(rows,Provider(full_days=('2026-11-17',)),
        cutoff=EARLY,run='old')
    for row in rows:
        item=earlier['instruments'][row['symbol']]
        item.update(status='retained',reason='PROVIDER_REQUEST_BUDGET_DEFERRED')
    validate_snapshot(earlier)
    assert planned_fallback_reserve(rows,earlier['instruments'],64,enabled=True)==0
    p=Provider()
    later=_build(rows,p,cutoff=LATE,run='new',previous=earlier,close_fallback=True)
    assert later['collection_metrics']['close_only_reserved_requests']==0
    assert later['collection_metrics']['primary_direct_http_cap']==64
    assert later['collection_metrics']['close_only']['candidate_symbols']==0
    assert later['collection_metrics']['close_only']['direct_http_requests']==0
    assert all(not log['close_only_test'] for log in p.request_log)


def test_priority_never_uses_wrong_exchange_or_symbol_not_found():
    rows=[foreign('NYSE:BAD','USD'),foreign('NYSE:NOTFOUND','USD')]
    prior=_build(rows,Provider(full_days=('2026-11-17',)),
        cutoff=EARLY,run='old')
    prior['instruments']['NYSE:BAD'].update(status='retained',
        reason='INVALID_REAL_OHLC')
    prior['instruments']['NYSE:NOTFOUND'].update(status='missing',
        reason='PROVIDER_SYMBOL_NOT_FOUND',bars={},last_final_session=None,
        last_final_close_at=None)
    validate_snapshot(prior)
    assert planned_fallback_reserve(rows,prior['instruments'],64,enabled=True)==1
    p=Provider(invalid=['NYSE:BAD'])
    out=_build(rows,p,cutoff=LATE,run='new',
        previous=prior,close_fallback=True)
    assert out['collection_metrics']['close_only']['candidate_symbols']==1
    assert 'close_only' in out['instruments']['NYSE:BAD']
    assert 'close_only' not in out['instruments']['NYSE:NOTFOUND']
    # The synthetic provider resolves the ordinary OHLC listing normally;
    # the originally missing name must not consume a *close-only* slot.
    assert out['instruments']['NYSE:NOTFOUND']['status']=='current'


def test_date_precise_history_audit_omits_weekends_and_preserves_provenance():
    rows=[foreign('NYSE:GAPPED','USD'),foreign('NYSE:NOTFOUND','USD')]
    class GapProvider(Provider):
        def fetch(self,spec,cutoff,**kwargs):
            if spec['symbol']=='NYSE:NOTFOUND':
                if self.calls>=self.max_calls:raise Unavailable('PROVIDER_REQUEST_BUDGET_DEFERRED')
                self.calls+=1
                raise Unavailable('PROVIDER_SYMBOL_NOT_FOUND')
            prices,identity,excluded=super().fetch(spec,cutoff,**kwargs)
            if not kwargs.get('close_only'):
                # Fri Nov 13, Tue Nov 17: Mon Nov 16 is the only
                # missing exchange trading session.
                return _prices(('2026-11-13','2026-11-17')),identity,excluded
            return prices,identity,excluded
    p=GapProvider()
    out=_build(rows,p,cutoff=LATE,run='audit')
    original=deepcopy(out)
    d=audit_missing_history(out)
    assert d['gap_symbol_count']==1
    assert d['missing_historical_session_count']==1
    assert d['gap_items'][0]['symbol']=='NYSE:GAPPED'
    assert d['gap_items'][0]['missing_dates']==['2026-11-16']
    assert d['gap_items'][0]['likely_cause'].startswith('UNDETERMINED_')
    assert d['missing_price_charts'][0]['symbol']=='NYSE:NOTFOUND'
    assert d['missing_price_charts'][0]['next_review']=='LISTING_OR_PROVIDER_IDENTITY_REVIEW'
    assert d['external_source_comparison_performed'] is False
    assert out==original


def test_read_only_history_audit_blocks_any_output_under_published(monkeypatch,tmp_path):
    import pytest
    from watchlist import manual_eod_gap_audit as module
    from watchlist import manual_eod_publish as publisher

    def never_read(_):
        raise AssertionError('must reject path before looking at source')
    monkeypatch.setattr(publisher,'read_previous',never_read)
    with pytest.raises(ValueError,match='CANNOT_EDIT_PUBLISHED_DATA'):
        module.main([
            '--published',str(tmp_path/'published'),
            '--output',str(tmp_path/'published'/'manual_eod'/'fake.json')])

