"""``docker compose up -d --wait`` returning 0 is not proof a stack is up.

It has a known race (moby/compose): a container stuck in a restart loop can be
observed "Running" at the instant ``--wait`` polls, satisfying the wait
condition and returning exit 0 even though the container never actually came
up. Issue #962 was hit live twice this way: once with Postgres stuck in
``Restarting (1)`` after ``up --wait`` reported success, once with the
frontend crash-looping on ``EMFILE`` (issue #961b) -- in both cases
``./opentr.sh start`` exited 0 and the caller had no signal the stack was
unusable.

``verify_stack_health`` is the second, independent check opentr.sh now runs
after ``up`` returns, regardless of its exit code. This drives the REAL
function body (never a reimplementation) via subprocess bash, against a fake
``docker`` on ``$PATH`` that reports canned container states -- so the test
needs no real Docker daemon and cannot start any container.

The Keycloak case is pinned explicitly: a container still ``health: starting``
must NOT be reported as broken, because Docker's own healthcheck state
machine already won't flip that to ``unhealthy`` before the service's own
``start_period`` elapses (Keycloak's 120s, the backend's 600s, ...) --
duplicating that budget with a second, global timeout here is exactly the
mistake that once marked a healthy Keycloak unhealthy at 3.5 minutes and
aborted a full gate run before a single test ran.
"""

from __future__ import annotations

import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
OPENTR = REPO_ROOT / "opentr.sh"

pytestmark = pytest.mark.skipif(
    not OPENTR.exists(), reason="opentr.sh not present in this checkout"
)


def _function_body(text: str, name: str) -> str:
    start = text.index(f"\n{name}() {{")
    end = text.index("\n}\n", start)
    return text[start : end + len("\n}\n")]


# One line per fake container: "<cid> <name> <status> <health-or-dash>".
_FakeContainer = tuple[str, str, str, str]


def _write_fake_docker(bin_dir: Path, containers: list[_FakeContainer]) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    state = bin_dir / "state.tsv"
    state.write_text("\n".join(" ".join(c) for c in containers) + "\n")

    script = bin_dir / "docker"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/bash
            # Fake docker CLI for verify_stack_health -- only understands the
            # exact call shapes that function makes.
            STATE="{state}"
            if [ "$1" = "compose" ]; then
                # .../ps -a -q -> print every fake container id, one per line.
                awk '{{print $1}}' "$STATE"
                exit 0
            fi
            if [ "$1" = "inspect" ]; then
                # argv: docker inspect --format '<fmt>' <cid>
                fmt="$3"
                cid="$4"
                line="$(grep "^${{cid}} " "$STATE")"
                name="$(echo "$line" | awk '{{print $2}}')"
                status="$(echo "$line" | awk '{{print $3}}')"
                health="$(echo "$line" | awk '{{print $4}}')"
                case "$fmt" in
                    '{{{{.Name}}}}') echo "/${{name}}" ;;
                    '{{{{.State.Status}}}}') echo "$status" ;;
                    '{{{{if .State.Health}}}}{{{{.State.Health.Status}}}}{{{{end}}}}')
                        [ "$health" = "-" ] && echo "" || echo "$health"
                        ;;
                esac
                exit 0
            fi
            if [ "$1" = "logs" ]; then
                shift
                echo "FAKE LOG for $*"
                exit 0
            fi
            exit 1
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, containers: list[_FakeContainer]) -> subprocess.CompletedProcess:
    bin_dir = tmp_path / "bin"
    _write_fake_docker(bin_dir, containers)
    body = _function_body(OPENTR.read_text(encoding="utf-8"), "verify_stack_health")
    script = f'{body}\nverify_stack_health "-f fake-compose.yml"\n'
    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    return subprocess.run(
        ["bash", "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True
    )


def test_all_running_and_healthy_passes(tmp_path: Path):
    result = _run(
        tmp_path,
        [
            ("cid1", "otfresh-x-postgres", "running", "healthy"),
            ("cid2", "otfresh-x-redis", "running", "-"),
        ],
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_a_restarting_container_fails_despite_up_waits_own_success(tmp_path: Path):
    """The exact live bug: Postgres stuck in `Restarting (1)`."""
    result = _run(
        tmp_path,
        [
            ("cid1", "otfresh-x-postgres", "restarting", "-"),
            ("cid2", "otfresh-x-redis", "running", "-"),
        ],
    )
    assert result.returncode == 1
    assert "otfresh-x-postgres" in result.stdout
    assert "restarting" in result.stdout


def test_an_unhealthy_container_fails(tmp_path: Path):
    result = _run(
        tmp_path,
        [("cid1", "otfresh-x-backend", "running", "unhealthy")],
    )
    assert result.returncode == 1
    assert "otfresh-x-backend" in result.stdout
    assert "unhealthy" in result.stdout


def test_an_exited_container_fails(tmp_path: Path):
    result = _run(
        tmp_path,
        [("cid1", "otfresh-x-frontend", "exited", "-")],
    )
    assert result.returncode == 1
    assert "otfresh-x-frontend" in result.stdout


def test_a_slow_starting_healthcheck_is_not_a_failure(tmp_path: Path):
    """Keycloak-shaped case: still inside its OWN start_period. Must pass --
    this is the regression this function must never reintroduce (a global
    timeout that marks a legitimately slow, healthy service as broken)."""
    result = _run(
        tmp_path,
        [("cid1", "otfresh-x-keycloak", "running", "starting")],
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_broken_service_logs_are_printed_for_diagnosis(tmp_path: Path):
    result = _run(
        tmp_path,
        [("cid1", "otfresh-x-postgres", "restarting", "-")],
    )
    assert "FAKE LOG for --tail=30 otfresh-x-postgres" in result.stdout
