"""Shared bounded research bundle contract; separate from core manifests."""
import gzip
import io
import json
import re
import math
from collections import Counter
from hashlib import sha256
from datetime import datetime

SCHEMA='exit-experiments-1.0'
VERSION='exit-research-v2'
MAX_COMPRESSED=16_000_000
MAX_EXPANDED=100_000_000
FILES=('results.json.gz','paths.json.gz','statistics.json.gz','pairs.json.gz')
RULE_IDS=('HOLD','FIXED','TARGET_2R','MA20_2C','MA50_2C','ATR_TRAIL','DONCHIAN_10')


def canonical(doc):return json.dumps(doc,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()
def digest(raw):return sha256(raw).hexdigest()
def pack(doc):
    plain=canonical(doc)
    if len(plain)>MAX_EXPANDED:raise ValueError('RESEARCH_EXPANDED_LIMIT')
    raw=gzip.compress(plain,mtime=0)
    if len(raw)>MAX_COMPRESSED:raise ValueError('RESEARCH_COMPRESSED_LIMIT')
    return raw


def _pairs(items):
    out={}
    for k,v in items:
        if k in out:raise ValueError('DUPLICATE_JSON_KEY')
        out[k]=v
    return out


def json_doc(raw):
    def bad(_):raise ValueError('NONFINITE_JSON')
    return json.loads(raw,object_pairs_hook=_pairs,parse_constant=bad)


def unpack(raw,info):
    if not isinstance(raw,bytes) or len(raw)>MAX_COMPRESSED or len(raw)!=info.get('bytes') or digest(raw)!=info.get('sha256'):raise ValueError('RESEARCH_FILE_INTEGRITY')
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:plain=stream.read(MAX_EXPANDED+1)
    if len(plain)>MAX_EXPANDED:raise ValueError('RESEARCH_EXPANDED_LIMIT')
    return json_doc(plain)


def manifest_path(pointer):
    sha=pointer.get('snapshot_id','')
    if pointer.get('schema')!=SCHEMA or pointer.get('experiment_version')!=VERSION or not re.fullmatch('[a-f0-9]{64}',str(sha)):raise ValueError('RESEARCH_POINTER_SCHEMA')
    expected=f'exit_experiments/{VERSION}/runs/{sha}/manifest.json'
    if pointer.get('manifest_file')!=expected:raise ValueError('RESEARCH_PATH_UNSAFE')
    return expected


def validate_manifest(man,pointer,raw):
    manifest_path(pointer)
    if len(raw)>4_000_000 or digest(raw)!=pointer.get('manifest_sha256'):raise ValueError('RESEARCH_MANIFEST_INTEGRITY')
    if man.get('schema')!=SCHEMA or man.get('experiment_version')!=VERSION or man.get('snapshot_id')!=pointer['snapshot_id']:raise ValueError('RESEARCH_MANIFEST_SCHEMA')
    if set(man.get('files',{}))!=set(FILES) or man.get('rules')!=list(RULE_IDS):raise ValueError('RESEARCH_FILE_CONTRACT')
    for key in ('experiment_config_hash','registry_sha256','source_manifest_sha256','source_signals_sha256'):
        if not re.fullmatch('[a-f0-9]{64}',str(man.get(key,''))):raise ValueError('RESEARCH_LINEAGE_HASH')
    for key in ('prospective_start_at','source_generated_at','evaluation_cutoff_at'):
        dt=datetime.fromisoformat(man[key].replace('Z','+00:00'))
        if dt.tzinfo is None:raise ValueError('RESEARCH_TIMEZONE')
    for name,info in man['files'].items():
        if not isinstance(info,dict) or type(info.get('bytes')) is not int or not 0<info['bytes']<=MAX_COMPRESSED or not re.fullmatch('[a-f0-9]{64}',str(info.get('sha256',''))):raise ValueError('RESEARCH_FILE_DESCRIPTOR')
    return man


def validate_payload(man,data):
    rows=data['results.json.gz'];paths=data['paths.json.gz'];stats=data['statistics.json.gz'];pairs=data['pairs.json.gz']
    if not isinstance(rows,list) or len(rows)>210_000 or not isinstance(paths,dict) or not isinstance(stats,list) or not isinstance(pairs,list):raise ValueError('RESEARCH_PAYLOAD_SHAPE')
    keys=set();identities={}
    for row in rows:
        key=(row['signal_id'],row['rule_id'],row['horizon'])
        if key in keys or row['rule_id'] not in RULE_IDS or row['horizon'] not in (20,60,120):raise ValueError('RESEARCH_ROW_IDENTITY')
        keys.add(key)
        if row.get('rule_version')!=VERSION or row.get('experiment_config_hash')!=man['experiment_config_hash']:raise ValueError('RESEARCH_ROW_VERSION')
        if row.get('comparison_eligible'):
            if row.get('eligibility_status')!='eligible' or row.get('completion_status')!='complete' or not row.get('maturity_reached') or not row.get('horizon_complete') or not row.get('r_eligible'):raise ValueError('RESEARCH_COHORT_INVALID')
        if row.get('eligibility_status')=='eligible':
            entry=row.get('entry_price');risk=row.get('initial_risk');cost=row.get('total_cost')
            if type(entry) not in (int,float) or not math.isfinite(entry) or entry<=0:raise ValueError('RESEARCH_ENTRY_PRICE')
            if row.get('r_eligible') and (type(risk) not in (int,float) or not math.isfinite(risk) or risk<=0):raise ValueError('RESEARCH_INITIAL_RISK')
            if row.get('exit_price') is not None:
                exit=row['exit_price']
                if not 1<=row['holding_sessions']<=row['horizon'] or not row['entry_date']<=row['exit_fill_date']<=row['horizon_end'] or exit<=0:raise ValueError('RESEARCH_FILL_TIMING')
                if abs(row['net_return_pct']-((exit/entry-1)*100-row['round_trip_cost_pct']))>1e-8:raise ValueError('RESEARCH_COST_MISMATCH')
                if row.get('r_eligible') and abs(row['net_R']-(exit-entry-cost)/risk)>1e-8:raise ValueError('RESEARCH_R_MISMATCH')
            logs=row.get('stop_path',[])
            if row.get('stop_path_ref'):
                if row['stop_path_ref']!=row['rule_id']:raise ValueError('RESEARCH_STOP_REFERENCE')
                logs=paths[row['signal_id']]['effective_stops'][row['rule_id']][:row['stop_path_length']]
                if len(logs)!=row['stop_path_length']:raise ValueError('RESEARCH_STOP_REFERENCE')
            for i,item in enumerate(logs):
                if item['information_as_of'] is None or item['information_as_of']>=item['effective_date']:raise ValueError('RESEARCH_LOOKAHEAD_STOP')
                if item['active_stop']<row['initial_stop']:raise ValueError('RESEARCH_STOP_BELOW_INITIAL')
                if i and (item['effective_date']<=logs[i-1]['effective_date'] or row['rule_id']=='ATR_TRAIL' and item['active_stop']<logs[i-1]['active_stop']):raise ValueError('RESEARCH_STOP_SEQUENCE')
            identity=tuple(row.get(k) for k in ('entry_date','entry_price','initial_stop','initial_risk','total_cost','price_path_hash','source_config_hash'))
            old=identities.setdefault(row['signal_id'],identity)
            if old!=identity:raise ValueError('RESEARCH_COMMON_ENTRY_MISMATCH')
    if any(count!=21 for count in Counter(r['signal_id'] for r in rows).values()):raise ValueError('RESEARCH_MISSING_POLICY_ROWS')
    scopes=set()
    for scope in stats:
        key=(scope['vintage'],scope['market'])
        if key in scopes or scope['vintage'] not in ('all','prospective','retrospective') or scope['market'] not in ('ALL','KR','US'):raise ValueError('RESEARCH_STAT_SCOPE')
        scopes.add(key)
        if sorted(s['horizon'] for s in scope['horizons'])!=[20,60,120]:raise ValueError('RESEARCH_STAT_HORIZON')
        for summary in scope['horizons']:
            if [r['rule_id'] for r in summary['policies']]!=list(RULE_IDS):raise ValueError('RESEARCH_STAT_POLICIES')
            if any(type(r['n']) is not int or r['n']!=summary['common_complete_n'] or r['n']<0 for r in summary['policies']):raise ValueError('RESEARCH_STAT_SAMPLE')
    if len(scopes)!=9:raise ValueError('RESEARCH_STAT_SCOPE')
    lookup={(r['signal_id'],r['rule_id'],r['horizon']):r for r in rows};seen=set()
    for pair in pairs:
        key=(pair['signal_id'],pair['horizon'],pair['rule_id'],pair['baseline'])
        if key in seen or pair['baseline'] not in ('FIXED','HOLD') or pair['rule_id']==pair['baseline']:raise ValueError('RESEARCH_PAIR_IDENTITY')
        seen.add(key)
        a=lookup[(pair['signal_id'],pair['rule_id'],pair['horizon'])];b=lookup[(pair['signal_id'],pair['baseline'],pair['horizon'])]
        if not a.get('comparison_eligible') or not b.get('comparison_eligible') or abs(pair['delta_net_R']-(a['net_R']-b['net_R']))>1e-8:raise ValueError('RESEARCH_PAIR_VALUE')

    return data


