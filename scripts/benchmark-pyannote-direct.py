#!/usr/bin/env python3
"""Direct PyAnnote diarization benchmark — stock vs optimized.

Runs PyAnnote speaker diarization directly on WAV files, bypassing the
OpenTranscribe API. Captures per-stage timing, speaker counts, segment counts,
per-stage peak VRAM, RTTM output for DER scoring, and optional torch.profiler
traces.

Usage:
    # Single-run baseline (stock)
    python scripts/benchmark-pyannote-direct.py --variant stock --files 0.5h_1899s

    # 5-run statistical benchmark with RTTM export (for DER scoring)
    python scripts/benchmark-pyannote-direct.py --variant optimized \\
        --files 0.5h_1899s 2.2h_7998s --runs 5 \\
        --tag baseline_a6000_20260421 \\
        --rttm-out benchmark/results/rttm/baseline_a6000_20260421

    # Profiled run (chrome trace export)
    python scripts/benchmark-pyannote-direct.py --variant optimized \\
        --files 2.2h_7998s --profiler --tag phase1_profile_cuda

    # MPS (Apple Silicon)
    python scripts/benchmark-pyannote-direct.py --variant optimized \\
        --device mps --files 0.5h_1899s --runs 5 --tag baseline_m2max

    # Compare saved results (single-run tags only; use compare-runs for statistical)
    python scripts/benchmark-pyannote-direct.py --compare stock optimized

Environment:
    HUGGINGFACE_TOKEN    HF token for gated model downloads
    CUDA_DEVICE_INDEX    Which GPU to use (default: 0)
    PYANNOTE_OPTIMIZED_SRC  Override path to optimized pyannote-audio src
                             (default: /mnt/nvm/repos/pyannote-audio-fork/src,
                             the canonical davidamacey/pyannote-audio fork on
                             the gpu-optimizations branch).
    PYANNOTE_MODEL       Override diarization model (default: v4 community-1)
"""

from pyannote_direct.cli import main

if __name__ == '__main__':
    main()
