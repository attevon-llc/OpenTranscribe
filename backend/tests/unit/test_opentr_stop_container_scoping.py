"""`opentr.sh stop`'s straggler-cleanup loop must never touch a container that
merely shares a name prefix with this project.

`stop_all_containers()`'s "catch stragglers" loop used to select victims with
`docker ps -a --format '{{.Names}}' | grep -E '^opentranscribe-|^transcribe-app-'`
-- a bare name-prefix match, with no check that the container actually
belongs to this project's compose deployment. Reproduced live on
2026-08-31: a completely unrelated container on the host, named
``opentranscribe-homepage`` (a dashboard app from a different compose
project, sharing the name prefix by pure coincidence), was matched by that
grep and destroyed by the ``docker stop && docker rm`` that followed --
`./opentr.sh stop` deleted infrastructure it was never told about and had no
business touching.

The fix scopes the loop to containers actually labeled with one of this
project's two legitimate compose project names (``opentranscribe`` for every
service with an explicit ``container_name``, ``transcribe-app`` for
``docker-compose.diar-native.yml``'s service, which has none and so falls
back to the checkout directory's basename).

This test drives the REAL loop body (extracted from the script, not
reimplemented) against REAL throwaway containers, so a regression back to a
bare name-prefix match fails here before it can delete something on a real
host again.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = [
    pytest.mark.skipif(not OPENTR.exists(), reason="opentr.sh not present in this checkout"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available"),
]


def _function_body(text: str, name: str) -> str:
    """Source of one top-level ``name() { ... }`` block, closing brace included."""
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=5, check=True)
        return True
    except Exception:
        return False


@pytest.fixture
def straggler_loop_only() -> str:
    """Just the 'catch stragglers' loop, isolated from the compose-down calls
    above it (which would otherwise try to reach real compose files/services
    this test has no interest in standing up)."""
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "stop_all_containers")
    loop_start = body.index("for container in")
    loop_end = body.index("\n}\n", loop_start)
    return body[loop_start:loop_end]


@pytest.fixture
def throwaway_containers():
    """Create real, stopped-friendly containers with distinguishing labels/names,
    and guarantee cleanup even if the test (or the code under test) misbehaves."""
    if not _docker_available():
        pytest.skip("docker daemon not reachable")

    suffix = uuid.uuid4().hex[:8]
    created: list[str] = []

    def _make(name: str, project_label: str | None) -> str:
        full_name = f"{name}-{suffix}"
        cmd = ["docker", "run", "-d", "--name", full_name]
        if project_label is not None:
            cmd += ["--label", f"com.docker.compose.project={project_label}"]
        cmd += ["alpine:3.20", "sleep", "300"]
        subprocess.run(cmd, capture_output=True, check=True, timeout=30)
        created.append(full_name)
        return full_name

    yield _make

    for name in created:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=15)


def _container_exists(name: str) -> bool:
    result = subprocess.run(
        ["docker", "ps", "-a", "--format", "{{.Names}}", "--filter", f"name=^{name}$"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return name in result.stdout.split()


def test_a_container_sharing_only_the_name_prefix_survives(
    straggler_loop_only, throwaway_containers
):
    """The exact live defect: a container named opentranscribe-<x> that is NOT
    labeled with this project's compose project must not be touched."""
    unrelated = throwaway_containers(
        "opentranscribe-unrelated-dashboard", project_label="some-other-app"
    )

    subprocess.run(["bash", "-c", straggler_loop_only], capture_output=True, timeout=60)

    assert _container_exists(unrelated), (
        "a container that only shares the 'opentranscribe-' name prefix, but belongs to a "
        "DIFFERENT compose project, was removed -- this is the exact live incident"
    )


def test_a_real_project_container_is_still_cleaned_up(straggler_loop_only, throwaway_containers):
    """Control: the loop must still do its actual job for a genuine straggler."""
    real = throwaway_containers("opentranscribe-genuine-straggler", project_label="opentranscribe")

    subprocess.run(["bash", "-c", straggler_loop_only], capture_output=True, timeout=60)

    assert not _container_exists(real), (
        "a container genuinely labeled as belonging to this project's compose deployment "
        "was NOT cleaned up -- the fix must not have made the loop a no-op"
    )


def test_the_transcribe_app_project_container_is_also_cleaned_up(
    straggler_loop_only, throwaway_containers
):
    """The diar-native service has no explicit container_name, so its project
    is the checkout directory's basename -- must be covered too, not just
    'opentranscribe'."""
    diar = throwaway_containers(
        "transcribe-app-diar-native-straggler", project_label="transcribe-app"
    )

    subprocess.run(["bash", "-c", straggler_loop_only], capture_output=True, timeout=60)

    assert not _container_exists(diar), (
        "a genuine transcribe-app-project straggler was not cleaned up"
    )


def test_an_unlabeled_container_sharing_the_name_prefix_survives(
    straggler_loop_only, throwaway_containers
):
    """A container with no compose project label at all (e.g. a bare `docker run`,
    not part of any compose deployment) must not be treated as a straggler just
    because its name happens to start with the right prefix."""
    bare = throwaway_containers("opentranscribe-bare-docker-run", project_label=None)

    subprocess.run(["bash", "-c", straggler_loop_only], capture_output=True, timeout=60)

    assert _container_exists(bare), "an unlabeled container was removed on name prefix alone"
