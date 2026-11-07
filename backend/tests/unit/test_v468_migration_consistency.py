"""v468 migration + detection-arm consistency (the organization owner).

The backfill, idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch
database (precedent: ``test_v467_migration_consistency.py``), so no DDL touches the shared suite
database; each takes the suite DDL lock EXCLUSIVE, which is what ``test_ddl_marker_discipline``
looks for in a test that runs DDL without ``db_session``.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v468_add_organization_owner"
_VERSIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
_PATH = _VERSIONS / f"{REVISION}.py"

#: The tables the backfill reads, reduced to the columns it touches.
_BASE_SQL = """
CREATE TABLE "user" (id serial PRIMARY KEY);
CREATE TABLE organization (id serial PRIMARY KEY, name text);
CREATE TABLE organization_membership (
    id serial PRIMARY KEY,
    organization_id integer NOT NULL REFERENCES organization (id) ON DELETE CASCADE,
    user_id integer NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    role varchar(20) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO "user" (id) SELECT g FROM generate_series(1, 6) g;
"""


def _module(revision: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(revision, _VERSIONS / f"{revision}.py")
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    name = f"ot_v468_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _member(conn, org: int, user: int, role: str, at: str) -> None:
    conn.execute(
        text(
            "INSERT INTO organization_membership (organization_id, user_id, role, created_at) "
            "VALUES (:o, :u, :r, :t)"
        ),
        {"o": org, "u": user, "r": role, "t": at},
    )


def _owners(conn) -> dict[int, int | None]:
    return dict(conn.execute(text("SELECT id, owner_user_id FROM organization")).all())


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_VERSIONS.parent))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v467_add_content_hold_authority"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v468_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_orm_mirrors_the_owner_column(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("organization")}
    assert set(live) == set(Base.metadata.tables["organization"].columns.keys())
    assert live["owner_user_id"]["nullable"] is True


# --- private scratch database ----------------------------------------------------------------


def test_the_backfill_names_the_earliest_admin_and_the_upgrade_is_idempotent():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text("INSERT INTO organization (id) VALUES (1), (2), (3), (4)"))
            # Org 1: a member joined first, then two admins; the earlier admin owns it.
            _member(conn, 1, 1, "org:member", "2026-01-01")
            _member(conn, 1, 3, "org:admin", "2026-03-01")
            _member(conn, 1, 2, "org:admin", "2026-02-01")
            # Org 2: same instant, the lower membership id wins.
            _member(conn, 2, 5, "org:admin", "2026-02-01")
            _member(conn, 2, 4, "org:admin", "2026-02-01")
            # Org 3: members only. Org 4: no memberships at all.
            _member(conn, 3, 1, "org:member", "2026-01-01")

            conn.execute(text(module.UPGRADE_SQL))
            first = _owners(conn)
            conn.execute(text(module.UPGRADE_SQL))

            assert first == {1: 2, 2: 5, 3: None, 4: None}
            assert _owners(conn) == first


def test_an_existing_owner_is_never_overwritten_by_the_backfill():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text("INSERT INTO organization (id) VALUES (1)"))
            _member(conn, 1, 2, "org:admin", "2026-02-01")
            _member(conn, 1, 3, "org:admin", "2026-03-01")
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text("UPDATE organization SET owner_user_id = 3"))
            conn.execute(text(module.UPGRADE_SQL))

            assert _owners(conn) == {1: 3}


def test_deleting_the_owners_account_clears_ownership_and_keeps_the_organization():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text("INSERT INTO organization (id) VALUES (1)"))
            _member(conn, 1, 2, "org:admin", "2026-02-01")
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text('DELETE FROM "user" WHERE id = 2'))

            assert _owners(conn) == {1: None}


def test_the_downgrade_drops_the_column_and_the_constraint_and_is_repeatable():
    module = _module(REVISION)
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch, scratch.begin() as conn:
            conn.execute(text(_BASE_SQL))
            conn.execute(text("INSERT INTO organization (id) VALUES (1)"))
            conn.execute(text(module.UPGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))
            conn.execute(text(module.DOWNGRADE_SQL))

            assert "owner_user_id" not in {
                c["name"] for c in inspect(conn).get_columns("organization")
            }
            assert not conn.execute(
                text("SELECT 1 FROM pg_constraint WHERE conname = 'fk_organization_owner_user'")
            ).first()
            conn.execute(text(module.UPGRADE_SQL))
            assert _owners(conn) == {1: None}
