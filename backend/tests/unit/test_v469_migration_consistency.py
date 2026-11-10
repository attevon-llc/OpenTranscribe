"""v469 migration + detection-arm consistency (audio tracks of a media file, issue #1263).

The idempotency and downgrade checks run the revision's real SQL in a PRIVATE scratch database
(precedent: ``test_v468_migration_consistency.py``); each takes the suite DDL lock EXCLUSIVE.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from tests.unit._migration_detection import assert_follows_with_single_head
from tests.unit._migration_detection import exclusive_scratch_connection
from tests.unit._migration_detection import load_revision_module

REVISION = "v469_add_media_file_audio_streams"
DOWN_REVISION = "v468_add_organization_owner"

_BASE_SQL = "CREATE TABLE media_file (id serial PRIMARY KEY, filename text);"


def test_revision_chain_has_one_head():
    assert_follows_with_single_head(REVISION, DOWN_REVISION)


def test_detection_arm_returns_v469_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


def test_the_orm_mirrors_the_audio_columns(db_session):
    from app.db.base import Base

    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("media_file")}
    assert set(live) == set(Base.metadata.tables["media_file"].columns.keys())
    assert live["audio_streams"]["nullable"] is True
    assert live["audio_stream_index"]["nullable"] is True


def test_upgrade_is_idempotent_keeps_rows_and_refuses_a_negative_index():
    module = load_revision_module(REVISION)
    with exclusive_scratch_connection("ot_v469") as conn:
        conn.execute(text(_BASE_SQL))
        conn.execute(text("INSERT INTO media_file (filename) VALUES ('a'), ('b')"))
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        rows = conn.execute(text("SELECT audio_streams, audio_stream_index FROM media_file")).all()
        assert [tuple(r) for r in rows] == [(None, None), (None, None)]
        conn.execute(text("UPDATE media_file SET audio_stream_index = 1 WHERE id = 1"))
        with pytest.raises(IntegrityError), conn.begin_nested():
            conn.execute(text("UPDATE media_file SET audio_stream_index = -1 WHERE id = 2"))


def test_the_downgrade_drops_columns_and_constraint_and_is_repeatable():
    module = load_revision_module(REVISION)
    with exclusive_scratch_connection("ot_v469") as conn:
        conn.execute(text(_BASE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))

        cols = {c["name"] for c in inspect(conn).get_columns("media_file")}
        assert not cols & {"audio_streams", "audio_stream_index"}
        assert not conn.execute(
            text("SELECT 1 FROM pg_constraint WHERE conname = 'ck_media_file_audio_stream_index'")
        ).first()
