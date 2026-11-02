"""v451 migration + detection-arm consistency (legal acceptance survives user deletion)."""

from __future__ import annotations

import importlib.util
import re
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v451_legal_acceptance_survives_user_deletion"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

_FK_RULE_SQL = """
    SELECT confdeltype FROM pg_constraint
     WHERE contype = 'f' AND conrelid = 'legal_acceptance'::regclass
       AND confrelid = '"user"'::regclass
"""


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v451_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert (
        scripts.get_revision(REVISION).down_revision
        == "v450_legal_acceptance_context_and_profile_interaction"
    )
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_frozen_retention_numbers_match_the_constants():
    from app.core import constants

    m = _module()
    assert m.DEFAULT_RETENTION_DAYS == constants.DEFAULT_LEGAL_ACCEPTANCE_RETENTION_DAYS == 2190
    assert m.MIN_RETENTION_DAYS == constants.MIN_LEGAL_ACCEPTANCE_RETENTION_DAYS == 365
    assert constants.LEGAL_ACCEPTANCE_RETENTION_DAYS_KEY in m.UPGRADE_SQL


def test_live_schema_has_the_survival_rule(db_session):
    conn = db_session.connection()
    assert conn.execute(text(_FK_RULE_SQL)).scalars().all() == ["n"]
    cols = {c["name"]: c for c in inspect(conn).get_columns("legal_acceptance")}
    assert cols["user_id"]["nullable"] is True
    for name in ("subject_user_uuid", "subject_erased_at", "retain_until"):
        assert name in cols
    triggers = set(
        conn.execute(
            text(
                "SELECT tgname FROM pg_trigger WHERE tgrelid = 'legal_acceptance'::regclass"
                " AND NOT tgisinternal"
            )
        ).scalars()
    )
    assert {"trg_legal_acceptance_guard", "trg_legal_acceptance_stamp_subject"} <= triggers


def _insert(conn, user_id, sha):
    return conn.execute(
        text(
            "INSERT INTO legal_acceptance (uuid, user_id, document_key, version, sha256,"
            " ip_address, user_agent) VALUES (:u, :uid, 'terms-of-use', '1', :sha,"
            " '10.0.0.9', 'UA/1') RETURNING id"
        ),
        {"u": uuid_pkg.uuid4(), "uid": user_id, "sha": sha},
    ).scalar()


@pytest.mark.ddl_exclusive
def test_upgrade_over_a_populated_v450_table(db_session, normal_user, admin_user):
    """3 rows, 2 users, one user with two acceptances: all kept, backfilled, then pseudonymised."""
    module = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        assert conn.execute(text(_FK_RULE_SQL)).scalars().all() == ["c"]
        ids = [
            _insert(conn, normal_user.id, "a" * 64),
            _insert(conn, normal_user.id, "b" * 64),
            _insert(conn, admin_user.id, "a" * 64),
        ]
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))  # idempotent
        assert conn.execute(text(_FK_RULE_SQL)).scalars().all() == ["n"]
        rows = conn.execute(
            text(
                "SELECT la.id, la.subject_user_uuid = u.uuid AS ok FROM legal_acceptance la"
                ' JOIN "user" u ON u.id = la.user_id WHERE la.id = ANY(:ids)'
            ),
            {"ids": ids},
        ).all()
        assert len(rows) == 3 and all(r.ok for r in rows)

        conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": normal_user.id})
        kept = conn.execute(
            text(
                "SELECT user_id, ip_address, user_agent, subject_erased_at, retain_until"
                " FROM legal_acceptance WHERE id = ANY(:ids) ORDER BY id"
            ),
            {"ids": ids[:2]},
        ).all()
        assert len(kept) == 2
        for r in kept:
            assert r.user_id is None and r.ip_address is None and r.user_agent is None
            assert r.subject_erased_at is not None and r.retain_until > r.subject_erased_at
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_downgrade_deletes_pseudonymised_rows_and_restores_cascade(db_session, normal_user):
    module = _module()
    conn = db_session.connection()
    try:
        _insert(conn, normal_user.id, "c" * 64)
        conn.execute(text('DELETE FROM "user" WHERE id = :u'), {"u": normal_user.id})
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        assert conn.execute(text(_FK_RULE_SQL)).scalars().all() == ["c"]
        assert (
            conn.execute(
                text("SELECT count(*) FROM legal_acceptance WHERE user_id IS NULL")
            ).scalar()
            == 0
        )
    finally:
        db_session.rollback()


def test_detection_arm_returns_v451_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_guard_trigger(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP TRIGGER trg_legal_acceptance_guard ON legal_acceptance"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
