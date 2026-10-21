"""v442 migration + detection-arm consistency (``media_file.takedown_origin``, issue #1253)."""

from __future__ import annotations

import importlib.util
import re
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.enums import FileStatus
from app.models.media import MediaFile

REVISION = "v442_add_media_file_takedown_origin"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_TRIGGER = "trg_media_file_takedown_origin"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _row(owner, **flags) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    return MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"v442_{file_uuid.hex[:8]}.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
        **flags,
    )


def _origin(conn, row: MediaFile):
    return conn.execute(
        text("SELECT takedown_origin FROM media_file WHERE id = :i"), {"i": row.id}
    ).scalar()


def test_v442_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v441_add_legal_acceptance"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_column_check_and_trigger_exist(db_session):
    conn = db_session.connection()
    cols = {c["name"]: c for c in inspect(conn).get_columns("media_file")}
    assert cols["takedown_origin"]["nullable"] is True
    assert cols["takedown_origin"]["type"].length == 16
    checks = {c["name"] for c in inspect(conn).get_check_constraints("media_file")}
    assert "ck_media_file_takedown_origin_valid" in checks
    assert conn.execute(
        text("SELECT count(*) FROM pg_trigger WHERE tgname = :t AND NOT tgisinternal"),
        {"t": _TRIGGER},
    ).scalar()


@pytest.mark.ddl_exclusive
def test_backfill_marks_every_existing_hold_as_platform(db_session, normal_user):
    """The only writer of either flag before this revision was the platform route, so every
    held row is the operator's. Replayed against rows that pre-date the column."""
    conn = db_session.connection()
    module = _module()
    conn.execute(text(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON media_file"))
    quarantined = _row(normal_user, is_quarantined=True)
    held = _row(normal_user, legal_hold=True)
    both = _row(normal_user, is_quarantined=True, legal_hold=True)
    neither = _row(normal_user)
    already = _row(normal_user, is_quarantined=True, takedown_origin="organization")
    db_session.add_all([quarantined, held, both, neither, already])
    db_session.flush()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        got = [_origin(conn, r) for r in (quarantined, held, both, neither, already)]
    finally:
        db_session.rollback()
    assert got == ["platform", "platform", "platform", None, "organization"]


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent_and_downgrade_removes_everything(db_session):
    module = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        cols = {c["name"] for c in inspect(conn).get_columns("media_file")}
        assert "takedown_origin" not in cols
        assert not conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = :t"), {"t": _TRIGGER}
        ).scalar()
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        assert "takedown_origin" in {c["name"] for c in inspect(conn).get_columns("media_file")}
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
class TestTrigger:
    """The trigger is what makes "held with no origin" unrepresentable.

    Each case first replays the revision's own ``UPGRADE_SQL`` inside the test's
    transaction, so it exercises the SQL the migration ships rather than whatever
    trigger the test database happened to be built with; the rollback puts it back.
    """

    @pytest.fixture(autouse=True)
    def _replay_revision(self, db_session):
        db_session.connection().execute(text(_module().UPGRADE_SQL))

    def test_a_writer_that_sets_only_the_flag_gets_the_fail_closed_origin(
        self, db_session, normal_user
    ):
        row = _row(normal_user, is_quarantined=True)
        db_session.add(row)
        db_session.flush()
        assert _origin(db_session.connection(), row) == "platform"

    def test_a_legal_hold_alone_is_enough_to_get_an_origin(self, db_session, normal_user):
        row = _row(normal_user, legal_hold=True)
        db_session.add(row)
        db_session.flush()
        assert _origin(db_session.connection(), row) == "platform"

    def test_an_explicit_origin_is_kept(self, db_session, normal_user):
        row = _row(normal_user, is_quarantined=True, takedown_origin="organization")
        db_session.add(row)
        db_session.flush()
        assert _origin(db_session.connection(), row) == "organization"

    def test_clearing_both_flags_clears_the_origin(self, db_session, normal_user):
        row = _row(normal_user, is_quarantined=True, takedown_origin="organization")
        db_session.add(row)
        db_session.flush()
        row.is_quarantined = False
        db_session.flush()
        assert _origin(db_session.connection(), row) is None

    def test_a_clean_file_cannot_be_given_an_origin(self, db_session, normal_user):
        row = _row(normal_user, takedown_origin="platform")
        db_session.add(row)
        db_session.flush()
        assert _origin(db_session.connection(), row) is None

    def test_a_value_outside_the_vocabulary_is_rejected(self, db_session, normal_user):
        row = _row(normal_user, is_quarantined=True, takedown_origin="tenant")
        db_session.add(row)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()


def test_detection_arm_returns_v442_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_trigger(db_session):
    """A column without the trigger is the state that must re-run the revision."""
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"DROP TRIGGER {_TRIGGER} ON media_file"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
