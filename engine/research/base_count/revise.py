"""Version a research measurement correction without changing any model events."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import shutil

import pandas as pd

from .study import aggregate


def revise_benchmark_basis(plan_path,source,output):
    source=Path(source);output=Path(output);plan=json.loads(Path(plan_path).read_bytes())
    manifest_raw=(source/'manifest.json').read_bytes();manifest=json.loads(manifest_raw)
    for name,expected in manifest['files'].items():
        if hashlib.sha256((source/name).read_bytes()).hexdigest()!=expected:raise ValueError('original research artifacts changed')
    if output.exists():raise FileExistsError(output)
    shutil.copytree(source,output)
    before=pd.read_csv(source/'attempts.csv',dtype={'ticker':str},float_precision='round_trip')
    revised=before.copy();mask=revised.market.eq('KR')
    for col in [c for c in revised if c.startswith('benchmark_excess_pp_')]:revised.loc[mask,col]=None
    revised['benchmark_comparison_quality']=revised.market.map({'US':'ADJUSTED_ETF_COMPARATOR','KR':'PRICE_INDEX_VS_ADJUSTED_EQUITY'})
    revised.to_csv(output/'attempts.csv',index=False)
    check=pd.read_csv(output/'attempts.csv',dtype={'ticker':str},float_precision='round_trip')
    protected=[c for c in before if not c.startswith('benchmark_excess_pp_')]
    pd.testing.assert_frame_equal(check[protected],before[protected])
    records=revised.astype(object).where(pd.notna(revised),None).to_dict('records')
    for row in records:
        if row['ordinal_at_signal'] is not None:row['ordinal_at_signal']=int(row['ordinal_at_signal'])
    summary=json.loads((source/'summary.json').read_bytes());summary['groups']=aggregate(records,plan)
    summary['research_revision']=dict(parent_manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
        corrected_at=datetime.now(timezone.utc).isoformat(),reason='KR price index is not comparable with dividend-adjusted stock return',
        model_observations_changed=False,model_parameters_changed=False,stock_returns_changed=False,
        original_output_preserved=True)
    (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    files={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest() for p in output.rglob('*') if p.is_file() and p.name!='manifest.json'}
    (output/'manifest.json').write_text(json.dumps(dict(schema='base-research-artifacts-1',files=files),indent=2)+'\n')
    print(json.dumps(summary['research_revision'],indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--plan',required=True);parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args();revise_benchmark_basis(args.plan,args.source,args.output)

