"""v444 migration + detection-arm consistency (takedown notification log, issue #1254)."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v444_add_takedown_notification"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_LAST_CHECK = "ck_takedown_notification_status"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v444_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v443_add_takedown_notice_tables"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_log_has_no_free_text_column(db_session):
    """Every textual column is a CHECK-bounded enum: no address, error text or body."""
    insp = inspect(db_session.connection())
    cols = {c["name"]: c for c in insp.get_columns("takedown_notification")}
    assert not [n for n, c in cols.items() if type(c["type"]).__name__ == "TEXT"]
    assert (
        max(
            c["type"].length
            for c in cols.values()
            if hasattr(c["type"], "length") and c["type"].length
        )
        <= 32
    )


def _insert(conn, link_id, owner_uuid, **overrides):
    values = {
        "link": link_id,
        "event": "quarantined",
        "kind": "owner",
        "owner": owner_uuid,
        "channel": "email",
        "status": "sent",
    }
    values.update(overrides)
    conn.execute(
        text(
            "INSERT INTO takedown_notification (link_id, event, recipient_kind, "
            "recipient_user_uuid, channel, status) "
            "VALUES (:link, :event, :kind, :owner, :channel, :status)"
        ),
        values,
    )


@pytest.fixture
def link_id(db_session, normal_user):
    conn = db_session.connection()
    notice = conn.execute(
        text(
            "INSERT INTO takedown_notice (uuid, notice_type, received_at, basis) "
            "VALUES (gen_random_uuid(), 'operator_action', now(), 'b') RETURNING id"
        )
    ).scalar()
    return conn.execute(
        text(
            "INSERT INTO takedown_notice_file (uuid, notice_id, file_uuid, owner_user_uuid, "
            "owner_reason, quarantined_at) VALUES (gen_random_uuid(), :n, gen_random_uuid(), "
            ":o, 'r', now()) RETURNING id"
        ),
        {"n": notice, "o": normal_user.uuid},
    ).scalar()


@pytest.mark.parametrize(
    "overrides",
    [{"event": "x"}, {"kind": "x"}, {"channel": "sms"}, {"status": "delivered"}],
)
def test_checks_reject_values_outside_the_vocabulary(db_session, normal_user, link_id, overrides):
    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert(conn, link_id, normal_user.uuid, **overrides)


def test_a_delivery_is_unique_per_link_event_recipient_and_channel(
    db_session, normal_user, link_id
):
    conn = db_session.connection()
    _insert(conn, link_id, normal_user.uuid)
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert(conn, link_id, normal_user.uuid)
    _insert(conn, link_id, normal_user.uuid, event="released")


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent(db_session):
    conn = db_session.connection()
    names = "SELECT count(*) FROM pg_constraint WHERE conrelid = 'takedown_notification'::regclass"
    before = conn.execute(text(names)).scalar()
    conn.execute(text(_module().UPGRADE_SQL))
    conn.execute(text(_module().UPGRADE_SQL))
    assert conn.execute(text(names)).scalar() == before


def test_downgrade_drops_only_this_revisions_table():
    assert re.findall(r"DROP TABLE IF EXISTS (\w+)", _module().DOWNGRADE_SQL) == [
        "takedown_notification"
    ]


def test_detection_arm_returns_v444_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_last_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE takedown_notification DROP CONSTRAINT {_LAST_CHECK}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
