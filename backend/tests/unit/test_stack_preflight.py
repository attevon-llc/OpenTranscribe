"""The pytest session preflight refuses a stack whose schema is not this checkout's head."""

from __future__ import annotations

import importlib.util
import json
import os
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

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


def test_observed_ports_follow_the_resolved_stack_not_the_stock_default(
    stack: Stack, tmp_path: Path
) -> None:
    _, _ = stack
    pg = mk("otfresh-x-postgres-1", "otfresh-x", "postgres", "127.0.0.1:5976->5432/tcp")
    (tmp_path / "model.json").write_text(json.dumps({"containers": [pg]}))
    env = {"COMPOSE_PROJECT_NAME": "otfresh-x", "POSTGRES_PORT": "5432"}
    assert stack_preflight.observed_ports(env)["POSTGRES_PORT"] == "5976"


def test_observed_ports_are_empty_when_no_single_stack_resolves(
    stack: Stack, tmp_path: Path
) -> None:
    _, _ = stack
    (tmp_path / "model.json").write_text(json.dumps({"containers": []}))
    assert stack_preflight.observed_ports({}) == {}


def _drift_script() -> ModuleType:
    path = Path(__file__).resolve().parents[3] / "scripts" / "check-schema-drift.py"
    spec = importlib.util.spec_from_file_location("drift_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_schema_drift_reports_a_schema_behind_head_as_that_not_as_drift(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, install = stack
    install("transcribe-app", "v0_old_revision")
    for var in ("POSTGRES_PORT", "COMPOSE_PROJECT_NAME", "ST_ALLOW"):
        monkeypatch.delenv(var, raising=False)
    assert _drift_script()._resolve_stack() == 22
    err = capsys.readouterr().err
    assert "SCHEMA(22)" in err and "NOT model drift" in err


def test_schema_drift_refuses_a_port_nothing_publishes_instead_of_the_stock_one(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, install = stack
    install("transcribe-app", "v471_b")
    monkeypatch.setenv("POSTGRES_PORT", "5999")
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)
    assert _drift_script()._resolve_stack() == 20
