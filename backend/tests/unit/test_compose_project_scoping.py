"""`tests/compose_project.py` must resolve containers in the stack UNDER TEST, and no other.

The defect this pins: the live-stack tests resolved their target with
``docker ps --filter name=<service>`` and took ``[0]``. That filter is not scoped to a
deployment, and this host routinely runs several OpenTranscribe stacks at once -- the plain
dev stack plus any ``./opentr.sh start dev --fresh <name>`` deployment -- each carrying a
container that matches. ``docker ps`` orders newest-first, so the target was decided by which
stack had been restarted most recently.

Every fixture below therefore lists the OTHER stack's container FIRST, reproducing the
dangerous ordering rather than the lucky one that happened to hold when this was found. Under
the old first-match logic each assertion here names the wrong container; a fix that merely
reordered, or hardcoded the dev stack's name, is caught by
``test_resolution_follows_the_port_this_process_targets``.
"""

from __future__ import annotations

import subprocess
import types
from pathlib import Path
from typing import NamedTuple

import pytest

from tests import compose_project


class _Container(NamedTuple):
    """One row of the fake `docker ps` table."""

    name: str
    project: str
    service: str
    publish: str | None = None


#: Deliberately newest-first with the DEMO stack ahead of the dev stack -- the ordering that
#: makes the old `[0]` logic select the wrong deployment.
_FAKE_CONTAINERS = [
    _Container("otfresh-demo-diar-native", "otfresh-demo", "diar-native"),
    _Container("otfresh-demo-postgres", "otfresh-demo", "postgres", publish="5276"),
    _Container("transcribe-app-diar-native-1", "transcribe-app", "diar-native"),
    _Container("opentranscribe-postgres", "transcribe-app", "postgres", publish="5176"),
]


def _fake_docker_ps(argv: list[str]) -> str:
    """Emulate `docker ps`'s server-side `--filter`/`--format` handling."""
    filters: list[str] = []
    fmt = "{{.Names}}"
    i = 0
    while i < len(argv):
        if argv[i] == "--filter":
            filters.append(argv[i + 1])
            i += 2
            continue
        if argv[i] == "--format":
            fmt = argv[i + 1]
            i += 2
            continue
        i += 1

    rows: list[str] = []
    for container in _FAKE_CONTAINERS:
        keep = True
        for spec in filters:
            if spec == "status=running":
                continue
            if spec.startswith("publish="):
                keep = keep and container.publish == spec.split("=", 1)[1]
            elif spec.startswith("label=com.docker.compose.project="):
                keep = keep and container.project == spec.split("=", 2)[2]
            elif spec.startswith("label=com.docker.compose.service="):
                keep = keep and container.service == spec.split("=", 2)[2]
            else:  # pragma: no cover - an unhandled filter would silently widen the result
                raise AssertionError(f"fake docker ps got an unmodelled filter: {spec}")
        if keep:
            rows.append(container.project if "Label" in fmt else container.name)
    return "".join(f"{row}\n" for row in rows)


@pytest.fixture
def fake_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        compose_project, "shutil", types.SimpleNamespace(which=lambda _: "/usr/bin/docker")
    )
    monkeypatch.setattr(
        compose_project,
        "subprocess",
        types.SimpleNamespace(
            run=lambda argv, **_: types.SimpleNamespace(stdout=_fake_docker_ps(argv)),
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    monkeypatch.delenv("COMPOSE_PROJECT_NAME", raising=False)


def test_container_is_scoped_to_the_project_under_test(
    fake_docker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE regression. Two diar-native containers exist and the other stack's is listed
    first; the dev stack's must still be the one selected."""
    monkeypatch.setenv("POSTGRES_PORT", "5176")

    assert compose_project.compose_project_name() == "transcribe-app"
    assert compose_project.compose_service_container("diar-native") == (
        "transcribe-app-diar-native-1"
    )


def test_resolution_follows_the_port_this_process_targets(
    fake_docker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the test above: pointed at the fresh stack's Postgres port, the SAME
    code must resolve the fresh stack. Without this, hardcoding the dev stack's name -- the
    other way to get a green run here -- would look like a fix."""
    monkeypatch.setenv("POSTGRES_PORT", "5276")

    assert compose_project.compose_project_name() == "otfresh-demo"
    assert compose_project.compose_service_container("diar-native") == "otfresh-demo-diar-native"


def test_explicit_compose_project_name_wins(
    fake_docker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Matches `scripts/lib/compose-project.sh`: an operator's explicit project overrides
    detection, whatever the ports say."""
    monkeypatch.setenv("POSTGRES_PORT", "5176")
    monkeypatch.setenv("COMPOSE_PROJECT_NAME", "otfresh-demo")

    assert compose_project.compose_project_name() == "otfresh-demo"
    assert compose_project.compose_service_container("diar-native") == "otfresh-demo-diar-native"


def test_unresolvable_project_selects_nothing_rather_than_guessing(
    fake_docker: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fail-safe direction. An unmatched port leaves two candidate Postgres containers, so
    detection falls through to the checkout basename; when that names no running project the
    answer must be None -- the caller then SKIPS as not-measured. Silently measuring another
    operator's stack is the outcome this module exists to prevent.

    `tmp_path` stands in for a checkout whose directory name matches no running deployment,
    which is exactly what a git worktree is (`scripts/lib/compose-project.sh`'s bug #1)."""
    monkeypatch.setenv("POSTGRES_PORT", "5999")
    monkeypatch.setattr(compose_project, "_REPO_ROOT", tmp_path)

    assert compose_project.compose_project_name() == tmp_path.name
    assert compose_project.compose_service_container("diar-native") is None


def test_all_replicas_of_one_service_are_returned_within_the_project(
    fake_docker: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`compose_service_containers` is the list form used for the `--gpu-scale` worker.
    Replicas inside ONE deployment are interchangeable; containers from another deployment
    are not, and must not appear."""
    monkeypatch.setenv("POSTGRES_PORT", "5176")

    containers = compose_project.compose_service_containers("diar-native")

    assert containers == ["transcribe-app-diar-native-1"]
    assert "otfresh-demo-diar-native" not in containers


def test_absent_docker_reports_nothing_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(compose_project, "shutil", types.SimpleNamespace(which=lambda _: None))

    assert compose_project.compose_service_container("diar-native") is None
