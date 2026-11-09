#!/usr/bin/env python3
"""Ratchet guard: raw process-environment reads in ``backend/app``.

Settings belong behind ``app/core/config.py``. Every other module that reaches into the process
environment directly (``os.getenv``, ``os.environ[...]``, ``os.environ.get``, ``os.environ.setdefault``,
``from os import environ``, ``import os as _os`` ...) is counted per file and recorded in
``scripts/guards/baselines/raw_env.txt``. The baseline can only shrink.

Counting is AST based, so mentions inside comments, docstrings and string literals are ignored,
and function bodies are walked like any other code. Each reference to an ``os`` environment
attribute counts once, so ``os.environ.get("X")`` is one site, not two. A file that does not
parse fails the run (exit 2) rather than being skipped.

Reference count when the baseline was written: 253 sites in 86 files, made of ``os.getenv`` 139,
``os.environ.get`` 93, ``os.environ[...]`` 10, ``os.environ.setdefault`` 4, bare ``os.environ``
in a conditional expression 4 and bare ``os.environ`` passed as an argument 3.

Usage::

    check_raw_env.py [--root DIR] [--baseline FILE]
    check_raw_env.py --tighten
    check_raw_env.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent
_APP = Path('backend') / 'app'
_DEFAULT_BASELINE = _HERE / 'baselines' / 'raw_env.txt'

# Repo-relative paths allowed to read the environment directly, each with the reason.
EXEMPT: dict[str, str] = {
    'backend/app/core/config.py': 'the sanctioned settings module; the one place env is read',
    # Settings fields moved verbatim out of config.py (v0.7.0 L07); `Settings` inherits them, so
    # these classes are the declaration half of the same sanctioned settings module.
    'backend/app/core/settings_identity.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_providers.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_search.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_security.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/settings_storage.py': 'Settings mixin split from config.py: field declarations',
    'backend/app/core/env.py': 'the sanctioned reader for tuning knobs (D4 policy); every other module calls it',
}

# Attributes of the ``os`` module that read or mutate the process environment.
_ENV_NAMES = frozenset({'getenv', 'getenvb', 'environ', 'environb', 'putenv', 'unsetenv'})


def count_sites(tree: ast.AST) -> int:
    """Number of raw environment references in ``tree``, following ``os`` and member aliases."""
    os_names: set[str] = set()
    env_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'os':
                    os_names.add(alias.asname or 'os')
                elif alias.name.startswith('os.') and alias.asname is None:
                    os_names.add('os')
        elif isinstance(node, ast.ImportFrom) and node.module == 'os' and node.level == 0:
            for alias in node.names:
                if alias.name in _ENV_NAMES:
                    env_names.add(alias.asname or alias.name)
    total = 0
    for node in ast.walk(tree):
        is_os_attr = (
            isinstance(node, ast.Attribute)
            and node.attr in _ENV_NAMES
            and isinstance(node.value, ast.Name)
            and node.value.id in os_names
        )
        is_member_alias = isinstance(node, ast.Name) and node.id in env_names
        if is_os_attr or is_member_alias:
            total += 1
    return total


def scan(root: Path) -> dict[str, int]:
    """Map repo-relative path -> site count for every non-exempt file with at least one site."""
    app = root / _APP
    if not app.is_dir():
        msg = f'{app} is not a directory; pass --root explicitly'
        raise bl.ScanError(msg)
    findings: dict[str, int] = {}
    for path in sorted(app.rglob('*.py')):
        rel = path.relative_to(root).as_posix()
        if rel in EXEMPT:
            continue
        try:
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=rel)
        except (SyntaxError, UnicodeDecodeError, ValueError) as exc:
            msg = f'cannot parse {rel}: {exc}'
            raise bl.ScanError(msg) from exc
        count = count_sites(tree)
        if count:
            findings[rel] = count
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument(
        '--root', type=Path, default=None, help='repository root (default: found from this file)'
    )
    parser.add_argument('--baseline', type=Path, default=None, help='baseline file')
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale entries')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root = args.root or bl.find_root(_HERE, ('backend/app',))
        baseline_path = args.baseline or (root / 'scripts' / 'guards' / 'baselines' / 'raw_env.txt')
        findings = scan(root)
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, baseline_path, findings)
            return written
        baseline = bl.load(baseline_path)
    except (bl.ScanError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    return bl.finish_ratchet(
        args,
        baseline_path,
        findings,
        baseline,
        unit='raw env reads',
        heading='raw environment reads outside app/core/config.py:',
        ok_message=f'raw env: {sum(findings.values())} sites in {len(findings)} files, within the baseline',
    )


if __name__ == '__main__':
    sys.exit(main())
