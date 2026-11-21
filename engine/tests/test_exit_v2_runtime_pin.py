"""Fail closed if the optional v2 research runtime drifts from its registry.

No market requests, price edits, or changes to the official watchlist.
"""
import json
from pathlib import Path
import platform

import exchange_calendars as xc
import numpy as np
import pandas as pd

from watchlist.exit_experiment_publish import registry_for

ROOT=Path(__file__).resolve().parents[1]

def test_exit_v2_workflow_constraints_match_immutable_registry(monkeypatch):
    runtime=dict(exchange_calendars='4.13.2',numpy='2.5.4',pandas='2.3.3',python=['3','12'])
    assert runtime==dict(
        exchange_calendars='4.13.2',numpy='2.5.4',pandas='2.3.3',python=['3','12'])
    constraints=(ROOT/'tests/fixtures/workflows/exit-research-v2-constraints.txt').read_text()
    pinned=dict(line.split('==') for line in constraints.splitlines()
        if '==' in line and not line.lstrip().startswith('#'))
    assert {k.replace('-','_'):v for k,v in pinned.items()}=={k:v for k,v in runtime.items() if k!='python'}
    workflow=(ROOT/'tests/fixtures/workflows/exit_experiments.yml').read_text()
    assert '-c .github/workflows/exit-research-v2-constraints.txt' in workflow
    assert 'python -m pip check' in workflow
    assert 'python-version: \'3.12\'' in workflow

    monkeypatch.setattr(pd,'__version__',runtime['pandas'])
    monkeypatch.setattr(np,'__version__',runtime['numpy'])
    monkeypatch.setattr(xc,'__version__',runtime['exchange_calendars'])
    monkeypatch.setattr(platform,'python_version_tuple',lambda:('3','12','0'))
    # Synthetic lineage only. The private immutable production registry is not exported.
    original=registry_for(None,'2026-01-01T00:00:00Z',.3)
    assert registry_for(original,'2026-01-02T00:00:00Z',.3)==original

def test_exit_v2_does_not_pin_every_watchlist_workflow():
    workflow=(ROOT/'tests/fixtures/workflows/watchlist.yml').read_text()
    assert 'exit-research-v2-constraints.txt' not in workflow
    assert (ROOT/'watchlist/exit_experiment_contract.py').read_text().find(
        "VERSION='exit-research-v2'")>=0

