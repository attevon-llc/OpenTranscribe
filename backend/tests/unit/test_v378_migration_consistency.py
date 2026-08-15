"""v378 migration + detection-arm consistency (IdP group mapping).

The alembic chain must contain v378 (revises v377), and the untracked-DB detection
in ``app/db/migrations.py`` must recognize a v378-shape schema by **both** markers:
the ``group_mapping`` table and ``user_group_member.source``. One without the other
is not this revision — a mapping table with no provenance column would leave
reconciliation unable to tell a directory-derived membership from a hand-added one,
which is the difference between "revocation works" and "a sync wipes manual work".

The substantive tests are the two CHECK constraints. ``ck_group_mapping_role_capped``
is the database's half of the rule that ``super_admin`` is unreachable from any
identity provider (the service enforces it too, but a code path can be bypassed and
a constraint cannot), and ``ck_group_mapping_grants_something`` rejects a mapping
that grants neither a group nor a role and would therefore be invisible dead config.
"""

from __future__ import annotations

import importlib.util
import uuid as uuid_pkg
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: `ddl_exclusive` is applied PER TEST below, never to the module. An EXCLUSIVE advisory-lock
#: acquisition drains every other xdist worker, so spending one on a read-only schema
#: assertion turns that assertion into a full-suite barrier — that is what made this group
#: 414 s of a 511 s wall clock. Only the tests that actually execute ALTER/DROP/CREATE carry
#: it; the lock's EXCLUSIVE mode already serialises them against each other across workers,
#: so `xdist_group` is not needed on top (issue #389, #431).
#: Both directions are enforced by `tests/unit/test_ddl_marker_discipline.py`.

REVISION = "v378_idp_group_mapping"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_user(conn) -> int:
    """A user row owned by this test, not borrowed from ambient data.

    ``SELECT id FROM "user" ORDER BY id LIMIT 1`` used to stand in for this —
    it works against a dev database with real accounts, but CI's fresh Postgres
    starts with zero rows until some other test happens to commit one first,
    which makes whether this passes depend on test execution order.
    """
    email = f"v378_{uuid_pkg.uuid4().hex[:8]}@example.com"
    new_id = conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
        ),
        {"e": email},
    ).scalar()
    return int(new_id)


def _new_group(conn, owner_id: int) -> int:
    """Create a throwaway ``user_group`` to hang mappings off; rolled back by the fixture."""
    new_id = conn.execute(
        text(
            "INSERT INTO user_group (uuid, name, owner_id) "
            "VALUES (gen_random_uuid(), :n, :o) RETURNING id"
        ),
        {"n": f"v378_{uuid_pkg.uuid4().hex[:8]}", "o": owner_id},
    ).scalar()
    return int(new_id)


def test_v378_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v377_harden_user_auth_invariants"

    heads = set(scripts.get_heads())
    assert len(heads) == 1
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v378_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    source = _REVISION_PATH.read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v378_on_current_schema(db_session):
    """An untracked DB carrying v378's markers must never stamp EARLIER than v378."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.ddl_exclusive
def test_detection_needs_both_markers(db_session):
    """The mapping table alone is not this revision.

    Modelled on v377's two-marker guard: a schema carrying only half the revision
    must stamp lower so the missing DDL is still applied on upgrade.
    """
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text("ALTER TABLE user_group_member DROP COLUMN IF EXISTS source"))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) != REVISION
    db_session.rollback()


def test_group_mapping_table_and_membership_source_exist(db_session):
    conn = db_session.connection()
    assert "group_mapping" in set(inspect(conn).get_table_names())
    columns = {c["name"] for c in inspect(conn).get_columns("user_group_member")}
    assert "source" in columns


def test_existing_memberships_are_manual(db_session):
    """Rows that predate v378 were added by a human and must stay untouchable."""
    conn = db_session.connection()
    non_manual = conn.execute(
        text("SELECT count(*) FROM user_group_member WHERE source IS DISTINCT FROM 'manual'")
    ).scalar()
    assert non_manual == 0, (
        "memberships that predate v378 must default to 'manual' so reconciliation "
        "never removes hand-added work"
    )


def test_membership_source_default_is_manual(db_session):
    """A row inserted without naming ``source`` must not become directory-owned."""
    conn = db_session.connection()
    owner_id = _insert_user(conn)
    group_id = _new_group(conn, owner_id)
    source = conn.execute(
        text(
            "INSERT INTO user_group_member (uuid, group_id, user_id, role) "
            "VALUES (gen_random_uuid(), :g, :u, 'member') RETURNING source"
        ),
        {"g": group_id, "u": owner_id},
    ).scalar()
    assert source == "manual"
    db_session.rollback()


def test_grants_role_check_rejects_super_admin(db_session):
    """The load-bearing rule: no identity provider may mint a super_admin.

    The service refuses it too, but a service check protects one code path and a
    CHECK constraint protects the database.
    """
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    owner_id = _insert_user(conn)
    group_id = _new_group(conn, owner_id)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO group_mapping (uuid, source, claim_value, user_group_id, grants_role) "
                "VALUES (gen_random_uuid(), 'ldap', :c, :g, 'super_admin')"
            ),
            {"c": f"CN=v378-{uuid_pkg.uuid4().hex[:8]},DC=example", "g": group_id},
        )
    db_session.rollback()


def test_a_mapping_must_grant_something(db_session):
    """Neither a group nor a role = invisible dead configuration."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO group_mapping (uuid, source, claim_value) "
                "VALUES (gen_random_uuid(), 'ldap', :c)"
            ),
            {"c": f"CN=v378-{uuid_pkg.uuid4().hex[:8]},DC=example"},
        )
    db_session.rollback()


def test_source_check_rejects_an_unknown_directory(db_session):
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    owner_id = _insert_user(conn)
    group_id = _new_group(conn, owner_id)
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO group_mapping (uuid, source, claim_value, user_group_id) "
                "VALUES (gen_random_uuid(), 'saml', :c, :g)"
            ),
            {"c": f"v378-{uuid_pkg.uuid4().hex[:8]}", "g": group_id},
        )
    db_session.rollback()


def test_ldap_claim_uniqueness_is_case_insensitive(db_session):
    """Two rows differing only in case would both match one AD group.

    DNs are case-insensitive and ``ldap_auth._is_member_of_groups`` already compares
    them lowercased, so the partial functional index is what keeps "one claim, one
    answer" true for LDAP.
    """
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    owner_id = _insert_user(conn)
    group_id = _new_group(conn, owner_id)
    dn = f"CN=v378-{uuid_pkg.uuid4().hex[:8]},OU=Groups,DC=example"
    insert = text(
        "INSERT INTO group_mapping (uuid, source, claim_value, user_group_id) "
        "VALUES (gen_random_uuid(), 'ldap', :c, :g)"
    )
    conn.execute(insert, {"c": dn, "g": group_id})
    with pytest.raises(IntegrityError):
        conn.execute(insert, {"c": dn.upper(), "g": group_id})
    db_session.rollback()


def test_oidc_claim_uniqueness_is_case_sensitive(db_session):
    """The mirror image: OIDC role strings are distinct identifiers, not DNs."""
    conn = db_session.connection()
    owner_id = _insert_user(conn)
    group_id = _new_group(conn, owner_id)
    role = f"v378-{uuid_pkg.uuid4().hex[:8]}"
    insert = text(
        "INSERT INTO group_mapping (uuid, source, claim_value, user_group_id) "
        "VALUES (gen_random_uuid(), 'oidc', :c, :g)"
    )
    conn.execute(insert, {"c": role, "g": group_id})
    conn.execute(insert, {"c": role.upper(), "g": group_id})
    count = conn.execute(
        text("SELECT count(*) FROM group_mapping WHERE lower(claim_value) = :c"),
        {"c": role.lower()},
    ).scalar()
    assert count == 2
    db_session.rollback()


def test_deleting_the_group_cascades_the_mapping(db_session):
    """A mapping to a deleted group would otherwise keep granting a role invisibly."""
    conn = db_session.connection()
    owner_id = _insert_user(conn)
    group_id = _new_group(conn, owner_id)
    conn.execute(
        text(
            "INSERT INTO group_mapping (uuid, source, claim_value, user_group_id, grants_role) "
            "VALUES (gen_random_uuid(), 'oidc', :c, :g, 'admin')"
        ),
        {"c": f"v378-{uuid_pkg.uuid4().hex[:8]}", "g": group_id},
    )
    conn.execute(text("DELETE FROM user_group WHERE id = :g"), {"g": group_id})
    remaining = conn.execute(
        text("SELECT count(*) FROM group_mapping WHERE user_group_id = :g"), {"g": group_id}
    ).scalar()
    assert remaining == 0
    db_session.rollback()


def test_downgrade_mirrors_the_upgrade():
    """Every object the upgrade creates has a guarded drop."""
    module = _revision_module()
    import inspect as py_inspect

    down = py_inspect.getsource(module.downgrade)
    for obj in (
        "group_mapping",
        "source",
        "ck_user_group_member_source_valid",
        "idx_user_group_member_user_source",
    ):
        assert obj in down
    assert down.count("IF EXISTS") >= 4
