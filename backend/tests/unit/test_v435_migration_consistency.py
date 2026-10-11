"""v435 migration + detection-arm consistency (per-file processing provenance, issue #1229).

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

REVISION = "v435_add_media_processing_provenance"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(conn) -> dict[str, tuple[str, bool]]:
    return {
        c["name"]: (str(c["type"]).lower(), c["nullable"])
        for c in inspect(conn).get_columns("media_file")
    }


def test_v435_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v433_add_requested_transcription_options"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_the_revision_is_vendor_neutral():
    source = _REVISION_PATH.read_text(encoding="utf-8").lower()
    assert "clerk" not in source and "stripe" not in source


def test_the_column_exists_nullable_jsonb_and_the_orm_mirrors_it(db_session):
    from app.db.base import Base

    assert _columns(db_session.connection())["processing_provenance"] == ("jsonb", True)
    model_column = Base.metadata.tables["media_file"].columns["processing_provenance"]
    assert model_column.nullable is True


@pytest.mark.ddl_exclusive
def test_downgrade_and_upgrade_are_idempotent_and_keep_other_columns(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        live = _columns(conn)
        assert "processing_provenance" not in live
        assert "diarization_provider" in live

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        assert _columns(conn)["processing_provenance"] == ("jsonb", True)
    finally:
        db_session.rollback()


def test_detection_arm_returns_v435_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker_column(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE media_file DROP COLUMN processing_provenance"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected == "v433_add_requested_transcription_options"
    order = _chain_order()
    assert order.index(detected) < order.index(REVISION)
