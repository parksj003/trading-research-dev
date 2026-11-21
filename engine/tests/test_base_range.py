from copy import deepcopy
import numpy as np
import pandas as pd
import pytest
from watchlist import trend_watchlist as e


def expanding():
    c=np.linspace(50,100,300)
    h=pd.DataFrame({'Open':c,'High':c+2.5,'Low':c-2.5,'Close':c,'Volume':1e6},index=pd.bdate_range(end='2026-11-04',periods=300))
    bench=h.copy();h.loc[h.index[-10:],'Close']=np.linspace(100,113,10)
    for k,d in [('Open',0),('High',2.27),('Low',-2.27)]:h[k]=h.Close+d
    h.loc[h.index[-15:],'Volume']=500000.
    return h,bench


def test_expansion_does_not_create_contraction_allocation_or_signal(tmp_path):
    h,b=expanding();cfg=deepcopy(e.DEFAULT_CONFIG);f=e.compute_features(h,b,cfg)
    assert f['range_10d_pct']==pytest.approx(.17947406118899)
    assert not f['is_true_vcp'] and not f['is_breakout']
    assert f['setup_stage']!='🎯 베이스수축' and not f['is_actionable']
    row={**f,'market':'US','sub_market':'SP500','ticker':'SYNTH','name':'Synthetic','sector':'S','macro_theme':'T','topdown_score':99.,'rs_score':99.,'is_leading_sector':False}
    _,actions,meta=e.build_action_table({'SP500':pd.DataFrame([row])},{'SP500':{'regime':'ATTACK','signal_light':'ATTACK'}},cfg)
    assert actions.empty and meta['total_alloc_pct']==0
    assert e.update_signal_ledger(tmp_path,actions,'2026-11-04')==0
    assert not (tmp_path/'history/signals.csv').exists()


@pytest.mark.parametrize('overrides,label',[
 ({'range_10d_pct':.1},'🎯 베이스수축'),
 ({'range_10d_pct':.100001},'📈 정배열진행'),
 ({'range_10d_pct':.02},'🎯 베이스수축'),
 ({'is_true_vcp':True},'🎯 VCP·거래량건조'),
 ({'is_breakout':True},'🔥 피벗돌파'),
 ({'sma20':119.},'🧲 이평선눌림목'),
 ({'sma20':90.},'⚠️ 단기과열'),
 ({'sma200':130.},'👀 추세회복'),
])
def test_other_setup_rules_and_contraction_boundary_unchanged(overrides,label):
    args=dict(last=120.,sma20=110.,sma50=105.,sma150=95.,sma200=90.,dist_high=-.05,is_breakout=False,is_true_vcp=False,range_10d_pct=.15,vol_dry_up=.7,pocket_pivot=False,atr14_pct=.05)
    args.update(overrides)
    assert e.classify_alltoo_setup(**args)[0]==label


def test_fixed_normal_candidate_and_numeric_features_preserved(monkeypatch):
    h,b=expanding();h.loc[h.index[-10:],'Close']=np.linspace(100,101,10)
    for k,d in [('Open',0),('High',2.27),('Low',-2.27)]:h[k]=h.Close+d
    cfg=deepcopy(e.DEFAULT_CONFIG)
    fixed=e.compute_features(h,b,cfg)
    classify=e.classify_alltoo_setup
    mdd=float(-(h.Close.tail(10)/h.Close.tail(10).cummax()-1).min())
    def legacy(*args,**kwargs):
        args=list(args);args[9]=mdd
        return classify(*args,**kwargs)
    monkeypatch.setattr(e,'classify_alltoo_setup',legacy)
    before=e.compute_features(h,b,cfg)
    assert fixed['is_actionable'] and before['is_actionable']
    assert fixed['setup_stage']==before['setup_stage']=='🎯 베이스수축'
    for key in ('raw_rs','pivot_price','stop_price','position_size_pct','trade_risk_pct','range_10d_pct'):
        assert fixed[key]==before[key]

