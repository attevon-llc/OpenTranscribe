"""Statement counting for N+1 regression checks (issue #1337).

A thin shared wrapper over SQLAlchemy's documented ``before_cursor_execute`` event
(https://docs.sqlalchemy.org/en/20/core/events.html#sqlalchemy.events.ConnectionEvents.before_cursor_execute),
the mechanism ``test_privacy_purge_opensearch_scope.py`` already uses ad hoc. Not reusable from
``app.core.db_metrics``: that counter is registered on the app engine and keyed per request,
while tests run on their own engine and connection (``tests/conftest.py``).

Assert that the count is EQUAL across two sizes of the driving entity, not that it is below a
magic number: equality survives a legitimate new query, a ceiling does not. Always add a control
assertion that the response really held both sizes' rows, so an empty result cannot pass.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.engine import Engine

# The savepoint dance of the db_session fixture and its advisory-lock calls are harness noise.
_HARNESS = re.compile(
    r"^\s*(SAVEPOINT|RELEASE SAVEPOINT|ROLLBACK TO SAVEPOINT)\b|pg_advisory", re.IGNORECASE
)


@dataclass
class StatementLog:
    """Statements executed while a ``count_statements`` block was open."""

    statements: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.statements)

    def matching(self, pattern: str) -> list[str]:
        regex = re.compile(pattern, re.IGNORECASE | re.DOTALL)
        return [s for s in self.statements if regex.search(s)]

    def describe(self) -> str:
        """Numbered statements, for an assertion message."""
        return "\n".join(f"{i:3d}. {s[:200]!r}" for i, s in enumerate(self.statements, 1))


@contextmanager
def count_statements(
    *targets: Engine | Connection,
    include_app_engine: bool = True,
    cold_auth_cache: bool = True,
) -> Iterator[StatementLog]:
    """Record every statement executed on ``targets`` (plus the app engine) inside the block.

    ``targets`` is normally ``db_session.connection()``. About 20 app paths open their own
    ``SessionLocal()`` during a request, so the app engine is listened to as well; a statement
    seen by both an engine and one of its connections is counted once.

    ``cold_auth_cache=False`` keeps the process auth/banner cache as the previous block left it,
    for a test whose subject is the warm-cache path itself.
    """
    # The login-banner gate answers from a process-level cache after its first read, so the
    # first block would count one more statement than a later one. Start every block cold.
    from app.core.process_auth_settings import clear_process_auth_settings_cache

    if cold_auth_cache:
        clear_process_auth_settings_cache()
    listen_on: list[Engine | Connection] = list(targets)
    if include_app_engine:
        from app.db.base import engine as app_engine

        listen_on.append(app_engine)

    log = StatementLog()
    seen: list[object] = []  # strong refs so ids cannot be reused while the block is open

    def record(conn, cursor, statement, parameters, context, executemany):
        if any(context is s for s in seen):
            return
        seen.append(context)
        if _HARNESS.search(statement):
            return
        log.statements.append(statement)

    for target in listen_on:
        event.listen(target, "before_cursor_execute", record)
    try:
        yield log
    finally:
        for target in listen_on:
            if event.contains(target, "before_cursor_execute", record):
                event.remove(target, "before_cursor_execute", record)
