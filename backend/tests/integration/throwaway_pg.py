"""Shared helpers for suites that stand up a throwaway PostgreSQL container.

Not a test module (no ``test_`` prefix), so pytest imports it but collects nothing from it.
The fixtures built on top of these helpers live in ``tests/integration/conftest.py``.

Where the time actually goes
----------------------------
Measured on this host 2026-09-07 (docker root is an 82 TB md RAID, not NVMe; load average
12-24, i.e. NOT an idle machine — quote the shape, not the digits):

===========================  ==========  =====================  ================
step                         no tmpfs    ``--tmpfs`` on PGDATA  removable by
===========================  ==========  =====================  ================
``docker run -d``            26-34 s     28 s                   sharing the container
postgres accepts queries     62-92 s     **4.6 s**              ``--tmpfs``
``CREATE DATABASE`` (each)   **7.1 s**   **0.12 s**             ``--tmpfs``
``docker exec`` steady       0.10 s      0.10 s                 (already free)
``docker rm -f``             17-22 s     22 s                   sharing the container
===========================  ==========  =====================  ================

Two independent findings, both counter-intuitive enough to be worth writing down:

1. **``initdb`` on the md RAID, not docker, dominated the old cost.** 62-92 s of a ~124 s
   fixture was the server refusing connections while ``initdb`` fsync'd a fresh cluster onto
   spinning storage. Putting ``PGDATA`` on a tmpfs takes that to **4.6 s** — a ~20x cut for
   one flag. (``test_cleanup_test_users_isolated_db.py`` and ``test_cleanup_test_data_isolated.py``
   already did this; the ``docker exec``-only suites did not, and that asymmetry was the bug.)
2. **``docker exec`` is cheap and was never the problem.** An earlier profile put a single
   ``docker exec ... CREATE DATABASE`` at 11.5 s, which reads as "the daemon is slow". It is
   not: steady-state ``docker exec`` is **0.10 s**. The 11.5 s was ``CREATE DATABASE`` copying
   ``template1``'s directory to the md RAID, and the 7.1 s figure above confirms it. This
   matters because it is what makes "one container, a fresh database per test" viable at all:
   at 7.1 s per database it would have eaten most of the saving.

What is left after both fixes is ``docker run`` + ``docker rm`` (~50 s), which is per-CONTAINER
and pure daemon cost — hence the session-scoped container in ``conftest.py``.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_COMPOSE_FILE = _REPO_ROOT / "docker-compose.yml"

DB_USER = "postgres"

# Every container these helpers start carries this label, so a run killed hard enough to skip
# its teardown (SIGKILL — a `finally` cannot save you there) leaves something findable:
#
#     docker ps -a --filter label=ot-throwaway-pg=1
#
# Deliberately NOT swept automatically: a blanket remove is exactly the "clean up before
# starting fresh" reflex that has destroyed live containers on this host before.
CONTAINER_LABEL = "ot-throwaway-pg=1"

# How long a throwaway postgres gets to accept connections before the fixture gives up.
#
# This was 30 s, copied into five modules, and it is the reason 17 integration tests errored
# with "postgres in ot-*-test-<hash> never became ready" during a full gate run. The budget was
# never the *container's* startup time in isolation — it is startup time while the same docker
# daemon is building overlay images and fielding `docker exec` from 48 pytest workers.
#
# With PGDATA on a tmpfs the measured ready time is ~4.6 s (see the module docstring), so this
# ceiling is now ~40x the observed cost rather than ~2x. Raising it costs nothing on a healthy
# run: every caller polls to TWO CONSECUTIVE successful queries and returns the moment it has
# them. The timeout is only reached on a genuine failure, where a longer wait buys a real
# diagnosis instead of a load-dependent flake.
_READY_TIMEOUT = 180.0


def run(cmd: list[str], *, stdin_text: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv with no shell, capturing both streams."""
    return subprocess.run(  # noqa: S603 - fixed argv, no shell
        cmd, capture_output=True, text=True, input=stdin_text
    )


def postgres_image_tag() -> str:
    """Parse the pinned Postgres image out of docker-compose.yml -- never hardcoded."""
    compose = _COMPOSE_FILE.read_text(encoding="utf-8")
    match = re.search(r"image:\s*(postgres:\S+)", compose)
    assert match, "could not find an `image: postgres:<tag>` line in docker-compose.yml"
    return match.group(1)


def start_container(name: str, password: str) -> None:
    """Start a network-isolated throwaway Postgres, with PGDATA on a tmpfs.

    ``--network none``: the container cannot reach anything, including the live dev stack.
    Every caller of this helper talks to it over ``docker exec`` (a local Unix socket inside
    the container), so it never needs a published port -- which is also why the two suites
    that DO connect over TCP keep their own containers rather than sharing this one.

    ``--tmpfs /var/lib/postgresql/data``: the ~20x win documented in the module docstring.
    Safe here because nothing restarts the container; the data is meant to die with it.
    """
    started = run(
        [
            "docker",
            "run",
            "-d",
            "--network",
            "none",
            "--name",
            name,
            "--label",
            CONTAINER_LABEL,
            "--tmpfs",
            "/var/lib/postgresql/data",
            "-e",
            f"POSTGRES_PASSWORD={password}",
            postgres_image_tag(),
        ]
    )
    assert started.returncode == 0, (
        f"failed to start throwaway postgres container: {started.stderr}"
    )


def wait_ready(container: str, timeout: float = _READY_TIMEOUT) -> None:
    """Poll an actual query in a loop -- never a bare sleep -- until the server is up.

    The official postgres image starts the server once to run initdb, stops it, then starts it
    again for real -- and a readiness probe can succeed in the brief window between those two
    starts, right before the shutdown. A single "ready" reading is therefore not trustworthy;
    require two consecutive successes with a real query in between, or ``CREATE DATABASE``
    calls made right after this returns intermittently fail with "the database system is
    shutting down".
    """
    deadline = time.monotonic() + timeout
    last: subprocess.CompletedProcess[str] | None = None
    consecutive = 0
    while time.monotonic() < deadline:
        last = run(
            [
                "docker",
                "exec",
                container,
                "psql",
                "-U",
                DB_USER,
                "-d",
                "postgres",
                "-c",
                "SELECT 1;",
            ]
        )
        if last.returncode == 0:
            consecutive += 1
            if consecutive >= 2:
                return
        else:
            consecutive = 0
        time.sleep(0.3)
    raise RuntimeError(
        f"postgres in {container} never became ready: {last.stdout if last else 'no attempt made'}"
    )


def remove_container(name: str) -> None:
    run(["docker", "rm", "-f", name])


def psql(
    container: str, dbname: str, sql: str, *, tuples_only: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run SQL through ``docker exec`` against ``dbname``, stopping on the first error."""
    argv = ["docker", "exec", "-i", container, "psql", "-v", "ON_ERROR_STOP=1", "-U", DB_USER]
    if tuples_only:
        argv += ["-tA"]
    argv += [dbname]
    return run(argv, stdin_text=sql)


def list_databases(container: str) -> frozenset[str]:
    """Every database currently in the cluster."""
    result = psql(container, "postgres", "SELECT datname FROM pg_database;", tuples_only=True)
    assert result.returncode == 0, f"could not list databases: {result.stderr}"
    return frozenset(line.strip() for line in result.stdout.splitlines() if line.strip())


def drop_databases_outside(container: str, keep: frozenset[str]) -> None:
    """Drop every database in ``container`` that is not in ``keep``.

    ``WITH (FORCE)`` because at least one test (``test_drop_database_with_force_terminates_
    an_open_connection``) deliberately leaves a client connected; a plain DROP would fail
    there and leave the cluster dirty for whatever runs next.

    Failure raises, on purpose. A silently-skipped drop shows up later as an unrelated test's
    ``CREATE DATABASE`` failing with "already exists", which is a much worse place to start
    debugging than here.
    """
    leftovers = sorted(list_databases(container) - keep)
    for dbname in leftovers:
        result = psql(container, "postgres", f'DROP DATABASE "{dbname}" WITH (FORCE);')
        assert result.returncode == 0, (
            f"could not drop leftover database {dbname!r} from the shared throwaway "
            f"container: {result.stderr}"
        )
