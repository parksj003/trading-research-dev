"""Paired, maturity-matched descriptive research. Never rank/select a policy."""
from collections import Counter
from hashlib import sha256
import math
import numpy as np
from .exit_policy_experiments import RULES,HORIZONS,finite


def values(rows,key):return [float(r[key]) for r in rows if finite(r.get(key)) is not None]
def average(x):return float(np.mean(x)) if len(x) else None
def median(x):return float(np.median(x)) if len(x) else None


def metrics(rows):
    ret=values(rows,'net_return_pct');r=values(rows,'net_R');wins=[v for v in ret if v>0];losses=[v for v in ret if v<0]
    pos=[v for v in r if v>0];neg=[v for v in r if v<0]
    result=dict(n=len(rows),net_return_n=len(ret),net_R_n=len(r),mean_net_return_pct=average(ret),median_net_return_pct=median(ret),mean_net_R=average(r),median_net_R=median(r),
        win_rate_pct=sum(v>0 for v in ret)/len(ret)*100 if ret else None,
        mean_win_pct=average(wins),mean_loss_pct=average(losses),payoff_ratio=average(wins)/abs(average(losses)) if wins and losses else None,
        profit_factor=sum(pos)/abs(sum(neg)) if neg else None,profit_factor_basis='net_R;equal_initial_risk_units;not_position_size_or_portfolio',
        profit_factor_equal_notional=sum(wins)/abs(sum(losses)) if losses else None,
        mean_holding_sessions=average(values(rows,'holding_sessions')),median_holding_sessions=median(values(rows,'holding_sessions')),
        worst_net_return_pct=min(ret) if ret else None,worst_net_R=min(r) if r else None,
        intrabar_ambiguous_n=sum(bool(x.get('intrabar_ambiguous')) for x in rows),
        exit_reasons=dict(Counter(x.get('exit_reason') for x in rows)),
        unique_tickers=len({(x.get('market'),x.get('ticker')) for x in rows}),unique_entry_dates=len({x.get('entry_date') for x in rows}))
    for field in ('mfe_pct','mae_pct','mfe_lower_pct','mfe_upper_pct','mae_lower_pct','mae_upper_pct','profit_giveback_pp','profit_giveback_amount','profit_giveback_pct','hindsight_capture_pct'):
        x=values(rows,field);result['mean_'+field]=average(x);result[field+'_n']=len(x)
    return result


def uncertainty(rows,horizon):
    """Crossed ticker x horizon-length entry-session block pigeonhole bootstrap.

    Approximate dependence sensitivity interval, not an independent-trade CI or
    a significance/optimal-policy decision. Adjacent blocks may remain dependent.
    """
    tickers=sorted({(r['market'],r['ticker']) for r in rows});blocks=sorted({(r['market'],int(r['entry_session_index'])//horizon) for r in rows})
    base={'method':'crossed_cluster_pigeonhole_percentile','samples':400,'minimum_n':100,'ticker_clusters':len(tickers),
        'entry_session_blocks':len(blocks),'block_length_sessions':horizon,'confidence_level':.95,
        'interpretation':'dependence_sensitivity_only;finite_cluster_and_adjacent_block_dependence_remain',
        'lower':None,'upper':None,'status':'insufficient_clusters'}
    if len(rows)<100 or len(tickers)<20 or len(blocks)<10:return base
    ti={k:i for i,k in enumerate(tickers)};bi={k:i for i,k in enumerate(blocks)}
    a=np.array([ti[(r['market'],r['ticker'])] for r in rows]);b=np.array([bi[(r['market'],int(r['entry_session_index'])//horizon)] for r in rows])
    data=np.array([r['delta_net_R'] for r in rows]);seed=int(sha256('|'.join(sorted(r['signal_id'] for r in rows)).encode()).hexdigest()[:8],16)
    rng=np.random.default_rng(seed);means=[]
    for _ in range(400):
        tw=np.bincount(rng.integers(0,len(tickers),len(tickers)),minlength=len(tickers));bw=np.bincount(rng.integers(0,len(blocks),len(blocks)),minlength=len(blocks))
        weight=tw[a]*bw[b]
        if weight.sum():means.append(float(np.average(data,weights=weight)))
    if len(means)<300:return {**base,'status':'insufficient_resampled_support'}
    return {**base,'status':'approximate','lower':float(np.quantile(means,.025)),'upper':float(np.quantile(means,.975))}


def aggregate(rows):
    """Fixed strategy order; complete same-signal common and pairwise cohorts."""
    summaries=[];pairs=[]
    for horizon in HORIZONS:
        at=[r for r in rows if r['horizon']==horizon];lookup={}
        for r in at:
            group=lookup.setdefault(r['signal_id'],{})
            if r['rule_id'] in group:raise ValueError('DUPLICATE_EXPERIMENT_ROW')
            group[r['rule_id']]=r
        common={sid for sid,group in lookup.items() if all(rule in group and group[rule].get('comparison_eligible') for rule in RULES)}
        for sid,group in sorted(lookup.items()):
            for baseline in ('FIXED','HOLD'):
                for rule in RULES:
                    if rule==baseline:continue
                    a,b=group.get(rule,{}),group.get(baseline,{})
                    if not a.get('comparison_eligible') or not b.get('comparison_eligible'):continue
                    # Identity assertions prevent accidental pairing across revisions/entries.
                    if any(a.get(k)!=b.get(k) for k in ('entry_date','entry_price','initial_risk','total_cost','price_path_hash','source_config_hash')):raise ValueError('PAIRED_IDENTITY_MISMATCH')
                    pairs.append({**{k:a.get(k) for k in ('signal_id','market','ticker','entry_date','entry_session_index','sector','regime','vintage')},
                        'horizon':horizon,'rule_id':rule,'baseline':baseline,'seven_common':sid in common,
                        'delta_net_R':a['net_R']-b['net_R'],'delta_net_return_pct':a['net_return_pct']-b['net_return_pct'],
                        'delta_holding_sessions':a['holding_sessions']-b['holding_sessions']})
        excluded_ids={sid for sid,g in lookup.items() if sid not in common and any(
            x.get('eligibility_status')=='excluded' or x.get('eligibility_status')=='eligible' and not x.get('r_eligible',False)
            or x.get('maturity_reached') and not x.get('horizon_complete') for x in g.values())}
        pending_ids=set(lookup)-common-excluded_ids
        reasons=Counter()
        for sid in excluded_ids:
            for x in lookup[sid].values():
                if x.get('eligibility_status')=='excluded':reasons[x.get('exclusion_reason') or 'POLICY_EXCLUDED']+=1
                elif x.get('eligibility_status')=='eligible' and not x.get('r_eligible',False):reasons['R_NOT_ELIGIBLE']+=1
                elif x.get('maturity_reached') and not x.get('horizon_complete'):reasons['MATURITY_PATH_INCOMPLETE']+=1
        overall={'horizon':horizon,'signals':len(lookup),'common_complete_n':len(common),
            'pending_n':len(pending_ids),'excluded_n':len(excluded_ids),'exclusion_reasons':dict(reasons),
            'policies':[],'pairwise':[],'descriptive_individual':[],'evidence':'insufficient_sample' if len(common)<20 else 'research_only_no_superiority_claim'}
        for rule in RULES:
            eligible=[g[rule] for sid,g in lookup.items() if sid in common]
            deltas=[x for x in pairs if x['horizon']==horizon and x['rule_id']==rule and x['baseline']=='FIXED' and x['seven_common']]
            overall['policies'].append(dict(rule_id=rule,**metrics(eligible),delta_R_vs_FIXED=average(values(deltas,'delta_net_R')) if rule!='FIXED' else 0. if eligible else None))
            own=[g[rule] for g in lookup.values() if rule in g and g[rule].get('eligibility_status')=='eligible' and g[rule].get('completion_status')=='complete']
            overall['descriptive_individual'].append(dict(rule_id=rule,**metrics(own),cohort='individual_not_rank_comparable'))
            for baseline in ('FIXED','HOLD'):
                if rule==baseline:continue
                paired=[x for x in pairs if x['horizon']==horizon and x['rule_id']==rule and x['baseline']==baseline]
                chosen=[lookup[x['signal_id']][rule] for x in paired]
                overall['pairwise'].append(dict(rule_id=rule,baseline=baseline,**metrics(chosen),delta_mean_R=average(values(paired,'delta_net_R')),
                    delta_median_R=median(values(paired,'delta_net_R')),uncertainty=uncertainty(paired,horizon)))
        common_rows=[g['FIXED'] for sid,g in lookup.items() if sid in common]
        by_ticker=Counter((r['market'],r['ticker']) for r in common_rows)
        overlaps=0
        for market in ('KR','US'):
            entries=sorted(r['entry_session_index'] for r in common_rows if r['market']==market)
            overlaps+=sum((i>0 and day-entries[i-1]<horizon) or (i+1<len(entries) and entries[i+1]-day<horizon) for i,day in enumerate(entries))
        contributions=Counter()
        for r in common_rows:contributions[(r['market'],r['ticker'])]+=abs(r['net_R'])
        overall['conditional_groups']=[]
        for dimension in ('market','regime'):
            for value in sorted({str(r.get(dimension)) for r in common_rows}):
                chosen={r['signal_id'] for r in common_rows if str(r.get(dimension))==value}
                overall['conditional_groups'].append({'dimension':dimension,'value':value,'n':len(chosen),'policies':[dict(rule_id=rule,**metrics([lookup[sid][rule] for sid in sorted(chosen)])) for rule in RULES]})
        overall['dependence']={'unique_tickers':len(by_ticker),'unique_entry_dates':len({r['entry_date'] for r in common_rows}),
            'repeated_ticker_signals':sum(n-1 for n in by_ticker.values()),'sector_counts':dict(Counter(str(r.get('sector')) for r in common_rows)),
            'regime_counts':dict(Counter(str(r.get('regime')) for r in common_rows)),
            'overlapping_intervals_n':int(overlaps),
            'largest_ticker_abs_net_R_contribution_share':max(contributions.values())/sum(contributions.values()) if contributions and sum(contributions.values()) else None,
            'largest_ticker_signal_share':max(by_ticker.values())/len(common_rows) if common_rows else None,
            'warnings':['overlapping_and_repeated_signals_not_independent','multiple_comparisons','sector_regime_concentration','no_portfolio_compounding']}
        summaries.append(overall)
    return summaries,pairs


def statistics(rows):
    """Precompute UI scopes. UI filters published results; no repeated backtest."""
    output=[];all_pairs=[]
    for vintage in ('all','prospective','retrospective'):
        for market in ('ALL','KR','US'):
            scope=[r for r in rows if (vintage=='all' or r.get('vintage')==vintage) and (market=='ALL' or r.get('market')==market)]
            summary,pairs=aggregate(scope)
            output.append({'vintage':vintage,'market':market,'horizons':summary})
            if vintage=='all' and market=='ALL':all_pairs=pairs
    return output,all_pairs

