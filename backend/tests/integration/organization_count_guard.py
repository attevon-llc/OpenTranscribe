"""Fail an integration session that leaves the shared database with a different number of organizations.

Integration tests commit for real (the code under test runs in another session or process),
and the stack's database is shared by every suite pointed at it. One committed
``organization`` row turns that whole deployment multi-tenant: while
``test_deletion_residue_live`` seeded an organization per file, single-tenant tests in other
suites (support access, trash permissions, org-admin GDPR, admin search over quarantine) failed
for as long as the row existed, and a run killed before its teardown left it there for good.

The session records the count before the first test and compares it after the last teardown,
in the xdist controller only (it starts before and finishes after every worker), so a fixture
that creates and removes its organization nets to zero and passes. A difference fails the
session by exit status with the delta named. The comparison cannot tell this session's rows
from another process's: if something else on the same database created or removed an
organization meanwhile, the message says so, and the answer is still to look.
"""

from __future__ import annotations

from typing import Any

import pytest

_BEFORE = pytest.StashKey[int | None]()


def organization_count() -> int | None:
    """Organizations in the stack's database, or None when it cannot be asked."""
    from sqlalchemy import create_engine
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.config import settings

    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SELECT count(*) FROM organization")).scalar_one())
    except SQLAlchemyError:
        return None
    finally:
        engine.dispose()


def _is_xdist_worker(config: pytest.Config) -> bool:
    return hasattr(config, "workerinput")


def record_before(session: pytest.Session) -> None:
    if not _is_xdist_worker(session.config):
        session.config.stash[_BEFORE] = organization_count()


def check_after(session: pytest.Session) -> str | None:
    """Fail the session if the count moved; returns the message it reported, if any."""
    if _is_xdist_worker(session.config):
        return None
    before = session.config.stash.get(_BEFORE, None)
    after = organization_count()
    if before is None or after is None or before == after:
        return None
    message = (
        f"organization rows in the shared database went from {before} to {after} during this "
        "integration session. A committed organization makes the whole deployment multi-tenant "
        "for every other suite on it; find the test that left it (or, if another process on "
        "this database was running, rule that out) and remove it in a finally."
    )
    reporter: Any = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(f"ORGANIZATION COUNT GUARD: {message}", red=True)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
    return message
