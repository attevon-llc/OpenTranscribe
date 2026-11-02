"""Paths, test-file labels and environment-derived settings for the direct PyAnnote benchmark."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger('benchmark-pyannote')

SCRIPT_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SCRIPT_DIR.parent
AUDIO_DIR = PROJECT_ROOT / 'benchmark' / 'test_audio'
RESULTS_DIR = PROJECT_ROOT / 'benchmark' / 'results'

TEST_FILES = [
    '0.5h_1899s',
    '1.0h_3758s',
    '2.2h_7998s',
    '3.2h_11495s',
    '4.7h_17044s',
]

DEFAULT_MODEL = os.environ.get('PYANNOTE_MODEL', 'pyannote/speaker-diarization-community-1')

HF_TOKEN = os.environ.get('HUGGINGFACE_TOKEN', '')
GPU_INDEX = int(os.environ.get('CUDA_DEVICE_INDEX', '0'))

# Default location of the canonical davidamacey/pyannote-audio fork (gpu-optimizations
# branch). Can be overridden via PYANNOTE_OPTIMIZED_SRC. When running inside
# docker-compose.benchmark.yml, the fork is bind-mounted over site-packages and
# this path is unused (the bind-mount wins regardless of sys.path).
DEFAULT_OPTIMIZED_SRC = Path('/mnt/nvm/repos/pyannote-audio-fork/src')
