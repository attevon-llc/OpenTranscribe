"""v399 migration + detection-arm consistency (document quarantine + ``task.document_id``).

Executes the revision's SQL directly (twice, for idempotency — the startup runner
stamps untracked databases by fingerprint and therefore re-runs a revision over its own
partial output), mirroring the v390/v394/v398 convention rather than grepping source text.

Two independent changes, so two groups of assertions: the document takedown columns
(shape copied from v370/v371's ``media_file`` columns) and ``task.document_id`` (shape
copied from ``task.media_file_id``, no XOR — a task may belong to neither).
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v399_add_document_quarantine_and_task_link"
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
            {"e": f"v399_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_document(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v399.pdf', 'x/v399.pdf', 1, "
                "'application/pdf') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def test_v399_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v398_widen_file_facts_for_documents"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v399_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_document_quarantine_columns_exist(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("document")}
    for name in (
        "is_quarantined",
        "quarantine_reason",
        "quarantined_at",
        "quarantined_by",
        "pre_quarantine_status",
        "legal_hold",
    ):
        assert name in columns, f"missing document.{name}"
    assert columns["is_quarantined"]["nullable"] is False
    assert columns["legal_hold"]["nullable"] is False
    assert columns["quarantine_reason"]["nullable"] is True


def test_document_quarantine_index_exists(db_session):
    conn = db_session.connection()
    assert conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'ix_document_is_quarantined')"
        )
    ).scalar()


def test_quarantined_by_fk_is_set_null(db_session):
    """Unlike media_file's, this one is ON DELETE SET NULL from the start (no v387-style
    repair needed) — assert the live rule directly against pg_constraint.
    """
    conn = db_session.connection()
    row = conn.execute(
        text("SELECT confdeltype FROM pg_constraint WHERE conname = 'document_quarantined_by_fkey'")
    ).first()
    assert row is not None, "document_quarantined_by_fkey is missing"
    assert row[0] == "n", f"expected ON DELETE SET NULL (n), got {row[0]!r}"


def test_quarantined_by_actually_sets_null_on_admin_deletion(db_session):
    conn = db_session.connection()
    try:
        admin_id = _new_user(conn)
        owner_id = _new_user(conn)
        document_id = _new_document(conn, owner_id)
        conn.execute(
            text("UPDATE document SET is_quarantined = true, quarantined_by = :a WHERE id = :d"),
            {"a": admin_id, "d": document_id},
        )
        conn.execute(text('DELETE FROM "user" WHERE id = :a'), {"a": admin_id})
        row = conn.execute(
            text("SELECT quarantined_by, is_quarantined FROM document WHERE id = :d"),
            {"d": document_id},
        ).first()
        assert row is not None
        assert row[0] is None, "quarantined_by must be NULLed, not block the admin's delete"
        assert row[1] is True, "the document's own quarantine state must survive"
    finally:
        db_session.rollback()


def test_task_document_id_column_and_fk_exist(db_session):
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("task")}
    assert "document_id" in columns
    assert conn.execute(
        text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'task_document_id_fkey')")
    ).scalar()
    assert conn.execute(
        text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'ix_task_document_id')")
    ).scalar()


def test_a_task_row_can_reference_a_document(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        document_id = _new_document(conn, user_id)
        conn.execute(
            text(
                "INSERT INTO task (id, user_id, document_id, task_type, status) "
                "VALUES (:tid, :uid, :did, 'document_parse', 'pending')"
            ),
            {"tid": f"v399-{uuid_pkg.uuid4().hex[:10]}", "uid": user_id, "did": document_id},
        )
        assert (
            conn.execute(
                text("SELECT count(*) FROM task WHERE document_id = :d"), {"d": document_id}
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


def test_the_orm_declares_every_constraint_the_database_enforces(db_session):
    """``document``, in full — same invariant every prior revision's test states.

    ``task`` is deliberately NOT swept the same way: it carries several pre-existing
    indexes (``idx_task_media_file_id``, ``idx_task_user_id``, ...) that predate this
    revision and were never declared as SQLAlchemy ``Index`` objects — a pre-existing
    DDL/ORM divergence this revision did not create and is out of scope to backfill.
    ``test_task_document_id_column_and_fk_exist`` already pins the two objects THIS
    revision adds (``task_document_id_fkey``, ``ix_task_document_id``) individually.
    """
    from app.db.base import Base

    conn = db_session.connection()
    table = Base.metadata.tables["document"]
    declared = {c.name for c in table.constraints if c.name} | {i.name for i in table.indexes}
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = 'document' "
                "AND c.contype IN ('u','c','f')"
            )
        )
    } | {
        row[0]
        for row in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'document'")
        )
        if not row[0].endswith("_pkey")
    }
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


def test_detection_arm_returns_v399_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_partial_index(db_session):
    """Drop the quarantine index and the ladder must stop matching v399.

    Asserted as a band, same reasoning v390/v398's equivalent tests give: an exact
    ``==`` on a lower revision goes red the next time the ladder above it changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX ix_document_is_quarantined"))
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v398_widen_file_facts_for_documents")
        <= order.index(detected)
        < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        for name in ("document_quarantined_by_fkey", "task_document_id_fkey"):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": name}
                ).scalar()
                == 1
            ), f"{name} was created twice — the guard is not a guard"
        for index_name in ("ix_document_is_quarantined", "ix_task_document_id"):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": index_name}
                ).scalar()
                == 1
            ), f"{index_name} was created twice — the guard is not a guard"
    finally:
        db_session.rollback()
