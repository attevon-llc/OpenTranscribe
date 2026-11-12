#!/usr/bin/env python3
"""Ratchet for unused locals, parameters and imports in ``.svelte`` files.

``@typescript-eslint/no-unused-vars`` crashes on svelte-eslint-parser nodes (an implicit reactive
declaration such as ``$: x = 1`` has no definition; reproduced with typescript-eslint 8.72.1 and
eslint-plugin-svelte 3.24.1 / svelte-eslint-parser 1.10.1), so it is off for ``.svelte`` files and
the TypeScript compiler does the job instead. ``frontend/tsconfig.ratchets.json`` extends the real
tsconfig with ``noUnusedLocals`` and ``noUnusedParameters`` (plus nuia); this guard reads
``svelte-check --output machine-verbose`` run against it (one run shared with ``check_nuia_ratchet.py``) and counts the diagnostics with codes 6133
(declared but never read), 6192 (all imports unused), 6196 (declared but never used) and 6198
(all destructured elements unused) per ``path::TS<code>``. ``.ts`` files are left to eslint.

Counts may only shrink against ``baselines/svelte_unused.txt``. Output that is not the expected
shape fails closed (exit 2).

Usage:
    check_svelte_unused.py [--root DIR] [--baseline FILE] [--svelte-check-output PATH]
                           [--timeout SEC] [--tighten]
    check_svelte_unused.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _svelte_check as sc

_UNUSED_CODES = frozenset({6133, 6192, 6196, 6198})


def _select(record: dict) -> str | None:
    if record.get('code') not in _UNUSED_CODES:
        return None
    filename = sc.filename_of(record, 'unused-symbol diagnostic')
    if not filename.endswith('.svelte'):
        return None
    return f'frontend/{filename}::TS{record["code"]}'


RATCHET = sc.Ratchet(
    baseline=Path(__file__).resolve().parent / 'baselines' / 'svelte_unused.txt',
    description=__doc__.split('\n\n')[0],
    select=_select,
    unit='unused symbol(s)',
    problem_label='svelte unused-symbol',
    ok_message=lambda current: (
        f'svelte unused ratchet ok: {sum(current.values())} in {len(current)} buckets'
    ),
)


def parse(text: str) -> dict[str, int]:
    """Unused-symbol diagnostics in ``.svelte`` files as ``frontend/<path>::TS<code>`` -> count."""
    return sc.parse(text, RATCHET)


def main(argv: list[str] | None = None) -> int:
    return sc.main(RATCHET, argv)


if __name__ == '__main__':
    sys.exit(main())
