"""v400 migration + detection-arm consistency (document sharing + document comments).

Executes the revision's SQL directly (twice, for idempotency — the startup runner
stamps untracked databases by fingerprint and therefore re-runs a revision over its own
partial output), mirroring the v390/v394/v398/v399 convention rather than grepping
source text.

Two independent changes, so two groups of assertions: ``document_share`` (shape copied
from ``collection_share``, re-scoped to one document) and ``comment``'s widened
media_file_id/document_id/document_chunk_id shape (the media-comment XOR pattern
``v398`` gave ``file_facts``).
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v400_add_document_sharing_and_comments"
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
            {"e": f"v400_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_document(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v400.pdf', 'x/v400.pdf', 1, "
                "'application/pdf') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_document_chunk(conn, document_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document_chunk (document_id, chunk_index, text, char_start, "
                "char_end, section_path, block_types) VALUES "
                "(:d, 0, 'hello', 0, 5, '[]'::jsonb, '[]'::jsonb) RETURNING id"
            ),
            {"d": document_id},
        ).scalar()
    )


def _new_group(conn, owner_id: int) -> int:
    return int(
        conn.execute(
            text("INSERT INTO user_group (uuid, name, owner_id) VALUES (:u, :n, :o) RETURNING id"),
            {
                "u": str(uuid_pkg.uuid4()),
                "n": f"v400-group-{uuid_pkg.uuid4().hex[:8]}",
                "o": owner_id,
            },
        ).scalar()
    )


def test_v400_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v399_add_document_quarantine_and_task_link"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v400_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_document_share_table_shape(db_session):
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("document_share")}
    for name in (
        "id",
        "uuid",
        "document_id",
        "shared_by_id",
        "target_type",
        "target_user_id",
        "target_group_id",
        "permission",
        "created_at",
        "updated_at",
    ):
        assert name in columns, f"missing document_share.{name}"


def test_document_share_target_check_enforced(db_session):
    """Naming both or neither target is refused by the database."""
    conn = db_session.connection()
    try:
        owner_id = _new_user(conn)
        sharer_id = _new_user(conn)
        document_id = _new_document(conn, owner_id)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO document_share (uuid, document_id, shared_by_id, target_type) "
                    "VALUES (:u, :d, :s, 'user')"
                ),
                {"u": str(uuid_pkg.uuid4()), "d": document_id, "s": sharer_id},
            )
    finally:
        db_session.rollback()


def test_document_share_partial_unique_index_per_user(db_session):
    conn = db_session.connection()
    try:
        owner_id = _new_user(conn)
        sharer_id = _new_user(conn)
        target_id = _new_user(conn)
        document_id = _new_document(conn, owner_id)
        conn.execute(
            text(
                "INSERT INTO document_share (uuid, document_id, shared_by_id, target_type, "
                "target_user_id) VALUES (:u, :d, :s, 'user', :t)"
            ),
            {"u": str(uuid_pkg.uuid4()), "d": document_id, "s": sharer_id, "t": target_id},
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO document_share (uuid, document_id, shared_by_id, target_type, "
                    "target_user_id) VALUES (:u, :d, :s, 'user', :t)"
                ),
                {"u": str(uuid_pkg.uuid4()), "d": document_id, "s": sharer_id, "t": target_id},
            )
    finally:
        db_session.rollback()


def test_document_share_cascades_on_document_delete(db_session):
    conn = db_session.connection()
    try:
        owner_id = _new_user(conn)
        target_id = _new_user(conn)
        document_id = _new_document(conn, owner_id)
        conn.execute(
            text(
                "INSERT INTO document_share (uuid, document_id, shared_by_id, target_type, "
                "target_user_id) VALUES (:u, :d, :s, 'user', :t)"
            ),
            {"u": str(uuid_pkg.uuid4()), "d": document_id, "s": owner_id, "t": target_id},
        )
        conn.execute(text("DELETE FROM document WHERE id = :d"), {"d": document_id})
        assert (
            conn.execute(
                text("SELECT count(*) FROM document_share WHERE document_id = :d"),
                {"d": document_id},
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()


def test_document_share_group_target_works(db_session):
    conn = db_session.connection()
    try:
        owner_id = _new_user(conn)
        document_id = _new_document(conn, owner_id)
        group_id = _new_group(conn, owner_id)
        conn.execute(
            text(
                "INSERT INTO document_share (uuid, document_id, shared_by_id, target_type, "
                "target_group_id, permission) VALUES (:u, :d, :s, 'group', :g, 'editor')"
            ),
            {"u": str(uuid_pkg.uuid4()), "d": document_id, "s": owner_id, "g": group_id},
        )
        row = conn.execute(
            text("SELECT permission FROM document_share WHERE document_id = :d"), {"d": document_id}
        ).first()
        assert row is not None
        assert row[0] == "editor"
    finally:
        db_session.rollback()


def test_comment_columns_widened(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("comment")}
    assert "document_id" in columns
    assert "document_chunk_id" in columns
    assert columns["media_file_id"]["nullable"] is True
    assert columns["document_id"]["nullable"] is True


def test_comment_xor_check_rejects_both_owners(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        media_file_id = int(
            conn.execute(
                text(
                    "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                    "content_type) VALUES (:u, :uid, 'v400.mp3', 'x/v400.mp3', 1, "
                    "'audio/mpeg') RETURNING id"
                ),
                {"u": str(uuid_pkg.uuid4()), "uid": user_id},
            ).scalar()
        )
        document_id = _new_document(conn, user_id)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO comment (uuid, media_file_id, document_id, user_id, text) "
                    "VALUES (:u, :m, :d, :uid, 'both')"
                ),
                {
                    "u": str(uuid_pkg.uuid4()),
                    "m": media_file_id,
                    "d": document_id,
                    "uid": user_id,
                },
            )
    finally:
        db_session.rollback()


def test_comment_xor_check_rejects_neither_owner(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        with pytest.raises(IntegrityError):
            conn.execute(
                text("INSERT INTO comment (uuid, user_id, text) VALUES (:u, :uid, 'neither')"),
                {"u": str(uuid_pkg.uuid4()), "uid": user_id},
            )
    finally:
        db_session.rollback()


def test_a_document_comment_can_be_created_and_cascades(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        document_id = _new_document(conn, user_id)
        chunk_id = _new_document_chunk(conn, document_id)
        comment_id = int(
            conn.execute(
                text(
                    "INSERT INTO comment (uuid, document_id, document_chunk_id, user_id, text) "
                    "VALUES (:u, :d, :c, :uid, 'a note') RETURNING id"
                ),
                {
                    "u": str(uuid_pkg.uuid4()),
                    "d": document_id,
                    "c": chunk_id,
                    "uid": user_id,
                },
            ).scalar()
        )
        # A reparse deletes and recreates document_chunk rows — the comment must
        # survive, degraded to unanchored, not be destroyed (SET NULL, not CASCADE).
        conn.execute(text("DELETE FROM document_chunk WHERE id = :c"), {"c": chunk_id})
        row = conn.execute(
            text("SELECT document_chunk_id FROM comment WHERE id = :i"), {"i": comment_id}
        ).first()
        assert row is not None
        assert row[0] is None

        conn.execute(text("DELETE FROM document WHERE id = :d"), {"d": document_id})
        assert (
            conn.execute(
                text("SELECT count(*) FROM comment WHERE id = :i"), {"i": comment_id}
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()


def test_the_orm_declares_document_share_constraints(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    table = Base.metadata.tables["document_share"]
    declared = {c.name for c in table.constraints if c.name} | {i.name for i in table.indexes}
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = 'document_share' "
                "AND c.contype IN ('u','c','f')"
            )
        )
    } | {
        row[0]
        for row in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'document_share'")
        )
        if not row[0].endswith("_pkey")
    }
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


def test_the_orm_declares_the_comment_xor_check(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    table = Base.metadata.tables["comment"]
    declared = {c.name for c in table.constraints if c.name}
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = 'comment' "
                "AND c.contype = 'c'"
            )
        )
    }
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


def test_detection_arm_returns_v400_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_document_share(db_session):
    """Drop ``document_share`` and the ladder must stop matching v400.

    Asserted as a band, same reasoning v390/v398/v399's equivalent tests give: an exact
    ``==`` on a lower revision goes red the next time the ladder above it changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TABLE document_share"))
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v399_add_document_quarantine_and_task_link")
        <= order.index(detected)
        < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_comment_xor_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE comment DROP CONSTRAINT ck_comment_exactly_one_owner"))
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v399_add_document_quarantine_and_task_link")
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

        for name in (
            "comment_document_id_fkey",
            "comment_document_chunk_id_fkey",
            "ck_comment_exactly_one_owner",
        ):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": name}
                ).scalar()
                == 1
            ), f"{name} was created twice — the guard is not a guard"
        for index_name in (
            "idx_comment_document_id",
            "idx_comment_document_chunk_id",
            "ix_document_share_document_id",
            "_document_share_user_uc",
            "_document_share_group_uc",
        ):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": index_name}
                ).scalar()
                == 1
            ), f"{index_name} was created twice — the guard is not a guard"
    finally:
        db_session.rollback()
