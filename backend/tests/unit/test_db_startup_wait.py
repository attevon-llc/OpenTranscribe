""" "Database not up yet" and "migration failed" are different facts.

``app/main.py``'s lifespan turns any exception out of ``run_migrations()`` into
``SystemExit(1)``. That is right for a broken migration — a half-applied schema must never
serve traffic — and wrong for a database that is merely still starting, which is a condition
that resolves itself in seconds.

Measured 2026-09-09 on a ``--fresh`` stack (the isolated one the visual-baseline tool stands
up): postgres started at 10:11:43, the backend at 10:13:17, and the backend still died with::

    psycopg2.OperationalError: connection to server at "postgres" (192.168.0.7), port 5432
    failed: Connection refused

That is the first-init race. On a **new volume** the official postgres entrypoint runs a
temporary server with TCP listening disabled while ``initdb`` and the bootstrap SQL run, so
``pg_isready`` answers over the unix socket — satisfying ``depends_on: service_healthy`` —
while TCP is still refused.

⚠️ **And the failure is permanent, not transient.** The dev backend runs ``uvicorn --reload``,
whose parent process holds the listening socket, so when the app aborts the container stays
**Up** with its port open and answering nothing (``backend/tests/CLAUDE.md`` documents this as
the blank-SPA shape). Compose's ``restart`` policy never fires, because the main process did
not exit. Observed::

    otfresh-visual-backend   Up 13 minutes (unhealthy)

so the entire stack start failed and the capture never ran.

⚠️ **A bounded wait, never infinite.** A database that is genuinely unreachable must still fail
loudly and quickly enough to be diagnosable, and the original ``OperationalError`` is re-raised
so the log names the real cause rather than a timeout of our own invention.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.db import migrations


def _refused(attempt: int) -> OperationalError:
    """An OperationalError shaped like the one Postgres' first-init race produces."""
    return OperationalError(
        f"SELECT 1 (attempt {attempt})",
        {},
        Exception('connection to server at "postgres", port 5432 failed: Connection refused'),
    )


class _FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *_a, **_kw):
        return None


class _FakeEngine:
    """Engine whose ``connect`` refuses ``fail_times`` times, then succeeds."""

    made: list[_FakeEngine] = []

    def __init__(self, fail_times: int, counter: dict):
        self.fail_times = fail_times
        self.counter = counter
        self.disposed = False

    def connect(self):
        self.counter["attempts"] += 1
        if self.counter["attempts"] <= self.fail_times:
            raise _refused(self.counter["attempts"])
        return _FakeConn()

    def dispose(self):
        self.disposed = True


@pytest.fixture
def engine_factory(monkeypatch: pytest.MonkeyPatch):
    """Patch create_engine in the migrations module and record the engines it builds."""

    def _install(fail_times: int):
        counter = {"attempts": 0}
        engines: list[_FakeEngine] = []

        def fake_create_engine(*_a, **_kw):
            eng = _FakeEngine(fail_times, counter)
            engines.append(eng)
            return eng

        monkeypatch.setattr(migrations, "create_engine", fake_create_engine)
        monkeypatch.setattr(migrations, "_DB_STARTUP_POLL_S", 0.0)
        return counter, engines

    return _install


def test_it_retries_a_refused_connection_instead_of_aborting(engine_factory):
    """The load-bearing behaviour: a transient refusal must not propagate.

    Before this existed, the FIRST OperationalError reached main.py's lifespan and became
    SystemExit(1) — a permanently unhealthy container from a condition that clears in seconds.
    """
    counter, _ = engine_factory(fail_times=3)

    migrations._await_database_available(budget_s=30.0)

    assert counter["attempts"] == 4, (
        "expected 3 refusals then a success; the helper is not retrying "
        f"(attempts={counter['attempts']})"
    )


def test_it_gives_up_and_reraises_the_real_error(engine_factory):
    """An unreachable database is still fatal, and the log must name the actual cause.

    Swallowing this, or replacing it with a generic timeout, would trade a diagnosable
    connectivity failure for a mystery.
    """
    engine_factory(fail_times=10_000)

    with pytest.raises(OperationalError) as excinfo:
        migrations._await_database_available(budget_s=0.05)

    assert "Connection refused" in str(excinfo.value), (
        "the re-raised error no longer carries the underlying cause, so an operator sees a "
        "timeout instead of the connectivity problem that produced it"
    )


def test_a_ready_database_costs_one_attempt(engine_factory):
    """Must-stay-clean: the common case must not pay a poll interval."""
    counter, _ = engine_factory(fail_times=0)

    migrations._await_database_available(budget_s=30.0)

    assert counter["attempts"] == 1, (
        f"a ready database should connect on the first attempt, took {counter['attempts']}"
    )


def test_every_probe_engine_is_disposed(engine_factory):
    """Each retry builds an engine; leaking them would exhaust connections on a slow start."""
    _, engines = engine_factory(fail_times=3)

    migrations._await_database_available(budget_s=30.0)

    assert engines, "no probe engine was created — the helper is not using create_engine"
    undisposed = [i for i, e in enumerate(engines) if not e.disposed]
    assert not undisposed, f"probe engine(s) {undisposed} were never disposed"


def test_a_zero_budget_disables_the_wait(engine_factory):
    """An orchestrated deploy with its own readiness gate must be able to opt out."""
    counter, _ = engine_factory(fail_times=10_000)

    migrations._await_database_available(budget_s=0)

    assert counter["attempts"] == 0, (
        "budget_s=0 still probed the database; the opt-out does not work"
    )


def test_run_migrations_waits_before_taking_the_advisory_lock(monkeypatch: pytest.MonkeyPatch):
    """Ordering matters: the lock connection is itself a connection that can be refused.

    Waiting *after* building the lock engine would leave the original race wide open, since
    ``lock_engine.connect()`` is the first thing ``run_migrations`` does.
    """
    calls: list[str] = []

    monkeypatch.setattr(
        migrations, "_await_database_available", lambda *a, **k: calls.append("waited")
    )

    def fake_create_engine(*_a, **_kw):
        calls.append("create_engine")
        raise RuntimeError("stop here — ordering is all this test needs")

    monkeypatch.setattr(migrations, "create_engine", fake_create_engine)

    with pytest.raises(RuntimeError):
        migrations.run_migrations()

    assert calls and calls[0] == "waited", (
        f"run_migrations touches the database before waiting for it: {calls}"
    )
