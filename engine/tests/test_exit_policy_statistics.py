from copy import deepcopy
import pytest
from watchlist.exit_policy_experiments import run_signal
from watchlist.exit_policy_statistics import metrics,aggregate,uncertainty
from test_exit_policy_core import fixture


def rows():
    s,h,cut=fixture();result,_=run_signal(s,h,cut)
    for r in result:r['vintage']='retrospective'
    return result


def test_all_common_and_pairwise_counts_and_deltas_are_signal_matched():
    data=rows();summary,pairs=aggregate(data)
    assert [r['common_complete_n'] for r in summary]==[1,1,1]
    assert len(pairs)==36
    for n in summary:
        assert n['evidence']=='insufficient_sample' and len(n['policies'])==7
        assert all(p['n']==1 for p in n['policies'])
    x=next(x for x in pairs if x['rule_id']=='DONCHIAN_10' and x['baseline']=='FIXED' and x['horizon']==20)
    assert x['delta_net_R']==pytest.approx(-.2)


def test_strategy_exclusion_changes_seven_common_but_preserves_other_pairs():
    data=rows()
    for r in data:
        if r['rule_id']=='ATR_TRAIL':r['comparison_eligible']=False;r['eligibility_status']='excluded';r['exclusion_reason']='ATR_WARMUP_INSUFFICIENT'
    summary,pairs=aggregate(data)
    assert all(s['common_complete_n']==0 for s in summary)
    assert any(p['rule_id']=='MA20_2C' and p['baseline']=='FIXED' for p in pairs)
    assert not any(p['rule_id']=='ATR_TRAIL' for p in pairs)


def test_profit_factor_uses_net_r_losses_and_zero_denominator_is_na():
    assert metrics([{'net_return_pct':3.,'net_R':2.,'holding_sessions':1}])['profit_factor'] is None
    data=[{'net_return_pct':4.,'net_R':2.},{'net_return_pct':-1.,'net_R':-1.},{'net_return_pct':0.,'net_R':0.}]
    m=metrics(data);assert m['profit_factor']==2 and m['profit_factor_equal_notional']==4 and m['payoff_ratio']==4
    assert m['win_rate_pct']==pytest.approx(100/3)


def test_early_closed_not_mature_has_no_common_or_pairwise_performance():
    s,h,cut=fixture(length=3);h.loc[h.index[-1],'Low']=85
    result,_=run_signal(s,h,cut);summary,pairs=aggregate(result)
    assert all(s['common_complete_n']==0 for s in summary) and not pairs


def test_pairing_rejects_different_initial_risk_or_entry():
    data=rows();data[0]['entry_price']=101
    with pytest.raises(ValueError,match='IDENTITY'):aggregate(data)


def test_uncertainty_waits_for_clusters_and_is_deterministic_when_supported():
    assert uncertainty([],20)['status']=='insufficient_clusters'
    data=[dict(signal_id=str(i),market='US',ticker=f'T{i%25}',entry_session_index=i*20,delta_net_R=float(i%7-2)) for i in range(125)]
    first=uncertainty(data,20);assert first['status']=='approximate' and first==uncertainty(data,20)
    assert first['lower']<=first['upper']

