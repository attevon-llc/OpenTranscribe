"""`run-mutation-tests.sh --fresh NAME` reaches stack_target resolution (no COMPOSE_PROJECT_NAME needed)."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "run-mutation-tests.sh"


def _run(tmp_path: Path, *args: str):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    log = tmp_path / "docker-argv.txt"
    docker = bindir / "docker"
    # Stub docker: records argv, reports no containers, so resolution of any NAME fails fast.
    docker.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n')
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(tmp_path),
        "OT_MUTATION_OUT_DIR": str(tmp_path / "out"),
    }
    return subprocess.run(
        [str(_SCRIPT), *args], capture_output=True, text=True, timeout=120, env=env
    ), log


def test_fresh_name_is_resolved_not_ignored(tmp_path):
    r, log = _run(tmp_path, "--fresh", "mutnamezz", "--check")
    combined = r.stdout + r.stderr
    assert "mutnamezz" in combined, combined
    assert "otfresh-mutnamezz" in combined or "no stack resolved" in combined


def test_fresh_without_a_name_is_misuse(tmp_path):
    r, _ = _run(tmp_path, "--check", "--fresh")
    assert r.returncode == 2
    assert "--fresh needs a NAME" in r.stderr


def test_fresh_followed_by_a_flag_is_misuse(tmp_path):
    r, _ = _run(tmp_path, "--fresh", "--check")
    assert r.returncode == 2
