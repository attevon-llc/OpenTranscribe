"""v375 migration + detection-arm consistency (user auth invariants).

The alembic chain must contain v375 (revises v374), and the untracked-DB
detection in ``app/db/migrations.py`` must recognize a v375-shape schema by its
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

REVISION = "v375_harden_user_auth_invariants"
_REVISION_PATH = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{REVISION}.py"


def _revision_module():
    """Load the revision file by path (``alembic/`` is not importable — see v374)."""
    spec = importlib.util.spec_from_file_location(REVISION, _REVISION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_v375_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v374_add_tag_user_id"

    heads = set(scripts.get_heads())
    assert len(heads) == 1
    assert REVISION in heads or any(r.down_revision == REVISION for r in scripts.walk_revisions())


def test_v375_migration_is_vendor_neutral():
    """The seam guard greps for vendor nouns — the migration must stay generic."""
    source = _REVISION_PATH.read_text()
    # Nouns assembled from parts so this test file itself never trips the guard.
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v375_on_current_schema(db_session):
    """An untracked DB with the current (post-v375) schema stamps at v375."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == REVISION


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
    """The invitation flow's schema ships in v375, not a later revision."""
    conn = db_session.connection()
    tables = set(inspect(conn).get_table_names())
    assert {"user_invitation", "email_verification_token"} <= tables


def test_user_has_email_verification_columns(db_session):
    """``require_email_verification`` had no reader AND no column to read."""
    conn = db_session.connection()
    columns = {c["name"] for c in inspect(conn).get_columns("user")}
    assert {"email_verified", "email_verified_at"} <= columns


def test_existing_accounts_are_grandfathered_verified(db_session):
    """Turning the setting on must not retroactively lock out the deployment."""
    conn = db_session.connection()
    unverified = conn.execute(
        text('SELECT count(*) FROM "user" WHERE email_verified IS NOT TRUE')
    ).scalar()
    assert unverified == 0, (
        "accounts that predate v375 must be marked verified by the migration's one-time backfill"
    )


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
            {"e": f"v375_{uuid_pkg.uuid4().hex[:8]}@example.com", "t": "x" * 64, "a": admin_id},
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
        email=f"v375_{uuid_pkg.uuid4().hex[:8]}@example.com",
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


def test_backfill_repairs_null_role_and_unknown_auth_type(db_session):
    """Replay the backfill against the exact shape the constraints could not catch.

    The row is inserted with raw SQL because the ORM model declares both columns
    NOT NULL — the whole point is that the *database* allowed what the model
    forbade.
    """
    conn = db_session.connection()
    email = f"v375_{uuid_pkg.uuid4().hex[:8]}@example.com"

    # Drop the constraints for the duration so we can recreate the legacy shape,
    # then let the backfill prove it repairs it. The savepoint-isolated session
    # rolls all of this back.
    conn.execute(text('ALTER TABLE "user" ALTER COLUMN role DROP NOT NULL'))
    conn.execute(text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_auth_type_valid'))
    conn.execute(text('ALTER TABLE "user" ALTER COLUMN auth_type DROP NOT NULL'))

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
