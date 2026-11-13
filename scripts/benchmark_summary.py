#!/usr/bin/env python3
"""Measure the multi-slice summary rules on a real model (see backend/tests/eval/CLAUDE.md).

Entry point only: the implementation, and the full ``--help`` text, live in the
``scripts/summary_bench/`` package.
"""

from summary_bench.cli import main, quiet_app_loggers

if __name__ == '__main__':
    quiet_app_loggers()
    raise SystemExit(main())
