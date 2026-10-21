"""v440 migration + detection-arm consistency (user.password_last_self_changed_at).

The column is the minimum-age clock: only the holder's own change sets it, NULL means the
first change is free. Expiry keeps reading ``password_changed_at``.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v440_add_password_last_self_changed_at"
COLUMN = "password_last_self_changed_at"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(conn) -> bool:
    return COLUMN in {c["name"] for c in inspect(conn).get_columns("user")}


def test_v440_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)

    assert rev.down_revision == "v439_infer_legacy_voiceprint_suggestion_source"
    assert len(set(scripts.get_heads())) == 1


def test_the_column_exists_and_is_nullable_timestamptz(db_session):
    conn = db_session.connection()
    column = next(c for c in inspect(conn).get_columns("user") if c["name"] == COLUMN)
    assert column["nullable"] is True
    assert column["type"].timezone is True


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent_and_downgrade_removes_the_column(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))  # already present: must not raise
        assert _has_column(conn)
        conn.execute(text(module.DOWNGRADE_SQL))
        assert not inspect(conn).has_table("user") or not _has_column(conn)
        conn.execute(text(module.DOWNGRADE_SQL))  # IF EXISTS: second run is a no-op
        conn.execute(text(module.UPGRADE_SQL))
        assert _has_column(conn)
    finally:
        db_session.rollback()


def test_detection_arm_returns_v440_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker_column(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f'ALTER TABLE "user" DROP COLUMN {COLUMN}'))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None
    order = _chain_order()
    assert order.index(detected) < order.index(REVISION)


def test_expiry_still_reads_password_changed_at_not_the_new_column(monkeypatch):
    """Expiry takes only the credential-set time, so a recent self-change cannot rescue it."""
    from app.auth.password_policy import PasswordPolicy

    monkeypatch.setattr(PasswordPolicy, "enabled", property(lambda self: True))
    monkeypatch.setattr(PasswordPolicy, "max_age_days", property(lambda self: 90))
    policy = PasswordPolicy()

    old = datetime.now(UTC) - timedelta(days=400)
    recent = datetime.now(UTC) - timedelta(minutes=1)
    assert policy.is_password_expired(old) is True
    assert policy.is_password_expired(recent) is False
    # The minimum-age clock is a different input: NULL there is "change allowed".
    assert policy.min_age_remaining(None) is None
