"""v420 migration + detection-arm consistency (tenant-owned tags, issue #1050).

Like ``v386``-``v397`` this suite **executes** the revision's SQL rather than
grepping its source. The backfill is replayed against freshly seeded rows in the
*pre-v420 shape* — a tag owned by a user, with no tenant, attached to files in
several tenants — which is exactly what an upgrading database holds.

The per-tenant unique indexes would reject part of that seed (two members' rows
of one name are only representable *before* the merge), so the tests that seed
duplicates drop them inside the test transaction and re-create them from the
revision's own ``CREATE_UNIQUES_SQL`` afterwards — which doubles as proof that
the backfilled data satisfies the new uniqueness. Everything rolls back.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v420_add_tag_organization_id"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package (see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Raw-SQL seeding helpers (the backfill is SQL, so is its fixture)             #
# --------------------------------------------------------------------------- #


def _user(conn) -> int:
    return int(
        conn.execute(
            text(
                'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
                "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
            ),
            {"e": f"v420_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _org(conn) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO organization (uuid, external_org_id, name, is_active) "
                "VALUES (gen_random_uuid(), :x, 'v420 org', true) RETURNING id"
            ),
            {"x": f"v420_{uuid_pkg.uuid4().hex[:10]}"},
        ).scalar()
    )


def _file(conn, user_id: int, org_id: int | None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, filename, storage_path, file_size, content_type, "
                "user_id, organization_id) VALUES (gen_random_uuid(), 'v420.mp3', :p, 1, "
                "'audio/mpeg', :u, :o) RETURNING id"
            ),
            {"p": f"v420/{uuid_pkg.uuid4().hex}", "u": user_id, "o": org_id},
        ).scalar()
    )


def _tag(conn, name: str, user_id: int | None) -> int:
    """A tag in the pre-v420 shape: owned (or system), no tenant."""
    return int(
        conn.execute(
            text(
                "INSERT INTO tag (uuid, name, source, normalized_name, user_id) "
                "VALUES (gen_random_uuid(), :n, 'manual', lower(:n), :u) RETURNING id"
            ),
            {"n": name, "u": user_id},
        ).scalar()
    )


def _attach(conn, file_id: int, tag_id: int) -> None:
    conn.execute(
        text(
            "INSERT INTO file_tag (uuid, media_file_id, tag_id, source) "
            "VALUES (gen_random_uuid(), :f, :t, 'manual')"
        ),
        {"f": file_id, "t": tag_id},
    )


def _tags_on(conn, file_id: int) -> list[tuple[int, int | None, int | None]]:
    """``(tag_id, organization_id, user_id)`` for every tag on a file."""
    return [
        (int(r[0]), r[1], r[2])
        for r in conn.execute(
            text(
                "SELECT t.id, t.organization_id, t.user_id FROM file_tag ft "
                "JOIN tag t ON t.id = ft.tag_id WHERE ft.media_file_id = :f ORDER BY t.id"
            ),
            {"f": file_id},
        )
    ]


# --------------------------------------------------------------------------- #
# Chain + shape                                                               #
# --------------------------------------------------------------------------- #


def test_v420_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v397_add_platform_super_admin_link_authorized"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v420_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_column_fk_and_per_tenant_uniques_exist(db_session):
    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("tag")}
    assert "organization_id" in live
    assert live["organization_id"]["nullable"] is True

    fks = {fk["name"]: fk for fk in inspect(conn).get_foreign_keys("tag")}
    assert fks["tag_organization_id_fkey"]["referred_table"] == "organization"

    indexdefs = dict(
        conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'tag'")
        ).fetchall()
    )
    assert "organization_id IS NULL" in indexdefs["uq_tag_user_name"]
    assert "organization_id IS NULL" in indexdefs["uq_tag_system_name"]
    assert "organization_id IS NOT NULL" in indexdefs["uq_tag_org_name"]


def test_the_orm_mirrors_the_column(db_session):
    from app.db.base import Base

    live = {c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("tag")}
    model = Base.metadata.tables["tag"]
    assert model.columns["organization_id"].nullable == live["organization_id"]


def test_detection_arm_returns_v420_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX uq_tag_org_name"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None
    order = _chain_order()
    assert (
        order.index("v397_add_platform_super_admin_link_authorized")
        <= order.index(detected)
        < order.index(REVISION)
    )


# --------------------------------------------------------------------------- #
# Backfill semantics                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.ddl_exclusive
def test_backfill_splits_a_tag_used_in_several_tenants(db_session):
    """One user's tag on a personal file and on files in two orgs → one row per tenant.

    The personal attachment keeps the original row (its id is unchanged); each
    organization gets its own row, and every file ends up carrying the row of
    its own tenant.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_org_name"))
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_user_name"))
        owner = _user(conn)
        org_a, org_b = _org(conn), _org(conn)
        personal_file = _file(conn, owner, None)
        file_a = _file(conn, owner, org_a)
        file_b = _file(conn, owner, org_b)
        name = f"v420 split {uuid_pkg.uuid4().hex[:6]}"
        original = _tag(conn, name, owner)
        for f in (personal_file, file_a, file_b):
            _attach(conn, f, original)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.CREATE_UNIQUES_SQL))

        assert _tags_on(conn, personal_file) == [(original, None, owner)]
        [(a_id, a_org, a_user)] = _tags_on(conn, file_a)
        [(b_id, b_org, b_user)] = _tags_on(conn, file_b)
        assert (a_org, a_user) == (org_a, owner)
        assert (b_org, b_user) == (org_b, owner)
        assert len({original, a_id, b_id}) == 3
        rows = conn.execute(text("SELECT count(*) FROM tag WHERE name = :n"), {"n": name}).scalar()
        assert rows == 3
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_moves_a_tag_used_only_inside_one_org(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_org_name"))
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_user_name"))
        owner = _user(conn)
        org = _org(conn)
        f1, f2 = _file(conn, owner, org), _file(conn, owner, org)
        tag = _tag(conn, f"v420 move {uuid_pkg.uuid4().hex[:6]}", owner)
        _attach(conn, f1, tag)
        _attach(conn, f2, tag)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.CREATE_UNIQUES_SQL))

        assert _tags_on(conn, f1) == [(tag, org, owner)]
        assert _tags_on(conn, f2) == [(tag, org, owner)]
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_merges_members_duplicates_within_an_org(db_session):
    """Alice's and Bob's same-named tags in one org collapse onto one row.

    A file that carried both ends up carrying the survivor exactly once.
    """
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_org_name"))
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_user_name"))
        alice, bob = _user(conn), _user(conn)
        org = _org(conn)
        alice_file = _file(conn, alice, org)
        bob_file = _file(conn, bob, org)
        both_file = _file(conn, alice, org)
        name = f"v420 merge {uuid_pkg.uuid4().hex[:6]}"
        alice_tag = _tag(conn, name, alice)
        bob_tag = _tag(conn, name, bob)
        _attach(conn, alice_file, alice_tag)
        _attach(conn, bob_file, bob_tag)
        _attach(conn, both_file, alice_tag)
        _attach(conn, both_file, bob_tag)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.CREATE_UNIQUES_SQL))

        survivor = min(alice_tag, bob_tag)
        for f in (alice_file, bob_file, both_file):
            assert [t for t, _o, _u in _tags_on(conn, f)] == [survivor]
        rows = conn.execute(
            text("SELECT organization_id FROM tag WHERE name = :n"), {"n": name}
        ).fetchall()
        assert rows == [(org,)]
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_split_folds_into_an_existing_org_row(db_session):
    """A split copy is not minted when the org already holds that name."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_org_name"))
        conn.execute(text("DROP INDEX IF EXISTS uq_tag_user_name"))
        alice, bob = _user(conn), _user(conn)
        org = _org(conn)
        name = f"v420 fold {uuid_pkg.uuid4().hex[:6]}"
        # Bob's tag lives only in the org → moved there by phase 1.
        bob_tag = _tag(conn, name, bob)
        _attach(conn, _file(conn, bob, org), bob_tag)
        # Alice's is used personally AND in the org → her org usage must join Bob's row.
        alice_tag = _tag(conn, name, alice)
        _attach(conn, _file(conn, alice, None), alice_tag)
        alice_org_file = _file(conn, alice, org)
        _attach(conn, alice_org_file, alice_tag)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.CREATE_UNIQUES_SQL))

        assert [t for t, _o, _u in _tags_on(conn, alice_org_file)] == [bob_tag]
        count = conn.execute(
            text("SELECT count(*) FROM tag WHERE name = :n AND organization_id = :o"),
            {"n": name, "o": org},
        ).scalar()
        assert count == 1
    finally:
        db_session.rollback()


def test_backfill_leaves_unattached_and_system_tags_alone(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org = _org(conn)
        unattached = _tag(conn, f"v420 idle {uuid_pkg.uuid4().hex[:6]}", owner)
        system = _tag(conn, f"v420 sys {uuid_pkg.uuid4().hex[:6]}", None)
        org_file = _file(conn, owner, org)
        _attach(conn, org_file, system)

        conn.execute(text(module.BACKFILL_SQL))

        row = conn.execute(
            text("SELECT user_id, organization_id FROM tag WHERE id = :i"), {"i": unattached}
        ).one()
        assert tuple(row) == (owner, None)
        assert _tags_on(conn, org_file) == [(system, None, None)]
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        before = conn.execute(text("SELECT count(*) FROM tag")).scalar()
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        after = conn.execute(text("SELECT count(*) FROM tag")).scalar()
        assert before == after
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_removes_the_column_and_the_upgrade_restores_it(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        assert "organization_id" not in {c["name"] for c in inspect(conn).get_columns("tag")}

        conn.execute(text(module.UPGRADE_SQL))
        assert "organization_id" in {c["name"] for c in inspect(conn).get_columns("tag")}
        assert conn.execute(
            text("SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'uq_tag_org_name')")
        ).scalar()
    finally:
        db_session.rollback()
