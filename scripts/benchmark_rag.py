#!/usr/bin/env python3
"""Measure retrieval quality over an injected eval corpus (#403 Stage 1).

Entry point only: the implementation, and the full ``--help`` text, live in the
``scripts/rag_bench/`` package.
"""

from rag_bench.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
