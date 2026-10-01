"""v430 migration + detection-arm consistency (issue #1110).

Two things change in one revision:

* legacy speaker profiles, speaker collections and custom-vocabulary terms that were
  never stamped with a tenant get one where the evidence is unambiguous, and
* the per-user unique names on those three tables become per-user **per tenant**
  (NULL-safe: a personal row is tenant 0 for uniqueness).

Like ``v422`` the suite **executes** the revision's own SQL against rows seeded in the
pre-v430 shape (``organization_id`` NULL). Everything rolls back.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

REVISION = "v430_per_tenant_speaker_and_vocab_names"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"
_ALL = "infinity"


def _revision_module():
    """Load the revision by path — ``alembic/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stamp(conn, module, cutoff: str = _ALL) -> None:
    for statement in module.STAMP_STATEMENTS:
        conn.execute(text(statement), {"cutoff": cutoff})


def _ambiguous(conn, module) -> dict[tuple[str, int], tuple[str, list[int | None]]]:
    rows = conn.execute(text(module.AMBIGUOUS_REPORT_SQL), {"cutoff": _ALL}).mappings()
    return {(r["kind"], r["id"]): (r["reason"], sorted(r["candidates"], key=str)) for r in rows}


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
            {"e": f"v430_{uuid_pkg.uuid4().hex[:10]}@example.com"},
        ).scalar()
    )


def _org(conn, *members: int) -> int:
    org_id = int(
        conn.execute(
            text(
                "INSERT INTO organization (uuid, external_org_id, name, is_active) "
                "VALUES (gen_random_uuid(), :x, 'v430 org', true) RETURNING id"
            ),
            {"x": f"v430_{uuid_pkg.uuid4().hex[:10]}"},
        ).scalar()
    )
    for user_id in members:
        conn.execute(
            text(
                "INSERT INTO organization_membership (organization_id, user_id, role) "
                "VALUES (:o, :u, 'org:member')"
            ),
            {"o": org_id, "u": user_id},
        )
    return org_id


def _file(conn, user_id: int, org_id: int | None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO media_file (uuid, filename, storage_path, file_size, content_type, "
                "user_id, organization_id) VALUES (gen_random_uuid(), 'v430.mp3', :p, 1, "
                "'audio/mpeg', :u, :o) RETURNING id"
            ),
            {"p": f"v430/{uuid_pkg.uuid4().hex}", "u": user_id, "o": org_id},
        ).scalar()
    )


def _profile(conn, user_id: int, name: str | None = None, org_id: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO speaker_profile (uuid, user_id, name, organization_id) "
                "VALUES (gen_random_uuid(), :u, :n, :o) RETURNING id"
            ),
            {"u": user_id, "n": name or f"v430 {uuid_pkg.uuid4().hex[:8]}", "o": org_id},
        ).scalar()
    )


def _link(conn, profile_id: int, file_id: int) -> None:
    """A speaker of ``file_id`` linked to ``profile_id``."""
    conn.execute(
        text(
            "INSERT INTO speaker (uuid, user_id, organization_id, media_file_id, profile_id, name) "
            "SELECT gen_random_uuid(), mf.user_id, mf.organization_id, mf.id, :p, 'SPEAKER_00' "
            "FROM media_file mf WHERE mf.id = :f"
        ),
        {"p": profile_id, "f": file_id},
    )


def _speaker_collection(conn, user_id: int, *profile_ids: int, name: str | None = None) -> int:
    coll_id = int(
        conn.execute(
            text(
                "INSERT INTO speaker_collection (uuid, name, user_id) "
                "VALUES (gen_random_uuid(), :n, :u) RETURNING id"
            ),
            {"n": name or f"v430 {uuid_pkg.uuid4().hex[:8]}", "u": user_id},
        ).scalar()
    )
    for profile_id in profile_ids:
        conn.execute(
            text(
                "INSERT INTO speaker_collection_member (uuid, collection_id, speaker_profile_id) "
                "VALUES (gen_random_uuid(), :c, :p)"
            ),
            {"c": coll_id, "p": profile_id},
        )
    return coll_id


def _term(conn, user_id: int | None, term: str | None = None, org_id: int | None = None) -> int:
    return int(
        conn.execute(
            text(
                "INSERT INTO custom_vocabulary (user_id, organization_id, term, domain, is_active) "
                "VALUES (:u, :o, :t, 'general', true) RETURNING id"
            ),
            {"u": user_id, "o": org_id, "t": term or f"v430 {uuid_pkg.uuid4().hex[:8]}"},
        ).scalar()
    )


def _org_of(conn, table: str, row_id: int) -> int | None:
    value = conn.execute(
        text(f"SELECT organization_id FROM {table} WHERE id = :i"),  # noqa: S608 - fixed names
        {"i": row_id},
    ).scalar()
    return int(value) if value is not None else None


# --------------------------------------------------------------------------- #
# Chain + shape                                                               #
# --------------------------------------------------------------------------- #


def test_v430_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    heads = set(scripts.get_heads())

    assert rev.down_revision == "v422_add_collection_tenancy"
    assert len(heads) == 1, "two heads mean two branches both claimed a revision number"
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v430_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_per_user_uniques_are_replaced_by_per_tenant_ones(db_session):
    conn = db_session.connection()
    for table in ("speaker_profile", "speaker_collection"):
        uniques = {tuple(u["column_names"]) for u in inspect(conn).get_unique_constraints(table)}
        assert ("user_id", "name") not in uniques, f"{table}: the cross-tenant unique must be gone"

    indexdefs = dict(
        conn.execute(
            text(
                "SELECT indexname, indexdef FROM pg_indexes WHERE tablename IN "
                "('speaker_profile', 'speaker_collection', 'custom_vocabulary')"
            )
        ).fetchall()
    )
    assert "_custom_vocab_unique" not in indexdefs
    for name in (
        "uq_speaker_profile_user_tenant_name",
        "uq_speaker_collection_user_tenant_name",
        "uq_custom_vocab_user_tenant_term",
    ):
        assert name in indexdefs, name
        assert "UNIQUE" in indexdefs[name]
        assert "COALESCE(organization_id, 0)" in indexdefs[name], name


def test_the_orm_mirrors_the_schema():
    from app.db.base import Base

    expected = {
        "speaker_profile": "uq_speaker_profile_user_tenant_name",
        "speaker_collection": "uq_speaker_collection_user_tenant_name",
        "custom_vocabulary": "uq_custom_vocab_user_tenant_term",
    }
    for table, index_name in expected.items():
        model = Base.metadata.tables[table]
        indexes = {str(ix.name): ix for ix in model.indexes}
        assert index_name in indexes, table
        assert indexes[index_name].unique
        assert not any(
            {c.name for c in getattr(uc, "columns", [])} == {"user_id", "name"}
            for uc in model.constraints
            if uc.__class__.__name__ == "UniqueConstraint"
        ), f"{table}: the ORM still declares the per-user unique"
    assert "_custom_vocab_unique" not in {
        ix.name for ix in Base.metadata.tables["custom_vocabulary"].indexes
    }


@pytest.mark.parametrize(
    "table",
    ["speaker_profile", "speaker_collection", "custom_vocabulary"],
)
def test_one_user_may_reuse_a_name_in_another_tenant_but_not_twice_in_one(db_session, table):
    conn = db_session.connection()
    user = _user(conn)
    org_a, org_b = _org(conn, user), _org(conn, user)
    name = f"Board {uuid_pkg.uuid4().hex[:6]}"

    def insert(org_id: int | None) -> None:
        if table == "speaker_profile":
            _profile(conn, user, name, org_id)
        elif table == "speaker_collection":
            conn.execute(
                text(
                    "INSERT INTO speaker_collection (uuid, name, user_id, organization_id) "
                    "VALUES (gen_random_uuid(), :n, :u, :o)"
                ),
                {"n": name, "u": user, "o": org_id},
            )
        else:
            _term(conn, user, name, org_id)

    try:
        insert(org_a)
        insert(org_b)
        insert(None)
        for org_id in (org_a, None):  # NULL-safe: a second personal row is a duplicate too
            nested = conn.begin_nested()
            with pytest.raises(IntegrityError):
                insert(org_id)
            nested.rollback()
    finally:
        db_session.rollback()


def test_owner_less_vocabulary_terms_stay_unique_per_tenant(db_session):
    conn = db_session.connection()
    org = _org(conn)
    term = f"Instance {uuid_pkg.uuid4().hex[:6]}"
    try:
        _term(conn, None, term)
        _term(conn, None, term, org)
        nested = conn.begin_nested()
        with pytest.raises(IntegrityError):
            _term(conn, None, term)
        nested.rollback()
    finally:
        db_session.rollback()


def test_detection_arm_returns_v430_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    assert_detected_at_or_after(conn, inspect(conn).get_table_names(), REVISION)


@pytest.mark.ddl_exclusive
def test_detection_stamps_lower_without_the_marker(db_session):
    from app.db.migrations import _detect_schema_version
    from tests.unit._migration_detection import _chain_order

    conn = db_session.connection()
    try:
        conn.execute(text("DROP INDEX uq_speaker_profile_user_tenant_name"))
        detected = _detect_schema_version(conn, inspect(conn).get_table_names())
    finally:
        db_session.rollback()

    assert detected is not None
    order = _chain_order()
    assert (
        order.index("v422_add_collection_tenancy") <= order.index(detected) < order.index(REVISION)
    )


# --------------------------------------------------------------------------- #
# Backfill semantics                                                          #
# --------------------------------------------------------------------------- #


def test_profile_takes_the_tenant_of_its_linked_files(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org_a, org_b = _org(conn, owner), _org(conn, owner)
        profile = _profile(conn, owner)
        _link(conn, profile, _file(conn, owner, org_b))
        _link(conn, profile, _file(conn, owner, org_b))

        _stamp(conn, module)

        assert _org_of(conn, "speaker_profile", profile) == org_b
        assert org_a != org_b
        assert ("profile", profile) not in _ambiguous(conn, module)
    finally:
        db_session.rollback()


def test_profile_linked_across_tenants_is_reported_not_guessed(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org_a = _org(conn, owner)
        profile = _profile(conn, owner)
        _link(conn, profile, _file(conn, owner, org_a))
        _link(conn, profile, _file(conn, owner, None))

        _stamp(conn, module)

        assert _org_of(conn, "speaker_profile", profile) is None
        reason, candidates = _ambiguous(conn, module)[("profile", profile)]
        assert reason == "linked_files_span_tenants"
        assert candidates == sorted([org_a, None], key=str)
    finally:
        db_session.rollback()


def test_profile_linked_only_to_personal_files_stays_personal_and_unreported(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        _org(conn, owner)
        profile = _profile(conn, owner)
        _link(conn, profile, _file(conn, owner, None))

        _stamp(conn, module)

        assert _org_of(conn, "speaker_profile", profile) is None
        assert ("profile", profile) not in _ambiguous(conn, module)
    finally:
        db_session.rollback()


def test_unlinked_profile_and_terms_follow_an_owner_with_exactly_one_tenant(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org = _org(conn, owner)
        _file(conn, owner, org)
        profile = _profile(conn, owner)
        term = _term(conn, owner)
        empty_coll = _speaker_collection(conn, owner)

        _stamp(conn, module)

        assert _org_of(conn, "speaker_profile", profile) == org
        assert _org_of(conn, "custom_vocabulary", term) == org
        assert _org_of(conn, "speaker_collection", empty_coll) == org
    finally:
        db_session.rollback()


def test_owner_in_several_tenants_is_reported_for_unlinked_rows(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org_a = _org(conn, owner)
        _file(conn, owner, None)  # also works in the personal workspace
        profile = _profile(conn, owner)
        term = _term(conn, owner)
        coll = _speaker_collection(conn, owner)

        _stamp(conn, module)

        report = _ambiguous(conn, module)
        for kind, row_id, table in (
            ("profile", profile, "speaker_profile"),
            ("vocabulary", term, "custom_vocabulary"),
            ("speaker_collection", coll, "speaker_collection"),
        ):
            assert _org_of(conn, table, row_id) is None
            assert report[(kind, row_id)] == (
                "owner_in_several_tenants",
                sorted([org_a, None], key=str),
            )
    finally:
        db_session.rollback()


def test_community_rows_are_untouched_and_unreported(db_session):
    """No organization anywhere: nothing is stamped and nothing is ambiguous."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        profile = _profile(conn, owner)
        _link(conn, profile, _file(conn, owner, None))
        term = _term(conn, owner)
        system_term = _term(conn, None)

        _stamp(conn, module)

        report = _ambiguous(conn, module)
        assert _org_of(conn, "speaker_profile", profile) is None
        assert _org_of(conn, "custom_vocabulary", term) is None
        assert _org_of(conn, "custom_vocabulary", system_term) is None
        assert not {k for k in report if k[1] in (profile, term, system_term)}
    finally:
        db_session.rollback()


def test_speaker_collection_follows_its_member_profiles(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org_a, org_b = _org(conn, owner), _org(conn, owner)
        p_a = _profile(conn, owner)
        _link(conn, p_a, _file(conn, owner, org_a))
        p_b = _profile(conn, owner)
        _link(conn, p_b, _file(conn, owner, org_b))
        only_a = _speaker_collection(conn, owner, p_a)
        mixed = _speaker_collection(conn, owner, p_a, p_b)

        _stamp(conn, module)

        assert _org_of(conn, "speaker_collection", only_a) == org_a
        assert _org_of(conn, "speaker_collection", mixed) is None
        reason, candidates = _ambiguous(conn, module)[("speaker_collection", mixed)]
        assert reason == "member_profiles_span_tenants"
        assert candidates == sorted([org_a, org_b], key=str)
    finally:
        db_session.rollback()


def test_stamping_never_touches_rows_created_after_the_cutoff(db_session):
    """The re-run path (the backfill command) is bounded so it cannot re-stamp rows a
    user deliberately created in the personal workspace after the deploy."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        _org(conn, owner)
        term = _term(conn, owner)

        _stamp(conn, module, cutoff="2000-01-01T00:00:00+00:00")

        assert _org_of(conn, "custom_vocabulary", term) is None
    finally:
        db_session.rollback()


def test_already_stamped_rows_are_never_restamped(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org_a, org_b = _org(conn, owner), _org(conn, owner)
        profile = _profile(conn, owner, org_id=org_a)
        _link(conn, profile, _file(conn, owner, org_b))

        _stamp(conn, module)
        _stamp(conn, module)

        assert _org_of(conn, "speaker_profile", profile) == org_a
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_rerunning_the_upgrade_is_a_no_op(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org = _org(conn, owner)
        profile = _profile(conn, owner)
        _link(conn, profile, _file(conn, owner, org))

        conn.execute(text(module.UPGRADE_SQL))
        conn.execute(text(module.UPGRADE_SQL))

        assert _org_of(conn, "speaker_profile", profile) == org
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_restores_the_old_shape_and_the_upgrade_reapplies(db_session):
    module = _revision_module()
    conn = db_session.connection()
    try:
        # A name reused across tenants cannot be represented in the old shape; clear
        # any inside the transaction so the full restore runs.
        for table, col in (
            ("speaker_profile", "name"),
            ("speaker_collection", "name"),
        ):
            conn.execute(
                text(
                    f"DELETE FROM {table} c USING {table} d "  # noqa: S608 - fixed names
                    f"WHERE c.user_id = d.user_id AND c.{col} = d.{col} AND c.id > d.id"
                )
            )
        conn.execute(
            text(
                "DELETE FROM custom_vocabulary c USING custom_vocabulary d "
                "WHERE COALESCE(c.user_id, 0) = COALESCE(d.user_id, 0) AND c.term = d.term "
                "AND c.domain = d.domain AND c.id > d.id"
            )
        )
        conn.execute(text(module.DOWNGRADE_SQL))
        conn.execute(text(module.DOWNGRADE_SQL))

        for table in ("speaker_profile", "speaker_collection"):
            uniques = {
                tuple(u["column_names"]) for u in inspect(conn).get_unique_constraints(table)
            }
            assert ("user_id", "name") in uniques, table
        names = {
            r[0]
            for r in conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'custom_vocabulary'")
            )
        }
        assert "_custom_vocab_unique" in names
        assert "uq_custom_vocab_user_tenant_term" not in names

        conn.execute(text(module.UPGRADE_SQL))
        assert conn.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM pg_indexes "
                "WHERE indexname = 'uq_speaker_profile_user_tenant_name')"
            )
        ).scalar()
        uniques = {
            tuple(u["column_names"])
            for u in inspect(conn).get_unique_constraints("speaker_profile")
        }
        assert ("user_id", "name") not in uniques
    finally:
        db_session.rollback()


@pytest.mark.ddl_exclusive
def test_the_downgrade_leaves_the_per_tenant_shape_when_a_name_spans_tenants(db_session):
    """Partial downgrade, the v422 stance: nothing is deleted or renamed to fit."""
    module = _revision_module()
    conn = db_session.connection()
    try:
        owner = _user(conn)
        org = _org(conn, owner)
        name = f"Twice {uuid_pkg.uuid4().hex[:6]}"
        _profile(conn, owner, name, org)
        _profile(conn, owner, name, None)

        conn.execute(text(module.DOWNGRADE_SQL))

        count = conn.execute(
            text("SELECT count(*) FROM speaker_profile WHERE user_id = :u AND name = :n"),
            {"u": owner, "n": name},
        ).scalar()
        assert count == 2
        uniques = {
            tuple(u["column_names"])
            for u in inspect(conn).get_unique_constraints("speaker_profile")
        }
        assert ("user_id", "name") not in uniques
    finally:
        db_session.rollback()
