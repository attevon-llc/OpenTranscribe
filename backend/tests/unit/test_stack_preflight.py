"""The pytest session preflight refuses a stack whose schema is not this checkout's head."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import stack_preflight
from tests.unit.test_stack_target import FAKE_DOCKER
from tests.unit.test_stack_target import mk

Stack = tuple[Path, Callable[[str, str], None]]


@pytest.fixture
def stack(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Stack:
    root = tmp_path / "checkout"
    versions = root / "backend" / "alembic" / "versions"
    versions.mkdir(parents=True)
    (versions / "v468_a.py").write_text('revision = "v468_a"\ndown_revision = None\n')
    (versions / "v471_b.py").write_text('revision = "v471_b"\ndown_revision = "v468_a"\n')
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DOCKER_LOG", str(tmp_path / "log"))
    monkeypatch.setenv("FAKE_DOCKER_MODEL", str(tmp_path / "model.json"))

    def install(project: str, version: str) -> None:
        pg = mk(
            "pg", project, "postgres", "127.0.0.1:5176->5432/tcp", sql={"alembic_version": version}
        )
        (tmp_path / "model.json").write_text(json.dumps({"containers": [pg]}))

    return root, install


def test_a_schema_behind_the_checkout_stops_the_session(stack: Stack) -> None:
    root, install = stack
    install("transcribe-app", "v468_a")
    problem = stack_preflight.run_preflight({"POSTGRES_PORT": "5176"}, root)
    assert problem is not None and "SCHEMA(22)" in problem and "behind" in problem


def test_a_schema_at_head_lets_it_run(stack: Stack) -> None:
    root, install = stack
    install("transcribe-app", "v471_b")
    assert stack_preflight.run_preflight({"POSTGRES_PORT": "5176"}, root) is None


def test_the_override_is_recorded_not_silent(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    root, install = stack
    install("transcribe-app", "v468_a")
    env = {"POSTGRES_PORT": "5176", "ST_ALLOW": "schema-head=lane runs old schema"}
    assert stack_preflight.run_preflight(env, root) is None
    assert "OVERRIDDEN schema-head (lane runs old schema)" in capsys.readouterr().err


def test_a_throwaway_database_is_not_judged(stack: Stack) -> None:
    root, install = stack
    install("opentranscribe-pytest-ab12", "v0_old")
    assert stack_preflight.run_preflight({"POSTGRES_PORT": "5176"}, root) is None


def test_no_compose_owner_is_not_applicable(
    stack: Stack, capsys: pytest.CaptureFixture[str]
) -> None:
    root, install = stack
    install("transcribe-app", "v468_a")
    assert stack_preflight.run_preflight({"POSTGRES_PORT": "5999"}, root) is None
    assert "not applicable" in capsys.readouterr().err
