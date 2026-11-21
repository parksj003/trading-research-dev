"""Blinded chart pack and manual-label scorer. Never manufactures ground truth."""
import argparse
import gzip
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .study import load_dataset,digest


def score_labels(labels,key):
    if labels.case_id.duplicated().any() or key.case_id.duplicated().any():raise ValueError('duplicate review cases')
    merged=labels.merge(key,on='case_id',how='left',validate='one_to_one',suffixes=('','_model'))
    if len(merged)!=len(labels) or merged['model_state'].isna().any():raise ValueError('unknown review case')
    reviewed=merged[merged.label_status=='REVIEWED']
    if reviewed.empty:
        return dict(status='UNREVIEWED',cases=len(labels),reviewed=0,state_accuracy=None,ordinal_accuracy=None)
    allowed={'UNKNOWN','NO_BASE','BASE_FORMING','BASE_CONFIRMED','BREAKOUT_PENDING','BREAKOUT_CONFIRMED','BASE_FAILED','TREND_RESET'}
    for row in reviewed.to_dict('records'):
        stamp=pd.Timestamp(row.get('reviewed_at'))
        if (not str(row.get('reviewer_code') or '').strip() or pd.isna(stamp) or stamp.tzinfo is None
            or row.get('manual_state') not in allowed):
            raise ValueError('reviewed labels require an anonymous reviewer, aware timestamp, and valid state')
        value=str(row.get('manual_ordinal','')).strip()
        if value!='UNKNOWN':
            try:number=float(value)
            except (TypeError,ValueError) as exc:raise ValueError('invalid manual ordinal') from exc
            if not np.isfinite(number) or number<1 or number!=int(number):
                raise ValueError('manual ordinal must be a positive integer or UNKNOWN')
    state_valid=reviewed.manual_state.fillna('').astype(str).str.strip().ne('')
    state=reviewed[state_valid]
    manual=pd.to_numeric(reviewed.manual_ordinal,errors='coerce')
    model=pd.to_numeric(reviewed.model_ordinal,errors='coerce')
    known=manual.notna() & model.notna()
    return dict(status='DESCRIPTIVE_CASE_CONTROL_REVIEW',cases=len(labels),reviewed=len(reviewed),
        state_assessed=len(state),state_accuracy=float((state.manual_state==state.model_state).mean()) if len(state) else None,
        ordinal_assessed=int(known.sum()),ordinal_unassessable=int((~known).sum()),
        ordinal_accuracy=float((manual[known]==model[known]).mean()) if known.any() else None,
        population_precision_established=False)


def create_pack(plan_path,data_dir,run_dir,output):
    plan,data,metadata,_=load_dataset(plan_path,data_dir);run_dir=Path(run_dir);output=Path(output)
    output.mkdir(parents=True,exist_ok=False);(output/'charts').mkdir();(output/'unblinded').mkdir()
    attempts=pd.read_csv(run_dir/'attempts.csv',dtype={'ticker':str,'date':str})
    reference=attempts[(attempts.profile=='reference') & (attempts.fold=='temporal_test')]
    rng=np.random.default_rng(plan['seed']);cases=[];used=set()
    observations={}
    for instrument in plan['instruments']:
        name=instrument['market']+'_'+instrument['ticker']
        with gzip.open(run_dir/'observations'/f'{name}.jsonl.gz','rt') as stream:
            observations[instrument['symbol']]={x['as_of']:x for x in map(json.loads,stream)}
    for market in ('US','KR'):
        candidates=reference[reference.market==market].sort_values(['ticker','date']).to_dict('records')
        for index in rng.permutation(len(candidates))[:8]:
            row=candidates[index];symbol=row['symbol'];date=row['date']
            if (symbol,date) in used:continue
            cases.append((symbol,date,'first_attempt'));used.add((symbol,date))
        pool=[]
        for instrument in plan['instruments']:
            if instrument['market']!=market:continue
            symbol=instrument['symbol']
            pool.extend((symbol,date) for date in observations[symbol] if date>='2022-01-01' and (symbol,date) not in used)
        for index in rng.permutation(len(pool))[:4]:
            symbol,date=pool[index];cases.append((symbol,date,'random_date'));used.add((symbol,date))
    # Hide strata in the separate model key, randomize case order and numeric IDs.
    cases=[cases[i] for i in rng.permutation(len(cases))]
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    labels=[];keys=[]
    for i,(symbol,date,stratum) in enumerate(cases,1):
        case_id=f'P4-C{i:03d}';obs=observations[symbol][date]
        history=data[symbol].loc[:date];visible=history.tail(260)
        # No forward bars, returns, model annotations, or candidate ordinal.
        fig,axes=plt.subplots(3,1,figsize=(12,8),gridspec_kw={'height_ratios':[1,2,1]},constrained_layout=True)
        axes[0].plot(history.index,history.Close,color='#475569',lw=.8)
        axes[0].set_yscale('log')
        axes[0].set_xlim(history.index[0],pd.Timestamp(date))
        axes[0].set_title(f'{case_id} | {symbol} | through {date} | current-vintage adjusted history')
        axes[1].plot(visible.index,visible.Close,color='#1d4ed8',lw=1)
        axes[1].fill_between(visible.index,visible.Low,visible.High,color='#64748b',alpha=.12)
        axes[1].set_title('Last 260 supplied rows through T; gaps/invalid vendor rows are not repaired')
        axes[2].bar(visible.index,visible.Volume,width=1,color='#94a3b8')
        axes[1].set_xlim(visible.index[0],pd.Timestamp(date))
        axes[2].set_xlim(visible.index[0],pd.Timestamp(date))
        for ax in axes:ax.grid(alpha=.2)
        axes[2].set_ylabel('Volume');fig.savefig(output/'charts'/f'{case_id}.png',dpi=120);plt.close(fig)
        labels.append(dict(case_id=case_id,symbol=symbol,market=obs['market'],as_of=date,
            source_sha256=metadata[symbol]['raw_sha256'],label_status='UNREVIEWED',reviewer_code='',
            reviewed_at='',manual_state='',manual_ordinal='',notes=''))
        keys.append(dict(case_id=case_id,model_state=obs['status'],model_quality=obs['quality'],
            model_ordinal=obs['current_base_ordinal'],model_completed=obs['completed_base_count'],
            sampling_stratum=stratum,segment_id=obs['segment_id'],snapshot_sha256=digest(obs)))
    label_frame=pd.DataFrame(labels);key_frame=pd.DataFrame(keys)
    label_frame.to_csv(output/'labels_to_review.csv',index=False)
    key_frame.to_csv(output/'unblinded/model_key.csv',index=False)
    result=score_labels(label_frame,key_frame)
    (output/'review_status.json').write_text(json.dumps(result,indent=2)+'\n')
    (output/'REVIEW_INSTRUCTIONS.md').write_text('''# 독립 수작업 라벨 검토\n\n알고리즘 답안은 unblinded 폴더에 있습니다. 라벨을 완료하기 전에는 보지 않습니다.\n차트에는 T 이후 가격·수익·모델 구간을 표시하지 않았습니다. 현재 시점 수정주가의 사후 재생입니다.\n\nlabels_to_review.csv를 별도 사본으로 저장해 manual_state와 manual_ordinal을 작성합니다.\n상태: UNKNOWN, NO_BASE, BASE_FORMING, BASE_CONFIRMED, BREAKOUT_PENDING, BREAKOUT_CONFIRMED, BASE_FAILED, TREND_RESET.\n순번을 판정할 충분한 과거 사이클 근거가 없으면 manual_ordinal에 UNKNOWN을 적습니다.\n추정으로 Base 1을 채우지 않습니다. 시작부터 주어진 자료가 전체 생애 이력이라는 보장은 없습니다.\nreviewer_code는 비식별 코드, reviewed_at은 실제 검토 시각을 기입하고 완료 시 label_status를 REVIEWED로 바꿉니다.\n\n이 표본은 모델 돌파 사례와 임의 날짜를 섞은 case-control 검토 자료입니다.\n정밀도·민감도·모집단 오탐률을 일반화할 표본이 아니며, 라벨 미완료 상태의 정확도는 계산하지 않습니다.\n''')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--data',required=True)
    parser.add_argument('--run',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();print(json.dumps(create_pack(args.plan,args.data,args.run,args.output),indent=2))

