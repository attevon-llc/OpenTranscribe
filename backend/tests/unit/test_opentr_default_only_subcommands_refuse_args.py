"""Subcommands that act on the DEFAULT stack only must refuse arguments, fail closed.

`remove` and `purge` ignored every argument, so `./opentr.sh remove --fresh test1` asked
"remove all data volumes?" and, on `y`, wiped the LIVE stack's volumes. `health`,
`healthcheck-all` and `build` ignored `--fresh` the same way. The real script runs here with
`docker`/`nvidia-smi` stubbed on PATH and `y` on stdin: a correct refusal means docker is never
invoked and the confirmation prompt never appears.
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
    env.pop("COMPOSE_PROJECT_NAME", None)
    proc = subprocess.run(
        ["bash", str(OPENTR), *args],
        cwd=REPO,
        env=env,
        input="y\n",
        capture_output=True,
        text=True,
        timeout=120,
    )
    # opentr.sh's prologue probes `docker info` / nvidia-smi before dispatch; those are read-only
    # and not what is under test. Anything else is a real call aimed at a stack.
    lines = log.read_text().splitlines() if log.exists() else []
    acting = [ln for ln in lines if ln != "docker info" and not ln.startswith("nvidia-smi")]
    return proc, "\n".join(acting)


@pytest.mark.unit
@pytest.mark.parametrize("command", ["remove", "purge"])
def test_remove_and_purge_refuse_fresh_before_prompt_or_docker(tmp_path, command):
    proc, calls = _run(tmp_path, command, "--fresh", "test1")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 2, out
    assert calls == "", f"docker was invoked: {calls}"
    assert "Continue?" not in out
    assert "fresh-destroy" in proc.stderr


@pytest.mark.unit
@pytest.mark.parametrize("command", ["remove", "purge", "health", "build"])
def test_default_only_commands_refuse_any_argument(tmp_path, command):
    proc, calls = _run(tmp_path, command, "--bogus")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert calls == ""
    assert "Continue?" not in proc.stdout


@pytest.mark.unit
@pytest.mark.parametrize("command", ["health", "healthcheck-all", "build"])
def test_health_and_build_refuse_fresh(tmp_path, command):
    proc, calls = _run(tmp_path, command, "--fresh", "test1")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert calls == ""
    assert "--fresh" in proc.stderr


@pytest.mark.unit
def test_logs_and_shell_refuse_unknown_flags_and_unknown_fresh_names(tmp_path):
    for args in (("logs", "--bogus"), ("shell", "--fresh", "no-such-deployment-xyz")):
        proc, calls = _run(_sub(tmp_path, args), *args)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert calls == ""


def _sub(tmp_path: Path, args: tuple[str, ...]) -> Path:
    d = tmp_path / "-".join(a.lstrip("-") for a in args)
    d.mkdir()
    return d
