"""``scripts/check-dependency-parity.sh`` must compare against the RIGHT backend container.

A bare ``docker ps --filter name=backend | head -1`` took whichever stack docker listed
first, so with a ``--fresh`` stack up the parity check could silently compare the venv with
the wrong image. The script is run here against a ``docker`` stand-in on PATH.
"""

from __future__ import annotations

import os
import stat
import subprocess  # nosec B404 - runs the repo's own script against a stub
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check-dependency-parity.sh"

_STUB = """#!/usr/bin/env bash
echo "$@" >> "$OT_DOCKER_LOG"
if [[ "$1" == "ps" ]]; then
    printf '%b' "$OT_PS_OUTPUT"
fi
"""


def _run(
    tmp_path: Path, ps_output: str, project: str | None
) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(_STUB, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "docker.log"
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "OT_DOCKER_LOG": str(log),
        "OT_PS_OUTPUT": ps_output,
    }
    if project is not None:
        env["COMPOSE_PROJECT_NAME"] = project
    proc = subprocess.run(  # nosec B603 - fixed argv
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, timeout=60, check=False
    )
    return proc, log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.unit
def test_a_set_project_scopes_the_docker_query_to_that_project(tmp_path):
    _, log = _run(tmp_path, "", "otfresh-demo")

    assert "label=com.docker.compose.project=otfresh-demo" in log
    assert "label=com.docker.compose.service=backend" in log


@pytest.mark.unit
def test_two_dev_backends_with_no_project_set_is_an_error_not_a_guess(tmp_path):
    proc, _ = _run(tmp_path, "one-backend one\ntwo-backend two\n", None)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "COMPOSE_PROJECT_NAME" in proc.stderr


@pytest.mark.unit
def test_a_fresh_stack_beside_the_dev_stack_is_not_ambiguous(tmp_path):
    """The default dev stack wins over an ``otfresh-*`` fresh stack when no project is set."""
    proc, log = _run(tmp_path, "otfresh-a-backend otfresh-a\nopentranscribe-backend dev\n", None)

    # The stub docker returns no package list, so the later comparison fails by design on a host
    # with a venv; what this test pins is WHICH container was picked and that it was not an
    # ambiguity error.
    assert proc.returncode != 2, proc.stdout + proc.stderr
    assert "COMPOSE_PROJECT_NAME" not in proc.stderr
    assert "exec opentranscribe-backend" in log
    assert "otfresh-a-backend" not in log.split("exec", 1)[-1]


@pytest.mark.unit
def test_no_running_backend_is_a_skip(tmp_path):
    proc, _ = _run(tmp_path, "", None)

    assert proc.returncode == 0
    assert "no running backend container" in proc.stdout
