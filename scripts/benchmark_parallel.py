#!/usr/bin/env python3
"""Progressive parallel transcription benchmark.

Tests reprocessing pipeline throughput with increasing parallelism.
Dispatches batches of 1, 3, 5, 8, 12, 20 files simultaneously, measures:
  - Per-file wall time (dispatch to completion)
  - Batch wall time (all files start to last finish)
  - Pipeline stage durations from Redis (preprocess, GPU, postprocess, gaps)
  - GPU VRAM usage sampled every 2s via nvidia-smi
  - Per-file VRAM profiles from Redis

Requires:
    - ENABLE_BENCHMARK_TIMING=true in .env
    - ENABLE_VRAM_PROFILING=true in .env (optional, for per-task VRAM)
    - All services running with --gpu-scale
    - PostgreSQL accessible via docker exec

Usage:
    source backend/venv/bin/activate
    python scripts/benchmark_parallel.py [--batches 1,3,5] [--output benchmarks/]

    # Recommended: use fixed corpus for repeatable whitepaper-quality results
    python scripts/benchmark_parallel.py --corpus-file docs/benchmark-corpus/corpus.json \\
        --batches 1,4,8,10,12,16 --gpu-id 2

    # Duration curve (sequential, one file at a time, full corpus)
    python scripts/benchmark_parallel.py --corpus-file docs/benchmark-corpus/corpus.json \\
        --sequential --output benchmarks/duration_curve/

    # Single file first to find bottlenecks
    python scripts/benchmark_parallel.py --batches 1

    # Progressive scaling
    python scripts/benchmark_parallel.py --batches 1,3,5,8,12,20

    # Custom GPU device to monitor
    python scripts/benchmark_parallel.py --gpu-id 2
"""

from __future__ import annotations

import sys

from bench_parallel.cli import main

# Force unbuffered stdout for real-time output in background mode
sys.stdout.reconfigure(line_buffering=True)

if __name__ == '__main__':
    main()
