"""The default deployment's worst-case connection count fits inside Postgres.

Concurrency audit F9 (``docs/design/concurrency_foundations_plan.md``). A SQLAlchemy
``QueuePool`` holds ``pool_size`` connections and opens up to ``max_overflow`` more
("Connection Pooling", https://docs.sqlalchemy.org/en/20/core/pooling.html), and the pool
belongs to one PROCESS: a Celery prefork worker is a MainProcess plus ``--concurrency``
children, and each child builds its own pool after the fork
(``core/celery_worker_lifecycle.py``). A threads-pool worker is one process whose threads
share one pool. So the worst case is

    sum over services of (pool_size + max_overflow) x processes

and it must stay within ``max_connections`` minus the slots Postgres keeps for superusers
(``superuser_reserved_connections``, default 3, plus ``reserved_connections``, default 0;
"Connections and Authentication", https://www.postgresql.org/docs/17/runtime-config-connection.html),
or the last process to connect, the API included, gets ``FATAL: sorry, too many clients
already``. The defaults used to reach 270 against 200.

This reads ``docker-compose.yml`` itself, every service including the profile-gated ones,
with each ``${VAR:-default}`` resolved to its default, so a change to a pool size, a
``--concurrency`` default, a ``--pool`` default or ``max_connections`` is re-checked.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
_COMPOSE = _REPO / "docker-compose.yml"

#: Postgres defaults for the slots ordinary roles cannot use (PostgreSQL 17 docs).
SUPERUSER_RESERVED_CONNECTIONS = 3
RESERVED_CONNECTIONS = 0

_DEFAULT = re.compile(r"\$\{(\w+)(?::?-([^}]*))?\}")


def _resolve(value: str) -> str:
    """Replace each ``${VAR:-default}`` with its default (nested defaults included)."""
    previous = None
    while previous != value:
        previous = value
        value = _DEFAULT.sub(lambda m: m.group(2) or "", value)
    return value


def _flag(command: str, name: str) -> str | None:
    match = re.search(rf"--{name}[= ](\S+)", command)
    return match.group(1) if match else None


def _environment(service: dict[str, Any]) -> dict[str, str]:
    env = service.get("environment") or {}
    if isinstance(env, list):
        env = dict(item.split("=", 1) for item in env if "=" in item)
    return {key: _resolve(str(value)) for key, value in env.items()}


def _api_pool() -> int:
    from app.core.config import Settings

    fields = Settings.model_fields
    return int(fields["DB_POOL_SIZE"].default) + int(fields["DB_MAX_OVERFLOW"].default)


def budget_rows(compose: dict[str, Any]) -> list[tuple[str, int, int]]:
    """``(service, pool capacity, processes)`` for every service that opens app connections."""
    rows: list[tuple[str, int, int]] = []
    for name, service in compose["services"].items():
        command = service.get("command") or ""
        if isinstance(command, list):
            command = " ".join(command)
        command = _resolve(" ".join(str(command).split()))
        env = _environment(service)
        if name == "backend":
            # One uvicorn process (no --workers), pool from the coded settings default.
            rows.append((name, _api_pool(), 1))
            continue
        if "celery" not in command or (" worker" not in command and " beat" not in command):
            continue
        assert "DB_POOL_SIZE" in env and "DB_MAX_OVERFLOW" in env, (
            f"{name} runs Celery without a sized pool; it would get the API's 20 + 40 per process"
        )
        capacity = int(env["DB_POOL_SIZE"]) + int(env["DB_MAX_OVERFLOW"])
        processes = 1
        if " worker" in command and (_flag(command, "pool") or "prefork") == "prefork":
            processes += int(_flag(command, "concurrency") or 0)
        rows.append((name, capacity, processes))
    return rows


#: Overlays that add a worker's command; merged so its process count is counted too.
_WORKER_OVERLAYS = (_REPO / "docker-compose.gpu-scale.yml",)


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """The base file, with each worker overlay's ``command`` laid over its service."""
    merged: dict[str, Any] = yaml.safe_load(_COMPOSE.read_text())
    for overlay in _WORKER_OVERLAYS:
        for name, service in (yaml.safe_load(overlay.read_text())["services"] or {}).items():
            if "command" in service and name in merged["services"]:
                merged["services"][name]["command"] = service["command"]
    return merged


def _max_connections(compose: dict[str, Any]) -> int:
    command = _resolve(" ".join(str(compose["services"]["postgres"]["command"]).split()))
    value = re.search(r"max_connections=(\d+)", command)
    assert value, "postgres no longer sets max_connections"
    for reserved in ("superuser_reserved_connections", "reserved_connections"):
        assert reserved not in command, f"{reserved} is set; this test assumes the default"
    return int(value.group(1))


def test_the_worst_case_fits_inside_max_connections(compose) -> None:
    rows = budget_rows(compose)
    total = sum(capacity * processes for _, capacity, processes in rows)
    usable = _max_connections(compose) - SUPERUSER_RESERVED_CONNECTIONS - RESERVED_CONNECTIONS
    table = "\n".join(f"  {n}: {c} x {p} = {c * p}" for n, c, p in rows)
    assert total <= usable, (
        f"worst-case connections {total} > {usable} usable of max_connections:\n{table}"
    )


def test_the_scan_sees_every_worker(compose) -> None:
    """The budget is only as good as the rows it found; a parser miss would read as headroom."""
    rows = {name: (capacity, processes) for name, capacity, processes in budget_rows(compose)}
    assert {"backend", "celery-beat", "celery-worker-gpu-scaled"} <= set(rows)
    # The five prefork services fork one pool per child.
    for prefork in (
        "celery-download-worker",
        "celery-cpu-worker",
        "celery-cloud-asr-worker",
        "celery-nlp-worker",
        "celery-embedding-worker",
    ):
        assert rows[prefork][1] > 1, f"{prefork} was not read as prefork: {rows[prefork]}"
    # The threads-pool workers are one process each.
    for threaded in ("celery-worker", "celery-redaction", "celery-worker-gpu-scaled"):
        assert rows[threaded][1] == 1, rows[threaded]


def test_the_budget_arithmetic_catches_an_overrun() -> None:
    """A deliberately oversized service must fail the same comparison the real test makes."""
    oversized = {
        "services": {
            "postgres": {"command": "postgres -c max_connections=${PG_MAX_CONNECTIONS:-50}"},
            "backend": {"command": "uvicorn app.main:app"},
            "worker": {
                "command": "celery -A app.core.celery worker -Q cpu --concurrency=${C:-8}",
                "environment": ["DB_POOL_SIZE=${P:-2}", "DB_MAX_OVERFLOW=${O:-3}"],
            },
        }
    }
    rows = budget_rows(oversized)
    assert ("worker", 5, 9) in rows
    total = sum(capacity * processes for _, capacity, processes in rows)
    assert total > _max_connections(oversized) - SUPERUSER_RESERVED_CONNECTIONS


def test_env_example_matches_the_compose_defaults(compose) -> None:
    """The documented values are the ones the budget was computed from."""
    example = (_REPO / ".env.example").read_text()

    def documented(key: str) -> str:
        match = re.search(rf"^{key}=(\S+)", example, re.MULTILINE)
        assert match, f".env.example does not set {key}"
        return match.group(1)

    assert documented("PG_MAX_CONNECTIONS") == str(_max_connections(compose))
    assert documented("WORKER_DB_POOL_SIZE") == "2"
    assert documented("WORKER_DB_MAX_OVERFLOW") == "0"
    assert documented("THREADED_WORKER_DB_MAX_OVERFLOW") == "3"
    assert int(documented("DB_POOL_SIZE")) + int(documented("DB_MAX_OVERFLOW")) == _api_pool()
