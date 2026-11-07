"""v463 migration + detection-arm consistency (the trash state on ``media_file``, #1101).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v461_migration_consistency.py``) holding a minimal ``user`` and ``media_file``,
so no DDL touches the shared suite database; each takes the suite DDL lock EXCLUSIVE, which is
what ``test_ddl_marker_discipline`` looks for in a test that runs DDL without ``db_session``.
"""

from __future__ import annotations

import importlib.util
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import pytest
from sqlalchemy import create_engine
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.db.base import engine as shared_engine
from tests.db_locks import acquire_ddl_lock_exclusive

REVISION = "v463_add_media_file_trash"
COLUMNS = {"deleted_at", "deleted_by_user_id", "purge_after", "trash_origin"}
CONSTRAINTS = {
    "fk_media_file_deleted_by_user",
    "ck_media_file_trash_origin",
    "ck_media_file_trash_state",
}
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

_SCRATCH_BASE_SQL = """
CREATE TABLE "user" (id serial PRIMARY KEY);
CREATE TABLE media_file (id serial PRIMARY KEY, user_id integer REFERENCES "user"(id));
"""

_TRASHED = (
    "deleted_at = now(), purge_after = now() + interval '30 days', trash_origin = 'user', "
    "deleted_by_user_id = :u"
)


def _revision_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@contextmanager
def _scratch_database() -> Iterator[Engine]:
    """An empty database of our own on the test server, with a minimal user/media_file."""
    name = f"ot_v463_{uuid.uuid4().hex[:10]}_test"
    url = make_url(settings.DATABASE_URL)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    scratch = create_engine(url.set(database=name))
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        with scratch.begin() as conn:
            conn.execute(text(_SCRATCH_BASE_SQL))
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _constraint_names(conn) -> set[str]:
    return {
        r[0]
        for r in conn.execute(
            text("SELECT conname FROM pg_constraint WHERE conrelid = 'media_file'::regclass")
        )
    }


# --- no database -----------------------------------------------------------------------------


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v461_add_privacy_purge_pending"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_revision_and_the_model_agree_on_the_origins():
    from app.models.media import TRASH_ORIGINS

    assert _revision_module().TRASH_ORIGINS == TRASH_ORIGINS
    assert all(f"'{o}'" in _revision_module().UPGRADE_SQL for o in TRASH_ORIGINS)


def test_the_revision_builds_no_index_itself():
    """The purge index is built CONCURRENTLY by the runner after the upgrade, never in it."""
    assert "CREATE INDEX" not in _revision_module().UPGRADE_SQL.upper()


def test_the_spec_is_the_partial_concurrent_index_the_model_declares():
    from app.db.base import Base
    from app.db.trash_indexes import TRASH_DUE
    from app.models.media import MediaFile

    sql = TRASH_DUE.create_sql()
    assert sql == (
        "CREATE INDEX CONCURRENTLY ix_media_file_trash_due ON media_file (purge_after) "
        "WHERE deleted_at IS NOT NULL"
    )
    names = {i.name for i in Base.metadata.tables[MediaFile.__tablename__].indexes}
    assert TRASH_DUE.name in names


# --- the migrated suite database -------------------------------------------------------------


def test_detection_arm_returns_v463_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_live_columns_and_constraints_exist(db_session):
    conn = db_session.connection()
    live = {c["name"] for c in inspect(conn).get_columns("media_file")}
    assert live >= COLUMNS
    assert _constraint_names(conn) >= CONSTRAINTS


def test_a_half_trashed_row_is_refused(db_session, normal_user):
    from app.models.media import MediaFile

    test_user = normal_user
    test_media_file = MediaFile(
        filename="trash_state.wav",
        storage_path=f"media/test/{uuid.uuid4()}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="pending",
        user_id=normal_user.id,
    )
    db_session.add(test_media_file)
    db_session.flush()
    conn = db_session.connection()
    with pytest.raises(IntegrityError, match="ck_media_file_trash_state"), conn.begin_nested():
        conn.execute(
            text("UPDATE media_file SET deleted_at = now() WHERE id = :i"),
            {"i": test_media_file.id},
        )
    with pytest.raises(IntegrityError, match="ck_media_file_trash_origin"), conn.begin_nested():
        conn.execute(
            text(
                "UPDATE media_file SET deleted_at = now(), purge_after = now(), "
                "trash_origin = 'cron' WHERE id = :i"
            ),
            {"i": test_media_file.id},
        )
    with conn.begin_nested():
        conn.execute(
            text(f"UPDATE media_file SET {_TRASHED} WHERE id = :i"),
            {"i": test_media_file.id, "u": test_user.id},
        )
    assert conn.execute(
        text("SELECT deleted_at IS NOT NULL FROM media_file WHERE id = :i"),
        {"i": test_media_file.id},
    ).scalar()


# --- private scratch database: idempotent upgrade, actor deletion, downgrade -----------------


def test_the_upgrade_is_idempotent_the_actor_fk_sets_null_and_the_downgrade_removes_it_all():
    module = _revision_module()
    with shared_engine.connect() as lock_conn, lock_conn.begin():
        acquire_ddl_lock_exclusive(lock_conn)
        with _scratch_database() as scratch:
            with scratch.begin() as conn:
                conn.execute(text(module.UPGRADE_SQL))
                conn.execute(text(module.UPGRADE_SQL))
                assert _constraint_names(conn) >= CONSTRAINTS
                validated: dict[str, bool] = {
                    str(name): bool(ok)
                    for name, ok in conn.execute(
                        text(
                            "SELECT conname, convalidated FROM pg_constraint "
                            "WHERE conrelid = 'media_file'::regclass"
                        )
                    )
                }
                assert all(validated[c] for c in CONSTRAINTS), validated
                owner = conn.execute(text('INSERT INTO "user" DEFAULT VALUES RETURNING id'))
                owner_id = owner.scalar()
                actor_id = conn.execute(
                    text('INSERT INTO "user" DEFAULT VALUES RETURNING id')
                ).scalar()
                file_id = conn.execute(
                    text("INSERT INTO media_file (user_id) VALUES (:o) RETURNING id"),
                    {"o": owner_id},
                ).scalar()
                conn.execute(
                    text(f"UPDATE media_file SET {_TRASHED} WHERE id = :i"),
                    {"i": file_id, "u": actor_id},
                )
                # The trashed row outlives the account of whoever trashed it.
                conn.execute(text('DELETE FROM "user" WHERE id = :a'), {"a": actor_id})
                row = conn.execute(
                    text(
                        "SELECT deleted_at IS NOT NULL, deleted_by_user_id FROM media_file "
                        "WHERE id = :i"
                    ),
                    {"i": file_id},
                ).one()
                assert tuple(row) == (True, None)
            with scratch.begin() as conn:
                conn.execute(text(module.DOWNGRADE_SQL))
                conn.execute(text(module.DOWNGRADE_SQL))
                live = {c["name"] for c in inspect(conn).get_columns("media_file")}
                assert not (COLUMNS & live)
                assert not (CONSTRAINTS & _constraint_names(conn))
