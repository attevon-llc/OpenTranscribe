"""v395 migration + detection-arm consistency (``watch_source_file.document_id``).

Same convention as ``v394``: **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v395_add_watch_source_file_document_id"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v395_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_document(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v395.pdf', 'x/v395.pdf', 1, "
                "'application/pdf') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_watch_source(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO watch_source (uuid, name, source_type, user_id, local_path) "
                "VALUES (:u, 'v395-source', 'local', :uid, '.') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_watch_source_file(conn, source_id: int, *, document_id: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO watch_source_file (uuid, watch_source_id, remote_path, filename, "
                "document_id) VALUES (:u, :s, :p, :fn, :document_id) RETURNING id"
            ),
            {
                "u": str(uuid_pkg.uuid4()),
                "s": source_id,
                "p": f"/watch/{uuid_pkg.uuid4().hex}.pdf",
                "fn": "v395.pdf",
                "document_id": document_id,
            },
        ).scalar()
    )


def test_v395_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v394_add_document_tables"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v395_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_column_fk_and_index_all_exist(db_session):
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("watch_source_file")}
    assert "document_id" in columns

    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint "
            "WHERE conname = 'watch_source_file_document_id_fkey')"
        )
    ).scalar()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes "
            "WHERE indexname = 'ix_watch_source_file_document_id')"
        )
    ).scalar()


def test_deleting_a_document_nulls_the_tracking_row_rather_than_deleting_it(db_session):
    """``ON DELETE SET NULL``, matching ``media_file_id`` — the tracking row (this path was
    seen, when, with what dedup fingerprint) outlives the object it produced.
    """
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        document_id = _new_document(conn, user_id)
        source_id = _new_watch_source(conn, user_id)
        row_id = _new_watch_source_file(conn, source_id, document_id=document_id)

        conn.execute(text("DELETE FROM document WHERE id = :d"), {"d": document_id})

        remaining = conn.execute(
            text("SELECT document_id FROM watch_source_file WHERE id = :r"), {"r": row_id}
        ).scalar_one_or_none()
        assert remaining is None, "the row should survive with document_id nulled, not vanish"
        assert (
            conn.execute(
                text("SELECT count(*) FROM watch_source_file WHERE id = :r"), {"r": row_id}
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


def test_the_orm_declares_the_new_index():
    """The FK itself is a known, accepted DDL-only divergence — ``media_file_id`` (the
    sibling column this migration mirrors) has the identical shape: an inline
    ``ForeignKey(...)`` with no explicit ``name=``, so SQLAlchemy declares no named
    constraint object for it either, only Postgres's own default-named one. The index IS
    declared (``index=True`` on the mapped column), so that half is checked here.
    """
    from app.db.base import Base

    model_table = Base.metadata.tables["watch_source_file"]
    declared = {i.name for i in model_table.indexes}
    assert "ix_watch_source_file_document_id" in declared


def test_the_orm_mirrors_document_id_nullability(db_session):
    live = {
        c["name"]: c["nullable"]
        for c in inspect(db_session.connection()).get_columns("watch_source_file")
    }
    from app.db.base import Base

    model = Base.metadata.tables["watch_source_file"]
    assert model.columns["document_id"].nullable == live["document_id"]


def test_detection_arm_returns_v395_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_column(db_session):
    """Drop the column and the ladder must stop matching v395.

    Asserted as a band — at or after v394, strictly before v395 — same reasoning
    v394's own downgrade-detection test gives for not asserting exact equality.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(
            text(
                "ALTER TABLE watch_source_file "
                "DROP CONSTRAINT IF EXISTS watch_source_file_document_id_fkey"
            )
        )
        conn.execute(text("ALTER TABLE watch_source_file DROP COLUMN document_id"))
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert order.index("v394_add_document_tables") <= order.index(detected) < order.index(REVISION)


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'watch_source_file_document_id_fkey'"
                )
            ).scalar()
            == 1
        ), "the guard is not a guard"
        assert (
            conn.execute(
                text(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE indexname = 'ix_watch_source_file_document_id'"
                )
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_fk_is_added_even_when_the_column_already_exists(db_session):
    """``ADD COLUMN IF NOT EXISTS``-style guard is a no-op once the column exists, so the FK
    needs its own independent guard — simulates a database that got the column from an
    earlier, interrupted run and is missing only the constraint.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(
            text("ALTER TABLE watch_source_file DROP CONSTRAINT watch_source_file_document_id_fkey")
        )
        conn.execute(text(module.UPGRADE_SQL))
        assert conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_constraint "
                "WHERE conname = 'watch_source_file_document_id_fkey')"
            )
        ).scalar()
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_column_and_the_upgrade_restores_it(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        columns = {c["name"] for c in inspect(conn).get_columns("watch_source_file")}
        assert "document_id" not in columns

        conn.execute(text(module.UPGRADE_SQL))
        columns = {c["name"] for c in inspect(conn).get_columns("watch_source_file")}
        assert "document_id" in columns
    finally:
        db_session.rollback()
