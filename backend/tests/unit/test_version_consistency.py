"""Version consistency, enforced by the existing CI backend-tests job.

``scripts/release/check-version-consistency.py`` is the single implementation.
This test only shells it in ``ci`` mode so the check runs on every PR without
anyone having to remember it — which is precisely what went wrong before:
``expected-schemas.tsv`` had a documented "append a row every release" rule and
no automated enforcement, so v0.4.1's row was simply never added.

The script is deliberately stdlib-only so it also runs in environments that have
no venv (bare CI runners, the release orchestrator's preflight).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECKER = REPO_ROOT / "scripts" / "release" / "check-version-consistency.py"

pytestmark = pytest.mark.skipif(
    not CHECKER.exists(), reason="release tooling not present in this checkout"
)


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )


def test_version_sources_are_consistent():
    """VERSION, pyproject, package.json, package-lock, alembic head, blog slugs."""
    proc = _run("--mode", "ci")
    assert proc.returncode == 0, f"version consistency check failed:\n{proc.stdout}\n{proc.stderr}"


def test_json_output_is_machine_readable():
    """Agents branch on this contract; a schema change must break a test, not a bot."""
    proc = _run("--mode", "ci", "--json")
    payload = json.loads(proc.stdout)

    assert payload["stage"] == "version-consistency"
    assert payload["status"] in {"pass", "fail"}
    assert payload["version"].startswith("v")
    assert isinstance(payload["criteria"], list) and payload["criteria"]
    assert isinstance(payload["next"], list)

    for criterion in payload["criteria"]:
        assert {"id", "status", "summary", "detail", "fix"} <= criterion.keys()
        assert criterion["status"] in {"pass", "fail", "warn", "skip"}


def test_exit_code_two_on_misuse():
    """Distinct exit codes let an agent tell "gate failed" from "I called it wrong"."""
    assert _run("--mode", "not-a-mode").returncode == 2
