"""Sessions whose ``commit()`` / ``rollback()`` behave as they do in production.

Opt-in fixtures, registered from the root conftest via ``pytest_plugins``. Use them
(never ``db_session``) in a test that has to observe what a ``rollback()`` undoes.
"""

from __future__ import annotations

import pytest

from app.db.base import SessionLocal
from app.db.base import engine
from tests.db_locks import acquire_ddl_lock_shared


@pytest.fixture(scope="function")
def production_txn_session():
    """A session whose ``commit()``/``rollback()`` mean what they mean in production.

    **The shared ``db_session`` fixture cannot be used to test a rollback**, and the
    reason is not obvious enough to rediscover a third time.

    ``db_session`` binds a Session to a Connection that already has a *plain*
    transaction open (``connection.begin()``). SQLAlchemy 2.0's default
    ``join_transaction_mode="conditional_savepoint"`` resolves to ``"rollback_only"``
    in that situation — a savepoint is only created when the incoming Connection is
    itself inside one. Under ``rollback_only`` the Session's **root** transaction *is*
    the externally-begun connection transaction, and the two halves are asymmetric:

    * ``session.commit()`` deliberately does **not** commit the outer transaction, so
      test data stays inside it and the fixture can undo everything at teardown; but
    * ``session.rollback()`` always rolls back the **topmost** transaction — which
      here is that same outer transaction. It emits a real ``ROLLBACK``, discarding
      every row the test created, including ones "committed" long before.

    Measured against this fixture: two rows inserted and committed, one more row
    flushed, then ``rollback()`` — and all three vanish, with
    ``connection_transaction.is_active`` flipping to False. The
    ``after_transaction_end`` restart-savepoint listener does not help; it re-creates a
    savepoint *after* the damage, and its condition never fires for the root
    transaction that was actually rolled back. No application code can be written
    around that, because production's ``session_scope()`` has the opposite (and
    correct) asymmetry: ``commit()`` is durable and ``rollback()`` only undoes work
    since the last one.

    So this fixture applies the modern SQLAlchemy 2.0 recipe for joining a session to
    an external transaction — an explicit ``join_transaction_mode="create_savepoint"``
    — on the real production ``SessionLocal``. The session's root transaction becomes a
    SAVEPOINT: ``commit()`` releases it and opens the next one, ``rollback()`` returns
    to it. That is exactly the production contract, while the outer transaction this
    fixture owns is still rolled back at teardown, so nothing is persisted.
    """
    connection = engine.connect()
    transaction = connection.begin()
    # This test opens its own connection, so `db_session`'s advisory lock cannot reach
    # it — take the shared form here or a `ddl_exclusive` test on another xdist worker
    # can deadlock against these `user` writes (see tests/db_locks.py).
    acquire_ddl_lock_shared(connection)
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        try:
            if transaction.is_active:
                transaction.rollback()
        finally:
            connection.close()


@pytest.fixture(scope="function")
def production_txn_view(db_session):
    """A second session on ``db_session``'s connection with production commit/rollback.

    Rows the test created through ``db_session`` are visible to it, but its
    ``rollback()`` returns to its own savepoint instead of discarding the whole
    test transaction (see ``production_txn_session`` for why ``db_session``
    cannot be used to test a rollback). Flush or commit through ``db_session``
    first.
    """
    session = SessionLocal(bind=db_session.connection(), join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
