"""Research ATR(14) Wilder RMA, not EMA or the 22-session Chandelier Exit."""
import math
import numpy as np
import pandas as pd


def wilder_atr(frame,valid=None):
    """Seed with 14 consecutive TRs that each have a valid previous close.

    The first row has no prior close: no TR is invented. Missing/invalid prices
    reset the seed; no smoothing across gaps, halts or incompatible bases.
    """
    valid=pd.Series(True,index=frame.index) if valid is None else valid.reindex(frame.index).fillna(False)
    out=pd.Series(np.nan,index=frame.index,dtype=float,name='atr14');seed=[];value=None;previous=None
    for date,row in frame.iterrows():
        if not valid.loc[date]:seed=[];value=None;previous=None;continue
        high,low,close=map(float,(row.High,row.Low,row.Close))
        if previous is None:previous=close;continue
        tr=max(high-low,abs(high-previous),abs(low-previous));previous=close
        if value is None:
            seed.append(tr)
            if len(seed)==14:value=math.fsum(seed)/14
        else:value=(13*value+tr)/14
        if value is not None:out.loc[date]=value
    return out


def next_atr_stop(initial,active,highest_high,atr):
    if not all(math.isfinite(float(v)) for v in (initial,active,highest_high,atr)) or atr<0:raise ValueError('ATR_NOT_READY')
    return max(initial,active,highest_high-3.0*atr)

