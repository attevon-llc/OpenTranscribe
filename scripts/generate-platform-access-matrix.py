#!/usr/bin/env python3
"""Regenerate or verify docs/security/platform-access-matrix.tsv (issue #1281).

Usage:
    backend/venv/bin/python scripts/generate-platform-access-matrix.py --write
    backend/venv/bin/python scripts/generate-platform-access-matrix.py --check

The matrix is derived from the live route table and an AST scan of ``backend/app`` by
``backend/tests/fixtures/platform_access_matrix.py``; ``backend/tests/unit/
test_platform_access_matrix.py`` makes the same comparison under pytest. Read the diff of a
regenerated file before committing it: a new row is a new way platform staff can reach a
tenant, and an ``UNCLASSIFIED`` cell means the classification tables in that module need a
decision, not that the file should be committed as is.
"""

from __future__ import annotations

import argparse
import difflib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / 'backend'


def _prepare_environment() -> None:
    tmp = tempfile.gettempdir()
    for name in ('TEMP_DIR', 'DATA_DIR', 'MODELS_DIR'):
        os.environ.setdefault(name, tmp)
    os.environ.setdefault('TESTING', 'True')
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))
    sys.path.insert(0, str(BACKEND_DIR / 'tests'))


def _generate() -> str:
    _prepare_environment()
    from fixtures import platform_access_matrix as matrix

    from app.main import app

    return matrix.render(matrix.build_rows(app))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--write', action='store_true')
    mode.add_argument('--check', action='store_true')
    args = parser.parse_args()

    generated = _generate()
    path = REPO_ROOT / 'docs' / 'security' / 'platform-access-matrix.tsv'
    if args.write:
        path.write_text(generated, encoding='utf-8')
        print(f'wrote {path}')
        return 0
    committed = path.read_text(encoding='utf-8') if path.is_file() else ''
    if committed == generated:
        print('platform-access-matrix.tsv is current')
        return 0
    diff = difflib.unified_diff(
        committed.splitlines(), generated.splitlines(), 'committed', 'generated', lineterm=''
    )
    print('\n'.join(list(diff)[:200]), file=sys.stderr)
    print('regenerate with: scripts/generate-platform-access-matrix.py --write', file=sys.stderr)
    return 1


if __name__ == '__main__':
    sys.exit(main())
