"""v433 migration + detection-arm consistency (the per-file request columns, issues #1203/#1198).

Like the earlier ``test_v43x`` suites this **executes** the revision's SQL rather than grepping
its source, and executes it twice, because the startup runner stamps untracked databases by
schema fingerprint and so re-runs a revision over its own partial output.

``v433`` adds four nullable ``media_file.requested_*`` columns and rewrites the old
watch-source default pair (1/20) to NULL, which is what lets a watch source defer to its
owner's saved speaker range.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v433_add_requested_transcription_options"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_COLUMNS = {
    "requested_min_speakers": "integer",
    "requested_max_speakers": "integer",
    "requested_num_speakers": "integer",
    "requested_disable_diarization": "boolean",
}


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
            {"e": f"v433_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _new_source(conn, user_id: int, min_speakers, max_speakers) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO watch_source (uuid, name, source_type, local_path, user_id, "
                "min_speakers, max_speakers) "
                "VALUES (:g, :n, 'local', 'x', :u, :lo, :hi) RETURNING id"
            ),
            {
                "g": str(uuid_pkg.uuid4()),
                "n": f"w-{uuid_pkg.uuid4().hex[:8]}",
                "u": user_id,
                "lo": min_speakers,
                "hi": max_speakers,
            },
        ).scalar()
    )


def _range_of(conn, source_id: int):
    return tuple(
        conn.execute(
            text("SELECT min_speakers, max_speakers FROM watch_source WHERE id = :i"),
            {"i": source_id},
        ).one()
    )


def test_v433_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v432_support_access_grant"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_the_columns_exist_and_are_nullable(db_session):
    live = {
        c["name"]: (str(c["type"]).lower(), c["nullable"])
        for c in inspect(db_session.connection()).get_columns("media_file")
    }
    for name, sql_type in _COLUMNS.items():
        assert name in live, f"media_file is missing {name}"
        assert live[name] == (sql_type, True)


def test_the_orm_mirrors_the_columns(db_session):
    from app.db.base import Base

    model = Base.metadata.tables["media_file"]
    for name in _COLUMNS:
        assert name in model.columns
        assert model.columns[name].nullable is True


def test_new_watch_sources_get_no_database_default(db_session):
    conn = db_session.connection()
    user_id = _new_user(conn)
    source_id = int(
        conn.execute(
            text(
                "INSERT INTO watch_source (uuid, name, source_type, local_path, user_id) "
                "VALUES (:g, 'no-range', 'local', 'x', :u) RETURNING id"
            ),
            {"g": str(uuid_pkg.uuid4()), "u": user_id},
        ).scalar()
    )
    assert _range_of(conn, source_id) == (None, None)
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_old_default_pair_becomes_null_and_a_chosen_range_is_kept(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        user_id = _new_user(conn)
        conn.execute(text("ALTER TABLE watch_source ALTER COLUMN min_speakers SET DEFAULT 1"))
        conn.execute(text("ALTER TABLE watch_source ALTER COLUMN max_speakers SET DEFAULT 20"))
        default_pair = _new_source(conn, user_id, 1, 20)
        chosen = _new_source(conn, user_id, 3, 5)
        only_max = _new_source(conn, user_id, None, 8)

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))  # idempotent

        assert _range_of(conn, default_pair) == (None, None)
        assert _range_of(conn, chosen) == (3, 5)
        assert _range_of(conn, only_max) == (None, 8)
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_columns_and_the_upgrade_restores_them(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        assert not set(_COLUMNS) & {c["name"] for c in inspect(conn).get_columns("media_file")}

        conn.execute(text(module.UPGRADE_SQL))
        assert set(_COLUMNS) <= {c["name"] for c in inspect(conn).get_columns("media_file")}
    finally:
        db_session.rollback()


def test_detection_arm_returns_v433_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker_column(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE media_file DROP COLUMN requested_disable_diarization"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None
    order = _chain_order()
    assert order.index("v430_per_tenant_speaker_and_vocab_names") <= order.index(detected)
    assert order.index(detected) < order.index(REVISION)
