"""Shared machinery for the ratchets that read ``svelte-check --output machine-verbose``.

``check_nuia_ratchet.py`` and ``check_svelte_unused.py`` differ only in which diagnostics they count and
under what key. Everything else
(running the tool, parsing its output, the CLI, baseline compare/tighten, exit codes) lives here.
Output that is not the expected shape fails closed (exit 2).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline as bl

_LINE = re.compile(r'^\d+ (?P<body>\{.*\})\s*$')
_TIMEOUT_SECONDS = 900
#: One tsconfig for every svelte-check ratchet: the real config plus noUnusedLocals,
#: noUnusedParameters and noUncheckedIndexedAccess. Each ratchet keeps its own diagnostics by code.
TSCONFIG = 'tsconfig.ratchets.json'
#: TypeScript diagnostics the noUnused* flags add. The nuia ratchet must not count them.
UNUSED_CODES = frozenset({6133, 6138, 6192, 6196, 6198, 6199, 6205})


@dataclass(frozen=True)
class Ratchet:
    """What distinguishes one svelte-check ratchet from another."""

    baseline: Path
    description: str
    select: Callable[[dict], str | None]  # diagnostic -> baseline key, or None to skip
    unit: str  # for bl.compare
    problem_label: str  # "<n> <label> ratchet problem(s)"
    ok_message: Callable[[dict[str, int]], str]


def parse(text: str, ratchet: Ratchet) -> dict[str, int]:
    """Count the diagnostics ``ratchet.select`` keeps, by the key it returns."""
    if ' COMPLETED ' not in text:
        msg = 'svelte-check output has no COMPLETED line (tool crash?)'
        raise bl.ScanError(msg)
    counts: Counter[str] = Counter()
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        try:
            record = json.loads(match['body'])
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        key = ratchet.select(record)
        if key is None:
            continue
        counts[key] += 1
    return dict(counts)


def filename_of(record: dict, label: str) -> str:
    """The diagnostic's file name; a diagnostic without one is a malformed run."""
    filename = record.get('filename')
    if not isinstance(filename, str):
        msg = f'{label} without a filename: {record!r:.120}'
        raise bl.ScanError(msg)
    return filename


def _tree_key(frontend: Path) -> str | None:
    """Hash of every tracked and untracked-unignored file under frontend/ (None: cannot tell).

    Two ratchets read the same svelte-check output, so the second one of a pre-commit run replays
    the first one's. Keyed by content, not mtime, and by every file svelte-check can see.
    """
    try:
        listing = subprocess.run(
            [
                'git',
                'ls-files',
                '-z',
                '--cached',
                '--others',
                '--exclude-standard',
                '--',
                '.',
                ':!.cache',
            ],
            cwd=frontend,
            capture_output=True,
            check=True,
            timeout=60,
        ).stdout
        digest = hashlib.sha256()
        for name in sorted(filter(None, listing.split(b'\0'))):
            path = frontend / os.fsdecode(name)
            digest.update(name + b'\0')
            if path.is_file():
                digest.update(path.read_bytes())
        return digest.hexdigest()[:24]
    except (OSError, subprocess.SubprocessError):
        return None


def _run(root: Path, tsconfig: str, timeout: int) -> str:
    frontend = root / 'frontend'
    key = _tree_key(frontend)
    cache = frontend / '.cache' / 'svelte-check' / f'{key}.txt' if key else None
    if cache is not None and cache.is_file():
        cached = cache.read_text(encoding='utf-8')
        if ' COMPLETED ' in cached:
            return cached
    output = _run_uncached(frontend, tsconfig, timeout)
    if cache is not None and ' COMPLETED ' in output:
        cache.parent.mkdir(parents=True, exist_ok=True)
        for old in cache.parent.glob('*.txt'):
            old.unlink(missing_ok=True)
        tmp = cache.with_suffix(f'.{os.getpid()}.tmp')
        tmp.write_text(output, encoding='utf-8')
        os.replace(tmp, cache)
    return output


def _run_uncached(frontend: Path, tsconfig: str, timeout: int) -> str:
    if not (frontend / tsconfig).is_file():
        msg = f'{frontend}/{tsconfig} is missing'
        raise bl.ScanError(msg)
    try:
        # Generates .svelte-kit/tsconfig.json, which tsconfig.json extends.
        sync = subprocess.run(
            ['npx', '--no-install', 'svelte-kit', 'sync'],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if sync.returncode != 0:
            msg = f'svelte-kit sync failed ({sync.returncode}): {sync.stderr.strip()[:300]}'
            raise bl.ScanError(msg)
        proc = subprocess.run(
            ['npx', '--no-install', 'svelte-check', '--tsconfig', f'./{tsconfig}']
            + ['--output', 'machine-verbose'],
            cwd=frontend,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        msg = f'svelte-check did not run: {exc}'
        raise bl.ScanError(msg) from exc
    if proc.returncode not in (0, 1):
        msg = f'svelte-check exited {proc.returncode}: {proc.stderr.strip()[:500]}'
        raise bl.ScanError(msg)
    return proc.stdout


def main(ratchet: Ratchet, argv: list[str] | None = None) -> int:
    """Run *ratchet* from the command line; returns the process exit code."""
    parser = argparse.ArgumentParser(description=ratchet.description)
    parser.add_argument('--root', type=Path, default=None, help='repo root (default: auto)')
    parser.add_argument('--baseline', type=Path, default=ratchet.baseline)
    parser.add_argument('--svelte-check-output', type=Path, default=None, help='prior run output')
    parser.add_argument('--timeout', type=int, default=_TIMEOUT_SECONDS)
    parser.add_argument('--tighten', action='store_true', help='lower values / drop stale lines')
    bl.add_baseline_args(parser)
    args = parser.parse_args(argv)
    try:
        if args.svelte_check_output:
            raw = args.svelte_check_output.read_text(encoding='utf-8')
        else:
            here = Path(__file__).resolve().parent
            root: Path = args.root or bl.find_root(here, (f'frontend/{TSCONFIG}',))
            raw = _run(root, TSCONFIG, args.timeout)
        current = parse(raw, ratchet)
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
    problems = bl.compare(current, baseline, unit=ratchet.unit)
    if problems:
        print('\n'.join(problems), file=sys.stderr)
        print(f'{len(problems)} {ratchet.problem_label} ratchet problem(s)', file=sys.stderr)
        return 1
    print(ratchet.ok_message(current))
    return 0
