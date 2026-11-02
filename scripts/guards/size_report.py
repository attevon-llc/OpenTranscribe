#!/usr/bin/env python3
"""Report file-size distribution, suggested limits and remaining split work, per group.

Reads exactly the files ``check_file_size.py`` enforces (its ``tracked_files``, ``metrics_for``
and ``measure``; there is no second definition of scope) and prints, for each group, the file
count, median, p90, p95, max, how many files are over the soft and hard limits, and a *debt*
figure: files over the hard cap, lines above the hard cap, lines above the soft target.

Suggestions (soft near p95, hard about twice the soft, rounded to 50) are advice only. Changing a
cap is a reviewed edit to ``check_file_size.py`` and its tests.

``baselines/size_stats.json`` is the committed snapshot. The default run compares against it,
prints the deltas, and exits 1 only when a group's *lines above hard* grew. ``--write-snapshot``
regenerates it (the date is refreshed only when the figures change, so re-running is a no-op).

Deterministic, no network; the hook is report-only apart from that growth check.
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _baseline
import check_file_size as cfs

REPO_ROOT = cfs.REPO_ROOT
SNAPSHOT_PATH = REPO_ROOT / 'scripts' / 'guards' / 'baselines' / 'size_stats.json'
SNAPSHOT_VERSION = 1
SUGGEST_STEP = 50
HARD_OVER_SOFT = 2
FAR_FACTOR = 2.0  # a configured cap more than this far from the suggestion is flagged

GROUPS = (
    'Python (non-test)',
    'TypeScript/JS (non-test)',
    'Shell (non-test)',
    'Tests (py/ts/js/sh)',
    'Svelte <script> logic',
    'Svelte total file',
    'CLAUDE.md',
)
_BY_CAPS = {
    (cfs.TEST_CAP, cfs.TEST_SOFT): 'Tests (py/ts/js/sh)',
    (cfs.SVELTE_SCRIPT_CAP, cfs.SVELTE_SCRIPT_SOFT): 'Svelte <script> logic',
    (cfs.SVELTE_TOTAL_CAP, cfs.SVELTE_TOTAL_SOFT): 'Svelte total file',
    (cfs.CLAUDE_MD_MAX, cfs.CLAUDE_MD_SOFT): 'CLAUDE.md',
}
_CODE_BY_SUFFIX = {
    '.py': 'Python (non-test)',
    '.sh': 'Shell (non-test)',
    '.ts': 'TypeScript/JS (non-test)',
    '.js': 'TypeScript/JS (non-test)',
    '.mjs': 'TypeScript/JS (non-test)',
}
STAT_FIELDS = ('files', 'median', 'p90', 'p95', 'max', 'over_soft', 'over_hard')
DEBT_FIELDS = ('files_over_hard', 'lines_above_hard', 'lines_above_soft')


@dataclass
class Group:
    soft: int
    hard: int
    sizes: list[int] = field(default_factory=list)


def group_of(key: str, cap: int, soft: int) -> str:
    """Group name for one ``metrics_for`` row; limits identify every group but plain code."""
    named = _BY_CAPS.get((cap, soft))
    if named is not None:
        return named
    return _CODE_BY_SUFFIX[key[key.rfind('.') :]]


def collect(repo_root: Path, files: list[str] | None = None) -> dict[str, Group]:
    """Measure every in-scope file and bucket the sizes by group."""
    groups: dict[str, Group] = {}
    for rel in files if files is not None else cfs.tracked_files(repo_root):
        if not cfs.metrics_for(rel, 0, 0):
            continue
        got = cfs.measure(repo_root / rel, want_script=rel.endswith('.svelte'))
        if got is None:
            continue
        for key, value, cap, soft in cfs.metrics_for(rel, got[0], got[1]):
            name = group_of(key, cap, soft)
            groups.setdefault(name, Group(soft=soft, hard=cap)).sizes.append(value)
    return groups


def percentile(sorted_sizes: list[int], pct: int) -> int:
    """Nearest-rank percentile of an ascending list (integer, no interpolation)."""
    if not sorted_sizes:
        return 0
    rank = max(1, -(-pct * len(sorted_sizes) // 100))
    return sorted_sizes[rank - 1]


def stats_for(group: Group) -> dict[str, Any]:
    sizes = sorted(group.sizes)
    return {
        'files': len(sizes),
        'median': percentile(sizes, 50),
        'p90': percentile(sizes, 90),
        'p95': percentile(sizes, 95),
        'max': sizes[-1] if sizes else 0,
        'over_soft': sum(1 for s in sizes if s > group.soft),
        'over_hard': sum(1 for s in sizes if s > group.hard),
        'debt': {
            'files_over_hard': sum(1 for s in sizes if s > group.hard),
            'lines_above_hard': sum(s - group.hard for s in sizes if s > group.hard),
            'lines_above_soft': sum(s - group.soft for s in sizes if s > group.soft),
        },
    }


def build_stats(groups: dict[str, Group]) -> dict[str, dict[str, Any]]:
    return {name: stats_for(groups[name]) for name in GROUPS if name in groups}


def _round_step(value: float) -> int:
    return max(SUGGEST_STEP, int(round(value / SUGGEST_STEP)) * SUGGEST_STEP)


def suggest(p95: int) -> tuple[int, int]:
    """``(soft, hard)``: soft near p95, hard about twice the soft, both rounded to 50."""
    soft = _round_step(p95)
    return soft, _round_step(soft * HARD_OVER_SOFT)


def _far(configured: int, suggested: int) -> bool:
    return not (suggested / FAR_FACTOR <= configured <= suggested * FAR_FACTOR)


def format_table(groups: dict[str, Group], stats: dict[str, dict[str, Any]]) -> str:
    head = ('group', 'files', 'median', 'p90', 'p95', 'max', 'soft', 'hard', '>soft', '>hard')
    rows: list[tuple[str, ...]] = [head]
    for name, st in stats.items():
        g = groups[name]
        rows.append(
            (name, *(str(st[k]) for k in ('files', 'median', 'p90', 'p95', 'max')))
            + (str(g.soft), str(g.hard), str(st['over_soft']), str(st['over_hard']))
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(head))]
    out = [
        '  '.join(c.ljust(widths[i]) if i == 0 else c.rjust(widths[i]) for i, c in enumerate(r))
        for r in rows
    ]
    out.append('')
    out.append('debt (work remaining to get under the hard cap / soft target)')
    for name, st in stats.items():
        d = st['debt']
        out.append(
            f'  {name}: {d["files_over_hard"]} file(s) over hard, '
            f'{d["lines_above_hard"]} lines above hard, {d["lines_above_soft"]} lines above soft'
        )
    out.append('')
    out.append('suggested limits (advice only; soft near p95, hard about 2x soft)')
    for name, st in stats.items():
        g = groups[name]
        s_soft, s_hard = suggest(st['p95'])
        flag = ''
        if _far(g.soft, s_soft) or _far(g.hard, s_hard):
            flag = '  <- configured caps are far from the data'
        out.append(f'  {name}: soft {s_soft}, hard {s_hard} (configured {g.soft}/{g.hard}){flag}')
    return '\n'.join(out)


def snapshot_body(stats: dict[str, dict[str, Any]]) -> dict[str, Any]:
    return {'version': SNAPSHOT_VERSION, 'groups': stats}


def load_snapshot(path: Path) -> dict[str, Any] | None:
    """The committed snapshot, or ``None`` when absent. Raises ``ValueError`` if malformed."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except json.JSONDecodeError as exc:
        msg = f'{path}: not valid JSON: {exc}'
        raise ValueError(msg) from exc
    if not isinstance(data, dict) or not isinstance(data.get('groups'), dict):
        msg = f'{path}: missing "groups" object'
        raise ValueError(msg)
    return data


def write_snapshot(path: Path, stats: dict[str, dict[str, Any]], today: str) -> bool:
    """Write the snapshot; keep the file untouched (date included) if the figures are unchanged."""
    body = snapshot_body(stats)
    old = load_snapshot(path)
    if old is not None and {k: v for k, v in old.items() if k != 'date'} == body:
        return False
    text = json.dumps({'date': today, **body}, indent=2, sort_keys=False) + '\n'
    path.write_text(text, encoding='utf-8')
    return True


def compare(
    old_groups: dict[str, Any], stats: dict[str, dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """``(delta lines, growth errors)``; only a rise in ``lines_above_hard`` is an error."""
    deltas: list[str] = []
    errors: list[str] = []
    for name in GROUPS:
        new = stats.get(name)
        old = old_groups.get(name)
        if new is None and old is None:
            continue
        for fld in DEBT_FIELDS:
            before = (old or {}).get('debt', {}).get(fld, 0)
            after = (new or {}).get('debt', {}).get(fld, 0)
            if before == after:
                continue
            deltas.append(f'{name}: {fld} {before} -> {after} ({after - before:+d})')
            if fld == 'lines_above_hard' and after > before:
                errors.append(f'{name}: lines above hard cap grew {before} -> {after}')
    return deltas, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=REPO_ROOT)
    parser.add_argument('--snapshot', type=Path, default=SNAPSHOT_PATH)
    parser.add_argument('--write-snapshot', action='store_true', help='Regenerate the snapshot.')
    parser.add_argument('--json', action='store_true', help='Print the stats as JSON.')
    args = parser.parse_args(argv)

    try:
        groups = collect(args.root)
        stats = build_stats(groups)
        old = load_snapshot(args.snapshot)
    except (_baseline.ScanError, ValueError) as exc:
        sys.stderr.write(f'error: {exc}\n')
        return 2

    if args.json:
        sys.stdout.write(json.dumps(snapshot_body(stats), indent=2) + '\n')
    else:
        sys.stdout.write(format_table(groups, stats) + '\n')

    if args.write_snapshot:
        changed = write_snapshot(args.snapshot, stats, datetime.date.today().isoformat())
        sys.stderr.write(f'snapshot {"written" if changed else "unchanged"}: {args.snapshot}\n')
        return 0
    if old is None:
        sys.stderr.write(f'error: no snapshot at {args.snapshot}; run with --write-snapshot\n')
        return 2

    deltas, errors = compare(old['groups'], stats)
    for line in deltas:
        sys.stderr.write(f'delta: {line}\n')
    if errors:
        sys.stderr.write('ERROR: split debt grew (lines above hard cap):\n')
        for e in errors:
            sys.stderr.write(f'  {e}\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
