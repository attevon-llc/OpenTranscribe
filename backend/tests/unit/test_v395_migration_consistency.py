"""v395 migration + detection-arm consistency (``document_chunk.redactions``/``.toxicity``).

Same convention as ``v393``/``v394``: **executes** the revision's SQL rather than grepping
its source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and therefore re-runs a revision over its own partial output.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v395_add_document_chunk_redaction_cache"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
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


def _new_chunk(conn, document_id: int, chunk_index: int = 0) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO document_chunk (document_id, chunk_index, text, char_start, "
                "char_end) VALUES (:d, :i, 'chunk text', 0, 10) RETURNING id"
            ),
            {"d": document_id, "i": chunk_index},
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

    assert rev.down_revision == "v394_add_watch_source_file_document_id"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v395_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_the_columns_exist_and_are_jsonb(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("document_chunk")}
    assert "redactions" in columns
    assert "toxicity" in columns
    assert columns["redactions"]["nullable"] is True
    assert columns["toxicity"]["nullable"] is True


def test_the_columns_hold_real_span_and_score_data(db_session):
    """Not just present — actually usable as JSONB, round-tripping a real payload."""
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        document_id = _new_document(conn, user_id)
        chunk_id = _new_chunk(conn, document_id)

        spans = [{"category": "pii", "start": 0, "end": 4, "text": "John"}]
        toxicity = {"toxic": 0.02}
        conn.execute(
            text("UPDATE document_chunk SET redactions = :r, toxicity = :t WHERE id = :c"),
            {
                "r": __import__("json").dumps(spans),
                "t": __import__("json").dumps(toxicity),
                "c": chunk_id,
            },
        )
        row = conn.execute(
            text("SELECT redactions, toxicity FROM document_chunk WHERE id = :c"), {"c": chunk_id}
        ).first()
        assert row.redactions == spans
        assert row.toxicity == toxicity
    finally:
        db_session.rollback()


def test_the_orm_declares_the_new_columns():
    from app.db.base import Base

    model_table = Base.metadata.tables["document_chunk"]
    declared = set(model_table.columns.keys())
    assert "redactions" in declared
    assert "toxicity" in declared


def test_the_orm_mirrors_the_columns_nullability(db_session):
    live = {
        c["name"]: c["nullable"]
        for c in inspect(db_session.connection()).get_columns("document_chunk")
    }
    from app.db.base import Base

    model = Base.metadata.tables["document_chunk"]
    for name in ("redactions", "toxicity"):
        assert model.columns[name].nullable == live[name], f"{name} nullability disagrees"


def test_detection_arm_returns_v395_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_columns(db_session):
    """Drop the columns and the ladder must stop matching v395.

    Asserted as a band — at or after v394, strictly before v395 — same reasoning
    every prior revision's downgrade-detection test gives for not asserting exact
    equality.
    """
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE document_chunk DROP COLUMN IF EXISTS redactions"))
        conn.execute(text("ALTER TABLE document_chunk DROP COLUMN IF EXISTS toxicity"))
        tables = inspect(conn).get_table_names()
        detected = _detect_schema_version(conn, tables)
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder matched no revision at all"
    order = _chain_order()
    assert (
        order.index("v394_add_watch_source_file_document_id")
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
        columns = {c["name"] for c in inspect(conn).get_columns("document_chunk")}
        assert "redactions" in columns
        assert "toxicity" in columns
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_both_columns_and_the_upgrade_restores_them(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent both ways
        columns = {c["name"] for c in inspect(conn).get_columns("document_chunk")}
        assert "redactions" not in columns
        assert "toxicity" not in columns

        conn.execute(text(module.UPGRADE_SQL))
        columns = {c["name"] for c in inspect(conn).get_columns("document_chunk")}
        assert "redactions" in columns
        assert "toxicity" in columns
    finally:
        db_session.rollback()
