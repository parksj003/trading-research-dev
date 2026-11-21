"""Weekly, evidence-only sector review. No providers, credentials or trading I/O.

The optional publication references immutable evidence objects. Only a maintainer's
explicit decision file can affect future classifications; proposals never do.
"""
from __future__ import annotations

import argparse
import copy
import csv
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import tempfile
from zoneinfo import ZoneInfo

SCHEMA='sector-review-1'
FILE_NAME='sector_review.json'
INTERVAL_DAYS=7
MAX_BYTES=8_000_000
OBJECTS=Path('history/sector_review/objects')
KST=ZoneInfo('Asia/Seoul')
FIELDS=('schema','decision_id','candidate_id','proposal_hash','decision','market','ticker',
        'current_macro_theme','current_sector','proposed_macro_theme','proposed_sector',
        'source_hash','reviewer','reviewed_at','effective_from')


def json_bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def digest(value):return hashlib.sha256(json_bytes(value)).hexdigest()


def stamp(value):
    date=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if date.tzinfo is None:raise ValueError('timezone-aware timestamp required')
    return date.astimezone(timezone.utc)


def identity(row):
    market=row.get('market');ticker=str(row.get('ticker','')).strip()
    if market=='KR':ticker=ticker.zfill(6)
    if market=='US':ticker=ticker.upper()
    if market not in ('KR','US') or not re.fullmatch(r'[A-Z0-9^][A-Z0-9^._-]{0,19}',ticker):
        raise ValueError('invalid sector observation identity')
    return market+':'+ticker


def semantic(row):
    # Query times, dates and daily ETF weights are evidence, not industry changes.
    norm=lambda v:' '.join(str(v or '').split()).casefold()
    return dict(name=norm(row.get('name')),raw_sector=norm(row.get('raw_sector')),
        raw_sub_industry=norm(row.get('raw_sub_industry')),provider=row.get('provider'),
        etfs=sorted((str(e['code']),str(e['macro_theme']),str(e['sector'])) for e in row.get('etfs',[])),
        etf_primary=row.get('etf_primary'))


def _object(base,sha):
    if not isinstance(sha,str) or not re.fullmatch(r'[a-f0-9]{64}',sha):raise ValueError('invalid evidence hash')
    path=Path(base)/OBJECTS/(sha+'.json.gz')
    with path.open('rb') as file,gzip.GzipFile(fileobj=file) as stream:raw=stream.read(MAX_BYTES+1)
    if len(raw)>MAX_BYTES or hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('evidence integrity failure')
    value=json.loads(raw)
    if not isinstance(value,dict):raise ValueError('invalid evidence object')
    return value


def _save_object(base,value):
    raw=json_bytes(value);sha=hashlib.sha256(raw).hexdigest()
    if len(raw)>MAX_BYTES:raise ValueError('sector evidence size limit')
    directory=Path(base)/OBJECTS;directory.mkdir(parents=True,exist_ok=True)
    path=directory/(sha+'.json.gz')
    if path.exists():
        if _object(base,sha)!=value:raise ValueError('immutable evidence differs')
        return sha
    fd,temp=tempfile.mkstemp(prefix='.object-',dir=directory)
    try:
        with os.fdopen(fd,'wb') as file:file.write(gzip.compress(raw,mtime=0))
        try:os.link(temp,path)
        except FileExistsError:
            if _object(base,sha)!=value:raise ValueError('immutable evidence conflict')
    finally:Path(temp).unlink(missing_ok=True)
    return sha


def previous_publication(base):
    latest=Path(base)/'latest';manifest=latest/'manifest.json'
    if not manifest.exists():return None
    descriptor=json.loads(manifest.read_bytes())
    meta=descriptor.get('files',{}).get(FILE_NAME)
    if meta is None:return None
    raw=(latest/FILE_NAME).read_bytes()
    if len(raw)>MAX_BYTES or len(raw)!=meta['bytes'] or hashlib.sha256(raw).hexdigest()!=meta['sha256']:
        raise ValueError('sector publication checksum mismatch')
    doc=json.loads(raw)
    if doc.get('schema')!=SCHEMA or doc.get('run_id')!=descriptor.get('run_id'):
        raise ValueError('sector publication lineage mismatch')
    return doc


def retained_classifications(base,*,static_keys=()):
    """Hold previously automatic assignments until an explicit decision changes them."""
    prior=previous_publication(base)
    if prior is None:return {}
    if prior.get('recovery_required') or not prior.get('state_ref'):
        raise ValueError('classification baseline requires verified recovery')
    state=_object(base,prior['state_ref']);retained={}
    for key,row in state['observations'].items():
        if (key not in static_keys and row.get('origin') in ('fallback','etf','retained_classification')
                and row.get('current_macro_theme') and row.get('current_sector')):
            retained[key]=(row['current_macro_theme'],row['current_sector'])
    return retained


def _valid_observation(row,now):
    identity(row);retrieved=stamp(row['retrieved_at'])
    if not all(isinstance(row.get(k),str) and row[k].strip() for k in ('current_macro_theme','current_sector')):return False
    if retrieved>now or now-retrieved>=timedelta(days=INTERVAL_DAYS):return False
    if row.get('source_valid') is False:return False
    if not row.get('provider') or not row.get('source_urls') or not row.get('policy_hash'):return False
    if not (str(row.get('raw_sector','')).strip() or str(row.get('raw_sub_industry','')).strip() or row.get('etfs')):return False
    if row.get('source_as_of'):
        source_day=datetime.fromisoformat(str(row['source_as_of'])).date()
        if source_day>now.astimezone(KST if row['market']=='KR' else ZoneInfo('America/New_York')).date():return False
    return True


def _candidate(before,after,now):
    old_pair=(before.get('external_macro_theme'),before.get('external_sector'))
    new_pair=(after.get('external_macro_theme'),after.get('external_sector'))
    current=(after.get('current_macro_theme'),after.get('current_sector'))
    unknown=not all(new_pair) or any('미분류' in str(v) or str(v).casefold() in ('unknown','일반산업') for v in new_pair)
    locked=after.get('origin')=='manual_override'
    provider_changed=(before.get('provider')!=after.get('provider') or
                      before.get('etf_evidence_complete')!=after.get('etf_evidence_complete'))
    actionable=bool(old_pair!=new_pair and new_pair!=current and not unknown and not locked and not provider_changed)
    reason=('MANUAL_OVERRIDE_LOCKED' if locked else 'PROVIDER_CHANGED' if provider_changed else
            'EXTERNAL_CATEGORY_CHANGED' if actionable else 'SOURCE_CHANGED_REVIEW_ONLY')
    row=dict(market=after['market'],ticker=identity(after).split(':',1)[1],name=after.get('name',''),
        current_macro_theme=current[0],current_sector=current[1],proposed_macro_theme=new_pair[0],
        proposed_sector=new_pair[1],origin=after.get('origin'),before=before,after=after,
        detected_at=now.isoformat(),source_hash=digest(semantic(after)),policy_hash=after['policy_hash'],
        actionable=actionable,reason=reason,status='PENDING' if actionable else 'REVIEW_ONLY')
    row['candidate_id']=digest(row)
    return row


def prepare_publication(base,payloads,*,run_id,generated_at,decisions=()):
    """Prepare optional sidecar; only the main manifest commits the new state."""
    now=stamp(generated_at);prior=None
    try:
        prior=previous_publication(base)
        if prior and prior.get('recovery_required'):
            raise ValueError('restore a verified review reference before resuming')
        state=_object(base,prior['state_ref']) if prior and prior.get('state_ref') else dict(schema='sector-review-state-1',observations={},markets={},candidates=[])
        if state.get('schema')!='sector-review-state-1':raise ValueError('invalid sector state schema')
        state=copy.deepcopy(state);changed=False;market_status={}
        for market,payload in sorted(payloads.items()):
            if market not in ('KR','US'):raise ValueError('unknown review market')
            last=(state['markets'].get(market) or {}).get('checked_at')
            expected_policy=payload.get('policy_hash')
            previous_policy=(state['markets'].get(market) or {}).get('policy_hash')
            policy_changed=bool(last and expected_policy and previous_policy!=expected_policy)
            if policy_changed:
                for candidate in state['candidates']:
                    if candidate['market']==market and candidate['status']=='PENDING':candidate['status']='SUPERSEDED';changed=True
            if last and not policy_changed and now-stamp(last)<timedelta(days=INTERVAL_DAYS):
                market_status[market]={**state['markets'][market],'status':'NOT_DUE'};continue
            rows=payload.get('observations') or []
            if payload.get('stale') or not rows:
                market_status[market]=dict(status='WAITING_FOR_FRESH_SOURCE',checked_at=last,observed=0,blocked=len(rows));continue
            keys=[identity(row) for row in rows]
            if len(set(keys))!=len(keys) or any(row['market']!=market for row in rows):
                raise ValueError('ambiguous review observations')
            valid=[]
            for row in rows:
                try:
                    if _valid_observation(row,now) and (not payload.get('policy_hash') or row['policy_hash']==payload['policy_hash']):valid.append(row)
                except (ValueError,TypeError,KeyError):pass
            if not valid:
                market_status[market]=dict(status='WAITING_FOR_FRESH_SOURCE',checked_at=last,observed=0,blocked=len(rows));continue
            initial=not bool(last)
            for after in valid:
                key=identity(after);before=state['observations'].get(key)
                if before:
                    rules_changed=before.get('policy_hash')!=after.get('policy_hash')
                    source_changed=semantic(before)!=semantic(after)
                    if rules_changed or source_changed:
                        for old in state['candidates']:
                            if identity(old)==key and old['status']=='PENDING':old['status']='SUPERSEDED'
                    if source_changed and not rules_changed:
                        candidate=_candidate(before,copy.deepcopy(after),now)
                        candidate['proposal_hash']=_save_object(base,dict(schema='sector-proposal-1',candidate=candidate))
                        state['candidates'].append(candidate)
                state['observations'][key]=copy.deepcopy(after)
            status=dict(status='BASELINE_CREATED' if initial else 'CHECKED',checked_at=now.isoformat(),
                        observed=len(valid),blocked=len(rows)-len(valid),policy_hash=valid[0]['policy_hash'])
            state['markets'][market]=status;market_status[market]=status;changed=True
        decision_map={d['candidate_id']:d for d in decisions}
        current_observations={identity(row):row for payload in payloads.values()
            for row in (payload.get('observations') or []) if not payload.get('stale') and _valid_observation(row,now)}
        for candidate in state['candidates']:
            decision=decision_map.get(candidate['candidate_id'])
            if decision:
                observed=current_observations.get(identity(candidate))
                target=(candidate['proposed_macro_theme'],candidate['proposed_sector'])
                pair=(observed.get('current_macro_theme'),observed.get('current_sector')) if observed else None
                if decision['decision']=='reject':status='REJECTED'
                elif datetime.fromisoformat(decision['effective_from']).date()>now.astimezone(KST).date():status='SCHEDULED'
                elif observed and observed.get('origin')=='manual_override' and pair!=target:status='APPROVED_BLOCKED'
                elif pair==target:
                    status='APPLIED'
                    if not candidate.get('applied_at'):candidate['applied_at']=now.isoformat();changed=True
                elif candidate.get('applied_at'):status='APPLIED'
                else:status='APPROVED_WAITING'
                if candidate['status']!=status:candidate['status']=status;changed=True
        ref=_save_object(base,state) if changed or not prior else prior.get('state_ref')
        for market,info in state['markets'].items():
            market_status.setdefault(market,{**info,'status':'NOT_REQUESTED'})
        return dict(schema=SCHEMA,run_id=run_id,generated_at=generated_at,status='ready' if ref else 'waiting',
            interval_days=INTERVAL_DAYS,state_ref=ref,markets=market_status,candidates=state['candidates'],
            automatic_application=False)
    except (ValueError,TypeError,KeyError,OSError,EOFError):
        return dict(schema=SCHEMA,run_id=run_id,generated_at=generated_at,status='unavailable',
                    interval_days=INTERVAL_DAYS,state_ref=prior.get('state_ref') if prior else None,
                    markets={},candidates=[],automatic_application=False,
                    recovery_required=not bool(prior and prior.get('state_ref')))


def make_decision(candidate,*,decision,reviewer,reviewed_at,effective_from=''):
    row=dict(schema='sector-review-decision-1',decision=decision,reviewer=reviewer.strip(),
             reviewed_at=reviewed_at,effective_from=effective_from)
    for field in ('candidate_id','proposal_hash','market','ticker','current_macro_theme','current_sector',
                  'proposed_macro_theme','proposed_sector','source_hash'):row[field]=candidate[field]
    row['decision_id']=digest(row)
    return row


def _validate_decision(base,row,now):
    if set(row)!=set(FIELDS) or row['schema']!='sector-review-decision-1':raise ValueError('decision schema mismatch')
    if row['decision_id']!=digest({k:v for k,v in row.items() if k!='decision_id'}):raise ValueError('decision checksum mismatch')
    envelope=_object(base,row['proposal_hash'])
    if envelope.get('schema')!='sector-proposal-1':raise ValueError('proposal schema mismatch')
    candidate=envelope['candidate']
    for key in ('candidate_id','market','ticker','current_macro_theme','current_sector','proposed_macro_theme','proposed_sector','source_hash'):
        if row[key]!=candidate[key]:raise ValueError('decision does not match evidence')
    if not candidate.get('actionable') or row['decision'] not in ('approve','reject'):raise ValueError('proposal is not actionable')
    reviewed=stamp(row['reviewed_at'])
    if not row['reviewer'].strip() or '\n' in row['reviewer'] or len(row['reviewer'])>100:raise ValueError('reviewer required')
    if reviewed>now or reviewed<stamp(candidate['detected_at']):raise ValueError('invalid review time')
    if row['decision']=='approve':
        effective=datetime.fromisoformat(row['effective_from']).date()
        if effective<=reviewed.astimezone(KST).date():raise ValueError('approval must take effect after the review date')
    return candidate


def _read_decisions(path):
    if not Path(path).exists():return []
    if Path(path).stat().st_size>MAX_BYTES:raise ValueError('decision file too large')
    with Path(path).open(encoding='utf-8-sig',newline='') as file:
        reader=csv.DictReader(file)
        if tuple(reader.fieldnames or ())!=FIELDS:raise ValueError('decision CSV columns mismatch')
        return list(reader)


def load_decisions(path,base,*,now):
    """Read explicitly maintained decisions. Invalid history never silently reverts."""
    now=stamp(now);rows=_read_decisions(path);seen=set();active={};order={}
    for row in rows:
        _validate_decision(base,row,now)
        if row['candidate_id'] in seen:raise ValueError('duplicate/conflicting decision')
        seen.add(row['candidate_id'])
        if row['decision']!='approve' or datetime.fromisoformat(row['effective_from']).date()>now.astimezone(KST).date():continue
        key=identity(row);when=(row['effective_from'],stamp(row['reviewed_at']))
        if key in order and order[key]==when:raise ValueError('ambiguous effective classification')
        if key not in order or when>order[key]:
            active[key]=(row['proposed_macro_theme'],row['proposed_sector']);order[key]=when
    prior=previous_publication(base)
    if prior:
        recorded={c['candidate_id'] for c in prior.get('candidates',[]) if c['status'] in ('APPLIED','SCHEDULED','REJECTED','APPROVED_WAITING','APPROVED_BLOCKED')}
        if not recorded.issubset(seen):raise ValueError('previously recorded review decisions are missing')
    return active,rows


def apply_decision(base,decision,path,*,now,apply=False):
    """CAS-protected explicit maintainer action. Dry-run is the default."""
    now=stamp(now);_validate_decision(base,decision,now)
    path=Path(path);original=path.read_bytes() if path.exists() else None
    rows=_read_decisions(path)
    for old in rows:
        if old['candidate_id']==decision['candidate_id']:
            if old!=decision:raise ValueError('decision already recorded differently')
            return dict(status='ALREADY_RECORDED',decision_id=decision['decision_id'])
    if decision['decision']=='approve' and datetime.fromisoformat(decision['effective_from']).date()<=now.astimezone(KST).date():
        raise ValueError('effective date must also follow the actual record-creation date')
    latest=previous_publication(base)
    if not latest or latest.get('status')!='ready':raise ValueError('verified current review publication required')
    current=next((c for c in latest['candidates'] if c['candidate_id']==decision['candidate_id']),None)
    if not current or current['status']!='PENDING' or current['proposal_hash']!=decision['proposal_hash']:
        raise ValueError('proposal was superseded or is no longer pending')
    if now-stamp(latest['generated_at'])>timedelta(days=INTERVAL_DAYS):raise ValueError('review publication is stale')
    state=_object(base,latest['state_ref'])
    observation=state['observations'].get(identity(decision))
    checked=(latest.get('markets',{}).get(decision['market']) or {}).get('checked_at')
    if (not checked or now-stamp(checked)>=timedelta(days=INTERVAL_DAYS) or not observation
            or not _valid_observation(observation,now) or digest(semantic(observation))!=decision['source_hash']
            or observation['policy_hash']!=current['policy_hash']):
        raise ValueError('latest source verification is stale or changed')
    for old in rows:_validate_decision(base,old,now)
    if not apply:return dict(status='DRY_RUN',decision_id=decision['decision_id'])
    path.parent.mkdir(parents=True,exist_ok=True);lock=path.with_suffix(path.suffix+'.lock')
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    except FileExistsError as exc:raise ValueError('decision file is locked') from exc
    temp=None
    try:
        os.close(fd)
        current_bytes=path.read_bytes() if path.exists() else None
        if current_bytes!=original:raise ValueError('decision file changed during review')
        stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows+[decision])
        fd,temp=tempfile.mkstemp(prefix='.decisions-',dir=path.parent)
        with os.fdopen(fd,'w',encoding='utf-8',newline='') as file:file.write(stream.getvalue())
        os.replace(temp,path);temp=None
    finally:
        if temp:Path(temp).unlink(missing_ok=True)
        lock.unlink(missing_ok=True)
    return dict(status='RECORDED',decision_id=decision['decision_id'],effective_from=decision['effective_from'])


def main():
    parser=argparse.ArgumentParser(description='Validate an explicit sector-review decision; default is dry-run')
    parser.add_argument('--base',required=True,help='published directory containing verified review evidence')
    parser.add_argument('--decision',required=True,help='JSON approval/rejection exported by the review UI')
    parser.add_argument('--decisions-csv',default='watchlist/sector_review_decisions.csv')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();decision=json.loads(Path(args.decision).read_bytes())
    print(json.dumps(apply_decision(args.base,decision,args.decisions_csv,
        now=datetime.now(timezone.utc).isoformat(),apply=args.apply),ensure_ascii=False))


if __name__=='__main__':main()

