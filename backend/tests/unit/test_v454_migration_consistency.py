"""v454 migration + detection-arm consistency (per-file voiceprint consent, issue #1356)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v454_add_media_file_voiceprint_consent"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v454_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    # Chained after the erasure receipt (and the append-only audit when it lands); the exact
    # parent moves as revisions are inserted before this one, so pin only that it resolves.
    parent = scripts.get_revision(REVISION).down_revision
    assert isinstance(parent, str) and scripts.get_revision(parent) is not None
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_states_match_the_model_the_check_and_the_schema():
    from typing import get_args

    from app.models.media import VOICEPRINT_CONSENT_STATES
    from app.schemas.file_consent import ConsentState

    assert _module().STATES == VOICEPRINT_CONSENT_STATES
    assert set(get_args(ConsentState)) == set(VOICEPRINT_CONSENT_STATES)


def test_columns_check_fk_and_partial_index(db_session):
    conn = db_session.connection()
    cols = {c["name"]: c for c in inspect(conn).get_columns("media_file")}
    assert cols["voiceprint_consent"]["nullable"] is False
    assert "unspecified" in str(cols["voiceprint_consent"]["default"])
    assert cols["voiceprint_consent_at"]["nullable"] is True
    assert cols["voiceprint_consent_by_user_id"]["nullable"] is True
    defs = dict(
        conn.execute(text("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint")).all()
    )
    assert "declined" in defs["ck_media_file_voiceprint_consent"]
    assert "ON DELETE SET NULL" in defs["fk_media_file_voiceprint_consent_by_user"]
    idx = conn.execute(
        text(
            "SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_media_file_voiceprint_declined'"
        )
    ).scalar()
    assert idx is not None and "WHERE" in idx and "declined" in idx


def test_the_check_refuses_an_invalid_state(db_session, normal_user):
    from sqlalchemy.exc import IntegrityError

    from tests.fixtures.speaker_cleanup_seams import make_file

    row = make_file(db_session, normal_user)
    conn = db_session.connection()
    try:
        with pytest.raises(IntegrityError, match="ck_media_file_voiceprint_consent"):
            conn.execute(
                text("UPDATE media_file SET voiceprint_consent = 'maybe' WHERE id = :i"),
                {"i": row.id},
            )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent_and_existing_rows_default_to_unspecified(db_session, normal_user):
    from tests.fixtures.speaker_cleanup_seams import make_file

    module = _module()
    conn = db_session.connection()
    try:
        row = make_file(db_session, normal_user)
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        state = conn.execute(
            text("SELECT voiceprint_consent FROM media_file WHERE id = :i"), {"i": row.id}
        ).scalar()
        assert state == "unspecified"
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_downgrade_refuses_while_a_file_is_declined(db_session, normal_user, monkeypatch):
    from alembic.operations import Operations
    from alembic.runtime.migration import MigrationContext

    from tests.fixtures.speaker_cleanup_seams import make_file

    module = _module()
    monkeypatch.delenv("OT_ALLOW_CONSENT_DOWNGRADE", raising=False)
    conn = db_session.connection()
    try:
        row = make_file(db_session, normal_user)
        conn.execute(
            text("UPDATE media_file SET voiceprint_consent = 'declined' WHERE id = :i"),
            {"i": row.id},
        )
        with Operations.context(MigrationContext.configure(conn)):
            with pytest.raises(RuntimeError, match="opted out"):
                module.downgrade()
        # The refusal changed nothing.
        assert "voiceprint_consent" in {c["name"] for c in inspect(conn).get_columns("media_file")}
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_downgrade_runs_without_declined_files_or_with_the_override(
    db_session, normal_user, monkeypatch
):
    module = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))  # idempotent
        assert "voiceprint_consent" not in {
            c["name"] for c in inspect(conn).get_columns("media_file")
        }
        conn.execute(text(module.UPGRADE_SQL))
    finally:
        db_session.rollback()


def test_detection_arm_returns_v454_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker_index(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX ix_media_file_voiceprint_declined"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
