"""One entry point for public-source checks and credential-free tests."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
IGNORED = {'.git', '__pycache__', '.pytest_cache', '.venv'}
FORBIDDEN = {'data', 'published', 'journals', 'logs', 'private', 'reports', '.cache'}
TOKEN = re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|AKIA[0-9A-Z]{16}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b\d{8,12}:[A-Za-z0-9_-]{30,}\b)')


def files():
    return sorted(p for p in ROOT.rglob('*') if p.is_file()
                  and not any(part in IGNORED for part in p.relative_to(ROOT).parts))


def check():
    manifest = json.loads((ROOT / 'public-files.json').read_text())
    actual = {p.relative_to(ROOT).as_posix() for p in files()}
    expected = set(manifest['files']) | {'public-files.json'}
    errors = [f'Unreviewed file: {p}' for p in sorted(actual - expected)]
    errors += [f'Missing file: {p}' for p in sorted(expected - actual)]
    for p in files():
        rel = p.relative_to(ROOT)
        if any(part in FORBIDDEN for part in rel.parts):
            errors.append(f'Operational data path: {rel}')
        if p.name.startswith('.env') or p.name == 'secrets.toml' or p.suffix in {'.pkl', '.pickle'}:
            errors.append(f'Forbidden file: {rel}')
        text = p.read_text(encoding='utf-8')
        if TOKEN.search(text):
            errors.append(f'Possible credential: {rel}')
        if rel.parts[:2] == ('.github', 'workflows'):
            if re.search(r'secrets\s*[.\[]|pull_request_target|workflow_run|repository_dispatch|schedule:', text):
                errors.append(f'Privileged trigger or secret in public CI: {rel}')
    # These contracts are intentionally duplicated for independent deployments.
    for name in ('manual_catalog.py', 'manual_eod_contract.py', 'exit_experiment_contract.py'):
        if (ROOT / 'engine/watchlist' / name).read_bytes() != (ROOT / 'app' / name).read_bytes():
            errors.append(f'Engine/app contract drift: {name}')
    if errors:
        raise SystemExit('\n'.join(errors))
    print(f'PASS: {len(actual)} reviewed paths; no operational data paths or token signatures.')


def test(project):
    env = {k: v for k, v in os.environ.items()
           if not re.search(r'TOKEN|SECRET|PASSWORD|^NH_|^KRX_|^TELEGRAM_|^DATA_|^PORTFOLIO_', k)}
    env['PYTHON_DOTENV_DISABLED'] = '1'
    subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q',
                    '--disable-socket', '--allow-unix-socket', '--tb=short'],
                   cwd=ROOT / project, env=env, check=True)
    if project == 'engine':
        subprocess.run(['node', 'tests/test_scheduler.cjs'], cwd=ROOT / project, env=env, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['check', 'test'])
    parser.add_argument('project', nargs='?', choices=['engine', 'app'])
    args = parser.parse_args()
    if args.command == 'check':
        check()
    else:
        if not args.project:
            parser.error('test requires engine or app')
        check()
        test(args.project)


if __name__ == '__main__':
    main()
