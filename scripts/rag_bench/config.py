"""Repo paths, the ``backend`` import path and defaults shared by the retrieval benchmark."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / 'backend'
# `backend` first: it is what makes both `app.*` and `tests.eval.harness.*`
# importable from a script that lives outside it.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logger = logging.getLogger('benchmark_rag')

DEFAULT_MANIFEST_ROOT = REPO_ROOT / '.rag-403' / 'injections'
DEFAULT_BASELINE_ROOT = BACKEND / 'tests' / 'eval' / 'baselines'
DEFAULT_DATA_DIR = Path('/mnt/nas/opentranscribe-benchmarks')
