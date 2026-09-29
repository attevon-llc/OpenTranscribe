"""v422 migration + detection-arm consistency (tenant-owned collections, issue #1051).

Like ``v420`` this suite **executes** the revision's SQL rather than grepping its
source. The backfill is replayed against freshly seeded rows in the *pre-v422
shape* — a collection owned by a user, with no tenant, holding files of several
tenants — which is exactly what an upgrading database holds.

The per-tenant unique indexes would reject part of that seed (two members'
same-named collections in one org are only representable *before* the merge), so
the tests that seed duplicates drop them inside the test transaction and re-create
them from the revision's own ``FINALIZE_SQL`` afterwards — which doubles as proof
that the backfilled data satisfies the new uniqueness. Everything rolls back.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

REVISION = "v422_add_collection_tenancy"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package."""
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
            {"e": f"v422_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _org(conn) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO organization (uuid, external_org_id, name, is_active) "
                "VALUES (gen_random_uuid(), :x, 'v422 org', true) RETURNING id"
            ),
            {"x": f"v422_{uuid_pkg.uuid4().hex[:10]}"},
        ).scalar()
    )


def _file(conn, user_id: int, org_id: int | None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, filename, storage_path, file_size, content_type, "
                "user_id, organization_id) VALUES (gen_random_uuid(), 'v422.mp3', :p, 1, "
                "'audio/mpeg', :u, :o) RETURNING id"
            ),
            {"p": f"v422/{uuid_pkg.uuid4().hex}", "u": user_id, "o": org_id},
        ).scalar()
    )


def _collection(conn, name: str, user_id: int, description: str | None = None) -> int:
    """A collection in the pre-v422 shape: owned, no tenant."""
    return int(
        conn.execute(
            text(
                "INSERT INTO collection (uuid, name, description, user_id, source) "
                "VALUES (gen_random_uuid(), :n, :d, :u, 'manual') RETURNING id"
            ),
            {"n": name, "d": description, "u": user_id},
        ).scalar()
    )


def _add(conn, collection_id: int, file_id: int) -> None:
    conn.execute(
        text(
            "INSERT INTO collection_member (uuid, collection_id, media_file_id) "
            "VALUES (gen_random_uuid(), :c, :f)"
        ),
        {"c": collection_id, "f": file_id},
    )


def _share(conn, collection_id: int, sharer: int, target: int) -> None:
    conn.execute(
        text(
            "INSERT INTO collection_share (uuid, collection_id, shared_by_id, target_type, "
            "target_user_id, permission) VALUES (gen_random_uuid(), :c, :s, 'user', :t, 'viewer')"
        ),
        {"c": collection_id, "s": sharer, "t": target},
    )


def _collections_of(conn, file_id: int) -> list[tuple[int, int | None, int | None]]:
    """``(collection_id, organization_id, user_id)`` for every collection holding a file."""
    return [
        (int(r[0]), r[1], r[2])
        for r in conn.execute(
            text(
                "SELECT c.id, c.organization_id, c.user_id FROM collection_member cm "
                "JOIN collection c ON c.id = cm.collection_id "
                "WHERE cm.media_file_id = :f ORDER BY c.id"
            ),
            {"f": file_id},
        )
    ]


def _drop_new_uniques(conn) -> None:
    conn.execute(text("DROP INDEX IF EXISTS uq_collection_org_name"))
    conn.execute(text("DROP INDEX IF EXISTS uq_collection_user_name"))


# --------------------------------------------------------------------------- #
# Chain + shape                                                               #
# --------------------------------------------------------------------------- #


def test_v422_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v421_add_media_playback_path"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v422_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_nullable_creator_check_and_per_tenant_uniques_exist(db_session):
    conn = db_session.connection()
    live = {c["name"]: c for c in inspect(conn).get_columns("collection")}
    assert live["user_id"]["nullable"] is True

    checks = {c["name"]: c for c in inspect(conn).get_check_constraints("collection")}
    assert "ck_collection_owner_or_org" in checks

    uniques = {tuple(u["column_names"]) for u in inspect(conn).get_unique_constraints("collection")}
    assert ("user_id", "name") not in uniques, "the cross-tenant per-user unique must be gone"

    indexdefs = dict(
        conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'collection'")
        ).fetchall()
    )
    assert "organization_id IS NULL" in indexdefs["uq_collection_user_name"]
    assert "organization_id IS NOT NULL" in indexdefs["uq_collection_org_name"]


def test_a_personal_collection_must_have_an_owner(db_session):
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    nested = conn.begin_nested()
    with pytest.raises(IntegrityError):
        conn.execute(
            text("INSERT INTO collection (uuid, name) VALUES (gen_random_uuid(), 'v422 orphan')")
        )
    nested.rollback()
    db_session.rollback()


def test_the_orm_mirrors_the_schema(db_session):
    from app.db.base import Base

    live = {
        c["name"]: c["nullable"] for c in inspect(db_session.connection()).get_columns("collection")
    }
    model = Base.metadata.tables["collection"]
    assert model.columns["user_id"].nullable == live["user_id"]
    index_names = {ix.name for ix in model.indexes}
    assert {"uq_collection_user_name", "uq_collection_org_name"} <= index_names
    assert "ck_collection_owner_or_org" in {c.name for c in model.constraints}


def test_detection_arm_returns_v422_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX uq_collection_org_name"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None
    order = _chain_order()
    assert (
        order.index("v421_add_media_playback_path") <= order.index(detected) < order.index(REVISION)
    )


# --------------------------------------------------------------------------- #
# Backfill semantics                                                          #
# --------------------------------------------------------------------------- #


@pytest.mark.ddl_exclusive
def test_backfill_moves_a_collection_whose_files_are_all_in_one_org(db_session):
    """Row id, shares and description survive the move."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        _drop_new_uniques(conn)
        owner, colleague = _user(conn), _user(conn)
        org = _org(conn)
        f1, f2 = _file(conn, owner, org), _file(conn, owner, org)
        coll = _collection(conn, f"v422 move {uuid_pkg.uuid4().hex[:6]}", owner, "kept")
        _add(conn, coll, f1)
        _add(conn, coll, f2)
        _share(conn, coll, owner, colleague)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.FINALIZE_SQL))

        assert _collections_of(conn, f1) == [(coll, org, owner)]
        assert _collections_of(conn, f2) == [(coll, org, owner)]
        shares = conn.execute(
            text("SELECT count(*) FROM collection_share WHERE collection_id = :c"), {"c": coll}
        ).scalar()
        assert shares == 1
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_splits_a_collection_holding_files_of_several_tenants(db_session):
    """Personal members keep the original row; each org gets its own copy, unshared."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        _drop_new_uniques(conn)
        owner, colleague = _user(conn), _user(conn)
        org_a, org_b = _org(conn), _org(conn)
        personal_file = _file(conn, owner, None)
        file_a = _file(conn, owner, org_a)
        file_b = _file(conn, owner, org_b)
        name = f"v422 split {uuid_pkg.uuid4().hex[:6]}"
        original = _collection(conn, name, owner, "about the thing")
        for f in (personal_file, file_a, file_b):
            _add(conn, original, f)
        _share(conn, original, owner, colleague)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.FINALIZE_SQL))

        assert _collections_of(conn, personal_file) == [(original, None, owner)]
        [(a_id, a_org, a_user)] = _collections_of(conn, file_a)
        [(b_id, b_org, b_user)] = _collections_of(conn, file_b)
        assert (a_org, a_user) == (org_a, owner)
        assert (b_org, b_user) == (org_b, owner)
        assert len({original, a_id, b_id}) == 3

        copies = conn.execute(
            text(
                "SELECT c.description, (SELECT count(*) FROM collection_share s "
                "WHERE s.collection_id = c.id) FROM collection c WHERE c.id IN (:a, :b)"
            ),
            {"a": a_id, "b": b_id},
        ).fetchall()
        assert [tuple(r) for r in copies] == [("about the thing", 0)] * 2
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_merges_members_same_named_collections_within_an_org(db_session):
    """Alice's and Bob's "Board" in one org collapse onto one row with both files once."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        _drop_new_uniques(conn)
        alice, bob, carol = _user(conn), _user(conn), _user(conn)
        org = _org(conn)
        alice_file = _file(conn, alice, org)
        bob_file = _file(conn, bob, org)
        both_file = _file(conn, alice, org)
        name = f"v422 merge {uuid_pkg.uuid4().hex[:6]}"
        alice_coll = _collection(conn, name, alice)
        bob_coll = _collection(conn, name, bob, "bob wrote this")
        _add(conn, alice_coll, alice_file)
        _add(conn, bob_coll, bob_file)
        _add(conn, alice_coll, both_file)
        _add(conn, bob_coll, both_file)
        _share(conn, alice_coll, alice, carol)
        _share(conn, bob_coll, bob, carol)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.FINALIZE_SQL))

        survivor = min(alice_coll, bob_coll)
        for f in (alice_file, bob_file, both_file):
            assert [c for c, _o, _u in _collections_of(conn, f)] == [survivor]
        rows = conn.execute(
            text("SELECT organization_id, description FROM collection WHERE name = :n"),
            {"n": name},
        ).fetchall()
        assert [tuple(r) for r in rows] == [(org, "bob wrote this")]
        shares = conn.execute(
            text("SELECT count(*) FROM collection_share WHERE collection_id = :c"),
            {"c": survivor},
        ).scalar()
        assert shares == 1
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_backfill_split_folds_into_an_existing_org_collection(db_session):
    """A split copy is not minted when the org already holds that name."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        _drop_new_uniques(conn)
        alice, bob = _user(conn), _user(conn)
        org = _org(conn)
        name = f"v422 fold {uuid_pkg.uuid4().hex[:6]}"
        # Bob's collection holds only org files -> moved there by phase 1.
        bob_coll = _collection(conn, name, bob)
        _add(conn, bob_coll, _file(conn, bob, org))
        # Alice's holds a personal file AND an org file -> her org file joins Bob's row.
        alice_coll = _collection(conn, name, alice)
        _add(conn, alice_coll, _file(conn, alice, None))
        alice_org_file = _file(conn, alice, org)
        _add(conn, alice_coll, alice_org_file)

        conn.execute(text(module.BACKFILL_SQL))
        conn.execute(text(module.FINALIZE_SQL))

        assert [c for c, _o, _u in _collections_of(conn, alice_org_file)] == [bob_coll]
        count = conn.execute(
            text("SELECT count(*) FROM collection WHERE name = :n AND organization_id = :o"),
            {"n": name, "o": org},
        ).scalar()
        assert count == 1
    finally:
        db_session.rollback()


def test_backfill_leaves_empty_and_personal_only_collections_personal(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        empty = _collection(conn, f"v422 idle {uuid_pkg.uuid4().hex[:6]}", owner)
        personal = _collection(conn, f"v422 mine {uuid_pkg.uuid4().hex[:6]}", owner)
        _add(conn, personal, _file(conn, owner, None))

        conn.execute(text(module.BACKFILL_SQL))

        rows = conn.execute(
            text("SELECT id, user_id, organization_id FROM collection WHERE id IN (:a, :b)"),
            {"a": empty, "b": personal},
        ).fetchall()
        assert sorted(tuple(r) for r in rows) == sorted(
            [(empty, owner, None), (personal, owner, None)]
        )
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        before = conn.execute(text("SELECT count(*) FROM collection")).scalar()
        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))
        after = conn.execute(text("SELECT count(*) FROM collection")).scalar()
        assert before == after
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_restores_the_old_shape_and_the_upgrade_reapplies(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        # Unattributed org rows / cross-tenant name reuse cannot be represented in
        # the old shape; clear them inside the transaction so the full restore runs.
        conn.execute(text("DELETE FROM collection WHERE user_id IS NULL"))
        conn.execute(
            text(
                "DELETE FROM collection c USING collection d WHERE c.user_id = d.user_id "
                "AND c.name = d.name AND c.id > d.id"
            )
        )
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))
        live = {c["name"]: c for c in inspect(conn).get_columns("collection")}
        assert live["user_id"]["nullable"] is False
        uniques = {
            tuple(u["column_names"]) for u in inspect(conn).get_unique_constraints("collection")
        }
        assert ("user_id", "name") in uniques

        conn.execute(text(module.UPGRADE_SQL))
        assert conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_indexes WHERE indexname = 'uq_collection_org_name')"
            )
        ).scalar()
        live = {c["name"]: c for c in inspect(conn).get_columns("collection")}
        assert live["user_id"]["nullable"] is True
    finally:
        db_session.rollback()
