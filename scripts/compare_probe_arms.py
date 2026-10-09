#!/usr/bin/env python3
"""#532 (plan unit U7): compare chat-RAG probe arms and apply the pre-registered rule.

Thin wrapper over ``tests/eval/harness/arm_compare.py``; all logic and the decision rule live
there (``docs/design/532_hybrid_summary_synthesis_plan.md`` section 5.2). Reads each arm's
full-fidelity ``results.json`` (which carries prose) and writes a metrics-only
``arm_compare.json`` / ``arm_compare.md`` that is safe to commit under
``backend/tests/eval/baselines/probe-532h-compare/``.

Usage::

    python3 scripts/compare_probe_arms.py \\
        --arm control=.rag-403/probe-runs/532h-C1-mfx-<sha> \\
        --arm arm-d=.rag-403/probe-runs/532h-D-mfx-<sha> \\
        --arm hybrid=.rag-403/probe-runs/532h-H-mfx-<sha> \\
        --arm repeat-control=.rag-403/probe-runs/532h-C2-mfx-<sha> \\
        --out backend/tests/eval/baselines/probe-532h-compare

Roles: control and hybrid are required; arm-d and repeat-control are optional. Pass
``--no-composition-check`` for arms that predate the overview counters.

Exit codes: 0 WIN, 1 FAIL, 2 bad input, 3 VOID (an applied-check failed or the A/A pair
differs; the window is not graded).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

EXIT_BY_VERDICT = {'WIN': 0, 'FAIL': 1, 'VOID': 3}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse the command line."""
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument(
        '--arm',
        action='append',
        required=True,
        metavar='ROLE=PATH',
        help='role (control|repeat-control|arm-d|hybrid) and its results.json or directory',
    )
    parser.add_argument('--out', required=True, help='directory for arm_compare.{json,md}')
    parser.add_argument('--category', default='multi_file', help='graded category')
    parser.add_argument(
        '--no-composition-check',
        action='store_true',
        help='skip the overview-counter applied-checks (arms that predate U4)',
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the comparison; returns the process exit code."""
    from tests.eval.harness import arm_compare, arm_compare_report
    from tests.eval.harness.report import dumps

    args = parse_args(argv)
    arms: dict[str, list] = {}
    try:
        for spec in args.arm:
            role, sep, path = spec.partition('=')
            if not sep or role in arms:
                print(f'bad or repeated --arm {spec!r}; want ROLE=PATH', file=sys.stderr)
                return 2
            arms[role] = arm_compare.load_records(Path(path))
        report = arm_compare.build_report(
            arms, category=args.category, check_composition=not args.no_composition_check
        )
    except (arm_compare.ArmDataError, OSError, ValueError) as exc:
        print(f'cannot compare: {exc}', file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'arm_compare.json').write_text(dumps(report), encoding='utf-8')
    (out / 'arm_compare.md').write_text(
        arm_compare_report.render_markdown(report), encoding='utf-8'
    )
    verdict = report['decision']['verdict']
    print(f'{verdict}: wrote {out}/arm_compare.json and arm_compare.md')
    return EXIT_BY_VERDICT[verdict]


if __name__ == '__main__':
    sys.exit(main())
