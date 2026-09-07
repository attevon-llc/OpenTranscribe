"""A test run must not inherit the operator's env file (issue #431, #403 handoff).

``Settings.model_config["env_file"]`` was the literal ``".env"``, which pydantic-settings resolves
against the **working directory**. So the suite's behaviour depended on where you launched
it from: ``pytest`` in ``backend/`` found no env file and used exactly what the fixtures
export, while the same command from the repo root loaded the real deployment ``.env`` on
top of them.

That is not a cosmetic difference. It produced two false failures, one of them a security
test that stopped exercising the SSRF guard and started asserting that nothing happened to
be listening on a local port — a control passing for the wrong reason, which is the exact
failure class this whole branch exists to remove.

These tests never read, open or print the contents of any env file. They assert only on the
resolved *configuration*, and on the process environment the fixtures own.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2]
_REPO_ROOT = _BACKEND.parent


def test_settings_loads_no_env_file_under_testing():
    """The guard itself: with ``TESTING`` set, there is no env file in the config at all.

    Asserted as ``is None`` rather than "not the repo root's path", because any non-None
    value is CWD-relative again and reintroduces the bug for whichever directory happens
    to hold a matching file.
    """
    from app.core.config import Settings

    assert os.getenv("TESTING", "").lower() in ("1", "true"), (
        "conftest is expected to set TESTING before app import — without it this test "
        "would assert the production branch and pass vacuously"
    )
    assert Settings.model_config["env_file"] is None


def test_the_production_branch_still_loads_dot_env():
    """The other direction, so the fix cannot degrade into "never load an env file".

    Deployments genuinely rely on ``.env``; a guard that disabled it everywhere would be a
    far worse bug than the one being fixed, and would show up only at runtime.
    """
    env = {k: v for k, v in os.environ.items() if k != "TESTING"}
    probe = (
        "import os, sys;"
        f"sys.path.insert(0, {str(_BACKEND)!r});"
        "assert 'TESTING' not in os.environ;"
        "from app.core.config import Settings;"
        "print(Settings.model_config['env_file'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_BACKEND),
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == ".env"


def test_the_same_settings_resolve_from_the_repo_root_and_from_backend():
    """The behaviour the bug actually broke: two invocations, one answer.

    Rather than naming any specific setting the operator's file might override, this
    compares a fingerprint of the *whole* resolved settings object across the two working
    directories. Anything the env file would have changed shows up as a difference, and the
    test needs no update when a new setting is added.
    """
    probe = (
        "import sys, json, hashlib;"
        f"sys.path.insert(0, {str(_BACKEND)!r});"
        "from app.core.config import settings;"
        "d = {k: str(v) for k, v in sorted(settings.model_dump().items())};"
        "print(hashlib.sha256(json.dumps(d, sort_keys=True).encode()).hexdigest())"
    )

    env = {**os.environ, "TESTING": "true", "SKIP_CELERY": "true"}
    fingerprints = {}
    for label, cwd in (("backend", _BACKEND), ("repo_root", _REPO_ROOT)):
        result = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(cwd),
            timeout=180,
        )
        assert result.returncode == 0, f"{label}: {result.stderr[-2000:]}"
        fingerprints[label] = result.stdout.strip()

    assert fingerprints["backend"], "the probe produced no fingerprint — it did not run"
    assert fingerprints["backend"] == fingerprints["repo_root"], (
        "settings differ depending on the directory pytest was launched from — the env "
        "file is being resolved against the working directory again"
    )
