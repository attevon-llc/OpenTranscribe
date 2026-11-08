"""``python -m app.startup.migrate`` — the one-shot migrate step (docs/design/migrate_job_plan.md).

Compose (``depends_on: condition: service_completed_successfully``) and a Kubernetes Job only
see the exit code, so each failure class must map to its documented code and a failure must
never read as 0. ``run_migrations`` and the database probe are replaced here; the lock path is
exercised against real Postgres in ``test_migration_lock_timeout.py``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import ProgrammingError

from app.db import migrations
from app.startup import migrate

BACKEND_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def calls(monkeypatch):
    """Record what the step ran; every dependency succeeds unless a test says otherwise."""
    seen: list[str] = []
    monkeypatch.setattr(migrations, "_await_database_available", lambda: seen.append("await"))
    monkeypatch.setattr(migrations, "run_migrations", lambda: seen.append("migrate"))
    monkeypatch.setattr(migrate, "seed_initial_data", lambda: seen.append("seed"))
    return seen


def test_success_migrates_then_seeds_and_exits_zero(calls):
    assert migrate.run() == migrate.EXIT_OK == 0
    assert calls == ["await", "migrate", "seed"]


def test_no_seed_flag_skips_the_seed(calls):
    assert migrate.main(["--no-seed"]) == 0
    assert calls == ["await", "migrate"]


def test_a_failed_migration_exits_one_and_does_not_seed(calls, monkeypatch):
    def boom() -> None:
        calls.append("migrate")
        raise ProgrammingError("ALTER TABLE x", {}, Exception("revision v999 failed"))

    monkeypatch.setattr(migrations, "run_migrations", boom)
    assert migrate.run() == migrate.EXIT_MIGRATION_FAILED == 1
    assert "seed" not in calls


def test_a_lock_timeout_has_its_own_exit_code(calls, monkeypatch):
    def held() -> None:
        raise migrations.MigrationLockTimeoutError("migration lock 42 still held after 900s")

    monkeypatch.setattr(migrations, "run_migrations", held)
    assert migrate.run() == migrate.EXIT_LOCK_TIMEOUT == 3
    assert "seed" not in calls


def test_an_unreachable_database_exits_four_without_migrating(calls, monkeypatch):
    def refused() -> None:
        raise OperationalError("SELECT 1", {}, Exception("Connection refused"))

    monkeypatch.setattr(migrations, "_await_database_available", refused)
    assert migrate.run() == migrate.EXIT_DATABASE_UNREACHABLE == 4
    assert calls == []


def test_the_exit_codes_are_distinct():
    codes = {
        migrate.EXIT_OK,
        migrate.EXIT_MIGRATION_FAILED,
        migrate.EXIT_LOCK_TIMEOUT,
        migrate.EXIT_DATABASE_UNREACHABLE,
    }
    assert len(codes) == 4


def test_the_module_is_runnable_and_rejects_unknown_arguments():
    """``python -m app.startup.migrate`` is the command compose runs; argparse misuse exits 2."""
    result = subprocess.run(
        [sys.executable, "-m", "app.startup.migrate", "--definitely-not-a-flag"],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert (
        "--no-seed"
        in subprocess.run(
            [sys.executable, "-m", "app.startup.migrate", "--help"],
            cwd=str(BACKEND_ROOT),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )


def test_an_unexpected_error_still_exits_non_zero():
    """A failure outside the classified ones is not swallowed into a 0 exit."""
    code = (
        "import sys; from app.startup import migrate; from app.db import migrations;"
        "migrations._await_database_available = lambda: None;"
        "migrations.run_migrations = lambda: (_ for _ in ()).throw(KeyError('boom'));"
        "sys.exit(migrate.main(['--no-seed']))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1, result.stderr
    assert "KeyError" in result.stderr
