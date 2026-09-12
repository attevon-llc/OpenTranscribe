#!/usr/bin/env python3
"""Generate or verify the committed OpenAPI schema snapshot (issue #798, step 1).

Usage:
    backend/venv/bin/python scripts/generate-openapi.py --write   # regenerate backend/openapi.json
    backend/venv/bin/python scripts/generate-openapi.py --check   # verify it is current (CI)

The developer venv runs Python 3.12; CI runs 3.13. Byte-identical output across both
was verified at current dependency pins before this script was written — see
scripts/CLAUDE.md.

Determinism notes (measured, do not "fix" what already works):
- The document is serialized with ``indent=2, sort_keys=True, ensure_ascii=False,
  separators=(",", ": ")`` plus a trailing ``"\\n"``. The trailing newline is
  mandatory: without it, the ``end-of-file-fixer`` pre-commit hook rewrites the file
  CI compares against and the check flaps.
- ``info.version`` is the one real churn source: ``app.main`` embeds ``APP_VERSION``,
  which ``scripts/release/20-bump.sh`` rewrites every release. Both --write and --check
  normalize it to the constant ``SNAPSHOT_VERSION`` below via the same function, so
  they can never disagree with each other because of a version bump alone.

This script needs no running stack: SQLAlchemy's ``create_engine`` is lazy, so
importing ``app.main`` and calling ``app.openapi()`` works under a bare environment.
Two things must happen before any ``app`` import:

1. ``TEMP_DIR``/``DATA_DIR``/``MODELS_DIR`` must be set (default ``/app/temp`` etc. do
   not exist on a host and ``Settings.__init__`` mkdirs them, raising
   ``PermissionError``), and ``TESTING=True`` so ``Settings`` skips other host-specific
   validation. These do not perturb the generated schema (verified).
2. The working directory must be ``backend/`` and ``backend`` must be first on
   ``sys.path`` — ``Settings`` resolves ``env_file`` against the CWD, and running this
   from the repo root would load the operator's real ``.env``.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

SNAPSHOT_VERSION = '0.0.0-snapshot'
MIN_PATHS = 300
MIN_SCHEMAS = 250
MAX_DIFF_LINES = 300

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / 'backend'
SNAPSHOT_PATH = BACKEND_DIR / 'openapi.json'


def _prepare_environment() -> None:
    """Set env vars and sys.path/CWD needed before importing anything from ``app``."""
    tmp = tempfile.gettempdir()
    os.environ.setdefault('TEMP_DIR', tmp)
    os.environ.setdefault('DATA_DIR', tmp)
    os.environ.setdefault('MODELS_DIR', tmp)
    os.environ.setdefault('TESTING', 'True')
    os.chdir(BACKEND_DIR)
    sys.path.insert(0, str(BACKEND_DIR))


def _normalize(schema: dict[str, Any]) -> dict[str, Any]:
    """Overwrite the one field known to churn release-to-release: info.version."""
    schema['info']['version'] = SNAPSHOT_VERSION
    return schema


def _serialize(schema: dict[str, Any]) -> str:
    return (
        json.dumps(
            schema,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            separators=(',', ': '),
        )
        + '\n'
    )


def _generate() -> dict[str, Any]:
    """Import the app and produce the normalized OpenAPI document."""
    _prepare_environment()
    from app.main import app  # noqa: PLC0415 (deferred: heavy import, see module docstring)

    schema = app.openapi()
    return _normalize(schema)


def _check_floors(schema: dict[str, Any], label: str) -> None:
    paths = schema.get('paths', {})
    schemas = schema.get('components', {}).get('schemas', {})
    if len(paths) < MIN_PATHS:
        print(
            f'error: {label} has {len(paths)} paths, expected >= {MIN_PATHS}. '
            'Refusing to treat this as a valid OpenAPI document.',
            file=sys.stderr,
        )
        sys.exit(1)
    if len(schemas) < MIN_SCHEMAS:
        print(
            f'error: {label} has {len(schemas)} component schemas, expected >= {MIN_SCHEMAS}. '
            'Refusing to treat this as a valid OpenAPI document.',
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_write() -> int:
    schema = _generate()
    _check_floors(schema, 'generated schema')
    SNAPSHOT_PATH.write_text(_serialize(schema), encoding='utf-8')
    print(f'wrote {SNAPSHOT_PATH}')
    return 0


def cmd_check() -> int:
    # A missing or empty snapshot is a hard failure, never a skip: unlike a hardened
    # deployment serving nothing (the analogous case in test-upgrade.sh), this file is
    # checked into git — its absence means the gate itself is broken, not that there is
    # nothing to check.
    if not SNAPSHOT_PATH.is_file() or SNAPSHOT_PATH.stat().st_size == 0:
        print(
            f'error: {SNAPSHOT_PATH} is missing or empty. '
            'Regenerate with: backend/venv/bin/python scripts/generate-openapi.py --write',
            file=sys.stderr,
        )
        return 1

    committed_text = SNAPSHOT_PATH.read_text(encoding='utf-8')
    committed_schema = json.loads(committed_text)
    _check_floors(committed_schema, 'committed backend/openapi.json')

    generated_schema = _generate()
    _check_floors(generated_schema, 'generated schema')
    generated_text = _serialize(generated_schema)

    if generated_text == committed_text:
        print('backend/openapi.json is current.')
        return 0

    diff = list(
        difflib.unified_diff(
            committed_text.splitlines(keepends=True),
            generated_text.splitlines(keepends=True),
            fromfile='backend/openapi.json (committed)',
            tofile='backend/openapi.json (generated)',
        )
    )
    truncated = diff[:MAX_DIFF_LINES]
    remainder = len(diff) - len(truncated)
    print('error: backend/openapi.json is stale.', file=sys.stderr)
    for line in truncated:
        print(line, end='', file=sys.stderr)
    if remainder > 0:
        print(f'... ({remainder} more diff lines truncated)', file=sys.stderr)
    print(
        '\nRegenerate with: backend/venv/bin/python scripts/generate-openapi.py --write',
        file=sys.stderr,
    )
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--write', action='store_true', help='regenerate backend/openapi.json')
    group.add_argument(
        '--check', action='store_true', help='verify backend/openapi.json is current'
    )
    args = parser.parse_args()

    if args.write:
        return cmd_write()
    return cmd_check()


if __name__ == '__main__':
    sys.exit(main())
