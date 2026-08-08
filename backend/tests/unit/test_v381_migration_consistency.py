"""v381 migration + detection-arm consistency (SAML auth-type CHECK + identity column).

Same shape as ``test_v378_migration_consistency.py`` for the OIDC identity columns —
the alembic chain must contain v381 (revises v380), and ``_detect_schema_version()``
must key on **both** markers this revision adds — the widened
``ck_user_auth_type_valid`` and ``user.saml_subject``. Half the revision is not the
revision, the same reasoning v380 pinned for SCIM.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy import text

#: Schema DDL (dropping a column/constraint to recreate the pre-revision shape)
#: takes an ACCESS EXCLUSIVE lock — must not run beside other database tests.
pytestmark = pytest.mark.xdist_group("migration_ddl")

REVISION = "v381_saml_auth_type"
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


def test_v381_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v380_scim_tokens"

    heads = set(scripts.get_heads())
    assert len(heads) == 1
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_detection_arm_returns_v381_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("user", "saml_subject"),
    ],
)
def test_detection_needs_every_marker(db_session, table, column):
    """Dropping the marker column must stamp lower so the rest of the DDL still runs."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS {column}'))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) != REVISION
    db_session.rollback()


def test_saml_subject_column_exists_and_is_unique(db_session):
    conn = db_session.connection()
    user_columns = {c["name"] for c in inspect(conn).get_columns("user")}
    assert "saml_subject" in user_columns

    indexes = inspect(conn).get_indexes("user")
    unique_on_subject = [
        ix for ix in indexes if ix["column_names"] == ["saml_subject"] and ix["unique"]
    ]
    assert unique_on_subject, "saml_subject has no UNIQUE index"


def test_the_check_accepts_saml(db_session):
    conn = db_session.connection()
    clause = _check_clause(conn, "ck_user_auth_type_valid")
    assert "'saml'" in clause
    # Widened, not replaced — every value v378 pre-authorised must still be there.
    for prior in ("'local'", "'ldap'", "'oidc'", "'pki'", "'proxy'"):
        assert prior in clause

    invitation_clause = _check_clause(conn, "ck_user_invitation_auth_type_valid")
    assert "'saml'" in invitation_clause


def test_a_saml_account_can_actually_be_written(db_session):
    """The behavioural half: the constraint accepts the value, not just its text."""
    conn = db_session.connection()
    user_id = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar()
    conn.execute(
        text('UPDATE "user" SET auth_type = :t WHERE id = :i'), {"t": "saml", "i": user_id}
    )
    assert (
        conn.execute(text('SELECT auth_type FROM "user" WHERE id = :i'), {"i": user_id}).scalar()
        == "saml"
    )
    db_session.rollback()


def test_the_application_constant_is_a_subset_of_the_check():
    """The database may be wider than the app; the app must never be wider than the DB.

    A value the code can write but the CHECK rejects is an IntegrityError at COMMIT,
    i.e. a 500 on a login. This is the up-to-date copy of the invariant
    ``test_v378_migration_consistency.py`` used to pin — see that file's note on why
    ownership moves to whichever revision most recently widened the constraint.
    """
    from app.auth.constants import AUTH_TYPE_SAML
    from app.auth.constants import VALID_AUTH_TYPES

    module = _revision_module()
    allowed = {part.strip().strip("'") for part in module.VALID_AUTH_TYPES_SQL.split(",")}
    assert set(VALID_AUTH_TYPES) <= allowed
    assert AUTH_TYPE_SAML in allowed
    assert AUTH_TYPE_SAML in VALID_AUTH_TYPES


def test_saml_has_no_local_password_fallback():
    """SAML groups with LDAP, not OIDC/PKI — an assertion has no local-credential
    concept for a per-user allow_local_fallback flag to opt into."""
    from app.auth.constants import AUTH_TYPE_SAML
    from app.auth.constants import AUTH_TYPES_NO_LOCAL_FALLBACK
    from app.auth.constants import AUTH_TYPES_SUPPORT_LOCAL_FALLBACK

    assert AUTH_TYPE_SAML in AUTH_TYPES_NO_LOCAL_FALLBACK
    assert AUTH_TYPE_SAML not in AUTH_TYPES_SUPPORT_LOCAL_FALLBACK


def test_downgrade_mirrors_the_upgrade():
    module = _revision_module()
    import inspect as py_inspect

    down = py_inspect.getsource(module.downgrade)
    assert "DOWNGRADE_SQL" in down
    for obj in ("saml_subject", "ck_user_auth_type_valid"):
        assert obj in module.DOWNGRADE_SQL
    assert "IF EXISTS" in module.DOWNGRADE_SQL
    # A downgrade must not strand SAML rows against a shrinking constraint.
    assert "auth_type = 'local'" in module.DOWNGRADE_SQL
    assert "auth_type = 'saml'" in module.DOWNGRADE_SQL
