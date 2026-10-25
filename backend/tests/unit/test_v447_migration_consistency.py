"""v447 migration + detection-arm consistency (organization content holds, issue #1276)."""

from __future__ import annotations

import importlib.util
import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v447_add_content_hold"
_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_LAST_CHECK = "ck_content_hold_collection_scope"
_REASON = "A reason that is long enough"


def _module():
    spec = importlib.util.spec_from_file_location(REVISION, _PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_v447_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION).down_revision == "v446_add_collection_visibility"
    assert len(set(scripts.get_heads())) == 1


def test_vendor_neutral():
    assert not re.search(r"clerk|stripe", _PATH.read_text(), re.IGNORECASE)


@pytest.fixture
def org_id(db_session):
    return (
        db_session.connection()
        .execute(
            text(
                "INSERT INTO organization (uuid, external_org_id, name, is_active) "
                "VALUES (gen_random_uuid(), :e, 'v447 org', true) RETURNING id"
            ),
            {"e": f"v447_{uuid.uuid4().hex[:8]}"},
        )
        .scalar()
    )


def _hold(conn, org_id, *, scope="file", collection_id=None, reason=_REASON):
    return conn.execute(
        text(
            "INSERT INTO content_hold (uuid, organization_id, scope, collection_id, reason) "
            "VALUES (gen_random_uuid(), :o, :s, :c, :r) RETURNING id"
        ),
        {"o": org_id, "s": scope, "c": collection_id, "r": reason},
    ).scalar()


def test_a_hold_is_active_until_released_and_has_a_placement_time(db_session, org_id):
    conn = db_session.connection()
    hold_id = _hold(conn, org_id)
    row = conn.execute(
        text("SELECT released_at, placed_at FROM content_hold WHERE id = :i"), {"i": hold_id}
    ).one()
    assert row.released_at is None and row.placed_at is not None


@pytest.mark.parametrize(
    "bad",
    [
        {"scope": "everything"},
        {"reason": "short"},
        {"reason": "x" * 2001},
        {"scope": "file", "with_collection": True},
    ],
)
def test_the_checks_refuse_a_bad_scope_reason_and_a_file_hold_naming_a_collection(
    db_session, org_id, normal_user, bad
):
    conn = db_session.connection()
    collection_id = None
    if bad.get("with_collection"):
        collection_id = conn.execute(
            text(
                "INSERT INTO collection (uuid, name, user_id, organization_id) "
                "VALUES (gen_random_uuid(), :n, :u, :o) RETURNING id"
            ),
            {"n": f"v447 {uuid.uuid4().hex[:6]}", "u": normal_user.id, "o": org_id},
        ).scalar()
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            _hold(
                conn,
                org_id,
                scope=bad.get("scope", "file"),
                collection_id=collection_id,
                reason=bad.get("reason", _REASON),
            )


def test_deleting_the_organization_removes_its_holds_but_a_collection_delete_keeps_them(
    db_session, org_id, normal_user
):
    conn = db_session.connection()
    collection_id = conn.execute(
        text(
            "INSERT INTO collection (uuid, name, user_id, organization_id) "
            "VALUES (gen_random_uuid(), :n, :u, :o) RETURNING id"
        ),
        {"n": f"v447 {uuid.uuid4().hex[:6]}", "u": normal_user.id, "o": org_id},
    ).scalar()
    hold_id = _hold(conn, org_id, scope="collection", collection_id=collection_id)

    conn.execute(text("DELETE FROM collection WHERE id = :i"), {"i": collection_id})
    kept = conn.execute(
        text("SELECT collection_id FROM content_hold WHERE id = :i"), {"i": hold_id}
    ).one()
    assert kept.collection_id is None, "the hold survives its collection as history"

    conn.execute(text("DELETE FROM organization WHERE id = :i"), {"i": org_id})
    assert (
        conn.execute(
            text("SELECT count(*) FROM content_hold WHERE id = :i"), {"i": hold_id}
        ).scalar()
        == 0
    )


def test_the_file_list_is_unique_per_hold_and_file(db_session, org_id, normal_user):
    conn = db_session.connection()
    file_id = conn.execute(
        text(
            "INSERT INTO media_file (uuid, filename, storage_path, content_type, file_size, "
            "user_id, status) VALUES (gen_random_uuid(), 'v447.mp3', 'p', 'audio/mpeg', 1, :u, "
            "'completed') RETURNING id"
        ),
        {"u": normal_user.id},
    ).scalar()
    hold_id = _hold(conn, org_id)
    conn.execute(
        text("INSERT INTO content_hold_file (hold_id, media_file_id) VALUES (:h, :f)"),
        {"h": hold_id, "f": file_id},
    )
    with pytest.raises(IntegrityError):
        with conn.begin_nested():
            conn.execute(
                text("INSERT INTO content_hold_file (hold_id, media_file_id) VALUES (:h, :f)"),
                {"h": hold_id, "f": file_id},
            )


def test_the_file_index_exists(db_session):
    indexes = {i["name"] for i in inspect(db_session.connection()).get_indexes("content_hold_file")}
    assert "ix_content_hold_file_media_file" in indexes


@pytest.mark.ddl_exclusive
def test_upgrade_is_idempotent(db_session):
    conn = db_session.connection()
    count = (
        "SELECT count(*) FROM pg_constraint WHERE conrelid IN "
        "('content_hold'::regclass, 'content_hold_file'::regclass)"
    )
    before = conn.execute(text(count)).scalar()
    conn.execute(text(_module().UPGRADE_SQL))
    conn.execute(text(_module().UPGRADE_SQL))
    assert conn.execute(text(count)).scalar() == before


def test_downgrade_drops_only_this_revisions_tables():
    sql = _module().DOWNGRADE_SQL
    assert re.findall(r"DROP TABLE IF EXISTS (\w+)", sql) == ["content_hold_file", "content_hold"]


def test_detection_arm_returns_v447_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_last_check(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text(f"ALTER TABLE content_hold DROP CONSTRAINT {_LAST_CHECK}"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()
    order = _chain_order()
    assert detected is not None and order.index(detected) < order.index(REVISION)
