"""v443 migration + detection-arm consistency (takedown notice records, issue #1254)."""

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

REVISION = "v443_add_takedown_notice_tables"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_LAST_CHECK = "ck_takedown_notice_file_outcome_at"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _backfill(conn):
    module = _module()
    conn.execute(text(module.BACKFILL_SQL), {"reason": module.BACKFILL_REASON})


def _file(owner, **flags) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    return MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        filename=f"v443_{file_uuid.hex[:8]}.wav",
        storage_path="",
        content_type="audio/wav",
        file_size=1,
        status=FileStatus.COMPLETED,
        **flags,
    )


def _links(conn, file: MediaFile):
    return conn.execute(
        text(
            "SELECT l.status, l.outcome_at IS NOT NULL, l.owner_reason, l.legal_hold_placed, "
            "n.notice_type, n.recorded_by_authority, n.requester_reference "
            "FROM takedown_notice_file l JOIN takedown_notice n ON n.id = l.notice_id "
            "WHERE l.file_uuid = :u ORDER BY l.id"
        ),
        {"u": file.uuid},
    ).all()


def test_v443_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v442_add_media_file_takedown_origin"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_tables_hold_no_requester_contact_columns(db_session):
    """The record is designed to outlive everything, so it has nowhere to put a person."""
    insp = inspect(db_session.connection())
    names = {
        c["name"] for t in ("takedown_notice", "takedown_notice_file") for c in insp.get_columns(t)
    }
    assert not {n for n in names if re.search(r"email|phone|address|contact|requester_name", n)}
    assert "requester_reference" in names


def test_foreign_keys_are_set_null_so_the_record_outlives_what_it_names(db_session):
    rows = (
        db_session.connection()
        .execute(
            text(
                "SELECT conrelid::regclass::text, a.attname, c.confdeltype "
                "FROM pg_constraint c JOIN pg_attribute a "
                "ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey) "
                "WHERE c.contype = 'f' AND conrelid::regclass::text IN "
                "('takedown_notice', 'takedown_notice_file')"
            )
        )
        .all()
    )
    rules = {(t, col): rule for t, col, rule in rows}
    assert rules[("takedown_notice", "handled_by_user_id")] == "n"
    assert rules[("takedown_notice_file", "file_id")] == "n"
    assert rules[("takedown_notice_file", "organization_id")] == "n"
    assert rules[("takedown_notice_file", "notice_id")] == "c"


def _notice_sql(**overrides):
    values = {
        "notice_type": "dmca",
        "reference": "CASE-1",
        "status": "open",
        "counter": None,
        "outcome": None,
    }
    values.update(overrides)
    return values


def _insert_pair(conn, owner, **overrides):
    v = _notice_sql(**overrides)
    notice_id = conn.execute(
        text(
            "INSERT INTO takedown_notice (uuid, notice_type, received_at, requester_reference, basis) "
            "VALUES (gen_random_uuid(), :notice_type, now(), :reference, 'b') RETURNING id"
        ),
        v,
    ).scalar()
    conn.execute(
        text(
            "INSERT INTO takedown_notice_file (uuid, notice_id, file_uuid, owner_user_uuid, "
            "owner_reason, status, quarantined_at, counter_notice_received_at, outcome_at) "
            "VALUES (gen_random_uuid(), :n, gen_random_uuid(), :o, 'r', :status, now(), "
            ":counter, :outcome)"
        ),
        {"n": notice_id, "o": owner.uuid, **v},
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"notice_type": "operator_action", "reference": "CASE-1"},
        {"notice_type": "dmca", "reference": None},
        {"notice_type": "bogus"},
        {"status": "bogus"},
        {"status": "counter_notice", "counter": None},
        {"status": "upheld", "outcome": None},
        {"status": "released", "outcome": None},
    ],
)
def test_the_checks_make_an_inconsistent_record_unrepresentable(db_session, normal_user, overrides):
    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert_pair(conn, normal_user, **overrides)


def test_the_checks_admit_the_consistent_shapes(db_session, normal_user):
    conn = db_session.connection()
    _insert_pair(conn, normal_user, notice_type="operator_action", reference=None)
    _insert_pair(conn, normal_user, status="counter_notice", counter="2026-01-01T00:00:00Z")
    _insert_pair(conn, normal_user, status="upheld", outcome="2026-01-01T00:00:00Z")
    stored = conn.execute(
        text("SELECT count(*) FROM takedown_notice_file WHERE owner_user_uuid = :o"),
        {"o": normal_user.uuid},
    ).scalar()
    assert stored == 3


def test_backfill_gives_every_existing_hold_a_record_and_a_rerun_is_a_noop(db_session, normal_user):
    conn = db_session.connection()
    quarantined = _file(normal_user, is_quarantined=True, quarantine_reason="old reason")
    held_only = _file(normal_user, legal_hold=True)
    both = _file(normal_user, is_quarantined=True, legal_hold=True, quarantine_reason="r2")
    neither = _file(normal_user)
    org_held = _file(normal_user, is_quarantined=True, takedown_origin="organization")
    db_session.add_all([quarantined, held_only, both, neither, org_held])
    db_session.flush()

    _backfill(conn)
    _backfill(conn)

    assert _links(conn, neither) == []
    (q,) = _links(conn, quarantined)
    assert q[:5] == ("open", False, "old reason", False, "operator_action")
    assert q[5] == "platform" and q[6] is None
    (h,) = _links(conn, held_only)
    assert h[0] == "released" and h[1] is True and h[3] is True
    assert h[2] == _module().BACKFILL_REASON
    (b,) = _links(conn, both)
    assert b[0] == "open" and b[3] is True
    (o,) = _links(conn, org_held)
    assert o[5] == "organization"


@pytest.mark.ddl_exclusive
def test_create_sql_is_idempotent(db_session):
    conn = db_session.connection()
    names = (
        "SELECT count(*) FROM pg_constraint WHERE conrelid IN "
        "('takedown_notice'::regclass, 'takedown_notice_file'::regclass)"
    )
    before = conn.execute(text(names)).scalar()
    conn.execute(text(_module().CREATE_SQL))
    conn.execute(text(_module().CREATE_SQL))
    assert conn.execute(text(names)).scalar() == before


def test_downgrade_drops_only_the_tables_this_revision_created():
    """Not executed: it destroys compliance evidence, so it must not be run against shared data."""
    statements = re.findall(r"DROP TABLE IF EXISTS (\w+)", _module().DOWNGRADE_SQL)
    assert statements == ["takedown_notice_file", "takedown_notice"]


def test_detection_arm_returns_v443_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_last_check(db_session):
    """A table without its last CHECK is the state that must re-run this revision."""
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE takedown_notice_file DROP CONSTRAINT {_LAST_CHECK}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
