"""Importing the Celery app must not load PyTorch (issue #1071)."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _run(code: str) -> str:
    env = {k: v for k, v in os.environ.items() if k != "SKIP_CELERY"}
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=BACKEND, env=env, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return out.stdout.strip().splitlines()[-1]


def test_importing_celery_app_does_not_load_torch():
    assert _run("import app.core.celery, sys; print('torch' in sys.modules)") == "False"


def test_torch_load_is_patched_on_first_torch_import():
    code = (
        "import app.core.celery, sys, torch; "
        "print(getattr(torch.load, '__name__', '') == '_patched_torch_load')"
    )
    assert _run(code) == "True"
