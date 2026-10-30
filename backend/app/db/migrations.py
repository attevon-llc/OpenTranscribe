"""
Database migration utilities.

Runs Alembic migrations automatically on application startup.
Alembic is the sole authority for database schema creation and upgrades.
Handles empty databases, existing untracked databases, and tracked databases.
"""

import functools
import logging
import os
import time
from pathlib import Path

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from alembic import command  # type: ignore[attr-defined]
from app.core.config import settings
from app.db.schema_detect.ladder import LADDER
from app.db.schema_detect.ladder import collect_facts

logger = logging.getLogger(__name__)


def get_alembic_config() -> Config:
    """Get Alembic configuration."""
    # Find alembic.ini relative to backend directory
    backend_dir = Path(__file__).parent.parent.parent
    alembic_ini = backend_dir / "alembic.ini"

    config = Config(str(alembic_ini))
    config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
    return config


@functools.lru_cache(maxsize=1)
def get_alembic_head() -> str | None:
    """Head revision of the migration scripts shipped with this build, parsed once per process.

    Loading the script directory imports every revision file (~45-50 ms of CPU), and the
    answer cannot change while the process runs — new revisions arrive with a new build.
    ``/health/ready`` asks on every probe (issue #1000), so it must not pay that each time.
    """
    from alembic.script import ScriptDirectory

    head: str | None = ScriptDirectory.from_config(get_alembic_config()).get_current_head()
    return head


def _detect_schema_version(conn, tables: list[str]) -> str | None:
    """Detect the schema version of an existing untracked database.

    Returns the Alembic revision to stamp, or None if no user table exists. Every probe runs
    first; then the newest-first ladder returns the FIRST arm that matches (see
    ``app/db/schema_detect/ladder.py``), and ``v010_baseline`` when none does.
    """
    if "user" not in tables:
        return None

    facts = collect_facts(conn, tables)
    for matches, revision in LADDER:
        if matches(facts):
            return revision
    return "v010_baseline"


def _repair_skipped_v230(config) -> None:
    """Apply v230 schema changes if they were skipped due to branch merge.

    The v230_add_auto_labeling migration may have been skipped on databases
    that were upgraded through the v250→v260→v270 branch before the migration
    chain was linearised. All SQL is idempotent (IF NOT EXISTS).
    """

    engine = create_engine(settings.DATABASE_URL)
    try:
        with engine.connect() as conn:
            missing = not _check_column_exists(conn, "media_file", "upload_batch_id")
        if not missing:
            return

        logger.info("Detected missing v230 schema changes — applying repair...")
        with engine.connect() as conn:
            # Import and run the v230 upgrade function directly
            # Run inside an alembic operation context
            from alembic.operations import Operations
            from alembic.runtime.migration import MigrationContext

            from alembic.versions.v230_add_auto_labeling import upgrade as v230_upgrade

            mc = MigrationContext.configure(conn)
            ops = Operations(mc)  # noqa: F841
            v230_upgrade()
            conn.commit()
        logger.info("v230 repair complete")
    except Exception:
        # Fall back to raw SQL for the critical missing column
        logger.warning("v230 module import failed, applying critical columns directly")
        with engine.connect() as conn:
            conn.execute(
                text("""
                CREATE TABLE IF NOT EXISTS upload_batch (
                    id SERIAL PRIMARY KEY,
                    uuid UUID UNIQUE NOT NULL DEFAULT gen_random_uuid(),
                    user_id INTEGER NOT NULL REFERENCES "user"(id) ON DELETE CASCADE,
                    source VARCHAR(50) NOT NULL,
                    file_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ DEFAULT NOW(),
                    grouping_status VARCHAR(50) DEFAULT 'pending'
                )
            """)
            )
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'media_file' AND column_name = 'upload_batch_id'
                    ) THEN
                        ALTER TABLE media_file
                        ADD COLUMN upload_batch_id INTEGER
                        REFERENCES upload_batch(id) ON DELETE SET NULL;
                    END IF;
                END $$
            """)
            )
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_media_file_upload_batch_id "
                    "ON media_file(upload_batch_id)"
                )
            )
            # tag columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tag' AND column_name = 'source')
                    THEN ALTER TABLE tag ADD COLUMN source VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'tag' AND column_name = 'normalized_name')
                    THEN ALTER TABLE tag ADD COLUMN normalized_name VARCHAR;
                    END IF;
                END $$
            """)
            )
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_tag_normalized_name ON tag(normalized_name)")
            )
            # file_tag columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'file_tag' AND column_name = 'source')
                    THEN ALTER TABLE file_tag ADD COLUMN source VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'file_tag' AND column_name = 'ai_confidence')
                    THEN ALTER TABLE file_tag ADD COLUMN ai_confidence FLOAT;
                    END IF;
                END $$
            """)
            )
            # collection columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'collection' AND column_name = 'source')
                    THEN ALTER TABLE collection ADD COLUMN source VARCHAR(50);
                    END IF;
                END $$
            """)
            )
            # collection_member columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'collection_member' AND column_name = 'source')
                    THEN ALTER TABLE collection_member ADD COLUMN source VARCHAR(50);
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'collection_member' AND column_name = 'ai_confidence')
                    THEN ALTER TABLE collection_member ADD COLUMN ai_confidence FLOAT;
                    END IF;
                END $$
            """)
            )
            # topic_suggestion columns
            conn.execute(
                text("""
                DO $$ BEGIN
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'topic_suggestion' AND column_name = 'auto_applied_tags')
                    THEN ALTER TABLE topic_suggestion ADD COLUMN auto_applied_tags JSONB DEFAULT '[]'::jsonb;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'topic_suggestion' AND column_name = 'auto_applied_collections')
                    THEN ALTER TABLE topic_suggestion ADD COLUMN auto_applied_collections JSONB DEFAULT '[]'::jsonb;
                    END IF;
                    IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'topic_suggestion' AND column_name = 'auto_apply_completed_at')
                    THEN ALTER TABLE topic_suggestion ADD COLUMN auto_apply_completed_at TIMESTAMPTZ;
                    END IF;
                END $$
            """)
            )
            # Backfill normalized_name
            conn.execute(
                text("""
                UPDATE tag
                SET normalized_name = LOWER(TRIM(REGEXP_REPLACE(
                    REGEXP_REPLACE(name, '[-_]+', ' ', 'g'), '\\s+', ' ', 'g')))
                WHERE normalized_name IS NULL
            """)
            )
            conn.commit()
        logger.info("v230 repair (direct SQL) complete")
    finally:
        engine.dispose()


def _check_column_exists(conn, table: str, column: str) -> bool:
    """Check if a column exists in a table."""
    result = conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column)"
        ),
        {"table": table, "column": column},
    )
    return bool(result.scalar())


#: How long to wait for Postgres to accept TCP before treating it as a real failure.
#: Overridable so an orchestrated deploy with its own readiness gate can set it to 0.
_DB_STARTUP_WAIT_S = float(os.environ.get("DB_STARTUP_WAIT_S", "120"))

#: Gap between connection attempts while waiting.
_DB_STARTUP_POLL_S = 2.0


def _await_database_available(budget_s: float | None = None) -> None:
    """Block until Postgres accepts a connection, or the budget expires.

    ⚠️ **"The database is not up yet" and "the migration failed" are different facts, and
    conflating them costs a deployment.** ``main.py``'s lifespan turns any exception out of
    ``run_migrations()`` into ``SystemExit(1)`` — correct for a broken migration, which must
    never serve a half-applied schema, and wrong for a database that is simply still starting.

    Measured 2026-09-09 on a ``--fresh`` stack: postgres started at 10:11:43, the backend at
    10:13:17, and the backend still died with ``connection to server at "postgres" … port
    5432 failed: Connection refused``. That is the well-known first-init race — on a **new
    volume** the official postgres entrypoint runs a temporary server with TCP listening
    disabled while ``initdb`` and the bootstrap SQL run, so ``pg_isready`` can answer over the
    unix socket (satisfying ``depends_on: service_healthy``) while TCP is still refused.

    The failure is permanent rather than transient because of the reloader trap documented in
    ``backend/tests/CLAUDE.md``: the dev backend runs ``uvicorn --reload``, whose parent holds
    the listening socket, so the container stays **Up** with its port open and answering
    nothing. Compose's ``restart`` never fires (the main process did not exit), the healthcheck
    fails forever, and the whole stack start fails. That is what it looks like:

        otfresh-visual-backend   Up 13 minutes (unhealthy)     # healthcheck exit 7, empty body

    A budget rather than infinite retries: a database that is genuinely unreachable must still
    fail loudly, and the last exception is re-raised so the log names the real cause.
    """
    budget = _DB_STARTUP_WAIT_S if budget_s is None else budget_s
    if budget <= 0:
        return

    deadline = time.monotonic() + budget
    started = time.monotonic()
    attempt = 0
    while True:
        attempt += 1
        probe = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
        try:
            with probe.connect() as conn:
                conn.execute(text("SELECT 1"))
            waited = time.monotonic() - started
            if attempt > 1:
                logger.info(
                    "Database accepted connections after %.1fs (%d attempt(s))", waited, attempt
                )
            return
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                logger.critical(
                    "Database still not accepting connections after %.0fs (%d attempts) — "
                    "giving up. This is a real connectivity failure, not a slow start.",
                    time.monotonic() - started,
                    attempt,
                )
                raise
            if attempt == 1:
                logger.info(
                    "Database not accepting connections yet (%s) — waiting up to %.0fs. "
                    "Normal on a fresh volume: initdb runs with TCP disabled.",
                    type(exc).__name__,
                    budget,
                )
            time.sleep(_DB_STARTUP_POLL_S)
        finally:
            probe.dispose()


def run_migrations() -> None:
    """Run database migrations on startup.

    Alembic is the sole authority for database schema. Handles:
    1. Empty database: Run all migrations from scratch (alembic upgrade head)
    2. Existing untracked DB: Detect version, stamp, then upgrade
    3. Already tracked: Apply any pending migrations

    Uses a PostgreSQL advisory lock to prevent concurrent migration runs
    when multiple backend instances start simultaneously.
    """

    logger.info("Checking database migrations...")

    _await_database_available()

    engine = create_engine(settings.DATABASE_URL)

    # Acquire the advisory lock on a DEDICATED connection held open for the whole
    # upgrade (issue #284 A1.4).
    #
    # pg_advisory_lock is SESSION-scoped. The previous code took it on a pooled
    # connection, returned that connection to the pool, then called engine.dispose()
    # below — closing the session and silently dropping the lock BEFORE
    # command.upgrade() ever ran. Concurrent replicas could therefore race Alembic.
    # The matching unlock in the finally used a *fresh* connection, which is a no-op
    # for the same reason: a session cannot release a lock it does not hold.
    #
    # A separate engine is used so the engine.dispose() further down (which exists to
    # free pooled connections before Alembic opens its own) cannot close this one.
    lock_engine = create_engine(settings.DATABASE_URL)
    lock_conn = lock_engine.connect()
    lock_conn.execute(text("SELECT pg_advisory_lock(42)"))
    lock_conn.commit()
    logger.info("Acquired migration advisory lock")

    try:
        # Ensure alembic_version column is wide enough for long revision IDs
        with engine.connect() as conn:
            if "alembic_version" in inspect(engine).get_table_names():
                conn.execute(
                    text("ALTER TABLE alembic_version ALTER COLUMN version_num TYPE VARCHAR(128)")
                )
                conn.commit()

        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            current_rev = context.get_current_revision()
            inspector = inspect(engine)
            tables = inspector.get_table_names()
            detected_version = _detect_schema_version(conn, tables)

        # Dispose the engine to release all pooled connections before Alembic opens its own
        engine.dispose()

        config = get_alembic_config()

        head_rev = get_alembic_head()

        if current_rev:
            logger.info(f"Current migration version: {current_rev}")
            if current_rev == head_rev:
                logger.info("Database is up to date, no migrations needed")
            else:
                logger.info(f"Upgrading from {current_rev} to {head_rev}...")
                command.upgrade(config, "head")
        elif detected_version:
            logger.info(f"Existing database detected, stamping {detected_version}...")
            command.stamp(config, detected_version)
            if detected_version != head_rev:
                logger.info("Applying migrations to upgrade to current version...")
                command.upgrade(config, "head")
        elif tables:
            logger.info("Existing untracked database detected, stamping current version...")
            command.stamp(config, "head")
        else:
            logger.info("Empty database detected, running full migration...")
            command.upgrade(config, "head")

        # Post-migration repair: apply any idempotent schema changes from v230
        # that may have been skipped due to a branch merge ordering issue.
        _repair_skipped_v230(config)

        logger.info("Database migrations complete")
    finally:
        # Release from the SAME session that took it — advisory locks are
        # session-scoped, so unlocking from anywhere else does nothing.
        try:
            lock_conn.execute(text("SELECT pg_advisory_unlock(42)"))
            lock_conn.commit()
            logger.info("Released migration advisory lock")
        except Exception as exc:  # noqa: BLE001 - closing the session releases it anyway
            logger.warning("Advisory unlock failed (%s); closing the session releases it", exc)
        finally:
            lock_conn.close()
            lock_engine.dispose()
