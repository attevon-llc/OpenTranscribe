"""Configuration of the idle-in-transaction backstop (issue #440).

The live-server proof that Postgres actually terminates an idle transaction
lives in ``tests/integration/test_idle_in_transaction_backstop.py`` — it must
sit under ``tests/integration/`` because that is where
``run-integration-tests.sh`` collects from. ``tests/unit/test_gate_phase_
coverage.py`` fails the build when an ``@pytest.mark.integration`` module sits
anywhere else, which is exactly how this file got split: the live tests were
written here first and would have been collected by nothing.

What stays here is pure and needs no database:

* The shared app engine sends no libpq ``options`` and carries the connect-event ``SET`` — the gap where a correct helper is
  written and never wired is the failure this audit opened with.
* The shipped default is short enough to have caught the 48-minute leak.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine

from app.core.config import DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS
from app.db.base import connect_args as app_connect_args
from app.db.base import engine as app_engine
from app.db.migration_connection import SessionSet
from app.db.migration_connection import install_idle_in_transaction_timeout


def test_the_app_engine_sends_no_libpq_startup_options() -> None:
    """Proxies (RDS Proxy, PgBouncer) reject ``options=-c``; the backstop must not use it."""
    assert "options" not in app_connect_args


def test_the_app_engine_applies_the_timeout_in_a_connect_event() -> None:
    """The timeout is a pool ``connect`` listener on the shared engine, not just a helper."""
    if not app_engine.pool.dispatch.connect:
        pytest.fail("shared engine has no pool connect listener, so no backstop is applied")
    from app.core.config import settings

    if settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS <= 0:
        pytest.skip("backstop disabled in this environment")
    sql = [
        fn.statement
        for fn in app_engine.pool.dispatch.connect
        if isinstance(fn, SessionSet) and "idle_in_transaction_session_timeout" in fn.statement
    ]
    assert sql == [
        f"SET idle_in_transaction_session_timeout = {settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS}"
    ]


@pytest.mark.parametrize("disabled", [0, -1])
def test_disabled_installs_no_listener(disabled: int) -> None:
    url = "postgresql+psycopg2://u:p@localhost/x"
    baseline = len(create_engine(url).pool.dispatch.connect)
    engine = create_engine(url)
    install_idle_in_transaction_timeout(engine, disabled)
    assert len(engine.pool.dispatch.connect) == baseline
    install_idle_in_transaction_timeout(engine, 1000)
    assert len(engine.pool.dispatch.connect) == baseline + 1


def test_the_shipped_default_is_shorter_than_the_leak_it_backstops() -> None:
    """A backstop longer than the observed pathology would not have caught it.

    The leak in issue #440 held a transaction idle for 48+ minutes. Pinning an
    upper bound means raising the default silently past that is a test failure
    rather than a config review nobody runs.

    Asserts the module CONSTANT, not ``settings.*``. An earlier version read the
    resolved setting and therefore failed whenever an operator legitimately set
    ``DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=0`` — turning a supported configuration
    into a broken test suite, which is how a control ends up disabled for real.
    """
    assert 0 < DEFAULT_DB_IDLE_IN_TRANSACTION_TIMEOUT_MS <= 15 * 60 * 1000
