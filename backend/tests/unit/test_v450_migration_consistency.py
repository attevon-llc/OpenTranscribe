"""v450 migration + detection-arm consistency (legal acceptance context, profile interaction)."""

from __future__ import annotations

import importlib.util
import re
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v450_legal_acceptance_context_and_profile_interaction"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_SHA = "a" * 64


def _module(revision: str = REVISION):
    path = _PATH.with_name(f"{revision}.py")
    spec = importlib.util.spec_from_file_location(revision, path)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


#: v451 builds a guard trigger on the v450 columns; step it down first, as alembic would.
_V451 = "v451_legal_acceptance_survives_user_deletion"


def _downgrade_to_v449(conn, module) -> None:
    conn.execute(text(_module(_V451).DOWNGRADE_SQL))
    conn.execute(text(module.DOWNGRADE_SQL))


def test_v450_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v449_add_speaker_identity_source"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_columns_and_constraints(db_session):
    conn = db_session.connection()
    insp = inspect(conn)
    cols = {c["name"]: c for c in insp.get_columns("legal_acceptance")}
    for name in ("scope", "organization_id", "organization_uuid", "user_agent", "locale"):
        assert name in cols
    assert cols["scope"]["nullable"] is False
    assert cols["user_agent"]["type"].length == 512
    checks = {c["name"] for c in insp.get_check_constraints("legal_acceptance")}
    assert "ck_legal_acceptance_scope" in checks
    # The user UNIQUE is now a partial index, not a table constraint.
    assert not insp.get_unique_constraints("legal_acceptance")
    idx = {
        r[0]: r[1]
        for r in conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'legal_acceptance'")
        )
    }
    assert "WHERE" in idx["uq_legal_acceptance_user_doc_sha"]
    assert "WHERE" in idx["uq_legal_acceptance_org_doc_sha"]
    assert "last_interaction_at" in {c["name"] for c in insp.get_columns("speaker_profile")}


def _insert(conn, user_id, *, scope="user", org_uuid=None, sha=_SHA, accepted="now()"):
    conn.execute(
        text(
            "INSERT INTO legal_acceptance (uuid, user_id, document_key, version, sha256,"
            " accepted_at, ip_address"
            + (", scope, organization_uuid" if scope != "user" else "")
            + ") VALUES (:u, :uid, 'terms-of-use', '1', :sha, "
            + accepted
            + ", '10.0.0.1'"
            + (", :scope, :org" if scope != "user" else "")
            + ")"
        ),
        {"u": uuid_pkg.uuid4(), "uid": user_id, "sha": sha, "scope": scope, "org": org_uuid},
    )


@pytest.mark.ddl_exclusive
def test_upgrade_over_populated_v441_table(db_session, normal_user, admin_user):
    """Rows written under v441 survive the upgrade unchanged and satisfy every new constraint."""
    module = _module()
    conn = db_session.connection()
    try:
        _downgrade_to_v449(conn, module)
        _insert(conn, normal_user.id)
        _insert(conn, normal_user.id, sha="b" * 64)
        _insert(conn, admin_user.id)
        conn.execute(
            text(
                "UPDATE speaker_profile SET last_embedding_update = NULL"
                " WHERE id IN (SELECT id FROM speaker_profile LIMIT 0)"
            )
        )
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        rows = conn.execute(
            text(
                "SELECT user_id, scope, ip_address, organization_uuid FROM legal_acceptance"
                " WHERE user_id IN (:a, :b) ORDER BY id"
            ),
            {"a": normal_user.id, "b": admin_user.id},
        ).all()
        assert [(r.scope, r.ip_address, r.organization_uuid) for r in rows] == [
            ("user", "10.0.0.1", None)
        ] * 3
        # Still unique per user for user scope...
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                _insert(conn, normal_user.id)
        # ...but the same user may record the same text for two organizations.
        _insert(conn, normal_user.id, scope="organization", org_uuid=uuid_pkg.uuid4())
        _insert(conn, normal_user.id, scope="organization", org_uuid=uuid_pkg.uuid4())
        with pytest.raises(IntegrityError):
            with conn.begin_nested():
                _insert(conn, normal_user.id, scope="organization", org_uuid=None)
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_takes_the_latest_existing_timestamp(db_session, normal_user):
    module = _module()
    conn = db_session.connection()
    try:
        _downgrade_to_v449(conn, module)
        pid = conn.execute(
            text(
                "INSERT INTO speaker_profile (uuid, user_id, name, created_at, updated_at,"
                " last_embedding_update) VALUES (:u, :uid, 'v450', '2020-01-01Z', '2021-01-01Z',"
                " '2022-06-01Z') RETURNING id"
            ),
            {"u": uuid_pkg.uuid4(), "uid": normal_user.id},
        ).scalar()
        conn.execute(text(module.UPGRADE_SQL))
        got = conn.execute(
            text(
                "SELECT to_char(last_interaction_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
                " FROM speaker_profile WHERE id = :i"
            ),
            {"i": pid},
        ).scalar()
    finally:
        db_session.rollback()
    assert got == "2022-06-01"


@pytest.mark.ddl_exclusive
def test_downgrade_keeps_the_oldest_duplicate_and_restores_the_constraint(db_session, normal_user):
    module = _module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.UPGRADE_SQL))
        _insert(conn, normal_user.id, accepted="'2020-01-01Z'")
        _insert(conn, normal_user.id, scope="organization", org_uuid=uuid_pkg.uuid4())
        _downgrade_to_v449(conn, module)
        conn.execute(text(module.DOWNGRADE_SQL))
        left = conn.execute(
            text("SELECT count(*) FROM legal_acceptance WHERE user_id = :u"), {"u": normal_user.id}
        ).scalar()
        assert left == 1
        assert inspect(conn).get_unique_constraints("legal_acceptance")
        conn.execute(text(module.UPGRADE_SQL))
    finally:
        db_session.rollback()


def test_detection_arm_returns_v450_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_scope_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("ALTER TABLE legal_acceptance DROP CONSTRAINT ck_legal_acceptance_scope"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
