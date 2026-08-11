#!/usr/bin/env python3
"""Analyse a pytest JUnit XML and surface what is actually costing wall clock.

The backend suite runs under ``-n auto --dist loadgroup``, which makes raw pass/fail
counts useless for performance work: a test's reported duration includes fixture
setup, so a four-line test can report 40 s because it sat in a lock queue. Two
signals separate real work from waiting:

``xdist_group`` totals
    ``--dist loadgroup`` sends an entire group to ONE worker, so a group's summed
    duration is a hard lower bound on wall clock no matter how many cores exist.

Duration clusters
    Unrelated tests cannot share a duration by coincidence. When tests from three or
    more distinct files finish inside a sub-second band, they were released from one
    queue together — that is a barrier, not work.

Usage::

    scripts/analyze-test-timing.py run.xml
    scripts/analyze-test-timing.py run.xml --baseline baseline.xml
    scripts/analyze-test-timing.py run.xml --json

``scripts/run-backend-tests.sh`` writes the XML this reads (``$OT_TEST_OUT_DIR/last.xml``,
default ``/tmp/ot-backend-tests``). Never re-run the suite just to get numbers — run it
once and read the artifact repeatedly.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: pytest-xdist appends "@<group>" to the test name when --dist loadgroup is active.
_GROUP_SUFFIX = re.compile(r'@([A-Za-z0-9_]+)$')

#: Cluster detection. Tests released together from one lock queue land within a few
#: hundred milliseconds of each other, so chain on a tight gap AND cap the total band
#: width — without the cap, dense-but-continuous durations chain into one runaway
#: "cluster" spanning 13 s, which is real work, not a barrier.
_CLUSTER_GAP_S = 0.35
_CLUSTER_MAX_WIDTH_S = 3.0
_CLUSTER_MIN_FILES = 3
_CLUSTER_MIN_TESTS = 5
_CLUSTER_FLOOR_S = 5.0


@dataclass
class Case:
    """One ``<testcase>`` row."""

    classname: str
    name: str
    duration: float
    group: str | None
    status: str  # "ran" | "skipped" | "failed" | "error"

    @property
    def nodeid(self) -> str:
        return f'{self.classname}::{self.name}'

    @property
    def module(self) -> str:
        """Test module, with any trailing ``.TestClass`` stripped."""
        parts = self.classname.split('.')
        while parts and parts[-1][:1].isupper():
            parts.pop()
        return '.'.join(parts) or self.classname


@dataclass
class Report:
    """Everything derived from one XML file."""

    path: Path
    wall: float
    total: int
    skipped: int
    failed: int
    cases: list[Case] = field(default_factory=list)

    @property
    def cpu(self) -> float:
        """Summed per-test duration — the work, ignoring how it was scheduled."""
        return sum(c.duration for c in self.cases)

    @property
    def parallelism(self) -> float:
        """Effective parallelism. Compare against worker count, not core count."""
        return self.cpu / self.wall if self.wall else 0.0

    def groups(self) -> list[tuple[str, int, float]]:
        """(group, n, summed duration), worst first. Each group is one worker."""
        total: Counter[str] = Counter()
        count: Counter[str] = Counter()
        for c in self.cases:
            if c.group:
                total[c.group] += c.duration
                count[c.group] += 1
        return [(g, count[g], t) for g, t in total.most_common()]

    def percentiles(self) -> dict[str, float]:
        if not self.cases:
            return {}
        ds = sorted(c.duration for c in self.cases)
        pick = lambda p: ds[min(int(len(ds) * p), len(ds) - 1)]  # noqa: E731
        return {
            'p50': pick(0.50),
            'p90': pick(0.90),
            'p95': pick(0.95),
            'p99': pick(0.99),
            'max': ds[-1],
        }

    def clusters(self) -> list[dict]:
        """Duration bands that look like a released lock queue rather than work.

        A cluster must be tightly packed (small inter-test gap), narrow overall, drawn
        from several distinct files, and above a floor. Unrelated tests sharing a
        duration to within a fraction of a second did not do the same amount of work —
        they waited on the same thing.
        """
        slow = sorted(
            (c for c in self.cases if c.duration > _CLUSTER_FLOOR_S), key=lambda c: c.duration
        )
        runs: list[list[Case]] = []
        run: list[Case] = []
        for case in slow:
            too_far = run and case.duration - run[-1].duration >= _CLUSTER_GAP_S
            too_wide = run and case.duration - run[0].duration > _CLUSTER_MAX_WIDTH_S
            if too_far or too_wide:
                runs.append(run)
                run = []
            run.append(case)
        if run:
            runs.append(run)
        out = [_describe(r) for r in runs]
        return [
            c
            for c in out
            if c['files'] >= _CLUSTER_MIN_FILES
            and c['n'] >= _CLUSTER_MIN_TESTS
            and (c['high'] - c['low']) <= _CLUSTER_MAX_WIDTH_S
        ]


def _describe(run: list[Case]) -> dict:
    return {
        'n': len(run),
        'low': run[0].duration,
        'high': run[-1].duration,
        'seconds': sum(c.duration for c in run),
        'files': len({c.module for c in run}),
        'examples': [c.nodeid for c in run[:3]],
    }


def load(path: Path) -> Report:
    """Parse a JUnit XML into a Report."""
    root = ET.parse(path).getroot()
    suite = root if root.tag == 'testsuite' else root[0]
    report = Report(
        path=path,
        wall=float(suite.get('time') or 0.0),
        total=int(suite.get('tests') or 0),
        skipped=int(suite.get('skipped') or 0),
        failed=int(suite.get('failures') or 0) + int(suite.get('errors') or 0),
    )
    for tc in suite.iter('testcase'):
        name = tc.get('name') or ''
        match = _GROUP_SUFFIX.search(name)
        if tc.find('skipped') is not None:
            status = 'skipped'
        elif tc.find('failure') is not None:
            status = 'failed'
        elif tc.find('error') is not None:
            status = 'error'
        else:
            status = 'ran'
        report.cases.append(
            Case(
                classname=tc.get('classname') or '',
                name=name,
                duration=float(tc.get('time') or 0.0),
                group=match.group(1) if match else None,
                status=status,
            )
        )
    return report


def _fmt_delta(new: float, old: float, unit: str = 's', lower_is_better: bool = True) -> str:
    delta = new - old
    if abs(delta) < 1e-9:
        return 'unchanged'
    arrow = '▼' if (delta < 0) == lower_is_better else '▲'
    pct = f' ({delta / old * 100:+.0f}%)' if old else ''
    return f'{arrow} {delta:+.1f}{unit}{pct}'


def render(report: Report, baseline: Report | None, top: int) -> None:
    """Print the human report."""
    print(f'\n\033[1m{report.path}\033[0m')
    print(f'  tests {report.total}  skipped {report.skipped}  failed {report.failed}')
    print(f'  wall clock        {report.wall:9.1f} s', end='')
    print(f'   {_fmt_delta(report.wall, baseline.wall)}' if baseline else '')
    print(f'  Σ test durations  {report.cpu:9.1f} s', end='')
    print(f'   {_fmt_delta(report.cpu, baseline.cpu)}' if baseline else '')
    print(f'  eff. parallelism  {report.parallelism:9.2f} x', end='')
    print(
        f'   {_fmt_delta(report.parallelism, baseline.parallelism, "x", lower_is_better=False)}'
        if baseline
        else ''
    )

    pct = report.percentiles()
    if pct:
        print('  ' + '  '.join(f'{k} {v:.2f}s' for k, v in pct.items()))

    groups = report.groups()
    if groups:
        print('\n  \033[1mxdist_group totals\033[0m (loadgroup pins each group to ONE worker)')
        prior = {g: t for g, _, t in baseline.groups()} if baseline else {}
        for name, n, total in groups[:top]:
            line = f'    {name:34s} n={n:4d}  {total:8.1f} s'
            if name in prior:
                line += f'   {_fmt_delta(total, prior[name])}'
            print(line)

    clusters = report.clusters()
    header = '\n  \033[1mduration clusters\033[0m'
    if clusters:
        parked = sum(c['n'] for c in clusters)
        parked_s = sum(c['seconds'] for c in clusters)
        share = parked_s / report.cpu * 100 if report.cpu else 0.0
        print(f'{header} — \033[31m{len(clusters)} found: barrier suspects\033[0m')
        print(
            f'    \033[31m{parked} tests parked in barriers, {parked_s:.0f} s ({share:.0f}% of Σ test time)\033[0m'
        )
        for c in clusters:
            print(
                f'    n={c["n"]:4d}  {c["low"]:6.2f}–{c["high"]:6.2f} s  across {c["files"]} files'
            )
            for ex in c['examples']:
                print(f'          {ex}')
    else:
        print(f'{header} — \033[32mnone\033[0m (no unrelated tests share a duration band)')

    print(f'\n  \033[1mslowest {top}\033[0m')
    for c in sorted(report.cases, key=lambda c: -c.duration)[:top]:
        print(f'    {c.duration:7.2f} s  {c.nodeid}')

    if baseline:
        print('\n  \033[1mregression checks\033[0m')
        ran_new = sum(1 for c in report.cases if c.status == 'ran')
        ran_old = sum(1 for c in baseline.cases if c.status == 'ran')
        _check('tests that ran', ran_new >= ran_old, f'{ran_old} → {ran_new}')
        _check(
            'skips did not grow',
            report.skipped <= baseline.skipped,
            f'{baseline.skipped} → {report.skipped}',
        )
        _check('no barrier clusters', not clusters, f'{len(baseline.clusters())} → {len(clusters)}')
    print()


def _check(label: str, ok: bool, detail: str) -> None:
    mark = '\033[32mPASS\033[0m' if ok else '\033[31mFAIL\033[0m'
    print(f'    [{mark}] {label:24s} {detail}')


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument('xml', type=Path, help='JUnit XML from the run under test')
    ap.add_argument('--baseline', type=Path, help='JUnit XML to compare against')
    ap.add_argument('--top', type=int, default=20, help='rows in the slowest/group tables')
    ap.add_argument('--json', action='store_true', help='emit machine-readable JSON instead')
    args = ap.parse_args()

    if not args.xml.exists():
        print(f'error: {args.xml} does not exist', file=sys.stderr)
        return 2

    report = load(args.xml)
    baseline = load(args.baseline) if args.baseline and args.baseline.exists() else None

    if args.json:
        print(
            json.dumps(
                {
                    'wall': report.wall,
                    'cpu': report.cpu,
                    'parallelism': report.parallelism,
                    'tests': report.total,
                    'skipped': report.skipped,
                    'failed': report.failed,
                    'groups': [{'name': g, 'n': n, 'total': t} for g, n, t in report.groups()],
                    'clusters': report.clusters(),
                    'percentiles': report.percentiles(),
                },
                indent=2,
            )
        )
        return 0

    render(report, baseline, args.top)
    return 0


if __name__ == '__main__':
    sys.exit(main())
