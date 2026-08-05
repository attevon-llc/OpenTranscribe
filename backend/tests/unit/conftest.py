"""Helpers for unit tests that must observe module-import-time configuration.

Several settings bind their value when the class body executes —
``ENVIRONMENT: str = os.getenv("ENVIRONMENT", "production")`` is read once at import.
The root ``conftest.py`` sets ``ENVIRONMENT=testing`` *before* ``app.*`` is imported, so
those defaults can never be observed in-process, and monkeypatching afterwards cannot
reach them.

Reloading ``app.core.config`` is NOT an acceptable substitute: it replaces the
``settings`` **object**, while every module that did ``from app.core.config import
settings`` keeps a reference to the old one. That silently desyncs the app and breaks
unrelated tests (it did — see the isolation fix in the Phase 1 commit).

So these tests run a clean child process instead.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

#: backend/ — the import root, and the cwd the CI job uses.
BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def run_in_clean_process():
    """Run a Python snippet in a child process with a controlled environment.

    Returns a callable ``(code, *, unset=(), **env) -> str`` giving the snippet's last
    stdout line. Raises with the child's stderr attached on failure — bare
    ``check=True`` hides it, which is exactly what made the first CI failure of this
    helper opaque.
    """

    def _run(code: str, *, unset: tuple[str, ...] = (), **env: str) -> str:
        child_env = {k: v for k, v in os.environ.items() if k not in unset}
        # PYTHONPATH is set explicitly rather than relying on `python -c` putting cwd on
        # sys.path — the CI job's cwd is not guaranteed to match the local one.
        child_env["PYTHONPATH"] = str(BACKEND_ROOT)
        child_env.setdefault("SKIP_CELERY", "true")
        child_env.update(env)

        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=child_env,
            cwd=str(BACKEND_ROOT),
            check=False,
        )
        if result.returncode != 0:
            raise AssertionError(
                f"child process failed (exit {result.returncode})\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
            )
        lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
        assert lines, f"child produced no output\n--- stderr ---\n{result.stderr}"
        return lines[-1]

    return _run
