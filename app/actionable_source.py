"""Bounded, on-demand reads of public publication paths at immutable Git SHAs."""
import json
import re

import requests
from actionable_changes import verified_snapshot, MAX_BYTES

MAX_COMMITS=88
MAX_SNAPSHOTS=44
MAX_REQUESTS=221
MAX_TOTAL_BYTES=32_000_000
MAX_TOTAL_ROWS=100_000


def load_public_snapshots(src, run_id, get=requests.get, snapshot_limit=8):
    if snapshot_limit not in (8,44):return [],["게시 이력 조회 범위 오류"]
    commit_limit=min(MAX_COMMITS,snapshot_limit*2)
    request_limit=1+commit_limit+snapshot_limit*3
    if src.get('mode')!='github':return [],['로컬 소스에는 GitHub 게시 이력 조회를 적용하지 않습니다.']
    if src.get('root') not in ('watchlist/published','published') or not re.fullmatch(r'[\w.-]+/[\w.-]+',src.get('repo','')):
        return [],['허용된 공개 게시 경로가 아닙니다.']
    headers={'Accept':'application/vnd.github.raw+json'}
    if src.get('token'):headers['Authorization']='Bearer '+src['token']
    base=f"https://api.github.com/repos/{src['repo']}"
    calls=0;total_bytes=0;total_rows=0
    def read(url,params,limit=MAX_BYTES):
        nonlocal calls,total_bytes
        calls+=1
        if calls>request_limit:raise ValueError('조회 요청 한도 도달')
        # Response bodies and tokens are never included in displayed failures.
        request_headers=dict(headers)
        if url.endswith('/commits'):request_headers['Accept']='application/vnd.github+json'
        response=get(url,params=params,headers=request_headers,timeout=(5,20),stream=True)
        try:
            response.raise_for_status();chunks=[];size=0
            for chunk in response.iter_content(chunk_size=65536):
                size+=len(chunk);total_bytes+=len(chunk)
                if total_bytes>MAX_TOTAL_BYTES:raise ValueError("누적 게시 응답 크기 제한 초과")
                if size>limit:raise ValueError('게시 응답 크기 제한 초과')
                chunks.append(chunk)
            return b''.join(chunks)
        finally:response.close()
    snapshots=[];warnings=[];seen=set();anchor=False
    try:
        commits=json.loads(read(base+'/commits',{'path':src['root']+'/latest/manifest.json',
                                                'sha':src.get('branch','main'),'per_page':commit_limit},1_000_000))
        if not isinstance(commits,list):raise ValueError('게시 이력 응답 오류')
        for record in commits[:commit_limit]:
            if not isinstance(record,dict):continue
            sha=record.get('sha','')
            if not re.fullmatch('[a-f0-9]{40}',sha):continue
            def file(name,limit=MAX_BYTES):
                return read(base+'/contents/'+src['root']+'/latest/'+name,{'ref':sha},limit)
            raw=file('manifest.json',100_000);manifest=json.loads(raw)
            if not isinstance(manifest,dict):raise ValueError('manifest 구조 오류')
            rid=manifest.get('run_id')
            if rid==run_id:anchor=True
            if not anchor or rid in seen:continue
            seen.add(rid)
            try:
                files={name:file(name) for name in ('summary.json','watchlist.csv','candidates.csv')}
                snapshot=verified_snapshot(raw,files,sha)
                total_rows+=len(snapshot.watch)+len(snapshot.candidates)
                if total_rows>MAX_TOTAL_ROWS:raise ValueError("누적 행 수 제한 초과")
                snapshots.append(snapshot)
            except ValueError:
                warnings.append('일부 게시본의 무결성/데이터 검증 실패 — 해당 게시본 제외')
            if len(snapshots)>=snapshot_limit:break
        if not anchor:return [],['현재 run_id의 고정 게시 commit을 찾지 못했습니다. 새로고침 후 다시 확인하세요.']
    except (requests.RequestException,ValueError,KeyError,TypeError):
        warnings.append('게시 이력 조회 실패 또는 요청/크기 한도 도달 — 확인된 자료만 사용합니다.')
    if not any(s.manifest['run_id']==run_id for s in snapshots):
        return [],warnings+['현재 게시본 무결성 검증 실패로 비교를 중단했습니다.']
    return snapshots,warnings

