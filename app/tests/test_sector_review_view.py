from copy import deepcopy
from datetime import datetime,timedelta,timezone
import hashlib
import json

import pytest
from streamlit.testing.v1 import AppTest

import sector_review_view as view


def payload():
    now=datetime.now(timezone.utc)
    evidence=dict(market='US',ticker='SYNTH',name='Synthetic',raw_sector='Old',raw_sub_industry='Old',
        retrieved_at=(now-timedelta(days=1)).isoformat(),source_urls=['https://example.org/industry'])
    row=dict(candidate_id='a'*64,proposal_hash='b'*64,source_hash='c'*64,market='US',ticker='SYNTH',
        name='Synthetic',current_macro_theme='Old',current_sector='Old sub',proposed_macro_theme='New',
        proposed_sector='New sub',status='PENDING',actionable=True,reason='EXTERNAL_CATEGORY_CHANGED',
        detected_at=(now-timedelta(hours=1)).isoformat(),before=evidence,after={**evidence,'raw_sector':'New'})
    return dict(schema=view.SCHEMA,run_id='run',generated_at=now.isoformat(),status='ready',
        automatic_application=False,markets={'US':dict(status='CHECKED',observed=1,blocked=0,checked_at=now.isoformat())},candidates=[row])


def bundle(doc=None):
    doc=payload() if doc is None else doc;raw=view.canonical(doc)
    return raw,dict(run_id='run',files={view.FILE_NAME:dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest())})


def test_legacy_and_verified_optional_review_payload():
    assert view.load_sector_review(None,{'files':{}})==({},{'status':'absent'})
    raw,man=bundle();doc,status=view.load_sector_review(raw,man)
    assert status['status']=='ready' and doc['candidates'][0]['current_sector']=='Old sub'


@pytest.mark.parametrize('problem',['hash','size','run','schema','auto_apply','duplicate','future','source_identity','source_url','status','bool','market_time','market_count','market_status'])
def test_corrupt_or_unsafe_review_payload_is_unavailable(problem):
    doc=payload();c=doc['candidates'][0]
    if problem=='run':doc['run_id']='other'
    if problem=='schema':doc['schema']='other'
    if problem=='auto_apply':doc['automatic_application']=True
    if problem=='duplicate':doc['candidates']*=2
    if problem=='future':c['detected_at']='2099-01-01T00:00:00Z'
    if problem=='source_identity':c['after']['ticker']='OTHER'
    if problem=='source_url':c['after']['source_urls']=['javascript:alert(1)']
    if problem=='status':c['status']='invented'
    if problem=='bool':c['actionable']='true'
    if problem=='market_time':doc['markets']['US']['checked_at']='broken'
    if problem=='market_count':doc['markets']['US']['observed']=True
    if problem=='market_status':doc['markets']['US']['status']='invented'
    raw,man=bundle(doc)
    if problem=='hash':raw+=b' '
    if problem=='size':man['files'][view.FILE_NAME]['bytes']+=1
    assert view.load_sector_review(raw,man)[1]['status']=='unavailable'


def test_export_is_forward_effective_and_does_not_change_candidate():
    row=payload()['candidates'][0];before=deepcopy(row);now=datetime.now(timezone.utc)
    tomorrow=(now.astimezone(view.KST).date()+timedelta(days=1)).isoformat()
    result=view.build_decision(row,decision='approve',reviewer='Maintainer',reviewed_at=now.isoformat(),effective_from=tomorrow)
    assert result['candidate_id']==row['candidate_id'] and row==before
    assert result['decision_id']==hashlib.sha256(view.canonical({k:v for k,v in result.items() if k!='decision_id'})).hexdigest()
    with pytest.raises(ValueError):view.build_decision(row,decision='approve',reviewer='M',reviewed_at=now.isoformat(),effective_from=now.date().isoformat())


def test_review_screen_exposes_evidence_and_requires_confirmation():
    raw,man=bundle()
    code='''
import json
import sector_review_view as view
doc,status=view.load_sector_review(RAW,MAN)
view.render_sector_review(doc,status)
'''.replace('RAW',repr(raw)).replace('MAN',repr(man))
    at=AppTest.from_string(code).run()
    assert not at.exception and len(at.dataframe)==1
    assert any('主力' in x.value or '主' in x.value or '사업 변화' in x.value for x in at.caption)
    at.button[0].click().run()
    assert not at.exception and len(at.error)==1
    assert not at.get('download_button')
    at.text_input[0].set_value('Maintainer');at.checkbox[0].check();at.button[0].click().run()
    assert not at.exception and len(at.get('download_button'))==1
    assert any('다운로드만으로 적용되지 않습니다' in x.value for x in at.info)


def test_unavailable_or_baseline_review_does_not_invent_candidates():
    at=AppTest.from_string("import sector_review_view as v;v.render_sector_review({}, {'status':'unavailable'})").run()
    assert not at.exception and len(at.warning)==1 and not at.get('download_button')
    doc=payload();doc['candidates']=[]
    at=AppTest.from_string('import sector_review_view as v;v.render_sector_review('+repr(doc)+", {'status':'ready'})").run()
    assert not at.exception and len(at.info)==1 and not at.selectbox

