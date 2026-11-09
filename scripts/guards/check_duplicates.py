#!/usr/bin/env python3
"""Ratchet for duplicated code (jscpd), keyed by file.

Runs the pinned ``jscpd`` from ``frontend/node_modules`` over ``backend/app``, ``backend/tests``,
``scripts`` and ``frontend/src`` (Python, TypeScript, JavaScript and Svelte) with the json reporter,
and counts, per file, how many clone fragments sit in it (a clone between two files counts once in
each). ``baselines/duplicates.txt`` holds the counts; they may only shrink and a file with no entry
may not gain a clone. Output that is not the expected shape fails closed (exit 2).

``--min-tokens 100`` is double jscpd's default of 50. At 50 the run reports ~3,500 clones that are
mostly import blocks, keyword-argument lists and signature boilerplate; at 100 it reports ~560
blocks that are worth merging. The threshold is a constant here, so changing it re-baselines
deliberately instead of silently.

Usage:
    check_duplicates.py [--root DIR] [--baseline FILE] [--jscpd-json PATH] [--report PATH]
                        [--timeout SEC] [--tighten]
    check_duplicates.py --write-baseline --reason TEXT [--force]

``--report PATH`` also writes every clone group (largest first, tagged backend/frontend) for the
duplicate-code burn-down lanes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_HERE = Path(__file__).resolve().parent
_DEFAULT_BASELINE = _HERE / 'baselines' / 'duplicates.txt'
SCAN_PATHS = ('backend/app', 'backend/tests', 'scripts', 'frontend/src')
FORMATS = 'python,typescript,javascript,svelte'
MIN_TOKENS = 100
IGNORE = ','.join(
    (
        '**/node_modules/**',
        '**/venv/**',
        '**/.svelte-kit/**',
        '**/alembic/versions/**',
        'frontend/src/lib/i18n/locales/**',
        '**/*.min.js',
    )
)
_TIMEOUT_SECONDS = 600


def _rel(name: str, root: Path) -> str:
    path = Path(name)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def parse(raw: str, root: Path) -> tuple[dict[str, int], list[dict[str, object]]]:
    """Return ``(clone fragments per file, clone groups)`` from a jscpd json report."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = f'jscpd report is not JSON: {exc}'
        raise bl.ScanError(msg) from exc
    if not isinstance(data, dict) or not isinstance(data.get('duplicates'), list):
        msg = 'jscpd report has no "duplicates" list'
        raise bl.ScanError(msg)
    counts: Counter[str] = Counter()
    clones: list[dict[str, object]] = []
    for dup in data['duplicates']:
        try:
            sides = [dup['firstFile'], dup['secondFile']]
            located = [
                {'file': _rel(s['name'], root), 'start': s['start'], 'end': s['end']} for s in sides
            ]
            tokens, lines, fmt = dup['tokens'], dup['lines'], dup['format']
        except (KeyError, TypeError) as exc:
            msg = f'unrecognised jscpd clone record: {str(dup)[:120]!r}'
            raise bl.ScanError(msg) from exc
        for side in located:
            counts[str(side['file'])] += 1
        area = {
            'frontend' if str(s['file']).startswith('frontend/') else 'backend' for s in located
        }
        clones.append(
            {
                'tokens': tokens,
                'lines': lines,
                'format': fmt,
                'area': '+'.join(sorted(area)),
                'a': located[0],
                'b': located[1],
            }
        )
    clones.sort(key=lambda c: (-int(str(c['tokens'])), str(c['a'])))
    return dict(counts), clones


def _run(root: Path, timeout: int) -> str:
    jscpd = root / 'frontend' / 'node_modules' / '.bin' / 'jscpd'
    if not jscpd.exists():
        msg = f'{jscpd} is missing; run npm ci in frontend/'
        raise bl.ScanError(msg)
    with tempfile.TemporaryDirectory(prefix='ot-jscpd.') as out:
        argv = [
            str(jscpd),
            *SCAN_PATHS,
            '--min-tokens',
            str(MIN_TOKENS),
            '--format',
            FORMATS,
            '--ignore',
            IGNORE,
            '--reporters',
            'json',
            '--output',
            out,
            '--absolute',
            '--no-colors',
        ]
        try:
            proc = subprocess.run(
                argv, cwd=root, capture_output=True, text=True, timeout=timeout, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            msg = f'jscpd did not run: {exc}'
            raise bl.ScanError(msg) from exc
        report = Path(out) / 'jscpd-report.json'
        if proc.returncode not in (0, 1) or not report.is_file():
            msg = f'jscpd exited {proc.returncode}: {proc.stderr.strip()[:500]}'
            raise bl.ScanError(msg)
        return report.read_text(encoding='utf-8')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--root', type=Path, default=None, help='repo root (default: auto)')
    parser.add_argument('--baseline', type=Path, default=_DEFAULT_BASELINE)
    parser.add_argument('--jscpd-json', type=Path, default=None, help='prior jscpd json report')
    parser.add_argument('--report', type=Path, default=None, help='write the clone groups here')
    parser.add_argument('--timeout', type=int, default=_TIMEOUT_SECONDS)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale lines')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        root: Path = args.root or bl.find_root(_HERE, ('frontend/package.json',))
        raw = (
            args.jscpd_json.read_text(encoding='utf-8')
            if args.jscpd_json
            else _run(root, args.timeout)
        )
        current, clones = parse(raw, root)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            payload = {'min_tokens': MIN_TOKENS, 'scan_paths': SCAN_PATHS, 'clones': clones}
            args.report.write_text(json.dumps(payload, indent=1) + '\n', encoding='utf-8')
        if args.write_baseline:
            written: int = bl.run_write_baseline(args, args.baseline, current)
            return written
        baseline = bl.load(args.baseline)
        if args.tighten:
            print(f'tightened {bl.tighten(args.baseline, current, baseline)} line(s)')
            baseline = bl.load(args.baseline)
    except (bl.ScanError, ValueError, OSError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    problems = bl.compare(current, baseline, unit='clone fragment(s)')
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        print(f'{len(problems)} duplicate-code ratchet problem(s)', file=sys.stderr)
        return 1
    print(f'duplicates ratchet ok: {len(clones)} clones, {len(current)} files')
    return 0


if __name__ == '__main__':
    sys.exit(main())
