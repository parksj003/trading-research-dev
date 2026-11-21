"""Optional cache-only Base Count publication. Never changes trading outputs.

Immutable objects hold checkpoints AND the observations/events that produced
them. The existing latest/manifest.json is the sole committed generation pointer.
An unpublished object is harmless: it is never used as an implicit new head.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, fields
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

import pandas as pd

if __package__:
    from .base_detector import BaseDetector, DetectorConfig, Snapshot, STATES, StateConflict, ALGORITHM_VERSION, CSAT
else:
    from base_detector import BaseDetector, DetectorConfig, Snapshot, STATES, StateConflict, ALGORITHM_VERSION, CSAT

FILE_NAME = 'base_counts.json'
SCHEMA = 'base-observations-1'
OBJECT_DIR = Path('history/base_count/objects')
MAX_OBJECT_BYTES = 64_000_000
HASH = re.compile(r'^[0-9a-f]{64}$')
PRICE_BASIS_COLUMN = '_base_price_basis'
US_BASIS = 'yfinance:auto_adjust=True'
KR_BASIS = 'pykrx:date:adjusted=True'
_prepared_writes = ContextVar('base_count_prepared_writes',default=None)


def _manifest_marker(base):
    try:
        return (Path(base)/'latest/manifest.json').read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return object()  # An unreadable marker must never authorize cleanup.


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


@contextmanager
def publication_lock(base):
    base = Path(base); base.mkdir(parents=True, exist_ok=True)
    path = base / '.base-count-publish.lock'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise StateConflict('Base Count publication already locked') from exc
    before = _manifest_marker(base)
    created = []
    token = _prepared_writes.set(created)
    try:
        os.close(fd)
        yield
    finally:
        _prepared_writes.reset(token)
        try:
            if _manifest_marker(base) == before:
                # Only this transaction's newly created, never-committed objects.
                # Existing history objects are never touched. A hard-kill orphan
                # with a missing committed pointer still requires manual recovery.
                for prepared in reversed(created):
                    prepared.unlink(missing_ok=True)
        finally:
            path.unlink()


def write_object(base, payload):
    raw = json_bytes(payload)
    if len(raw) > MAX_OBJECT_BYTES:
        raise StateConflict('Base Count object size limit')
    digest = hashlib.sha256(raw).hexdigest()
    path = Path(base) / OBJECT_DIR / (digest + '.json.gz')
    path.parent.mkdir(parents=True, exist_ok=True)
    packed = gzip.compress(raw, mtime=0)
    fd, temporary = tempfile.mkstemp(prefix='.prepared-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(packed); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary,path)
            pending = _prepared_writes.get()
            if pending is not None:
                pending.append(path)
        except FileExistsError:
            if read_object(base, digest) != payload:
                raise StateConflict('Existing immutable object differs')
    finally:
        os.unlink(temporary)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return digest


def read_object(base, digest):
    if not isinstance(digest, str) or not HASH.fullmatch(digest):
        raise StateConflict('Invalid Base Count object identity')
    try:
        with gzip.open(Path(base) / OBJECT_DIR / (digest + '.json.gz'), 'rb') as stream:
            raw = stream.read(MAX_OBJECT_BYTES + 1)
        if len(raw) > MAX_OBJECT_BYTES or hashlib.sha256(raw).hexdigest() != digest:
            raise StateConflict('Base Count object checksum/size mismatch')
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise StateConflict('Base Count object must be a JSON object')
        return result
    except (OSError, ValueError, EOFError) as exc:
        raise StateConflict('Base Count object unavailable or corrupt') from exc


def validate_publication(doc, run_id):
    errors = []
    if not isinstance(doc, dict) or doc.get('schema') != SCHEMA or doc.get('run_id') != run_id:
        return ['Base Count schema/run mismatch']
    if doc.get('status') not in ('enabled', 'disabled', 'unavailable'):
        errors.append('Base Count publication status invalid')
    if doc.get('generation') is not None and not HASH.fullmatch(str(doc['generation'])):
        errors.append('Base Count generation invalid')
    if not isinstance(doc.get('observations'), list):
        return errors + ['Base Count observations missing']
    identities = set()
    for row in doc['observations']:
        if not isinstance(row, dict):
            return errors + ['Base Count observation must be an object']
        key = (row.get('market'), row.get('ticker'))
        if not all(isinstance(x, str) for x in key) or key[0] not in ('KR', 'US') or key in identities:
            return errors + ['Base Count duplicate/invalid identity']
        identities.add(key)
        if row.get('status') not in STATES or row.get('quality') not in ('UNKNOWN','HISTORY_INSUFFICIENT','ESTIMATED','CONFIRMED'):
            errors.append('Base Count state/quality invalid')
        if row.get('status') == 'UNKNOWN' and row.get('quality') != 'UNKNOWN':
            errors.append('Unknown state must have unknown quality')
        for name in ('completed_base_count', 'current_base_ordinal'):
            value = row.get(name)
            if value is not None and (type(value) is not int or value < (1 if name=='current_base_ordinal' else 0)):
                errors.append('Base Count quantity invalid')
            if (row.get('quality') in ('UNKNOWN','HISTORY_INSUFFICIENT') or row.get('status')=='UNKNOWN') and value is not None:
                errors.append('Unknown history cannot expose an absolute count')
        try:
            date = pd.Timestamp(row['as_of'])
            recorded = pd.Timestamp(row['recorded_at'])
            generated = pd.Timestamp(doc['generated_at'])
            if (pd.isna(date) or date.tzinfo is not None or date != date.normalize() or pd.isna(recorded)
                or recorded.tzinfo is None or pd.isna(generated) or generated.tzinfo is None or recorded>generated):
                raise ValueError('Invalid observation dates')
            for name in ('base_start_date','base_first_observed_date','base_confirmed_date','base_breakout_date',
                         'base_breakout_confirmed_date','base_end_date'):
                if row.get(name) is not None:
                    point = pd.Timestamp(row[name])
                    if pd.isna(point) or point.tzinfo is not None or point>date:
                        raise ValueError('Future or invalid structure date')
            if row.get('mode') not in ('LIVE','REPLAY'):
                raise ValueError('Invalid observation mode')
        except (KeyError, ValueError, TypeError):
            errors.append('Base Count date/mode invalid')
    return errors


def read_previous(base):
    latest = Path(base) / 'latest'
    path = latest / 'manifest.json'
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_bytes())
        meta = manifest.get('files', {}).get(FILE_NAME)
        if not meta:
            if (latest / FILE_NAME).exists():
                raise StateConflict('Unadvertised Base Count file')
            return None
        raw = (latest / FILE_NAME).read_bytes()
        if hashlib.sha256(raw).hexdigest() != meta['sha256'] or len(raw) != meta['bytes']:
            raise StateConflict('Committed Base Count checksum mismatch')
        doc = json.loads(raw)
        if validate_publication(doc, manifest['run_id']):
            raise StateConflict('Committed Base Count contract invalid')
        return doc
    except (ValueError, TypeError, KeyError, OSError, AttributeError) as exc:
        raise StateConflict('Committed Base Count reference unavailable') from exc


def unknown_row(market, ticker, as_of, reason, recorded_at):
    row = {field.name: None for field in fields(Snapshot)}
    row.update(market=market, ticker=ticker, as_of=as_of, status='UNKNOWN', quality='UNKNOWN',
               reason=reason, base_on_base=False, algorithm_version=ALGORITHM_VERSION,
               recorded_at=recorded_at, mode='REPLAY')
    return row


def prepare_prices(raw, as_of, market, expected_basis):
    """No normalization, deduplication, interpolation, or retrospective attestation."""
    if not isinstance(raw, pd.DataFrame) or raw.empty:
        raise StateConflict('CACHE_MISSING')
    if not isinstance(raw.index, pd.DatetimeIndex) or raw.index.tz is not None:
        raise StateConflict('CANONICAL_CACHE_INDEX_REQUIRED')
    cutoff = pd.Timestamp(as_of)
    out = raw.loc[raw.index <= cutoff].copy()
    if out.empty or out.index[-1] != cutoff:
        raise StateConflict('CACHE_AS_OF_MISMATCH')
    if PRICE_BASIS_COLUMN not in out or not out[PRICE_BASIS_COLUMN].eq(expected_basis).all():
        raise StateConflict('PRICE_BASIS_UNVERIFIED')
    if market == 'KR':
        affected = out.index.strftime('%Y-%m-%d').isin(CSAT)
        version = pd.to_numeric(out.get('_session_finality_version',pd.Series(0,index=out.index)),errors='coerce')
        if (affected & ~version.ge(2).to_numpy()).any():
            raise StateConflict('KR_SPECIAL_SESSION_PROVENANCE_UNVERIFIED')
    return out


def _candidate_identities(candidates):
    groups = {}
    if candidates.empty:
        return groups
    if not {'market','ticker','as_of'}.issubset(candidates):
        raise StateConflict('CANDIDATE_IDENTITY_MISSING')
    for row in candidates.to_dict('records'):
        market = str(row['market']).upper()
        ticker = str(row['ticker']).zfill(6) if market=='KR' else str(row['ticker']).upper()
        if market not in ('KR','US') or not re.fullmatch(r'[A-Z0-9^][A-Z0-9^._-]*',ticker):
            raise StateConflict('CANDIDATE_IDENTITY_INVALID')
        date = pd.Timestamp(row['as_of'])
        if pd.isna(date) or date.tzinfo is not None or date != date.normalize():
            raise StateConflict('CANDIDATE_DATE_INVALID')
        groups.setdefault((market,ticker),set()).add(date.strftime('%Y-%m-%d'))
    return groups


def prepare_publication(base, candidates, settings, price_getter, *, run_id, generated_at):
    """Prepare immutable objects; only the caller's successful latest swap commits.

    Call under publication_lock for all cooperating publishers. Do not scan an
    object directory for a newer state: failed publications may leave orphans.
    """
    groups = _candidate_identities(candidates)
    enabled = settings.get('enabled') is True
    prior = None
    try:
        prior = read_previous(base)
        if not enabled and prior is None:
            return None
        parent = prior.get('generation') if prior else None
        doc = dict(schema=SCHEMA,run_id=run_id,generated_at=generated_at,
                   status='enabled' if enabled else 'disabled',generation=parent,observations=[])
        if not enabled:
            return doc
        if parent:
            generation = read_object(base,parent)
            if generation.get('schema') != 'base-generation-1' or not isinstance(generation.get('instruments'),dict):
                raise StateConflict('Committed Base Count generation invalid')
            instruments = dict(generation['instruments'])
        else:
            # Existing objects without a committed reference could indicate lost
            # state. Never reinterpret this as an empty/new absolute cycle.
            directory = Path(base)/OBJECT_DIR
            if directory.exists() and any(directory.glob('*.json.gz')):
                raise StateConflict('Base Count committed state reference missing')
            instruments = {}
        for (market,ticker), dates in sorted(groups.items()):
            as_of = max(dates); key = f'{market}:{ticker}'
            if len(dates) != 1:
                doc['observations'].append(unknown_row(market,ticker,as_of,'CONFLICTING_CANDIDATE_DATES',generated_at))
                continue
            mcfg = settings.get('markets',{}).get(market)
            try:
                if not isinstance(mcfg,dict):
                    raise StateConflict('MARKET_PARAMETERS_NOT_CONFIGURED')
                context = dict(config=DetectorConfig(**mcfg['parameters']),market=market,ticker=ticker,
                               price_basis=mcfg['price_basis'],data_revision=mcfg['data_revision'])
                old_id = instruments.get(key)
                if old_id:
                    old = read_object(base,old_id)
                    if old.get('schema') != 'base-instrument-1' or old.get('instrument') != key:
                        raise StateConflict('CHECKPOINT_OBJECT_INVALID')
                    try:
                        detector = BaseDetector.restore(old['checkpoint'],**context)
                    except StateConflict as exc:
                        raise StateConflict('CHECKPOINT_LINEAGE_CONFLICT') from exc
                else:
                    detector = BaseDetector(**context)
                prices = prepare_prices(price_getter(market,ticker),as_of,market,mcfg['price_basis'])
                if detector._state['last'] and detector._state['last'] > as_of:
                    raise StateConflict('CANDIDATE_PRECEDES_CHECKPOINT')
                batch = detector.update(prices,observed_at=generated_at,mode='LIVE')
                row = asdict(batch.latest) if batch.latest else unknown_row(market,ticker,as_of,'NO_OBSERVATION',generated_at)
                if batch.issue:
                    row = unknown_row(market,ticker,as_of,batch.issue,generated_at)
                if batch.observations:
                    payload = dict(schema='base-instrument-1',instrument=key,parent=old_id,
                        checkpoint=detector.checkpoint(),observations=[asdict(s) for s in batch.observations],
                        events=[asdict(e) for e in batch.events])
                    instruments[key] = write_object(base,payload)
                doc['observations'].append(row)
            except (StateConflict,KeyError,ValueError,TypeError,OSError) as exc:
                reason = str(exc) if isinstance(exc,StateConflict) else 'CACHE_OR_CONFIGURATION_INVALID'
                doc['observations'].append(unknown_row(market,ticker,as_of,reason,generated_at))
        generation = dict(schema='base-generation-1',parent=parent,run_id=run_id,
                          generated_at=generated_at,instruments=instruments,observations=doc['observations'])
        doc['generation'] = write_object(base,generation)
        return doc
    except (StateConflict,ValueError,TypeError,KeyError,OSError):
        return dict(schema=SCHEMA,run_id=run_id,generated_at=generated_at,status='unavailable',
                    generation=prior.get('generation') if prior else None,
                    observations=[unknown_row(m,t,max(d),'DURABLE_STATE_UNAVAILABLE',generated_at) for (m,t),d in sorted(groups.items())])

