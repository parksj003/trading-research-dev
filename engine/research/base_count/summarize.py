"""Descriptive artifacts; no configuration selection or significance claims."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .study import load_dataset,calendar


def summarize(plan_path,data_dir,run_dir,output):
    plan,data,metadata,_=load_dataset(plan_path,data_dir);run_dir=Path(run_dir);output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    manifest=json.loads((run_dir/'manifest.json').read_bytes())
    for name,expected in manifest['files'].items():
        if hashlib.sha256((run_dir/name).read_bytes()).hexdigest()!=expected:raise ValueError('research output changed')
    summary=json.loads((run_dir/'summary.json').read_bytes())
    sensitivity=pd.read_csv(run_dir/'sensitivity.csv',dtype={'ticker':str})
    attempts=pd.read_csv(run_dir/'attempts.csv',dtype={'ticker':str})
    ref=attempts[attempts.profile=='reference'].copy()
    rs=[]
    for row in ref.to_dict('records'):
        cal=calendar(row['market']);day=pd.Timestamp(row['date']);old=cal.session_offset(day,-126)
        stock=data[row['symbol']];benchmark=data.get(row['benchmark']);value=np.nan
        if row['market']=='US' and day in stock.index and old in stock.index and benchmark is not None and day in benchmark.index and old in benchmark.index:
            values=[stock.loc[day,'Close'],stock.loc[old,'Close'],benchmark.loc[day,'Close'],benchmark.loc[old,'Close']]
            if np.isfinite(values).all() and min(values)>0:
                value=(values[0]/values[1]-values[2]/values[3])*100
        rs.append(value)
    ref['research_rs_126_excess_pp']=rs
    ref['official_rs_percentile_reconstructed']=False
    ref.to_csv(output/'reference_attempts_enriched.csv',index=False)
    strata=[]
    for dimension in ('benchmark_price_regime_proxy','sector_current'):
        for (market,label),group in ref.groupby(['market',dimension],dropna=False):
            selected=group[group.nonoverlap_20d]
            done=selected[selected.outcome_20=='COMPLETE']
            strata.append(dict(market=market,dimension=dimension,label=label,attempts=len(group),nonoverlap=len(selected),
                completed_20d=len(done),incomplete=len(selected)-len(done),tickers=done.ticker.nunique(),
                mean_net_return_pp_20=done.net_return_pp_20.mean() if len(done) else None,
                mean_rs_126_excess_pp=group.research_rs_126_excess_pp.mean(),
                confirmatory=False,historical_sector_membership_available=False))
    pd.DataFrame(strata).to_csv(output/'exploratory_strata.csv',index=False)
    reference=sensitivity[sensitivity.profile=='reference']
    coverage=[]
    for market in ('US','KR'):
        q=[q for q in summary['quality'] if q['market']==market];s=reference[reference.market==market]
        coverage.append(dict(market=market,raw_rows=sum(x['rows'] for x in q),valid_segmented_rows=int(s.sessions.sum()),
            strict_prefix_rows=sum(x['strict_processed'] for x in q),absolute_known_rows=int(s.absolute_known_sessions.sum()),
            strict_prefix_fraction=sum(x['strict_processed'] for x in q)/sum(x['rows'] for x in q),
            known_fraction_of_segmented_rows=float(s.absolute_known_sessions.sum()/s.sessions.sum()),
            reference_attempts=int(s.first_attempts.sum()),known_ordinal_attempts=int(s.known_ordinal_attempts.sum()),
            segments=sum(x['segments'] for x in q),missing_session_records=sum(len(x['missing_sessions']) for x in q),
            invalid_row_records=sum(len(x['invalid_rows']) for x in q),non_session_records=sum(len(x['non_sessions']) for x in q)))
    counts=sensitivity.groupby(['profile','market']).first_attempts.sum().unstack().fillna(0)
    counts.to_csv(output/'profile_attempt_counts.csv')
    # Ordinary descriptive figures: current survivor sample, no performance-ranking chart.
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    fig,ax=plt.subplots(figsize=(8,4),constrained_layout=True)
    x=np.arange(2);width=.34
    ax.bar(x-width/2,[c['strict_prefix_fraction']*100 for c in coverage],width,label='Strict accepted prefix / supplied rows')
    ax.bar(x+width/2,[c['known_fraction_of_segmented_rows']*100 for c in coverage],width,label='Known ordinal / valid research islands')
    ax.set_xticks(x,['US','KR']);ax.set_ylim(0,105);ax.set_ylabel('% of sessions');ax.legend(fontsize=8)
    ax.set_title('Input readiness and absolute-count observability are different measures')
    fig.savefig(output/'coverage.png',dpi=150);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,7),constrained_layout=True)
    counts[['US','KR']].plot.barh(stacked=True,ax=ax,color=['#2563eb','#f59e0b'])
    ax.set_xlabel('First breakout attempts across 20 current-survivor instruments');ax.set_ylabel('Research profile')
    ax.set_title('All 15 predeclared profiles; no winner chosen by realized return')
    fig.savefig(output/'sensitivity.png',dpi=150);plt.close(fig)
    test=[g for g in summary['groups'] if g['profile']=='reference' and g['fold']=='temporal_test' and g['horizon']==20]
    fig,ax=plt.subplots(figsize=(8,4),constrained_layout=True)
    labels=[g['market']+' '+g['ordinal'] for g in test];values=[g['complete'] for g in test]
    ax.bar(labels,values,color='#64748b');ax.set_ylabel('Completed, nonoverlapping 20-session outcomes')
    ax.set_title('2022-2026 chronological slice: sparse ordinal cohorts, not prospective OOS')
    for i,n in enumerate(values):ax.text(i,n+.08,str(n),ha='center')
    fig.savefig(output/'cohort_sizes.png',dpi=150);plt.close(fig)
    result=dict(schema='base-research-analysis-1',coverage=coverage,
        reference_attempts=len(ref),reference_complete_20d=int(((ref.nonoverlap_20d)&(ref.outcome_20=='COMPLETE')).sum()),
        profiles=len(plan['profiles']),profile_total_attempts_min=int(counts.sum(axis=1).min()),
        profile_total_attempts_max=int(counts.sum(axis=1).max()),
        temporal_test_cohorts=test,rs_is_research_covariate_only=True,
        prospective_out_of_sample=False,performance_superiority=False,manual_accuracy_established=False)
    (output/'findings.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--data',required=True)
    parser.add_argument('--run',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();summarize(args.plan,args.data,args.run,args.output)

