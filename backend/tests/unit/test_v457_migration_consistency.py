"""v457 (audit guard follow-ups): the guard lets the application do what it really does.

The earlier guard tests seeded rows and tried hand-written UPDATEs; none of them replayed the
columns the services actually write after insert, which is how the self-service withdrawal and
the reconcile cursor were refused by the trigger.
"""

from __future__ import annotations

import importlib.util
import re
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

from app.core.constants import GDPR_SELF_ERASURE_ENABLED_KEY
from app.core.constants import GDPR_SELF_ERASURE_GRACE_DAYS_KEY
from app.models.erasure import ErasureLedgerEntry
from app.services import self_erasure_service as service
from app.services.system_settings_service import set_setting
from tests.unit._audit_rows import assert_refused
from tests.unit._audit_rows import seed_audit_rows

REVISION = "v457_audit_guard_followups"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


@pytest.fixture(autouse=True)
def journal_dir(tmp_path, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "DATA_DIR", tmp_path)


@pytest.fixture()
def enabled(db_session):
    set_setting(db_session, GDPR_SELF_ERASURE_ENABLED_KEY, True)
    set_setting(db_session, GDPR_SELF_ERASURE_GRACE_DAYS_KEY, 14)


def test_revision_chain_has_one_head():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(_PATH.parents[1]))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v456_unique_object_keys"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_self_erasure_withdrawal_updates_the_guarded_ledger_row(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    cancelled = service.cancel_self_erasure(
        db_session, normal_user, entry.uuid, now=NOW + timedelta(days=1)
    )
    db_session.expire_all()
    row = db_session.get(ErasureLedgerEntry, cancelled.id)
    assert row is not None
    assert row.status == "cancelled"
    assert row.cancelled_at == NOW + timedelta(days=1)


def test_the_index_reconcile_cursor_can_advance(db_session, normal_user, enabled):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    entry.index_checked_at = NOW
    db_session.commit()
    db_session.expire_all()
    row = db_session.get(ErasureLedgerEntry, entry.id)
    assert row is not None and row.index_checked_at == NOW


@pytest.mark.parametrize("column", ["scheduled_for", "request_digest"])
def test_insert_time_columns_stay_frozen(db_session, normal_user, enabled, column):
    entry = service.request_self_erasure(db_session, normal_user, now=NOW)
    value = "'" + "a" * 64 + "'" if column == "request_digest" else "now()"
    conn = db_session.connection()
    with assert_refused(conn):
        conn.execute(
            text(f"UPDATE erasure_ledger SET {column} = {value} WHERE id = :i"), {"i": entry.id}
        )


def test_erasure_receipt_is_insert_only(db_session, normal_user):
    conn = db_session.connection()
    ids = seed_audit_rows(conn, normal_user.id)
    with assert_refused(conn):
        conn.execute(
            text("UPDATE erasure_receipt SET outcome = 'residual' WHERE id = :i"),
            {"i": ids["erasure_receipt"]},
        )
    with assert_refused(conn):
        conn.execute(
            text("DELETE FROM erasure_receipt WHERE id = :i"), {"i": ids["erasure_receipt"]}
        )


@pytest.mark.ddl_exclusive
def test_truncate_is_refused_on_receipt_and_legal_acceptance(db_session, normal_user):
    conn = db_session.connection()
    seed_audit_rows(conn, normal_user.id)
    for table in ("erasure_receipt", "legal_acceptance"):
        with assert_refused(conn):
            conn.execute(text(f"TRUNCATE {table} CASCADE"))


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent_and_downgrade_restores_v453(db_session):
    m = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(m.UPGRADE_SQL))
        conn.execute(text(m.UPGRADE_SQL))
        conn.execute(text(m.DOWNGRADE_SQL))
        conn.execute(text(m.DOWNGRADE_SQL))
        definition = conn.execute(
            text(
                "SELECT pg_get_triggerdef(oid) FROM pg_trigger "
                "WHERE tgname = 'trg_audit_guard_erasure_ledger'"
            )
        ).scalar_one()
        assert "index_checked_at" not in definition
        assert not conn.execute(
            text("SELECT count(*) FROM pg_trigger WHERE tgname = 'trg_audit_guard_erasure_receipt'")
        ).scalar()
    finally:
        db_session.rollback()


def test_detection_arm_returns_v457_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_widened_guard(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    m = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(m.DOWNGRADE_SQL))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
