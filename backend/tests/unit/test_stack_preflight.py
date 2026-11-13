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


# ---------------------------------------------------------------- e2e (plan P3)

HEALTH = {"State": {"Status": "running", "Health": {"Status": "healthy"}}}


def _e2e_model(tmp_path: Path, project: str, src: Path, other: str | None = None) -> None:
    """`project` publishes 5273/5274 and bind-mounts `src`; `other` (listed FIRST) owns 5173/5174."""
    containers = []
    if other:
        containers += [
            mk(f"{other}-frontend", other, "frontend", "127.0.0.1:5173->5173/tcp", inspect=HEALTH),
            mk(f"{other}-backend", other, "backend", "127.0.0.1:5174->8080/tcp", inspect=HEALTH),
        ]
    for svc, host, cport in (("frontend", 5273, 5173), ("backend", 5274, 8080)):
        mounts = [{"Type": "bind", "Source": f"{src}/{svc}", "Destination": "/app"}]
        containers.append(
            mk(
                f"{project}-{svc}",
                project,
                svc,
                f"127.0.0.1:{host}->{cport}/tcp",
                inspect={**HEALTH, "Mounts": mounts},
            )
        )
    (tmp_path / "model.json").write_text(json.dumps({"containers": containers}))


E2E_ENV = {"ST_ALLOW": "healthy=http probe is not faked", "ST_HEALTH_TIMEOUT": "1"}


def test_e2e_urls_published_by_two_projects_are_refused(stack: Stack, tmp_path: Path) -> None:
    _e2e_model(tmp_path, "otfresh-demo", tmp_path, other="transcribe-app")
    problem = stack_preflight.e2e_stack_problem(
        "http://localhost:5173", "http://localhost:5274", E2E_ENV
    )
    assert problem is not None and "WRONG_PROJECT(21)" in problem
    assert "transcribe-app" in problem and "otfresh-demo" in problem


def test_e2e_urls_of_one_project_without_fonts_are_refused(stack: Stack, tmp_path: Path) -> None:
    """The fonts are gitignored, so a bare worktree has none: refuse before the capture runs."""
    src = tmp_path / "bare-worktree"
    (src / "frontend" / "static" / "fonts").mkdir(parents=True)
    _e2e_model(tmp_path, "otfresh-demo", src, other="transcribe-app")
    problem = stack_preflight.e2e_stack_problem(
        "http://localhost:5273", "http://localhost:5274", E2E_ENV
    )
    assert problem is not None and "MISSING_ASSET(26)" in problem


def test_e2e_urls_of_one_project_with_fonts_pass(stack: Stack, tmp_path: Path) -> None:
    src = tmp_path / "setup-checkout"
    fonts = src / "frontend" / "static" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "Poppins-Regular.woff2").write_text("x")
    _e2e_model(tmp_path, "otfresh-demo", src, other="transcribe-app")
    assert (
        stack_preflight.e2e_stack_problem("http://localhost:5273", "http://localhost:5274", E2E_ENV)
        is None
    )


def test_a_url_nobody_publishes_is_not_judged(stack: Stack, tmp_path: Path) -> None:
    _e2e_model(tmp_path, "otfresh-demo", tmp_path)
    assert (
        stack_preflight.e2e_stack_problem("http://localhost:9", "http://localhost:10", E2E_ENV)
        is None
    )


def test_isolation_is_decided_by_the_owning_project_not_by_the_port(
    stack: Stack, tmp_path: Path
) -> None:
    """The visual suite used to compare against the literals 5173/5174; a live stack on any
    other port, or a --fresh one ON 5174, was misjudged."""
    _e2e_model(tmp_path, "otfresh-demo", tmp_path, other="transcribe-app")
    assert stack_preflight.is_isolated_stack("http://localhost:5274") is True
    assert stack_preflight.is_isolated_stack("http://localhost:5174") is False
    assert stack_preflight.is_isolated_stack("http://localhost:9") is False


def test_observed_ports_name_the_stack_the_environment_selects(
    stack: Stack, tmp_path: Path
) -> None:
    _e2e_model(tmp_path, "otfresh-demo", tmp_path, other="transcribe-app")
    ports = stack_preflight.observed_ports({"COMPOSE_PROJECT_NAME": "otfresh-demo"})
    assert ports["BACKEND_PORT"] == 5274 and ports["FRONTEND_PORT"] == 5273
    assert stack_preflight.observed_ports({"COMPOSE_PROJECT_NAME": "nothing-here"}) == {}


def test_the_search_corpus_fixture_takes_its_backend_from_the_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import importlib

    monkeypatch.setenv("E2E_BACKEND_URL", "http://localhost:5274")
    monkeypatch.delenv("BACKEND_PORT", raising=False)
    module = importlib.import_module("tests.fixtures.search_corpus_stack")
    module = importlib.reload(module)
    assert module.BASE == "http://localhost:5274/api"
    monkeypatch.delenv("E2E_BACKEND_URL")
    assert importlib.reload(module).BASE == "/api"  # never a guessed live port
