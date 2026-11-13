#!/usr/bin/env python3
"""Map each surviving mutant to the SOURCE file:line it edits.

mutmut 3.x writes ``mutants/<path>.spans`` (JSON ``{"spans": {"<func>__mutmut_N": [first, last]}}``,
1-based inclusive line ranges inside the generated ``mutants/<path>`` file; see
``mutmut/mutation/data.py`` and ``file_mutation.collect_line_spans``). Each mutant is a full copy of
its function, as is ``<func>__mutmut_orig``, so the first line where the two copies differ is the
mutated line. That line's index inside the original copy is mapped to the real source file by
locating the copy's body in the source.

    mutation_survivor_lines.py --backend DIR --log LOG --module-path app/auth/lockout.py

Prints ``<mutant>\t<file>:<line>\t<mutated line>`` per survivor read from LOG's summary block.
Never raises on a mutant it cannot place; it prints ``<file>:?`` so the gap is visible.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_SURVIVOR = re.compile(r'^(\S+)__mutmut_(\d+): survived$')


def _from_def(lines: list[str]) -> list[str]:
    """Lines from the ``def`` on, without trailing blank lines."""
    start = next((i for i, ln in enumerate(lines) if re.match(r'\s*(async\s+)?def\s', ln)), 0)
    out = lines[start:]
    while out and not out[-1].strip():
        out.pop()
    return out


def locate(backend: Path, module_path: str, mutant: str) -> tuple[int | None, str]:
    """Return (source line or None, mutated source text) for ``<func>__mutmut_N``."""
    spans_file = backend / 'mutants' / (module_path + '.spans')
    mutated = backend / 'mutants' / module_path
    source = backend / module_path
    try:
        spans = json.loads(spans_file.read_text())['spans']
        gen = mutated.read_text().splitlines()
        src = source.read_text().splitlines()
        func = mutant.rsplit('__mutmut_', 1)[0]
        o0, o1 = spans[f'{func}__mutmut_orig']
        m0, m1 = spans[mutant]
    except (OSError, KeyError, ValueError):
        return None, ''
    norm = re.compile(r'__mutmut_(?:orig|\d+)')
    # A span can start on leading blank lines and carries a decorator or two: anchor on `def`.
    orig, mut = (_from_def(gen[a - 1 : b]) for a, b in ((o0, o1), (m0, m1)))
    idx = next(
        (
            i
            for i, (a, b) in enumerate(zip(orig, mut, strict=False))
            if norm.sub('', a) != norm.sub('', b)
        ),
        min(len(orig), len(mut)),
    )
    text = mut[idx].strip() if idx < len(mut) else ''
    real = func.split('ǁ')[-1].removeprefix('x_') if 'ǁ' in func else func.removeprefix('x_')
    defs = [
        k for k, ln in enumerate(src) if re.match(rf'\s*(async\s+)?def\s+{re.escape(real)}\b', ln)
    ]
    # Several same-named methods: the right one ends with the same line as the original copy.
    pick = next(
        (
            k
            for k in defs
            if k + len(orig) <= len(src) and src[k + len(orig) - 1].strip() == orig[-1].strip()
        ),
        None,
    )
    if pick is None:
        return None, text
    return pick + idx + 1, text  # 0-based def line + offset, as a 1-based line number


def survivors_in_log(log: Path) -> list[str]:
    """Survivor mutant names from the LAST summary block, qualified as in the log."""
    text = log.read_text(errors='replace')
    block = text.split('=== OT-MUTATION-SUMMARY BEGIN')[-1]
    return [
        m.group(1) + '__mutmut_' + m.group(2)
        for ln in block.splitlines()
        if (m := _SURVIVOR.match(ln))
    ]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--backend', required=True)
    p.add_argument('--log', required=True)
    p.add_argument('--module-path', required=True)
    a = p.parse_args(argv)
    for full in survivors_in_log(Path(a.log)):
        short = full.rsplit('.', 1)[-1]
        line, text = locate(Path(a.backend), a.module_path, short)
        print(f'{full}\t{a.module_path}:{line if line else "?"}\t{text}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
