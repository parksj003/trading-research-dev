"""Experimental checkpoint encoding. NOT imported by the operating publisher.

Uses existing immutable-object primitives but never chooses an operating path or
block size. Hydration restores the EXACT legacy payload and checksum before the
unchanged BaseDetector validates its expected lineage. No old hash is omitted.
"""
import copy
import hashlib
import re
from pathlib import Path

import pandas as pd

from watchlist.base_detector import BaseDetector,StateConflict
from watchlist.base_count_publish import json_bytes,write_object,read_object,OBJECT_DIR

SCHEMA='base-instrument-blocks-experiment-2'
CHUNK_SCHEMA='base-seen-block-experiment-1'
HEX=re.compile(r'^[0-9a-f]{64}$')


def _sha(value):return hashlib.sha256(json_bytes(value)).hexdigest()


def _put(base,payload):
    reference=_sha(payload)
    if (Path(base)/OBJECT_DIR/(reference+'.json.gz')).exists():
        if read_object(base,reference)!=payload:raise StateConflict('Existing block differs')
        return reference
    return write_object(base,payload)


def _entry(date,fingerprint):
    if not isinstance(date,str) or not isinstance(fingerprint,str) or not HEX.fullmatch(fingerprint):
        raise StateConflict('Invalid historical fingerprint entry')
    try:
        parsed=pd.Timestamp(date)
        if pd.isna(parsed) or parsed.tzinfo is not None or parsed.strftime('%Y-%m-%d')!=date:
            raise ValueError('noncanonical date')
    except (ValueError,TypeError):raise StateConflict('Invalid fingerprint date')


def store_instrument(base,payload,*,block_size):
    """Create experimental objects only; caller explicitly supplies storage policy."""
    if type(block_size) is not int or block_size<1:raise ValueError('explicit positive integer block_size required')
    try:
        if payload['schema']!='base-instrument-1':raise StateConflict('Legacy payload required')
        original=copy.deepcopy(payload);checkpoint=original['checkpoint']
        if checkpoint['sha256']!=_sha(checkpoint['payload']):raise StateConflict('Original checkpoint checksum mismatch')
        seen=checkpoint['payload']['state']['seen']
        if not isinstance(seen,dict):raise StateConflict('Fingerprint map required')
        entries=sorted(seen.items())
        for date,fingerprint in entries:_entry(date,fingerprint)
        references=[]
        for start in range(0,len(entries),block_size):
            references.append(_put(base,dict(schema=CHUNK_SCHEMA,entries=[list(x) for x in entries[start:start+block_size]])))
        skeleton=copy.deepcopy(original)
        del skeleton['checkpoint']['payload']['state']['seen']
        root=dict(schema=SCHEMA,block_size=block_size,seen_count=len(entries),seen_blocks=references,
                  original_payload_sha256=_sha(original),legacy_payload_without_seen=skeleton)
        return _put(base,root)
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,StateConflict):raise
        raise StateConflict('Invalid instrument payload') from exc


def load_instrument(base,reference,*,expected_context):
    """Read v1 or experimental v2; fail closed, with no fresh-detector fallback."""
    try:
        root=read_object(base,reference)
        if root.get('schema')=='base-instrument-1':
            payload=root
        elif root.get('schema')==SCHEMA:
            size=root['block_size'];count=root['seen_count'];refs=root['seen_blocks']
            if type(size) is not int or size<1 or type(count) is not int or count<0 or not isinstance(refs,list):
                raise StateConflict('Invalid block manifest')
            if len(refs)!=(count+size-1)//size:raise StateConflict('Fingerprint block count mismatch')
            seen={};previous=None
            for index,ref in enumerate(refs):
                block=read_object(base,ref)
                if block.get('schema')!=CHUNK_SCHEMA or not isinstance(block.get('entries'),list):
                    raise StateConflict('Invalid fingerprint block')
                expected=min(size,count-index*size)
                if len(block['entries'])!=expected:raise StateConflict('Fingerprint block length mismatch')
                for pair in block['entries']:
                    if not isinstance(pair,list) or len(pair)!=2:raise StateConflict('Invalid fingerprint pair')
                    date,fingerprint=pair;_entry(date,fingerprint)
                    if previous is not None and date<=previous:raise StateConflict('Reordered/duplicate historical fingerprints')
                    seen[date]=fingerprint;previous=date
            if len(seen)!=count:raise StateConflict('Incomplete historical fingerprint map')
            payload=copy.deepcopy(root['legacy_payload_without_seen'])
            if 'seen' in payload['checkpoint']['payload']['state']:raise StateConflict('Unexpected duplicate seen map')
            payload['checkpoint']['payload']['state']['seen']=seen
            if _sha(payload)!=root['original_payload_sha256']:raise StateConflict('Reconstructed instrument checksum mismatch')
        else:raise StateConflict('Unsupported instrument schema')
        if payload['schema']!='base-instrument-1':raise StateConflict('Invalid restored schema')
        identity=payload['checkpoint']['payload']['context']
        if payload.get('instrument')!=identity['market']+':'+identity['ticker']:
            raise StateConflict('Instrument identity does not match checkpoint')
        BaseDetector.restore(payload['checkpoint'],**expected_context)
        return payload
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,StateConflict):raise
        raise StateConflict('Invalid blocked checkpoint') from exc

