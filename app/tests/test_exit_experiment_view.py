from copy import deepcopy
import json
import base64,gzip
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest
from exit_experiment_view import load_exit_publication,comparison_figures,signal_figure,RULE_IDS
from exit_experiment_contract import canonical,digest,pack,VERSION

FIXTURE=Path(__file__).parent/'fixtures/exit_research_synthetic.json.gz.b64'


def doc():return json.loads(gzip.decompress(base64.b64decode(FIXTURE.read_bytes())))


def files_for(data):
    d=deepcopy(data);mapping={'results.json.gz':d['results'],'paths.json.gz':d['paths'],'statistics.json.gz':d['statistics'],'pairs.json.gz':d['pairs']}
    raw={name:pack(value) for name,value in mapping.items()}
    d['manifest']['files']={name:{'bytes':len(value),'sha256':digest(value)} for name,value in raw.items()}
    manifest=canonical(d['manifest']);d['pointer']['manifest_sha256']=digest(manifest);directory=d['pointer']['manifest_file'].rsplit('/',1)[0]
    return {**{directory+'/'+k:v for k,v in raw.items()},directory+'/manifest.json':manifest,
        'exit_experiments/'+VERSION+'/registry.json':canonical(d['registry']),
        'exit_experiments/'+VERSION+'/latest.json':canonical(d['pointer'])}


@pytest.fixture(autouse=True)
def reset_cache():
    load_exit_publication.clear()
    yield
    load_exit_publication.clear()


def app():
    return AppTest.from_string('from pathlib import Path\nimport json,base64,gzip\nfrom exit_experiment_view import render_exit_experiments\ndoc=json.loads(gzip.decompress(base64.b64decode(Path('+repr(str(FIXTURE))+').read_bytes())))\nrender_exit_experiments(doc,{"status":"ready"})',default_timeout=30)


def test_loader_verifies_all_hashes_and_never_requests_other_namespaces():
    data=doc();files=files_for(data);requested=[]
    def read(path):requested.append(path);return files.get(path)
    actual,state=load_exit_publication('fixture',data['manifest']['source_run_id'],read)
    assert state['status']=='ready' and len(actual['results'])==21
    assert all(p.startswith('exit_experiments/'+VERSION+'/') for p in requested)
    assert all('input_evidence' not in p for p in actual['paths'].values())
    again,_=load_exit_publication('fixture',data['manifest']['source_run_id'],lambda p:pytest.fail('cached bundle refetched'))
    assert again==actual


@pytest.mark.parametrize('case',['unsafe_path','bad_manifest_hash','bad_file_hash','registry','duplicate_row','entry_mismatch','causal_stop'])
def test_corrupt_payload_is_rejected_without_partial_statistics(case):
    data=doc()
    if case=='duplicate_row':data['results'].append(data['results'][0])
    elif case=='entry_mismatch':data['results'][1]['entry_price']=101
    elif case=='causal_stop':
        path=next(iter(data['paths'].values()));path['effective_stops']['ATR_TRAIL'][0]['information_as_of']=path['effective_stops']['ATR_TRAIL'][0]['effective_date']
    files=files_for(data);root='exit_experiments/'+VERSION
    ptr=json.loads(files[root+'/latest.json'])
    if case=='unsafe_path':ptr['manifest_file']='../../private/account.json';files[root+'/latest.json']=canonical(ptr)
    elif case=='bad_manifest_hash':ptr['manifest_sha256']='0'*64;files[root+'/latest.json']=canonical(ptr)
    elif case=='bad_file_hash':
        path=ptr['manifest_file'].rsplit('/',1)[0]+'/results.json.gz';files[path]+=b'bad'
    elif case=='registry':files[root+'/registry.json']=b'{}'
    actual,state=load_exit_publication('fixture',data['manifest']['source_run_id'],files.get)
    assert not actual and state['status']=='unavailable'


def test_previous_good_publication_and_new_core_are_explicit():
    data=doc();files=files_for(data);root='exit_experiments/'+VERSION
    files[root+'/previous.json']=files[root+'/latest.json'];files[root+'/latest.json']=b'corrupt'
    actual,state=load_exit_publication('fixture','new-core',files.get)
    assert actual and state['status']=='previous'


def test_first_screen_separates_prospective_and_warns_before_comparison():
    at=app().run();assert not at.exception
    assert at.selectbox(key='exit_vintage').value=='전향적'
    assert any('20건 미만' in x.value for x in at.warning)
    at.selectbox(key='exit_vintage').set_value('회고적').run()
    assert not at.exception
    table=at.dataframe[0].value
    assert len(table)==7 and table['전략'].str.split(' · ').str[0].tolist()==list(RULE_IDS)
    assert table['공통 적격 n'].tolist()==[1]*7
    assert at.radio(key='exit_horizon').value==20


def test_horizons_and_pairwise_controls_do_not_fetch_or_backtest(monkeypatch):
    import requests
    monkeypatch.setattr(requests,'get',lambda *a,**k:pytest.fail('research UI fetched prices'))
    at=app().run();at.selectbox(key='exit_vintage').set_value('회고적').run()
    for n in (20,60,120):
        at.radio(key='exit_horizon').set_value(n).run();assert not at.exception
    at.radio(key='exit_cohort').set_value('FIXED와 쌍별 · 참고').run()
    assert not at.exception and any('각 행의 공통 신호가 다릅니다' in c.value for c in at.caption)
    assert at.dataframe[0].value.iloc[1]['공통 적격 n'] is None or __import__('pandas').isna(at.dataframe[0].value.iloc[1]['공통 적격 n'])


@pytest.mark.parametrize('theme',['light','dark'])
def test_chart_policy_names_and_effective_atr_dates_are_actual_labels(theme):
    d=doc();scope=next(x for x in d['statistics'] if x['vintage']=='retrospective' and x['market']=='ALL');summary=scope['horizons'][0]
    a,b=comparison_figures(summary,[x for x in d['pairs'] if x['horizon']==20],theme)
    assert len(a.data)==2 and all(rule in ' '.join(a.data[0].x) for rule in RULE_IDS)
    assert all(any(rule in trace.name for rule in RULE_IDS) for trace in b.data)
    rows=[r for r in d['results'] if r['horizon']==20];path=next(iter(d['paths'].values()))
    fig=signal_figure(path,rows,['ATR_TRAIL','DONCHIAN_10','FIXED'],theme)
    line=next(t for t in fig.data if t.name=='ATR_TRAIL · 당일 유효 스톱')
    logs=path['effective_stops']['ATR_TRAIL'][:next(r['stop_path_length'] for r in rows if r['rule_id']=='ATR_TRAIL')]
    assert list(line.x)==[x['effective_date'] for x in logs] and list(line.customdata)==[x['information_as_of'] for x in logs]
    assert all(a<b for a,b in zip(line.customdata,line.x))
    assert all(x<=rows[0]['horizon_end'] for x in fig.data[0].x)
    with pytest.raises(ValueError):signal_figure(path,rows,list(RULE_IDS[:4]),theme)


def test_individual_selects_one_policy_by_default_and_all_seven_results_survive():
    at=app().run();at.selectbox(key='exit_vintage').set_value('회고적').run()
    assert at.multiselect(key='exit_selected_rules').value==['ATR_TRAIL']
    at.multiselect(key='exit_selected_rules').set_value(['FIXED','ATR_TRAIL','DONCHIAN_10']).run()
    assert not at.exception
    assert any(len(frame.value)==3 and '청산 원인' in frame.value.columns for frame in at.dataframe)


