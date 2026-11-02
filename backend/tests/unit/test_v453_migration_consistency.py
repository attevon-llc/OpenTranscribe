"""v453 migration + detection-arm consistency (audit row immutability)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v453_audit_row_immutability"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _guard_triggers(conn) -> set[str]:
    return set(
        conn.execute(
            text("SELECT tgname FROM pg_trigger WHERE tgname LIKE 'trg\\_audit\\_guard\\_%'")
        ).scalars()
    )


def test_v453_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert (
        scripts.get_revision(REVISION).down_revision
        == "v451_legal_acceptance_survives_user_deletion"
    )
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_frozen_floor_matches_the_constant():
    from app.core.constants import AUDIT_RETENTION_FLOOR_DAYS

    assert _module().RETENTION_FLOOR_DAYS == AUDIT_RETENTION_FLOOR_DAYS == 365


def test_live_schema_has_the_guards(db_session):
    conn = db_session.connection()
    m = _module()
    triggers = _guard_triggers(conn)
    for table, _, _ in m.GUARDED:
        assert f"trg_audit_guard_{table}" in triggers
        assert f"trg_audit_guard_truncate_{table}" in triggers
    functions = set(
        conn.execute(
            text(
                "SELECT proname FROM pg_proc WHERE proname IN "
                "('audit_row_guard', 'audit_prune', 'audit_maintenance_allowed')"
            )
        ).scalars()
    )
    assert functions == {"audit_row_guard", "audit_prune", "audit_maintenance_allowed"}


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent_and_downgrade_removes_the_guards(db_session):
    m = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(m.UPGRADE_SQL))
        conn.execute(text(m.UPGRADE_SQL))
        assert len(_guard_triggers(conn)) == 2 * len(m.GUARDED)
        conn.execute(text(m.DOWNGRADE_SQL))
        conn.execute(text(m.DOWNGRADE_SQL))
        assert _guard_triggers(conn) == set()
        assert not conn.execute(
            text("SELECT count(*) FROM pg_proc WHERE proname LIKE 'audit\\_%'")
        ).scalar()
        # legal_acceptance keeps its own guard (v451): this revision never touches it.
        assert conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_legal_acceptance_guard'")
        ).scalar()
    finally:
        db_session.rollback()


def test_detection_arm_returns_v453_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_guard_trigger(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TRIGGER trg_audit_guard_erasure_ledger ON erasure_ledger"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
