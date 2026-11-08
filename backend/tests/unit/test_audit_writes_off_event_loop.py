"""An async handler never writes an audit event on the event loop (audit M8).

``AuditLogger.log`` writes the log line and then, with ``AUDIT_LOG_TO_OPENSEARCH`` on (the
default), indexes the event into OpenSearch with a blocking HTTP call. Called inline from an
``async def``, that round trip stalls every other request and stream the process is serving;
``logout`` and ``logout_all_sessions`` did exactly that. Inside an async function an audit
write must be handed to a worker thread (``await run_in_threadpool(audit_logger.log, ...)``).

The scan walks every ``async def`` under ``app/`` and reports a direct call to an
``audit_logger`` method or to ``record_audit()``. A call passed BY NAME to
``run_in_threadpool`` is not a call, and a nested ``def`` runs wherever its caller sends it,
so neither is reported.
"""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Iterator
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"


def _inline_audit_calls(source: str) -> Iterator[tuple[str, int]]:
    tree = ast.parse(source)
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef):
            continue
        stack: list[ast.AST] = list(fn.body)
        while stack:
            node = stack.pop()
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                continue
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                target = node.func
                on_audit_logger = isinstance(target.value, ast.Name) and (
                    target.value.id == "audit_logger"
                )
                if on_audit_logger or target.attr == "record_audit":
                    yield fn.name, node.lineno
            stack.extend(ast.iter_child_nodes(node))


def test_no_async_function_writes_an_audit_event_inline() -> None:
    offenders = [
        f"{path.relative_to(APP)}:{line} in {name}"
        for path in sorted(APP.rglob("*.py"))
        for name, line in _inline_audit_calls(path.read_text())
    ]
    assert offenders == [], "audit writes on the event loop: " + ", ".join(offenders)


def test_the_scan_reads_the_logout_handlers() -> None:
    """Vacuity guard: the handlers this exists for are async and do write audit events."""
    source = (APP / "api" / "endpoints" / "auth" / "sessions.py").read_text()
    tree = ast.parse(source)
    async_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)}
    assert {"logout", "logout_all_sessions"} <= async_names
    assert "audit_logger.log" in source


def test_selftest_fires_on_an_inline_audit_write() -> None:
    source = textwrap.dedent(
        """
        async def handler(pending):
            audit_logger.log(event_type=1)
            pending.record_audit()
        """
    )
    assert [line for _name, line in _inline_audit_calls(source)] == [4, 3]


def test_selftest_stays_quiet_on_offloaded_and_sync_writes() -> None:
    source = textwrap.dedent(
        """
        async def handler(pending):
            await run_in_threadpool(audit_logger.log, event_type=1)
            await run_in_threadpool(pending.record_audit)

            def _later():
                audit_logger.log(event_type=2)

        def sync_handler():
            audit_logger.log(event_type=3)
        """
    )
    assert list(_inline_audit_calls(source)) == []
