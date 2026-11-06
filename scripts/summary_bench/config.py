"""Repo paths, the ``backend`` import path, defaults and exit codes for the summary harness."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / 'backend'
# `backend` first: it is what makes both `app.*` and `tests.eval.harness.*` importable from a
# script that lives outside it.
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logger = logging.getLogger('benchmark_summary')

HARNESS_VERSION = '1'
DEFAULT_DATA_DIR = Path('/mnt/nas/opentranscribe-benchmarks')
#: Run artifacts hold corpus text, so they live outside the repo and not under /tmp (not durable).
DEFAULT_WORK_DIR = Path.home() / 'ot-cleanup-work' / 'summary-eval'
DEFAULT_BASELINE_ROOT = BACKEND / 'tests' / 'eval' / 'baselines' / 'summary-quality'
SABOTAGE_ARM_FIXTURE = BACKEND / 'tests' / 'eval' / 'fixtures' / 'summary_sabotage_arm.json'
#: Run ``k`` uses ``SEED_BASE + k`` for every arm, so run k of two arms forms a pair. The seed
#: pairs runs; it does not make a batched vLLM server reproduce (vLLM "Reproducibility" page).
SEED_BASE = 20261105
#: The model the owner chose (decision 1), as `--with-llm-test` serves it.
DEFAULT_MODEL = 'gemma-4-e4b'
CONTEXT_CEILING = 60000  # --max-model-len of that server


class ExitCode:
    """Process exit codes: the repo's `run_phase` convention."""

    OK = 0
    FAILED = 1
    MISUSE = 2
    PRECONDITION = 3
    NOT_MEASURED = 4


class HarnessError(Exception):
    """A refusal that maps to an exit code; the message is safe to print (no corpus text)."""

    def __init__(self, message: str, code: int = ExitCode.MISUSE) -> None:
        super().__init__(message)
        self.code = code


def ensure_app_environment() -> None:
    """Point the app's writable directories at a scratch dir before `app.core.config` loads.

    Reuses the injection tooling's bootstrap. The root passed is the work directory, which holds
    no ``.env``: the harness needs no database, so no secret file is read and no connection
    setting is imported from one. Idempotent (explicit environment variables always win).
    """
    import os

    from app.scripts.corpus_injection.env import bootstrap

    bootstrap(DEFAULT_WORK_DIR)
    # `settings` reads this once, at import: the SSRF guard refuses a loopback vLLM otherwise
    # (llm_service.py). This process is a local evaluation tool, not a request path.
    os.environ.setdefault('LLM_ALLOW_PRIVATE_ENDPOINTS', 'true')
