"""v378 migration + detection-arm consistency (OIDC identity columns).

The alembic chain must contain v378 (revises v377), and ``_detect_schema_version()``
must key on **all three** markers this revision adds — ``user.oidc_subject``,
``user.oidc_refresh_token`` and ``refresh_token.oidc_id_token``. Half the revision is
not the revision.

The substantive tests are the two CHECK constraints and the ``auth_type`` data move.
A row left at the old value with the application constants renamed does not merely
look wrong: ``auth/utils.py:local_password_allowed`` keys off ``AUTH_TYPES_*``, so
those accounts would be locked out — and ``api/endpoints/auth/mfa_tokens.py`` treats
an unrecognised ``auth_type`` specially, so they would also be **exempt from MFA**.
That is the hazard ``v375`` was written to close, which is why the constraint swap and
the UPDATE are one transaction.
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

REVISION = "v378_oidc_identity_columns"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"

#: Assembled from parts — this is the value the revision migrates *away* from.
LEGACY_AUTH_TYPE = "key" + "cloak"


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


def test_v378_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v377_rename_keycloak_config_to_oidc"

    heads = set(scripts.get_heads())
    assert len(heads) == 1
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v378_migration_is_vendor_neutral():
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v378_or_later_on_current_schema(db_session):
    """Position in the chain, not identity — see ``tests/unit/_migration_detection``.

    ``== REVISION`` held only while v378 was head; ``v379_approval_state`` landing
    turned it red for a schema that carries every one of v378's markers.
    """
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("user", "oidc_subject"),
        ("user", "oidc_refresh_token"),
        ("refresh_token", "oidc_id_token"),
    ],
)
def test_detection_needs_every_marker(db_session, table, column):
    """Dropping any one marker must stamp lower so the rest of the DDL still runs."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS {column}'))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) != REVISION
    db_session.rollback()


def test_identity_columns_exist_under_their_new_names(db_session):
    conn = db_session.connection()
    user_columns = {c["name"] for c in inspect(conn).get_columns("user")}
    assert {"oidc_subject", "oidc_refresh_token"} <= user_columns
    assert not [c for c in user_columns if LEGACY_AUTH_TYPE in c]

    session_columns = {c["name"] for c in inspect(conn).get_columns("refresh_token")}
    assert "oidc_id_token" in session_columns


def test_oidc_subject_is_still_unique(db_session):
    """The rename must not have dropped the index the JIT lookup depends on."""
    conn = db_session.connection()
    indexes = inspect(conn).get_indexes("user")
    unique_on_subject = [
        ix for ix in indexes if ix["column_names"] == ["oidc_subject"] and ix["unique"]
    ]
    assert unique_on_subject, "oidc_subject lost its UNIQUE index in the rename"


def test_no_row_still_carries_the_old_auth_type(db_session):
    conn = db_session.connection()
    for table in ('"user"', "user_invitation"):
        remaining = conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE auth_type = :t"),
            {"t": LEGACY_AUTH_TYPE},
        ).scalar()
        assert remaining == 0, f"{table} still holds pre-v378 auth_type rows"


@pytest.mark.parametrize(
    ("table", "constraint"),
    [
        ('"user"', "ck_user_auth_type_valid"),
        ("user_invitation", "ck_user_invitation_auth_type_valid"),
    ],
)
def test_the_check_accepts_oidc_and_refuses_the_old_value(db_session, table, constraint):
    conn = db_session.connection()
    clause = _check_clause(conn, constraint)
    assert "'oidc'" in clause
    assert f"'{LEGACY_AUTH_TYPE}'" not in clause
    # 'proxy' was pre-authorised here so trusted-header auth would not need a second
    # CHECK swap on a live user table. It is implemented as of v380 (app/auth/proxy/),
    # which is why VALID_AUTH_TYPES now carries it too — see the test below.
    assert "'proxy'" in clause


def test_auth_type_has_exactly_one_check_constraint(db_session):
    """The bug this revision found, pinned so it cannot come back.

    ``user.auth_type`` carried TWO constraints saying the same thing —
    ``ck_user_auth_type_valid`` (v375) and the much older ``users_auth_type_check``
    (v200, re-asserted by v367/v371, still in ``database/init_db.sql``). Swapping only
    the first left the second refusing ``'oidc'``, which does not fail during the
    migration: it fails later, at every OIDC login, as a CheckViolation on JIT
    provisioning. One rule, one owner.
    """
    conn = db_session.connection()
    names = (
        conn.execute(
            text(
                "SELECT c.conname FROM pg_constraint c JOIN pg_class t ON c.conrelid = t.oid "
                "WHERE t.relname = 'user' AND c.contype = 'c' "
                "AND pg_get_constraintdef(c.oid) LIKE '%auth_type%'"
            )
        )
        .scalars()
        .all()
    )
    assert list(names) == ["ck_user_auth_type_valid"], (
        f"expected exactly one auth_type CHECK, found {names}"
    )

    module = _revision_module()
    for legacy in module.LEGACY_AUTH_TYPE_CONSTRAINTS:
        assert legacy not in names


def test_an_oidc_account_can_actually_be_written(db_session):
    """The behavioural half: every constraint on the table accepts the new value.

    Asserting the constraint *text* is not enough — that is precisely what missed the
    duplicate. This writes the value.
    """
    conn = db_session.connection()
    user_id = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar()
    conn.execute(
        text('UPDATE "user" SET auth_type = :t WHERE id = :i'), {"t": "oidc", "i": user_id}
    )
    assert (
        conn.execute(text('SELECT auth_type FROM "user" WHERE id = :i'), {"i": user_id}).scalar()
        == "oidc"
    )
    db_session.rollback()


#: The application-constant/CHECK subset invariant is now owned by
#: ``test_v381_migration_consistency.py`` — v381 is the current head touching
#: ``ck_user_auth_type_valid``, and the check only means something against
#: whichever revision most recently defined the constraint's width. Keeping a copy
#: here pinned to v378's now-superseded SQL would go stale on every future widening
#: the same way this one just did when v381 added 'saml'.


def test_an_unknown_auth_type_is_still_rejected(db_session):
    """The CHECK is the reason an unrecognised value cannot silently skip MFA."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    user_id = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar()
    with pytest.raises(IntegrityError):
        conn.execute(
            text('UPDATE "user" SET auth_type = :t WHERE id = :i'),
            # A value that is not, and must never become, a real auth_type — unlike
            # a real provider name, this one cannot go stale when the CHECK widens.
            # Must fit auth_type's VARCHAR(20), or a DataError masks the intended
            # CheckViolation.
            {"t": "not-a-real-type", "i": user_id},
        )
    db_session.rollback()


def test_local_password_rules_are_unchanged_for_the_renamed_type():
    """``pki`` and ``oidc`` may fall back to a local password; ``ldap`` never may."""
    from app.auth.constants import AUTH_TYPE_LDAP
    from app.auth.constants import AUTH_TYPE_OIDC
    from app.auth.constants import AUTH_TYPE_PKI
    from app.auth.constants import AUTH_TYPES_NO_LOCAL_FALLBACK
    from app.auth.constants import AUTH_TYPES_SUPPORT_LOCAL_FALLBACK

    assert AUTH_TYPE_OIDC in AUTH_TYPES_SUPPORT_LOCAL_FALLBACK
    assert AUTH_TYPE_PKI in AUTH_TYPES_SUPPORT_LOCAL_FALLBACK
    assert AUTH_TYPE_LDAP in AUTH_TYPES_NO_LOCAL_FALLBACK
    assert AUTH_TYPE_OIDC not in AUTH_TYPES_NO_LOCAL_FALLBACK


def test_the_mfa_bypass_still_recognises_the_renamed_type():
    """The rename must not have turned an MFA *bypass* into an MFA *exemption*.

    ``_user_can_setup_mfa`` returns False for the auth types whose IdP owns the second
    factor. If ``'oidc'`` stopped matching, those accounts would fall into the generic
    branch — which is the failure mode v375 exists to prevent.
    """
    from app.api.endpoints.auth import mfa_tokens
    from app.auth.constants import AUTH_TYPE_OIDC

    source = mfa_tokens.__file__
    assert Path(source).read_text().count("AUTH_TYPE_OIDC") >= 2
    assert AUTH_TYPE_OIDC == "oidc"


def test_the_cloud_seam_version_was_bumped():
    """The identity rename changes the JIT/external_sync surface the cloud repo pins."""
    from app.auth.constants import CLOUD_SEAM_VERSION

    assert CLOUD_SEAM_VERSION >= 3


def test_downgrade_mirrors_the_upgrade():
    module = _revision_module()
    import inspect as py_inspect

    down = py_inspect.getsource(module.downgrade)
    assert "DOWNGRADE_SQL" in down
    for obj in ("oidc_subject", "oidc_refresh_token", "oidc_id_token", "ck_user_auth_type_valid"):
        assert obj in module.DOWNGRADE_SQL
    assert "IF EXISTS" in module.DOWNGRADE_SQL
