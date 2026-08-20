"""v398 migration + detection-arm consistency (``file_facts`` widened for documents).

Like ``v390``/``v394``, this suite **executes** the revision's SQL rather than grepping
its source, and executes it twice, because the startup runner stamps untracked databases
by schema fingerprint and therefore re-runs a revision over its own partial output. A
migration failure is ``SystemExit(1)``, so a non-idempotent revision does not degrade —
the backend refuses to start.

Three things get their own tests because they are the parts with a *decision* in them:
the XOR CHECK (a row naming both or neither owner must be genuinely refused BY THE
DATABASE, not merely by the model), the two partial unique indexes (a plain composite
UNIQUE would not catch two document-owned rows sharing a ``document_id``, since Postgres
treats NULLs as distinct), and the destructive downgrade (deletes document-owned rows —
see the revision's own docstring for why that is deliberate and non-destructive in
practice).
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v398_widen_file_facts_for_documents"
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
            {"e": f"v398_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_file(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v398.wav', 'x/v398.wav', 1, 'audio/wav') "
                "RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_document(conn, user_id: int) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document (uuid, user_id, filename, storage_path, file_size, "
                "content_type) VALUES (:u, :uid, 'v398.pdf', 'x/v398.pdf', 1, "
                "'application/pdf') RETURNING id"
            ),
            {"u": str(uuid_pkg.uuid4()), "uid": user_id},
        ).scalar()
    )


def _new_facts(
    conn, *, media_file_id: int | None = None, document_id: int | None = None, **overrides
) -> int:
    params = {
        "mf": media_file_id,
        "doc": document_id,
        "gv": "1.1.1",
        "fp": "0" * 64,
        "dw": 100,
        "sc": 2,
        "ms": 42,
        **overrides,
    }
    return int(
        conn.execute(
            text(
                "INSERT INTO file_facts (media_file_id, document_id, generator_version, "
                "source_fingerprint, facts, digest, keyphrases, digest_word_count, "
                "section_count, generation_ms) VALUES (:mf, :doc, :gv, :fp, '{}'::jsonb, "
                "'{}'::jsonb, '{}'::jsonb, :dw, :sc, :ms) RETURNING id"
            ),
            params,
        ).scalar()
    )


def test_v398_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v397_backfill_document_tenancy_and_hash"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v398_migration_is_vendor_neutral():
    """CI's seam guard greps core for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_media_file_id_is_now_nullable(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("file_facts")}
    assert "media_file_id" in columns
    assert columns["media_file_id"]["nullable"] is True, (
        "v398 must drop the NOT NULL on media_file_id — a document-owned row leaves it NULL"
    )


def test_the_constraints_and_indexes_all_exist(db_session):
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("file_facts")}
    assert "document_id" in columns

    for name in (
        "ck_file_facts_exactly_one_owner",
        "file_facts_document_id_fkey",
        "file_facts_media_file_id_fkey",
    ):
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = :n)"), {"n": name}
        ).scalar(), f"missing constraint {name}"

    for index_name in ("uq_file_facts_media_file_id", "uq_file_facts_document_id"):
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = :n)"), {"n": index_name}
        ).scalar(), f"missing index {index_name}"

    # The old single-column UNIQUE must be gone — replaced, not left alongside.
    assert not conn.execute(
        text(
            "SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname = 'uq_file_facts_media_file')"
        )
    ).scalar()


def test_a_media_owned_row_is_accepted(db_session):
    conn = db_session.connection()
    try:
        file_id = _new_file(conn, _new_user(conn))
        _new_facts(conn, media_file_id=file_id)
        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE media_file_id = :f"), {"f": file_id}
            ).scalar()
            == 1
        )
    finally:
        db_session.rollback()


def test_a_document_owned_row_is_accepted(db_session):
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        document_id = _new_document(conn, user_id)
        _new_facts(conn, document_id=document_id)
        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE document_id = :d"), {"d": document_id}
            ).scalar()
            == 1
        )
        row = conn.execute(
            text("SELECT media_file_id FROM file_facts WHERE document_id = :d"), {"d": document_id}
        ).first()
        assert row[0] is None
    finally:
        db_session.rollback()


def test_the_xor_check_refuses_both_owners_set(db_session):
    """Asserts the DATABASE raises, not just that the ORM/model declines."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        file_id = _new_file(conn, user_id)
        document_id = _new_document(conn, user_id)
        with pytest.raises(IntegrityError):
            _new_facts(conn, media_file_id=file_id, document_id=document_id)
    finally:
        db_session.rollback()


def test_the_xor_check_refuses_neither_owner_set(db_session):
    """Asserts the DATABASE raises, not just that the ORM/model declines."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        with pytest.raises(IntegrityError):
            _new_facts(conn, media_file_id=None, document_id=None)
    finally:
        db_session.rollback()


def test_a_second_document_owned_row_for_one_document_is_refused(db_session):
    """The partial unique index on ``document_id`` is what makes regeneration an
    upsert rather than a race — the document-owned mirror of v390's media-file test.
    """
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    try:
        document_id = _new_document(conn, _new_user(conn))
        _new_facts(conn, document_id=document_id)
        with pytest.raises(IntegrityError):
            _new_facts(conn, document_id=document_id)
    finally:
        db_session.rollback()


def test_the_fk_cascades_so_deleting_a_document_takes_its_artifacts(db_session):
    conn = db_session.connection()
    try:
        document_id = _new_document(conn, _new_user(conn))
        _new_facts(conn, document_id=document_id)
        conn.execute(text("DELETE FROM document WHERE id = :d"), {"d": document_id})
        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE document_id = :d"), {"d": document_id}
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()


def test_the_orm_declares_every_constraint_the_database_enforces(db_session):
    """Same invariant v390's own test states: a new revision is the cheapest possible
    moment to not become the 25th DDL-only ORM/database divergence.
    """
    from app.db.base import Base

    table = Base.metadata.tables["file_facts"]
    declared = {c.name for c in table.constraints if c.name} | {i.name for i in table.indexes}

    conn = db_session.connection()
    live = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT conname FROM pg_constraint c "
                "JOIN pg_class t ON t.oid = c.conrelid WHERE t.relname = 'file_facts' "
                "AND c.contype IN ('u','c','f')"
            )
        )
    } | {
        row[0]
        for row in conn.execute(
            text("SELECT indexname FROM pg_indexes WHERE tablename = 'file_facts'")
        )
        if not row[0].endswith("_pkey")
    }
    missing = live - declared
    assert not missing, f"the database enforces {sorted(missing)} and the ORM declares none of it"


def test_the_orm_mirrors_the_columns_nullability(db_session):
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("file_facts")
    }
    model = Base.metadata.tables["file_facts"]
    assert set(live) == set(model.columns.keys())
    for name, nullable in live.items():
        assert model.columns[name].nullable == nullable, f"{name} nullability disagrees"


def test_detection_arm_returns_v398_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_check_constraint(db_session):
    """Drop the XOR CHECK and the ladder must stop matching v398.

    Asserted as a band — at or after v397, strictly before v398 — same reasoning
    v390's own equivalent test gives: an exact ``==`` on a lower revision goes red or
    vacuous the next time the ladder above it changes.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE file_facts DROP CONSTRAINT ck_file_facts_exactly_one_owner"))
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v397_backfill_document_tenancy_and_hash")
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
            "ck_file_facts_exactly_one_owner",
            "file_facts_document_id_fkey",
        ):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :n"), {"n": name}
                ).scalar()
                == 1
            ), f"{name} was created twice — the guard is not a guard"
        for index_name in ("uq_file_facts_media_file_id", "uq_file_facts_document_id"):
            assert (
                conn.execute(
                    text("SELECT count(*) FROM pg_indexes WHERE indexname = :n"), {"n": index_name}
                ).scalar()
                == 1
            )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_deletes_document_owned_rows_and_preserves_media_owned_rows(db_session):
    """Executed, not merely read — the same rule every other revision's consistency
    test applies (issue #431). Deliberately destructive for document-owned rows; see
    the revision's own docstring for why that is documented and non-destructive in
    practice (the artifacts regenerate from ``document_chunk``).
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        media_file_id = _new_file(conn, user_id)
        document_id = _new_document(conn, user_id)
        _new_facts(conn, media_file_id=media_file_id)
        _new_facts(conn, document_id=document_id)

        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways

        columns = {c["name"] for c in inspect(conn).get_columns("file_facts")}
        assert "document_id" not in columns
        assert (
            conn.execute(
                text("SELECT count(*) FROM file_facts WHERE media_file_id = :f"),
                {"f": media_file_id},
            ).scalar()
            == 1
        ), "the media-owned row must survive the downgrade"

        conn.execute(text(module.UPGRADE_SQL))
        columns = {c["name"] for c in inspect(conn).get_columns("file_facts")}
        assert "document_id" in columns
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_restores_media_file_id_not_null(db_session):
    """A separate test (rather than continuing the one above past an expected error):
    once an ``IntegrityError`` aborts a Postgres transaction, nothing on that
    connection may run again until a rollback — matching the shape every other
    ``pytest.raises(IntegrityError)`` case in this suite uses.
    """
    from sqlalchemy.exc import IntegrityError

    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO file_facts (generator_version, source_fingerprint, facts, "
                    "digest, keyphrases, digest_word_count, section_count) "
                    "VALUES ('1.1.1', :fp, '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, 0, 0)"
                ),
                {"fp": "1" * 64},
            )
    finally:
        db_session.rollback()
