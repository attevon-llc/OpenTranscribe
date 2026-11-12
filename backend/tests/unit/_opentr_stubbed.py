"""Run the real ``opentr.sh`` with ``docker`` and ``nvidia-smi`` stubbed on PATH.

Each stub appends ``"<tool> <args>"`` to a log and exits 0, so a test can assert which calls the
script made without ever reaching a real container or GPU. ``COMPOSE_PROJECT_NAME`` is dropped
from the environment so an operator's shell cannot point the script at another project.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
OPENTR = REPO / "opentr.sh"


def run_opentr_stubbed(
    tmp_path: Path, *args: str, stdin: str | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run ``opentr.sh <args>`` in ``REPO`` and return the process and the stub call lines."""
    stubs = tmp_path / "bin"
    stubs.mkdir()
    log = tmp_path / "docker.log"
    for tool in ("docker", "nvidia-smi"):
        stub = stubs / tool
        stub.write_text(f'#!/bin/sh\necho "{tool} $*" >> "{log}"\nexit 0\n')
        stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{stubs}:{os.environ['PATH']}"}
    env.pop("COMPOSE_PROJECT_NAME", None)
    proc = subprocess.run(
        ["bash", str(OPENTR), *args],
        cwd=REPO,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc, log.read_text().splitlines() if log.exists() else []
