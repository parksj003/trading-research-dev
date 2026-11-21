from datetime import datetime,timezone
import pandas as pd
from manual_catalog import csv_bytes
from manual_eod_contract import SCHEMA,canonical,digest


def snapshot_for(rows,length=300):
    dates=[d.date().isoformat() for d in pd.bdate_range(end='2026-11-11',periods=length)]
    entries={}
    for row in rows:
        symbol=row['symbol']
        item=dict(symbol=symbol,exchange=symbol.split(':')[0],market='KR' if symbol.startswith('KRX:') else 'US',asset_type=row['asset_type'],
            currency='KRW' if symbol.startswith('KRX:') else 'USD',exchange_timezone='Asia/Seoul' if symbol.startswith('KRX:') else 'America/New_York',
            source_symbol=symbol,source='SYNTHETIC_TEST_DATA',adjustment='synthetic fixture',calendar_policy='synthetic',identity_evidence={'test':True},
            expected_session=dates[-1],last_final_session=dates[-1],last_final_close_at='2026-11-11T21:00:00+00:00',
            verified_at='2026-11-11T23:00:00+00:00',last_success_at='2026-11-11T23:00:00+00:00',status='current',reason='',missing_sessions=0,
            bars=dict(d=dates,o=[100+i for i in range(length)],h=[103+i for i in range(length)],l=[99+i for i in range(length)],c=[102+i for i in range(length)],v=[1000]*length))
        if row['asset_type'] not in ('stock','etf'):
            item.update(bars={},last_final_session=None,last_final_close_at=None,status='unsupported',reason='MACRO_EOD_POLICY_NOT_CONFIGURED')
        entries[symbol]=item
    core={'run_id':'20261111T220000Z-abcdef','generated_at':'2026-11-11T22:00:00+00:00'}
    return dict(schema=SCHEMA,run_id=core['run_id']+'-snapshot',core_run_id=core['run_id'],core_generated_at=core['generated_at'],
        core_manifest_sha256=digest(canonical(core)),core_publication_commit='a'*40,
        generated_at='2026-11-11T23:00:00+00:00',cutoff_at='2026-11-11T23:00:00+00:00',catalog_sha256=digest(csv_bytes(rows)),
        instruments=entries),core

