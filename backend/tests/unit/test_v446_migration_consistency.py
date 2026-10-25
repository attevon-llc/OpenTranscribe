"""v446 migration + detection-arm consistency (collection visibility mode, issue #1275)."""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v446_add_collection_visibility"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_LAST_CHECK = "ck_collection_visibility"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v446_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v444_add_takedown_notification"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


def test_the_column_defaults_to_organization_and_is_not_null(db_session):
    column = next(
        c
        for c in inspect(db_session.connection()).get_columns("collection")
        if c["name"] == "visibility"
    )
    assert column["nullable"] is False
    assert "organization" in str(column["default"])


def _insert(conn, *, org_id, user_id, visibility=None):
    cols = "uuid, name, user_id, organization_id" + (", visibility" if visibility else "")
    vals = ":u, :n, :uid, :org" + (", :vis" if visibility else "")
    params = {"u": uuid.uuid4(), "n": f"v446 {uuid.uuid4().hex[:8]}", "uid": user_id, "org": org_id}
    if visibility:
        params["vis"] = visibility
    return conn.execute(
        text(f"INSERT INTO collection ({cols}) VALUES ({vals}) RETURNING visibility"), params
    ).scalar()


@pytest.fixture
def org_id(db_session):
    conn = db_session.connection()
    return conn.execute(
        text(
            "INSERT INTO organization (uuid, external_org_id, name, is_active) "
            "VALUES (gen_random_uuid(), :e, 'v446 org', true) RETURNING id"
        ),
        {"e": f"v446_{uuid.uuid4().hex[:8]}"},
    ).scalar()


def test_a_row_inserted_without_a_mode_is_organization_wide(db_session, normal_user, org_id):
    assert _insert(db_session.connection(), org_id=org_id, user_id=normal_user.id) == "organization"


def test_restricted_is_allowed_on_an_organization_collection(db_session, normal_user, org_id):
    conn = db_session.connection()
    assert (
        _insert(conn, org_id=org_id, user_id=normal_user.id, visibility="restricted")
        == "restricted"
    )


@pytest.mark.parametrize(
    ("with_org", "visibility"),
    [(True, "private"), (False, "restricted"), (False, "bogus")],
)
def test_the_check_refuses_an_unknown_mode_and_a_restricted_personal_collection(
    db_session, normal_user, org_id, with_org, visibility
):
    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _insert(
                conn,
                org_id=org_id if with_org else None,
                user_id=normal_user.id,
                visibility=visibility,
            )


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent(db_session):
    conn = db_session.connection()
    count = "SELECT count(*) FROM pg_constraint WHERE conrelid = 'collection'::regclass"
    before = conn.execute(text(count)).scalar()
    conn.execute(text(_module().UPGRADE_SQL))
    conn.execute(text(_module().UPGRADE_SQL))
    assert conn.execute(text(count)).scalar() == before


def test_downgrade_drops_only_this_revisions_objects():
    sql = _module().DOWNGRADE_SQL
    assert re.findall(r"DROP CONSTRAINT IF EXISTS (\w+)", sql) == [_LAST_CHECK]
    assert re.findall(r"DROP INDEX IF EXISTS (\w+)", sql) == ["ix_collection_org_visibility"]
    assert re.findall(r"DROP COLUMN IF EXISTS (\w+)", sql) == ["visibility"]


def test_detection_arm_returns_v446_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_last_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE collection DROP CONSTRAINT {_LAST_CHECK}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
