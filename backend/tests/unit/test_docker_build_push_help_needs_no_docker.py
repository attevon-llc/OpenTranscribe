"""``docker-build-push.sh help`` must print usage without touching Docker.

The help branch used to sit at the end of ``main``, after ``check_docker``,
``check_docker_login`` and the buildx builder setup. Asking for help therefore failed on a
host without Docker and, on one with it, logged in checks and created or switched builders.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO_ROOT / "scripts" / "docker-build-push.sh"


@pytest.mark.parametrize("flag", ["help", "--help", "-h"])
def test_help_prints_usage_and_never_invokes_docker(flag: str, tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "docker-was-called"
    fake_docker = bin_dir / "docker"
    fake_docker.write_text(f'#!/bin/sh\necho "$@" >> "{called}"\nexit 1\n', encoding="utf-8")
    fake_docker.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}

    result = subprocess.run(  # fixed argv, no shell
        ["bash", str(_SCRIPT), flag],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        timeout=60,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Usage:" in result.stdout + result.stderr
    assert not called.exists(), f"help invoked docker: {called.read_text(encoding='utf-8')}"
