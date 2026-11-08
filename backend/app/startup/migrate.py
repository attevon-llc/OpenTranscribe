"""One-shot migrate step: ``python -m app.startup.migrate``.

Runs everything that must happen once per deploy, before any API replica serves:
``run_migrations()`` (Alembic, plus the post-upgrade work it owns: the v230 repair, the
concurrent index retries, the role reconcile and the idle-transaction backstop) and the
initial-data seed. Then it exits, so it fits a Compose one-shot service
(``depends_on: condition: service_completed_successfully``), a Kubernetes Job or a Helm
pre-upgrade hook. API replicas started with ``RUN_MIGRATIONS_ON_STARTUP=false`` never
migrate; ``/health/ready`` keeps them out of rotation until the schema is at head.

Idempotent, and safe to run twice at once: the runner's advisory lock serialises them and
the second finds the database at head. Exit codes are stable, so an orchestrator can tell
the failures apart:

* ``0`` — the schema is at head and the seed ran;
* ``1`` — a migration failed (the schema may be part-way; do not start the API);
* ``3`` — another session held the migration lock past ``MIGRATION_LOCK_TIMEOUT_S``;
* ``4`` — the database did not accept connections within ``DB_STARTUP_WAIT_S``.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence

from alembic.util.exc import CommandError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger("app.startup.migrate")

EXIT_OK = 0
EXIT_MIGRATION_FAILED = 1
EXIT_LOCK_TIMEOUT = 3
EXIT_DATABASE_UNREACHABLE = 4


def seed_initial_data() -> None:
    """Seed the admin user, default tags and system prompts (idempotent, never raises).

    A seeding failure is logged and does not fail the deploy: the schema is what the API
    cannot run without, and the lifespan's elected seed retries it on the next boot.
    """
    try:
        from app.db.base import SessionLocal
        from app.initial_data import init_db

        db = SessionLocal()
        try:
            init_db(db)
        finally:
            db.close()
    except (SQLAlchemyError, ValueError, RuntimeError, OSError) as e:
        logger.warning(f"Initial data seeding failed (non-fatal): {e}")


def run(seed: bool = True) -> int:
    """Run the migrate step and return its exit code (see the module docstring).

    Args:
        seed: Also seed the initial data after the schema is at head.

    Returns:
        The process exit code.
    """
    from app.db import migrations

    logger.info("Migrate step starting")
    try:
        migrations._await_database_available()
    except OperationalError as e:
        logger.critical(f"Migrate step: database unreachable, nothing was changed: {e}")
        return EXIT_DATABASE_UNREACHABLE

    try:
        migrations.run_migrations()
    except migrations.MigrationLockTimeoutError as e:
        logger.critical(f"Migrate step: {e}")
        return EXIT_LOCK_TIMEOUT
    except (SQLAlchemyError, CommandError):
        # Anything else propagates: the interpreter exits 1 with the traceback, which is
        # the same "migration failed" code.
        logger.critical(
            "Migrate step: migration FAILED — do not start the API on this database",
            exc_info=True,
        )
        return EXIT_MIGRATION_FAILED

    if seed:
        seed_initial_data()
    logger.info("Migrate step complete: schema at head")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, configure logging and run the step."""
    parser = argparse.ArgumentParser(
        prog="python -m app.startup.migrate",
        description="Apply database migrations and once-per-deploy setup, then exit.",
    )
    parser.add_argument(
        "--no-seed",
        action="store_true",
        help="skip seeding the admin user, default tags and system prompts",
    )
    args = parser.parse_args(argv)

    from app.core.logging_config import configure_logging

    configure_logging()
    return run(seed=not args.no_seed)


if __name__ == "__main__":
    sys.exit(main())
