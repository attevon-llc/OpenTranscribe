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


@pytest.fixture(scope="session")
def revisions_at_or_after():
    """Return ``base -> {base and every revision that descends from it}``.

    The migration-consistency tests assert that ``_detect_schema_version`` does not
    fall BELOW a given revision on the current schema. Only the newest revision can
    assert an exact value — every older arm stops being the answer the moment a new
    revision lands — so they compare against this set instead of a hard-coded list
    that has to be edited on every schema change.
    """
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    def _at_or_after(base: str) -> set[str]:
        return {rev.revision for rev in scripts.iterate_revisions("heads", base)}

    return _at_or_after
