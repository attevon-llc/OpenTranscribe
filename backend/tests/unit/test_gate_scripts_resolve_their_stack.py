"""The gate scripts (STK2 / plan P2) resolve the stack they test before they act on it.

Resolution itself is tested in ``test_stack_target.py``; these tests run the REAL scripts against
a fake ``docker`` and pin the behaviour that was wrong: a script that carried on against
"whatever listens on the stock ports" when no stack could be resolved.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.unit.test_stack_target import FAKE_DOCKER
from tests.unit.test_stack_target import mk

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "scripts"


def _env(tmp_path: Path, containers: list[dict]) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    (tmp_path / "model.json").write_text(json.dumps({"containers": containers}))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "POSTGRES_"))}
    env.update(
        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
        FAKE_DOCKER_LOG=str(tmp_path / "log"),
        FAKE_DOCKER_MODEL=str(tmp_path / "model.json"),
    )
    return env


def _run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, env=env, capture_output=True, text=True, timeout=60, check=False)


@pytest.mark.unit
def test_run_dev_tests_refuses_when_no_stack_resolves(tmp_path: Path) -> None:
    result = _run(
        [str(SCRIPTS / "run-dev-tests.sh"), "--backend-only", "--no-overlays"],
        _env(tmp_path, []),
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "could not resolve the stack to test" in result.stderr
    assert "NO_PROJECT(20)" in result.stderr


@pytest.mark.unit
def test_run_dev_tests_resolves_before_it_sources_the_overlay_library() -> None:
    text = (SCRIPTS / "run-dev-tests.sh").read_text(encoding="utf-8")
    assert text.index("dev_tests_resolve_stack\n") < text.index(
        'source "$REPO_ROOT/scripts/lib/dev-test-overlays.sh"'
    )
    assert "st_target" in (SCRIPTS / "lib" / "dev-test-stack.sh").read_text(encoding="utf-8")


@pytest.mark.unit
def test_the_gate_scripts_hold_no_stock_port_default() -> None:
    for rel in ("run-dev-tests.sh", "run-integration-tests.sh", "gpu-scale-smoke.sh"):
        code = "\n".join(
            ln
            for ln in (SCRIPTS / rel).read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert not re.search(r"PORT[:=-]+\"?51(?:7[3-9]|8\d|90)", code), rel


@pytest.mark.unit
def test_run_mutation_tests_probes_the_observed_postgres_and_redis_ports() -> None:
    code = (SCRIPTS / "run-mutation-tests.sh").read_text(encoding="utf-8")
    lib = (SCRIPTS / "lib" / "mutation-stack.sh").read_text(encoding="utf-8")
    assert "mutation_check_stack" in code
    assert "st_target" in lib and "schema-head" in lib
    assert not re.search(r"port_open\s+51\d\d", code + lib)


def _fake_docker_with_revision(tmp_path: Path, revision: str) -> dict[str, str]:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "docker").write_text(
        '#!/bin/bash\nfor a in "$@"; do [ "$a" = "--format" ] && { echo "'
        + revision
        + '"; exit 0; }; done\nexit 0\n'
    )
    (bindir / "docker").chmod(0o755)
    return {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.unit
def test_diarization_gpu_script_refuses_a_base_image_that_is_not_this_checkout(
    tmp_path: Path,
) -> None:
    env = _fake_docker_with_revision(tmp_path, "0" * 40)
    result = _run([str(SCRIPTS / "run-diarization-gpu-tests.sh")], env)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "not an ancestor of HEAD" in result.stderr or "was built from" in result.stderr


@pytest.mark.unit
def test_diarization_gpu_script_refuses_an_image_with_no_revision_label(tmp_path: Path) -> None:
    env = _fake_docker_with_revision(tmp_path, "")
    result = _run([str(SCRIPTS / "run-diarization-gpu-tests.sh")], env)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "CANNOT be shown" in result.stderr


@pytest.mark.unit
def test_gpu_scale_smoke_stops_as_not_measured_when_no_stack_resolves(tmp_path: Path) -> None:
    result = _run([str(SCRIPTS / "gpu-scale-smoke.sh"), "--check-only"], _env(tmp_path, []))
    assert result.returncode == 4, result.stdout + result.stderr
    assert "no stack resolved" in result.stderr


@pytest.mark.unit
def test_gpu_scale_smoke_uses_the_observed_flower_port_of_the_resolved_project(
    tmp_path: Path,
) -> None:
    env = _env(
        tmp_path,
        [
            mk("otfresh-g-backend-1", "otfresh-g", "backend", "127.0.0.1:6174->8080/tcp"),
            mk("otfresh-g-flower-1", "otfresh-g", "flower", "127.0.0.1:6175->5555/tcp"),
        ],
    )
    env.update(FLOWER_REFRESH_ATTEMPTS="1", FLOWER_REFRESH_INTERVAL="0")
    result = _run([str(SCRIPTS / "gpu-scale-smoke.sh"), "--fresh", "g", "--check-only"], env)
    # No Flower answers on the fake stack's 6175: what matters is WHERE it looked.
    assert result.returncode != 0
    assert "6175" in result.stdout + result.stderr
    assert "5175" not in result.stdout + result.stderr
