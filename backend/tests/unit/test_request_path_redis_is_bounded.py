"""Request-path code never uses the unbounded Redis client (audit M2).

``get_redis()`` keeps redis-py's unbounded socket defaults on purpose, for workers. A
Redis that accepts the connection and stops answering would hold an API thread on any
call made through it, or the event loop for one made from an async handler. Code that
runs while serving a request uses ``get_request_redis()``, whose calls are bounded by
``REDIS_REQUEST_*_TIMEOUT_SECONDS``.

The scan covers ``app/api``, ``app/auth`` and ``app/services`` (services are what
handlers call), plus the core and utility modules a request reaches. A module that
genuinely needs the unbounded client (a blocking read, a pub/sub wait) goes in
``ALLOWED`` with its reason.
"""

from __future__ import annotations

import ast
import textwrap
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

GUARDED_TREES = ("api", "auth", "services")
#: Outside those trees, but called while serving a request.
GUARDED_MODULES = (
    "core/celery_metrics.py",  # /metrics: queue depth and lease gauges
    "core/task_cancellation.py",  # the cancel endpoints arm the flag
    "core/task_liveness.py",  # stuck-task detection and retry from admin routes
    "core/task_replay.py",  # stuck-task detection
    "utils/task_lock.py",  # reindex and mirror-settings routes check the locks
    "utils/task_secret_handoff.py",  # dispatch from a request
    "utils/websocket_notify.py",  # published from request handlers
)

#: ``<path under app/>`` -> why it needs the unbounded client. Each entry is a reason,
#: not a convenience: say which blocking call it makes.
ALLOWED: dict[str, str] = {}


def _unbounded_uses(source: str) -> list[int]:
    """Lines that import or call ``get_redis`` (the unbounded client)."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "get_redis" for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and node.attr == "get_redis":
            lines.append(node.lineno)
    return sorted(lines)


def _guarded_files() -> list[Path]:
    files = [path for tree in GUARDED_TREES for path in (APP / tree).rglob("*.py")]
    files += [APP / module for module in GUARDED_MODULES]
    return sorted(set(files))


def test_request_path_code_uses_only_the_bounded_client():
    offenders = [
        f"{path.relative_to(APP)}:{line}"
        for path in _guarded_files()
        if str(path.relative_to(APP)) not in ALLOWED
        for line in _unbounded_uses(path.read_text())
    ]
    assert offenders == [], "unbounded get_redis() on the request path: " + ", ".join(offenders)


def test_every_guarded_module_and_allowance_exists():
    for module in (*GUARDED_MODULES, *ALLOWED):
        assert (APP / module).is_file(), module
    assert all(reason.strip() for reason in ALLOWED.values())


def test_the_scan_sees_the_request_path_clients():
    """Vacuity guard: the bounded client is what these modules actually call."""
    sources = "".join((APP / module).read_text() for module in GUARDED_MODULES)
    assert sources.count("get_request_redis") >= len(GUARDED_MODULES)


def test_selftest_fires_on_an_import_and_on_a_module_attribute():
    source = textwrap.dedent(
        """
        from app.core.redis import get_redis
        from app.core import redis as redis_core

        def handler():
            return redis_core.get_redis().get("k")
        """
    )
    assert _unbounded_uses(source) == [2, 6]


def test_selftest_stays_quiet_on_the_bounded_and_async_clients():
    source = textwrap.dedent(
        """
        from app.core.redis import get_async_redis, get_request_redis
        from app.core import redis as redis_core

        def handler():
            return redis_core.get_request_redis().get("k") or get_request_redis()
        """
    )
    assert _unbounded_uses(source) == []
