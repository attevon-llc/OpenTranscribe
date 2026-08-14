"""v393 migration + detection-arm consistency (``document`` / ``document_chunk``).

Like ``v387``-``v392``, this suite **executes** the revision's SQL rather than grepping its
source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output. A migration
failure is ``SystemExit(1)``, so a non-idempotent revision does not degrade — the backend
refuses to start.

Two things get their own tests because they are the parts with a *decision* in them: the
``document_chunk.document_id`` ``ON DELETE CASCADE`` (every FK style elsewhere in this schema
is deliberately ``NO ACTION``, so the exception needs a reason and a test — the same
reasoning ``file_facts.media_file_id`` used, and the second instance of it), and the guarded
FK block, which a plain ``CREATE TABLE IF NOT EXISTS`` would silently skip on a database
created by an earlier partial run.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: ``ddl_exclusive`` is applied PER TEST, never to the module: every EXCLUSIVE advisory
#: lock drains all other xdist workers (issue #431).

REVISION = "v393_add_document_tables"
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
            {"e": f"v393_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_document(conn, user_id: int, **overrides) -> int:
    params = {
        "u": str(uuid_pkg.uuid4()),
        "uid": user_id,
        "fn": "v393.pdf",
        "sp": "x/v393.pdf",
        "fs": 1,
        "ct": "application/pdf",
        **overrides,
    }
    return int(
        conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, :fn, :sp, :fs, :ct) RETURNING id"
            ),
            params,
        ).scalar()
    )


def _new_chunk(conn, document_id: int, chunk_index: int = 0, **overrides) -> int:
    params = {
        "d": document_id,
        "i": chunk_index,
        "t": "chunk text",
        "cs": 0,
        "ce": 10,
        **overrides,
    }
    return int(
        conn.execute(
            text(
                "INSERT INTO document_chunk (document_id, chunk_index, text, char_start, "
                "char_end) VALUES (:d, :i, :t, :cs, :ce) RETURNING id"
            ),
            params,
        ).scalar()
    )


def test_v393_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v392_add_redaction_coverage"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    # True while it is head, and still true once a later revision revises it.
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v393_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_tables_their_constraints_and_their_indexes_all_exist(db_session):
    """All of them, not just the tables.

    A partial hand-repair is the realistic failure: the guarded FK block is separate from
    the ``CREATE TABLE``s, so a database that got both tables without the FKs is reachable.
    """
    conn = db_session.connection()
    document_columns = {c["name"]: c for c in inspect(conn).get_columns("document")}
    for required in (
        "uuid",
        "user_id",
        "organization_id",
        "filename",
        "storage_path",
        "file_size",
        "content_type",
        "file_hash",
        "status",
        "parser",
        "parser_version",
        "parse_version",
        "page_count",
        "language",
        "has_embedded_text",
        "ocr_applied",
        "ocr_pages",
        "parse_warnings",
        "word_count",
        "chunk_count",
        "redaction_status",
        "redaction_model_version",
        "redaction_coverage",
        "created_at",
        "updated_at",
        "parsed_at",
    ):
        assert required in document_columns, f"document is missing {required}"

    chunk_columns = {c["name"]: c for c in inspect(conn).get_columns("document_chunk")}
    for required in (
        "document_id",
        "chunk_index",
        "text",
        "char_start",
        "char_end",
        "page",
        "section_path",
        "block_types",
    ):
        assert required in chunk_columns, f"document_chunk is missing {required}"

    for name in (
        "uq_document_uuid",
        "ck_document_file_size",
        "ck_document_page_count",
        "ck_document_ocr_pages",
        "ck_document_word_count",
        "ck_document_chunk_count",
        "document_user_id_fkey",
        "document_organization_id_fkey",
        "uq_document_chunk_index",
        "ck_document_chunk_char_range",
        "document_chunk_document_id_fkey",
    ):
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = :n)"), {"n": name}
        ).scalar(), f"missing constraint {name}"

    for index in (
        "ix_document_uuid",
        "ix_document_organization_id",
        "ix_document_filename",
        "ix_document_file_hash",
        "ix_document_status",
        "ix_document_error_category",
    ):
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = :n)"), {"n": index}
        ).scalar(), f"missing index {index}"


def test_the_fk_cascades_so_deleting_a_document_takes_its_chunks(db_session):
    """The one other FK in this schema that is deliberately NOT ``NO ACTION``.

    The org-stamp house rule is NO ACTION so a tenant delete cannot silently strip rows of
    their tenancy. Here the chunk is *derived* from the document's parse, so leaving it
    behind would be an orphan nothing can regenerate or reach. Executed rather than read off
    ``confdeltype``, because the behaviour is what matters.
    """
    conn = db_session.connection()
    try:
        document_id = _new_document(conn, _new_user(conn))
        _new_chunk(conn, document_id, 0)
        _new_chunk(conn, document_id, 1)
        assert (
            conn.execute(
                text("SELECT count(*) FROM document_chunk WHERE document_id = :d"),
                {"d": document_id},
            ).scalar()
            == 2
        )

        conn.execute(text("DELETE FROM document WHERE id = :d"), {"d": document_id})

        assert (
            conn.execute(
                text("SELECT count(*) FROM document_chunk WHERE document_id = :d"),
                {"d": document_id},
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()


def test_a_duplicate_chunk_index_for_one_document_is_refused(db_session):
    """``UNIQUE (document_id, chunk_index)`` is what keeps a retried parse idempotent."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        document_id = _new_document(conn, _new_user(conn))
        _new_chunk(conn, document_id, 0)
        with pytest.raises(IntegrityError):
            _new_chunk(conn, document_id, 0)
    finally:
        db_session.rollback()


def test_the_check_constraint_refuses_a_negative_file_size(db_session):
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        with pytest.raises(IntegrityError):
            _new_document(conn, _new_user(conn), fs=-1)
    finally:
        db_session.rollback()


def test_the_check_constraint_refuses_char_end_before_char_start(db_session):
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        document_id = _new_document(conn, _new_user(conn))
        with pytest.raises(IntegrityError):
            _new_chunk(conn, document_id, 0, cs=10, ce=5)
    finally:
        db_session.rollback()


@pytest.mark.parametrize("table", ["document", "document_chunk"])
def test_the_orm_declares_every_constraint_the_database_enforces(db_session, table):
    """Neither table may become the 25th/26th DDL-only divergence.

    ``.rag-403/ddl-orm-divergence.md`` catalogues 24 constraints Postgres enforces and
    Python never stated; a new table is the cheapest possible moment to not repeat that.
    """
    from app.db.base import Base

    model_table = Base.metadata.tables[table]
    declared = {c.name for c in model_table.constraints if c.name} | {
        i.name for i in model_table.indexes
    }

    conn = db_session.connection()
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = :t "
                "AND c.contype IN ('u','c','f')"
            ),
            {"t": table},
        )
    } | {
        row[0]
        for row in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = :t"), {"t": table}
        )
        if not row[0].endswith("_pkey")
    }
    # A UNIQUE constraint also creates an index of the same name; one declaration covers
    # both, so compare on names rather than object classes.
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


@pytest.mark.parametrize("table", ["document", "document_chunk"])
def test_the_orm_mirrors_the_columns_nullability(db_session, table):
    """The half ``test_schema_drift.py`` cannot see: it compares columns, not nullability."""
    from app.db.base import Base

    live = {c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns(table)}
    model = Base.metadata.tables[table]
    assert set(live) == set(model.columns.keys()), (
        "the ORM and the table do not even agree on the column set, so the per-column "
        "comparison below would silently check a subset"
    )
    for name, nullable in live.items():
        assert model.columns[name].nullable == nullable, f"{name} nullability disagrees"


def test_detection_arm_returns_v393_or_later_on_current_schema(db_session):
    """Step 4 of the procedure in backend/app/db/CLAUDE.md — the step that gets skipped.

    Skip the arm and an untracked database is mis-stamped to the PREVIOUS revision and
    never receives this DDL, so the parse task inserts into tables that are not there.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_tables(db_session):
    """Drop the markers and the ladder must stop matching v393.

    Asserted as a *band* — at or after v392, strictly before v393 — because an exact ``==``
    on a lower revision goes red or vacuous the next time the ladder above it changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TABLE document_chunk"))
        conn.execute(text("DROP TABLE document"))
        tables = [
            t for t in inspect(conn).get_table_names() if t not in ("document", "document_chunk")
        ]
        detected = _detect_schema_version(conn, tables)
    finally:
        # finally, not a trailing call: this mutates the SHARED dev schema.
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v392_add_redaction_coverage") <= order.index(detected) < order.index(REVISION)
    )


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The invariant the startup runner depends on, executed rather than asserted about."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        assert "document" in inspect(conn).get_table_names()
        assert "document_chunk" in inspect(conn).get_table_names()
        for name in (
            "document_user_id_fkey",
            "document_organization_id_fkey",
            "document_chunk_document_id_fkey",
        ):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": name}
                ).scalar()
                == 1
            ), f"{name} was created twice — the guard is not a guard"
        for index in ("ix_document_uuid", "ix_document_filename", "ix_document_status"):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": index}
                ).scalar()
                == 1
            )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_fks_are_added_even_when_the_tables_already_exist(db_session):
    """``CREATE TABLE IF NOT EXISTS`` is a no-op, so inline FKs would be skipped forever.

    Simulates the real partial-run state: a database that got both tables from an earlier,
    interrupted run and is missing only the constraints.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(
            text("ALTER TABLE document_chunk DROP CONSTRAINT document_chunk_document_id_fkey")
        )
        conn.execute(text("ALTER TABLE document DROP CONSTRAINT document_user_id_fkey"))
        conn.execute(text("ALTER TABLE document DROP CONSTRAINT document_organization_id_fkey"))
        conn.execute(text(module.UPGRADE_SQL))
        for name in (
            "document_user_id_fkey",
            "document_organization_id_fkey",
            "document_chunk_document_id_fkey",
        ):
            assert conn.execute(
                text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = :n)"), {"n": name}
            ).scalar(), f"{name} was not re-added"
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_both_tables_and_the_upgrade_restores_them(db_session):
    """The downgrade is executed here, not merely read.

    Before issue #431 no downgrade in this chain had ever been run by a test, so
    "``downgrade()`` mirrors ``upgrade()``" was a claim about source text.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        tables = inspect(conn).get_table_names()
        assert "document" not in tables
        assert "document_chunk" not in tables

        conn.execute(text(module.UPGRADE_SQL))
        tables = inspect(conn).get_table_names()
        assert "document" in tables
        assert "document_chunk" in tables
    finally:
        db_session.rollback()
