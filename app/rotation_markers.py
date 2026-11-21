"""Presentation-only RRG sizes and sample cautions. Never changes engine fields."""
import numpy as np
import pandas as pd


def classify_sector_sample_size(value):
    n=pd.to_numeric(value,errors='coerce')
    if pd.isna(n) or not np.isfinite(n) or n<=0 or n!=int(n):
        return '표본 확인 불가'
    if n<5:return '표본 매우 적음'
    if n<10:return '표본 주의'
    return '별도 표본 경고 없음'


def rotation_marker_metrics(df):
    def numeric(key):
        return pd.to_numeric(df.get(key,pd.Series(np.nan,index=df.index)),errors='coerce').astype(float)
    total,passed,provided=numeric('univ_total'),numeric('passed_cnt'),numeric('pass_rate_pct')
    valid_counts=(np.isfinite(total)&np.isfinite(passed)&total.gt(0)&passed.ge(0)&passed.le(total)
                  &total.eq(np.floor(total))&passed.eq(np.floor(passed)))
    calculated=(passed/total.where(total.gt(0))*100).where(valid_counts)
    valid_provided=np.isfinite(provided)&provided.between(0,100)
    consistent=valid_counts&valid_provided&np.isclose(provided,calculated,atol=.051,rtol=0)
    rate=provided.where(valid_provided)
    # Impossible known populations/counts must not be disguised by a valid-looking percentage.
    invalid_known=(total.notna()&(~np.isfinite(total)|total.le(0)|total.ne(np.floor(total)))) | (passed.notna()&(~np.isfinite(passed)|passed.lt(0)|passed.ne(np.floor(passed)))) | (total.notna()&passed.gt(total))
    rate=rate.mask(invalid_known)
    rate=rate.mask(valid_counts&~consistent,calculated)
    source=pd.Series('확인 불가',index=df.index)
    source.loc[rate.notna()]='엔진 제공(건수 대조 불가)'
    source.loc[consistent]='엔진 제공·건수 대조 일치'
    source.loc[valid_counts&~consistent]='건수로 보완'
    mismatch=valid_counts&valid_provided&~consistent
    source.loc[mismatch]='엔진 값 불일치·건수로 보완'
    sample=total.map(classify_sector_sample_size)
    return pd.DataFrame({'pass_rate_pct':rate,'rate_source':source,'rate_mismatch':mismatch,
                         'sample_status':sample,
                         'univ_total':total.where(np.isfinite(total)&total.ge(0)&total.eq(np.floor(total))),
                         'passed_cnt':passed.where(np.isfinite(passed)&passed.ge(0)&passed.eq(np.floor(passed)))},index=df.index)


def calculate_rotation_marker_sizes(df,size_mode='pass_rate'):
    if size_mode=='passed_count':
        passed=pd.to_numeric(df.get('passed_cnt',pd.Series(np.nan,index=df.index)),errors='coerce')
        return (passed.fillna(1).clip(lower=1)*4.5+12).clip(12,42)
    if size_mode!='pass_rate':raise ValueError('unknown rotation size mode')
    rate=rotation_marker_metrics(df)['pass_rate_pct']
    return 12+30*np.sqrt(rate.fillna(0).clip(0,100)/100)


def sample_outlines(status):
    styles={'표본 매우 적음':(1.4,'#DC2626'),'표본 주의':(1.2,'#D97706'),
            '표본 확인 불가':(1.,'#64748B'),'별도 표본 경고 없음':(1.,'#64748B')}
    return [styles[x][0] for x in status],[styles[x][1] for x in status]

