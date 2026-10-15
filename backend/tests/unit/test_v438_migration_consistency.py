"""v438 migration + detection-arm consistency (speaker correction carry-over, issue #1233).

Like the other ``test_v43x`` suites this **executes** the revision's SQL rather than grepping
its source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and so re-runs a revision over its own partial output.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v438_add_speaker_correction_carryover"
TABLE = "speaker_correction_carryover"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _delete_rules(conn) -> dict[str, str]:
    """``column -> confdeltype`` for the table's foreign keys ('c' = CASCADE)."""
    rows = conn.execute(
        text(
            "SELECT a.attname, c.confdeltype FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY(c.conkey) "
            "WHERE c.conrelid = CAST(:t AS regclass) AND c.contype = 'f'"
        ),
        {"t": TABLE},
    ).all()
    return {str(name): str(rule) for name, rule in rows}


def test_v438_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v437_add_media_processing_provenance"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_the_revision_is_vendor_neutral():
    source = _REVISION_PATH.read_text(encoding="utf-8").lower()
    assert "clerk" not in source and "stripe" not in source


def test_the_table_cascades_from_every_parent_and_the_orm_mirrors_it(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    assert _delete_rules(conn) == {
        "media_file_id": "c",
        "profile_id": "c",
        "cannot_link_speaker_id": "c",
    }, "a correction must never outlive the file, profile or speaker it names"
    live = {c["name"] for c in inspect(conn).get_columns(TABLE)}
    assert live == set(Base.metadata.tables[TABLE].columns.keys())


def test_a_row_names_exactly_one_target(db_session, normal_user):
    """The CHECK: a correction rules a voice out of a profile OR a speaker, never both."""
    from sqlalchemy.exc import IntegrityError

    from tests.fixtures.speaker_cleanup_seams import make_file

    media = make_file(db_session, normal_user)
    conn = db_session.connection()
    with pytest.raises(IntegrityError, match="ck_speaker_correction_carryover_one_target"):
        with conn.begin_nested():
            conn.execute(
                text(
                    "INSERT INTO speaker_correction_carryover (media_file_id, spans) "
                    "VALUES (:fid, '[]'::jsonb)"
                ),
                {"fid": media.id},
            )
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_downgrade_and_upgrade_are_idempotent(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        assert TABLE not in inspect(conn).get_table_names()

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        assert TABLE in inspect(conn).get_table_names()
    finally:
        db_session.rollback()


def test_detection_arm_returns_v438_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker_table(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TABLE speaker_correction_carryover"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None, "the ladder no longer recognizes the pre-v438 schema"
    order = _chain_order()
    assert order.index(detected) < order.index(REVISION)
