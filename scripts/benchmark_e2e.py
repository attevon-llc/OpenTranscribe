#!/usr/bin/env python3
"""End-to-end transcription pipeline benchmark.

Triggers a reprocess via the API, polls Redis for timing data,
calculates stage durations + inter-stage gaps, runs N iterations,
and outputs a CSV + summary table + VRAM map.

Requires:
    - ENABLE_BENCHMARK_TIMING=true in .env
    - ENABLE_VRAM_PROFILING=true in .env (optional, for VRAM data)
    - Backend running and accessible at BACKEND_URL
    - Redis accessible at REDIS_URL

Usage:
    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \\
        python scripts/benchmark_e2e.py --file-uuid <UUID> [--iterations 3] [--output benchmark_results.csv]

    # Detailed report with VRAM breakdown and realtime factor
    python scripts/benchmark_e2e.py --file-uuid <UUID> --detailed

    # Longer timeout for 3hr+ files (default 10min, set to 60min)
    python scripts/benchmark_e2e.py --file-uuid <UUID> --timeout 3600

Credentials are read from environment variables to avoid exposure in shell
history and process listings:
    BENCHMARK_EMAIL     Login email    (default: admin@example.com)
    BENCHMARK_PASSWORD  Login password (default: password)
"""

import sys

from e2e_bench.cli import main

# Force unbuffered stdout for real-time output in background mode
sys.stdout.reconfigure(line_buffering=True)

if __name__ == '__main__':
    main()
