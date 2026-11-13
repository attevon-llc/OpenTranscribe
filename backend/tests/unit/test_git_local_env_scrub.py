"""Hook-exported git variables must not reach tests or scratch-repo scripts (githooks(5))."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
REPO = BACKEND.parent


def _local_env_vars() -> list[str]:
    out = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
    ).stdout
    return out.split()


def test_no_git_local_env_var_reaches_a_test():
    leaked = [v for v in _local_env_vars() if v in os.environ]
    assert leaked == []


def test_scrub_runs_even_when_parent_exports_git_dir():
    env = {**os.environ, "GIT_DIR": "/nonexistent", "GIT_INDEX_FILE": "/nonexistent/index"}
    target = f"{Path(__file__)}::test_no_git_local_env_var_reaches_a_test"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n0", "-p", "no:cacheprovider", target],
        env=env,
        cwd=BACKEND,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def test_negative_control_script_unsets_local_env_vars():
    r = subprocess.run(
        [
            "bash",
            "-c",
            'source <(sed -n "/^while read -r _git_local_var/p" "$0"); env | grep -c "^GIT_DIR=" || true',
            str(REPO / "scripts" / "mypy-hook-negative-control.sh"),
        ],
        env={**os.environ, "GIT_DIR": "/nonexistent"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.stdout.strip() == "0", r.stdout + r.stderr
