"""v377 migration + detection-arm consistency (user auth invariants).

The alembic chain must contain v377 (revises v374), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v377-shape schema by its
markers (the ``ck_user_auth_type_valid`` constraint **and** the
``user_invitation`` table — the revision also carries the invitation /
email-verification schema, so the constraint alone would mis-stamp a database
that predates that half and it would never receive the new DDL).

The substantive test is ``test_backfill_repairs_null_role``: v369 added
``ck_user_superuser_matches_role`` to make ``is_superuser`` a derived mirror of
``role``, but left ``role`` NULLABLE — and PostgreSQL passes a CHECK that
evaluates to UNKNOWN, so a NULL role satisfied *both* v369 constraints while
carrying ``is_superuser = TRUE``. This replays the backfill against exactly that
shape.
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

REVISION = "v377_harden_user_auth_invariants"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v377_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v376_add_chat_projects"

    heads = set(scripts.get_heads())
    assert len(heads) == 1
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v377_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    source = _REVISION_PATH.read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v377_on_current_schema(db_session):
    """An untracked DB carrying v377's markers must never stamp EARLIER than v377."""
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


def test_role_and_auth_type_are_not_nullable(db_session):
    """A NULL role makes both v369 CHECKs evaluate to UNKNOWN, which passes."""
    conn = db_session.connection()
    for column in ("role", "auth_type"):
        nullable = conn.execute(
            text(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name='user' AND column_name=:c"
            ),
            {"c": column},
        ).scalar()
        assert nullable == "NO", f"user.{column} must be NOT NULL"


def test_invitation_and_verification_tables_exist(db_session):
    """The invitation flow's schema ships in v377, not a later revision."""
    conn = db_session.connection()
    tables = set(inspect(conn).get_table_names())
    assert {"user_invitation", "email_verification_token"} <= tables


def test_user_has_email_verification_columns(db_session):
    """``require_email_verification`` had no reader AND no column to read."""
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("user")}
    assert {"email_verified", "email_verified_at"} <= columns


@pytest.mark.ddl_exclusive
def test_existing_accounts_are_grandfathered_verified(db_session):
    """Turning ``require_email_verification`` on must not lock out the deployment.

    The grandfather UPDATE lives INSIDE the ADD COLUMN guard, so replaying the
    revision against a database that already has the column is correctly a no-op —
    an address an admin deliberately un-verified is never silently re-verified.
    Exercising it therefore means recreating the pre-v377 shape: drop the columns,
    then let the revision add them back.

    The previous version of this test counted unverified rows in the live table,
    so it went red the moment anybody registered — which proves nothing about the
    migration and turns a shared database into a source of false failures.
    """
    conn = db_session.connection()
    email = f"v377_grandfather_{uuid_pkg.uuid4().hex[:8]}@example.com"

    conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local')"
        ),
        {"e": email},
    )

    # Back to the shape the revision expects to find.
    conn.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS email_verified'))
    conn.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS email_verified_at'))

    conn.execute(text(_revision_module().EMAIL_VERIFIED_COLUMNS_SQL))

    verified = conn.execute(
        text('SELECT email_verified FROM "user" WHERE email = :e'), {"e": email}
    ).scalar()
    assert verified is True, (
        "an account that predates v377 must be grandfathered verified, or enabling "
        "require_email_verification strands everyone who already had an account"
    )
    db_session.rollback()


def test_invitation_auth_type_check_rejects_an_unknown_value(db_session):
    """An invitation is a promise about a row that does not exist yet.

    Without the CHECK, an out-of-set ``auth_type`` would only be caught when the
    account was created — i.e. after the invite had been emailed.
    """
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    admin_id = conn.execute(text('SELECT id FROM "user" ORDER BY id LIMIT 1')).scalar()
    if admin_id is None:
        pytest.skip("no user rows to attribute an invitation to")

    with pytest.raises(IntegrityError):
        conn.execute(
            text(
                "INSERT INTO user_invitation "
                "(uuid, email, role, auth_type, token_hash, expires_at, created_by_id) "
                "VALUES (gen_random_uuid(), :e, 'user', 'not-a-real-method', :t, "
                "now() + interval '1 day', :a)"
            ),
            {"e": f"v377_{uuid_pkg.uuid4().hex[:8]}@example.com", "t": "x" * 64, "a": admin_id},
        )
    db_session.rollback()


def test_auth_type_check_constraint_exists(db_session):
    conn = db_session.connection()
    assert conn.execute(
        text("SELECT EXISTS(SELECT 1 FROM pg_constraint WHERE conname='ck_user_auth_type_valid')")
    ).scalar()


def test_auth_type_check_rejects_an_unknown_value(db_session):
    """An unrecognised auth_type silently exempted the account from local MFA."""
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    user = User(
        email=f"v377_{uuid_pkg.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="not-a-real-method",
    )
    db_session.add(user)
    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


def test_refresh_token_has_session_lifetime_columns(db_session):
    """``refresh_token`` owns the session, so it owns the timeouts.

    Their only previous implementation, ``auth/session.py:SessionManager``, had no
    call sites — so ``SESSION_IDLE_TIMEOUT_MINUTES`` and
    ``SESSION_ABSOLUTE_TIMEOUT_MINUTES`` were configuration that changed nothing.
    """
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("refresh_token")}
    assert {"last_activity_at", "absolute_expires_at"} <= columns


def test_session_lifetime_columns_are_nullable(db_session):
    """NULL means "no cap recorded" for sessions that predate the columns.

    Making them NOT NULL (or backfilling them) would invalidate every live session
    on upgrade — a second forced sign-out in a release that already causes one via
    the token-type change.
    """
    conn = db_session.connection()
    nullable = {
        c["name"]: c["nullable"]
        for c in inspect(conn).get_columns("refresh_token")
        if c["name"] in ("last_activity_at", "absolute_expires_at")
    }
    assert nullable == {"last_activity_at": True, "absolute_expires_at": True}


def test_retired_pki_config_keys_are_deleted(db_session):
    """``pki_support_cac`` / ``pki_support_piv`` gated parsing that is unconditional."""
    conn = db_session.connection()
    conn.execute(text(_revision_module().RETIRED_AUTH_CONFIG_KEYS_SQL))
    remaining = conn.execute(
        text(
            "SELECT count(*) FROM auth_config "
            "WHERE config_key IN ('pki_support_cac', 'pki_support_piv')"
        )
    ).scalar()
    assert remaining == 0


@pytest.mark.ddl_exclusive
def test_backfill_repairs_null_role_and_unknown_auth_type(db_session):
    """Replay the backfill against the exact shape the constraints could not catch.

    The row is inserted with raw SQL because the ORM model declares both columns
    NOT NULL — the whole point is that the *database* allowed what the model
    forbade.
    """
    conn = db_session.connection()
    email = f"v377_{uuid_pkg.uuid4().hex[:8]}@example.com"

    # Drop the constraints for the duration so we can recreate the legacy shape,
    # then let the backfill prove it repairs it. The savepoint-isolated session
    # rolls all of this back.
    conn.execute(text('ALTER TABLE "user" ALTER COLUMN role DROP NOT NULL'))
    conn.execute(text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid'))
    conn.execute(text('ALTER TABLE "user" ALTER COLUMN auth_type DROP NOT NULL'))
    # The legacy shape is `is_superuser=true` with a NULL role, which v369's
    # mirror constraint also rejects — a NULL role makes it evaluate to UNKNOWN,
    # which is exactly the hole v377 closed. Drop it too or the row cannot be
    # created to be repaired.
    conn.execute(
        text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_superuser_matches_role')
    )

    conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, true, NULL, 'bogus')"
        ),
        {"e": email},
    )

    # The hole: is_superuser TRUE with a NULL role satisfied v369's CHECK.
    before = conn.execute(
        text('SELECT role, is_superuser, auth_type FROM "user" WHERE email=:e'), {"e": email}
    ).one()
    assert before.role is None
    assert before.is_superuser is True

    conn.execute(text(_revision_module().BACKFILL_SQL))

    after = conn.execute(
        text('SELECT role, is_superuser, auth_type FROM "user" WHERE email=:e'), {"e": email}
    ).one()
    assert after.role == "user", "a NULL role must be demoted, never guessed upward"
    assert after.is_superuser is False, "the derived mirror must be recomputed"
    assert after.auth_type == "local"
