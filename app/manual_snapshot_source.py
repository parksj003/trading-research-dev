"""Read only the optional EOD publication, with immutable-object integrity."""
import json
import streamlit as st
from manual_eod_contract import SCHEMA,digest,canonical,load_snapshot


def _path(pointer):
    import re
    if pointer.get('schema')!=SCHEMA:raise ValueError('unsupported descriptor')
    info=pointer.get('snapshot') or {};sha=info.get('sha256','')
    if not re.fullmatch('[a-f0-9]{64}',str(sha)) or info.get('file')!=f'manual_eod/objects/{sha}.json.gz':raise ValueError('invalid snapshot path')
    return info['file']


@st.cache_data(ttl=300,max_entries=2,show_spinner=False)
def load_published_eod(source_key,_read):
    del source_key
    errors=[]
    for rel in ('manual_eod/latest.json','manual_eod/previous.json'):
        try:
            raw=_read(rel)
            if raw is None:continue
            pointer=json.loads(raw);path=_path(pointer);doc=load_snapshot(pointer,_read(path))
            try:
                core_raw=_read('latest/manifest.json');core=json.loads(core_raw) if core_raw else {}
            except Exception:
                core={}  # The price object remains valid; explicitly show linkage pending.
            if core.get('run_id')==doc['core_run_id'] and digest(canonical(core))!=doc['core_manifest_sha256']:
                raise ValueError('linked core manifest changed')
            pending=core.get('run_id')!=doc['core_run_id']
            return doc,dict(status='previous' if rel.endswith('previous.json') else 'pending' if pending else 'ready',
                pending_core=pending,reason='이전 정상 EOD 게시본 사용' if errors else '',errors=errors)
        except Exception:
            # No token, URL or private source exception detail reaches the UI.
            errors.append(rel+' 검증/조회 실패')
    return {},dict(status='unavailable' if errors else 'absent',reason='; '.join(errors))

