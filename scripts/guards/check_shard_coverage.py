#!/usr/bin/env python3
"""Fail unless the CI test shards together run every test exactly once.

Each shard writes the node ids it selected (``backend/tests/shard_manifest.py``, enabled with
``-p tests.shard_manifest`` and ``OT_SHARD_MANIFEST=<file>``); one unsplit collection writes the
full list. Splitting is only a speed-up if it is lossless, so this checks, over those files:

* no node id appears in two shards (a test run twice);
* no node id of the full collection is missing from every shard (a test never run);
* no shard holds an id the full collection lacks (the shards disagree on what exists);
* no shard manifest is empty (an empty group means the matrix outgrew the suite or a group
  index was mistyped, and would pass the other checks only by accident).

Usage::

    check_shard_coverage.py --full all.txt shard-1.txt shard-2.txt ...
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path


def read_ids(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def check(full: list[str], shards: dict[str, list[str]]) -> list[str]:
    """Return one message per violation; an empty list means the shards are a perfect partition."""
    problems: list[str] = []
    if not full:
        problems.append('the full collection is empty: nothing to compare the shards against')
    if len(set(full)) != len(full):
        problems.append('the full collection lists a node id more than once')
    if not shards:
        problems.append('no shard manifests were given')
    for name, ids in shards.items():
        if not ids:
            problems.append(f'{name}: selected no tests')
    seen = Counter(i for ids in shards.values() for i in set(ids))
    duplicated = sorted(i for i, n in seen.items() if n > 1)
    missing = sorted(set(full) - set(seen))
    unknown = sorted(set(seen) - set(full))
    if duplicated:
        problems.append(
            f'{len(duplicated)} test(s) run in more than one shard, e.g. {duplicated[0]}'
        )
    if missing:
        problems.append(f'{len(missing)} test(s) run in no shard, e.g. {missing[0]}')
    if unknown:
        problems.append(
            f'{len(unknown)} shard test(s) absent from the full collection, e.g. {unknown[0]}'
        )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument(
        '--full', required=True, type=Path, help='manifest of the unsplit collection'
    )
    parser.add_argument('shards', nargs='+', type=Path, help='one manifest per shard')
    args = parser.parse_args(argv)
    full = read_ids(args.full)
    shards = {str(p): read_ids(p) for p in args.shards}
    problems = check(full, shards)
    for problem in problems:
        print(f'FAIL: {problem}', file=sys.stderr)
    if problems:
        return 1
    print(f'OK: {len(full)} tests, each in exactly one of {len(shards)} shards')
    return 0


if __name__ == '__main__':
    sys.exit(main())
