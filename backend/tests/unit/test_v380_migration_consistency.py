"""v380 migration + detection-arm consistency (SCIM tokens, widened group sources).

The revision does two things, and the detection arm keys on **both**: a database with
``scim_token`` but not the widened ``ck_group_mapping_source_valid`` would be stamped
as already having v380 and would then reject every proxy login and every SCIM group
write with a CheckViolation, with no revision left to repair it.

The other thing pinned here is that the CHECK bodies in the revision and the tuples in
``app/models/group.py`` agree. Those are two hand-written lists of the same value set;
they disagreeing is a runtime IntegrityError on a write the ORM believed was legal.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: These suites perform schema DDL (dropping a column or constraint to recreate the
#: pre-revision shape). Postgres takes an ACCESS EXCLUSIVE lock for that, so they must
#: not run beside other database tests — `--dist loadgroup` keeps a group on one worker.
pytestmark = pytest.mark.xdist_group("migration_ddl")

REVISION = "v380_scim_tokens"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_clause(conn, name: str) -> str:
    clause = conn.execute(
        text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
        {"n": name},
    ).scalar()
    assert clause, f"{name} is missing"
    return str(clause)


def test_v380_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v379_approval_state"
    assert len(set(scripts.get_heads())) == 1


def test_v380_migration_is_vendor_neutral():
    """The CI seam guard greps for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v380_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


def test_detection_needs_the_table(db_session):
    """Without ``scim_token`` the ladder must stamp lower so the DDL still runs."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text("DROP TABLE IF EXISTS scim_token CASCADE"))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == "v379_approval_state"
    db_session.rollback()


def test_detection_needs_the_widened_check(db_session):
    """The table alone is not the revision — see this module's docstring."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text("ALTER TABLE group_mapping DROP CONSTRAINT ck_group_mapping_source_valid"))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == "v379_approval_state"
    db_session.rollback()


def test_the_table_has_the_documented_shape(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("scim_token")}

    assert columns["name"]["nullable"] is False
    assert columns["token_hash"]["nullable"] is False
    # NULL means "until revoked"; NULL last_used_at means "never used".
    assert columns["expires_at"]["nullable"] is True
    assert columns["last_used_at"]["nullable"] is True
    assert columns["revoked_at"]["nullable"] is True
    # created_by must survive the issuing administrator's deletion.
    assert columns["created_by"]["nullable"] is True


def test_token_hash_is_unique(db_session):
    """Verification is one indexed equality; a duplicate digest would break it."""
    conn = db_session.connection()
    indexes = conn.execute(
        text("SELECT indexdef FROM pg_indexes WHERE tablename = 'scim_token'")
    ).scalars()
    assert any("UNIQUE" in d and "token_hash" in d for d in indexes)


def test_created_by_does_not_cascade_deletes(db_session):
    """Deleting the issuing admin must not delete the integration's credential."""
    conn = db_session.connection()
    rule = conn.execute(
        text("SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_scim_token_created_by'")
    ).scalar()
    assert rule == "n", f"expected ON DELETE SET NULL ('n'), got {rule!r}"


@pytest.mark.parametrize(
    ("constraint", "expected"),
    [
        ("ck_group_mapping_source_valid", ("ldap", "oidc", "proxy")),
        ("ck_user_group_member_source_valid", ("manual", "scim", "ldap", "oidc", "proxy")),
    ],
)
def test_the_live_checks_carry_the_new_values(db_session, constraint, expected):
    clause = _check_clause(db_session.connection(), constraint)
    for value in expected:
        assert f"'{value}'" in clause


def test_the_revision_and_the_model_agree_on_the_value_sets():
    """Two hand-written copies of one value set; pin that they are the same set."""
    from app.models.group import MAPPING_SOURCES_SQL
    from app.models.group import MEMBERSHIP_SOURCES_SQL

    module = _revision_module()

    def _values(sql: str) -> set[str]:
        return {part.strip().strip("'") for part in sql.split(",")}

    assert _values(module.MAPPING_SOURCES_SQL) == _values(MAPPING_SOURCES_SQL)
    assert _values(module.MEMBERSHIP_SOURCES_SQL) == _values(MEMBERSHIP_SOURCES_SQL)


def test_the_check_still_refuses_an_unknown_source(db_session):
    """Widening is not the same as removing the constraint."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO group_mapping (uuid, source, claim_value, grants_role) "
                "VALUES (gen_random_uuid(), 'saml', 'CN=Nope', 'user')"
            )
        )
    db_session.rollback()
