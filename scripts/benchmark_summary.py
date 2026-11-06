#!/usr/bin/env python3
"""Measure the multi-slice summary rules on a real model (plan: summary_quality_eval_plan.md).

Entry point only: the implementation, and the full ``--help`` text, live in the
``scripts/summary_bench/`` package.
"""

from summary_bench.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
