"""Exercise the journal workflow's actual sparse module list without private data."""
from pathlib import Path
import shutil
import subprocess
import sys

import yaml


def test_journal_sparse_checkout_has_all_eager_watchlist_dependencies(tmp_path):
    root=Path(__file__).resolve().parents[1]
    workflow=yaml.safe_load((root/'tests/fixtures/workflows/trading_journal.yml').read_text())
    checkout=workflow['jobs']['collect-data']['steps'][0]['with']['sparse-checkout']
    files=[name.strip() for name in checkout.splitlines() if name.strip().startswith('watchlist/')]
    assert 'watchlist/session_calendar.py' in files
    for name in files:
        shutil.copyfile(root/name,tmp_path/Path(name).name)
    # -I excludes the checkout/PYTHONPATH: a missing sparse dependency cannot be
    # accidentally satisfied by the full repository on the test runner.
    result=subprocess.run([sys.executable,'-I','-c',
        'import sys;sys.path.insert(0,sys.argv[1]);import trend_watchlist',str(tmp_path)],
        cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr

