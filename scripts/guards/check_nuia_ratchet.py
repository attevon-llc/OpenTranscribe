#!/usr/bin/env python3
"""Ratchet for ``noUncheckedIndexedAccess`` errors in the frontend.

``arr[i]`` and record lookups are typed as present by default, so an out-of-range index or a missing
key is ``undefined`` at runtime with no compiler warning. ``frontend/tsconfig.ratchets.json``
extends the real tsconfig with ``noUncheckedIndexedAccess`` (TypeScript tsconfig reference) and the
two noUnused* flags; the real config is error-free under ``npm run check``, so every error in this
run is one a flag introduced. The guard runs svelte-check against it (one run shared with
``check_svelte_unused.py``) and counts the ERROR diagnostics that are not unused-symbol codes per file. Production files are held at zero; ``*.test.ts`` files are baselined
until a later pass converts them, after which the flag can move into the main tsconfig.

Counts may only shrink against ``baselines/nuia.txt``. Output that is not the expected shape
fails closed (exit 2).

Usage:
    check_nuia_ratchet.py [--root DIR] [--baseline FILE] [--svelte-check-output PATH]
                          [--timeout SEC] [--tighten]
    check_nuia_ratchet.py --write-baseline --reason TEXT [--force]
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _svelte_check as sc


def _select(record: dict) -> str | None:
    if record.get('type') != 'ERROR' or record.get('code') in sc.UNUSED_CODES:
        return None  # unused-symbol errors belong to check_svelte_unused.py
    return f'frontend/{sc.filename_of(record, "error diagnostic")}'


RATCHET = sc.Ratchet(
    baseline=Path(__file__).resolve().parent / 'baselines' / 'nuia.txt',
    description=__doc__.split('\n\n')[0],
    select=_select,
    unit='unchecked index access error(s)',
    problem_label='noUncheckedIndexedAccess',
    ok_message=lambda current: (
        f'noUncheckedIndexedAccess ratchet ok: {sum(current.values())} in {len(current)} files'
    ),
)


def parse(text: str) -> dict[str, int]:
    """ERROR diagnostics per file as ``frontend/<path>`` -> count."""
    return sc.parse(text, RATCHET)


def main(argv: list[str] | None = None) -> int:
    return sc.main(RATCHET, argv)


if __name__ == '__main__':
    sys.exit(main())
