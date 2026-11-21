"""No-network cache census; only unpickle an explicitly trusted main-run restore.

Example (after exact actions/cache/restore):
python -m watchlist.manual_eod_audit --catalog catalog.csv --data trusted/data \
  --cutoff 2026-11-13T00:47:17Z --source-run-id 43339475868 --cache-restored
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from .manual_catalog import filter_rows, parse_csv, unique_instruments
from .manual_eod_cache import inspect_core_cache
from .manual_eod_contract import aware, digest
from .manual_eod_provider import specification, Unavailable

CLASSIFICATIONS = ('reusable', 'identity_pending', 'stale', 'basis_mismatch', 'no_cache', 'other', 'not_inspected')
CHECKS = ('exists', 'fresh', 'raw_ohlcv', 'sessions', 'basis', 'identity')


def audit_population(rows, *, data_dir, cutoff, previous=None, now=None, source_run_id=None, cache_restored=None, inspections=None):
    results = []
    prior = (previous or {}).get('instruments', {})
    for row in unique_instruments(filter_rows(rows)):
        venue = row['symbol'].split(':')[0]
        if venue not in ('KRX', 'NYSE', 'NASDAQ', 'AMEX') or row['asset_type'] not in ('stock', 'etf'):
            continue
        item = dict(symbol=row['symbol'], market='KR' if venue == 'KRX' else 'US', asset_type=row['asset_type'])
        try:
            spec = specification(row)
            old = prior.get(row['symbol'], {})
            inspection = inspect_core_cache(data_dir, spec, cutoff,
                evidence=old.get('listing_identity') or old.get('identity_evidence'), now=now or cutoff)
            if inspections is not None:
                inspections[row['symbol']] = inspection
            item.update(inspection.diagnostic())
        except Unavailable as exc:
            item.update(reason=str(exc), classification='other', checks={k: None for k in CHECKS})
        results.append(item)

    def group(items):
        counts = Counter(r['classification'] for r in items)
        return dict(population=len(items), classifications={k: counts[k] for k in CLASSIFICATIONS},
            passed={k: sum(r['checks'][k] is True for r in items) for k in CHECKS},
            unobserved={k: sum(r['checks'][k] is None for r in items) for k in CHECKS},
            external_verification_or_collection=None if counts['not_inspected'] else sum(r['classification']!='reusable' for r in items))

    groups = {market+'_'+kind: group([r for r in results if r['market']==market and r['asset_type']==kind])
              for market in ('KR', 'US') for kind in ('stock', 'etf')}
    groups.update({market: group([r for r in results if r['market']==market]) for market in ('KR','US')})
    groups['total'] = group(results)
    return dict(schema='manual-eod-cache-audit-v1', cutoff_at=cutoff.isoformat(), source_run_id=source_run_id,
        exact_cache_key='wl-cache-'+str(source_run_id) if source_run_id else None, cache_restored=cache_restored,
        cache_access='not_inspected' if data_dir is None else 'trusted_directory_inspected',
        classification_precedence=['not_inspected','no_cache','other','basis_mismatch','stale','identity_pending','reusable'], groups=groups, instruments=results)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--catalog', required=True)
    parser.add_argument('--data', help='Trusted restored engine data; omit to report unobserved counts')
    parser.add_argument('--cutoff', required=True)
    parser.add_argument('--source-run-id')
    parser.add_argument('--cache-restored', action='store_true')
    parser.add_argument('--published', default='watchlist/published')
    parser.add_argument('--output')
    args = parser.parse_args()
    from .manual_eod_publish import read_previous
    raw = Path(args.catalog).read_bytes()
    report = audit_population(parse_csv(raw), data_dir=args.data, cutoff=aware(args.cutoff),
        previous=read_previous(args.published), source_run_id=args.source_run_id, cache_restored=args.cache_restored)
    report['catalog_sha256'] = digest(raw)
    text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
    if args.output:
        Path(args.output).write_text(text+'\n', encoding='utf-8')
    else:
        print(text)


if __name__ == '__main__':
    main()

