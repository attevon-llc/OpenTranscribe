"""`./opentr.sh rebuild-frontend|rebuild-backend` refuse options they do not implement.

`rebuild-frontend --fresh <name> --dry-run` once ignored both flags and rebuilt and started
the DEFAULT project's frontend and docs for real; `rebuild-backend` printed "ignoring unknown
arg" and carried on the same way. A rebuild aimed at a fresh stack, or asked for a dry run,
must stop before any docker call. `docker` and `nvidia-smi` are stubbed on PATH, so this test
can never touch a real container.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OPENTR = REPO / "opentr.sh"

pytestmark = pytest.mark.skipif(not OPENTR.exists(), reason="opentr.sh not present")


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "docker.log"
    for tool in ("docker", "nvidia-smi"):
        stub = stubs / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
    proc = subprocess.run(
        ["bash", str(OPENTR), *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = log.read_text() if log.exists() else ""
    return proc, calls


@pytest.mark.unit
@pytest.mark.parametrize("command", ["rebuild-frontend", "rebuild-backend"])
def test_a_fresh_or_dry_run_rebuild_is_refused_before_any_compose_call(
    tmp_path: Path, command: str
) -> None:
    proc, calls = _run(tmp_path, command, "--fresh", "ldape2e", "--dry-run")

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "docker compose" not in calls
    assert "--fresh" in proc.stderr


@pytest.mark.unit
def test_rebuild_frontend_without_options_still_reaches_compose(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "rebuild-frontend")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "compose up -d --build --no-deps frontend docs" in calls
