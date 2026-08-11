"""v381 migration + detection-arm consistency (account approval state).

The chain must contain v381 (revising v380), and ``_detect_schema_version()`` must
key on **both** markers the revision adds — the ``user.approval_status`` column and
the ``ck_user_approval_status_valid`` CHECK. Both, because the enforcement helpers
(``app/auth/approval.is_pending`` / ``is_rejected``) read the column with a fail-safe
default: an unrecognised value there is treated as neither pending nor rejected, i.e.
it fails **open**. The constraint is what makes that read sound, so a database with
the column and no constraint has not had this revision and must still receive it.

The substantive test is :func:`test_existing_rows_are_approved_not_pending`. The
whole upgrade story is the ``'approved'`` default: get it wrong and every account in
every existing deployment is locked out behind a queue that only an administrator
— who is also locked out — can clear.
"""

# mypy: disable-error-code="arg-type"
# This suite passes structural stand-ins (dict payloads, fake sessions, fake
# users) to signatures declaring the real dataclasses. Declared once here
# rather than as a cast at every call site — casts bury the assertion, and
# widening a production signature to suit a test is worse.
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

REVISION = "v381_approval_state"
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
    starts with zero rows, so ``user_id`` was ``None`` and every assertion below
    quietly compared against a no-op UPDATE instead of testing the constraint.
    """
    email = f"v381_{uuid_pkg.uuid4().hex[:8]}@example.com"
    new_id = conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local') RETURNING id"
        ),
        {"e": email},
    ).scalar()
    return int(new_id)


def test_v381_revision_chain():
    from alembic.script import ScriptDirectory

    from app.db.migrations import get_alembic_config

    config = get_alembic_config()
    # alembic.ini's script_location is cwd-relative; pin it for the test runner.
    backend_dir = Path(__file__).resolve().parents[2]
    config.set_main_option("script_location", str(backend_dir / "alembic"))

    scripts = ScriptDirectory.from_config(config)
    rev = scripts.get_revision(REVISION)
    assert rev.down_revision == "v380_oidc_identity_columns"
    assert len(set(scripts.get_heads())) == 1


def test_v381_migration_is_vendor_neutral():
    """The CI seam guard greps for the managed edition's vendor nouns."""
    source = _REVISION_PATH.read_text()
    for vendor_noun in ("cl" + "erk", "str" + "ipe"):
        assert vendor_noun not in source.lower()


def test_detection_arm_returns_v381_or_later_on_current_schema(db_session):
    from tests.unit._migration_detection import assert_detected_at_or_after

    conn = db_session.connection()
    tables = inspect(conn).get_table_names()
    assert_detected_at_or_after(conn, tables, REVISION)


@pytest.mark.ddl_exclusive
def test_detection_needs_the_column(db_session):
    """Without the column the ladder must stamp lower so the DDL still runs."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS approval_status CASCADE'))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == "v380_oidc_identity_columns"
    db_session.rollback()


@pytest.mark.ddl_exclusive
def test_detection_needs_the_check_constraint(db_session):
    """The column alone is not the revision — see this module's docstring."""
    from app.db.migrations import _detect_schema_version

    conn = db_session.connection()
    conn.execute(text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_approval_status_valid'))
    tables = inspect(conn).get_table_names()
    assert _detect_schema_version(conn, tables) == "v380_oidc_identity_columns"
    db_session.rollback()


def test_the_columns_exist_with_the_documented_shape(db_session):
    conn = db_session.connection()
    columns = {c["name"]: c for c in inspect(conn).get_columns("user")}

    assert columns["approval_status"]["nullable"] is False
    assert "approved" in str(columns["approval_status"].get("default") or "")
    assert columns["approved_at"]["nullable"] is True
    assert columns["approved_by"]["nullable"] is True


def test_approved_by_is_a_self_fk_that_does_not_cascade_deletes(db_session):
    """Deleting the approving admin must not delete the accounts they approved."""
    conn = db_session.connection()
    rule = conn.execute(
        text("SELECT confdeltype FROM pg_constraint WHERE conname = 'fk_user_approved_by'")
    ).scalar()
    assert rule == "n", f"expected ON DELETE SET NULL ('n'), got {rule!r}"


@pytest.mark.ddl_exclusive
def test_existing_rows_are_approved_not_pending(db_session):
    """The entire upgrade story: nobody is locked out by taking this revision.

    Recreates the pre-v381 shape (drop the column, let the revision add it back)
    rather than counting un-approved rows in the live table. The counting version
    was red whenever anybody had legitimately been rejected — which says nothing
    about the migration, and made a shared test database a source of false
    failures. Same trap as v377's grandfathering test.
    """
    conn = db_session.connection()
    email = f"v381_upgrade_{uuid_pkg.uuid4().hex[:8]}@example.com"

    conn.execute(
        text(
            'INSERT INTO "user" (email, hashed_password, is_active, is_superuser, '
            "role, auth_type) VALUES (:e, 'x', true, false, 'user', 'local')"
        ),
        {"e": email},
    )

    # Back to the shape the revision expects to find.
    conn.execute(text('ALTER TABLE "user" DROP CONSTRAINT IF EXISTS ck_user_approval_status_valid'))
    conn.execute(text('ALTER TABLE "user" DROP COLUMN IF EXISTS approval_status'))

    conn.execute(text(_revision_module().UPGRADE_SQL))

    status = conn.execute(
        text('SELECT approval_status FROM "user" WHERE email = :e'), {"e": email}
    ).scalar()
    assert status == "approved", (
        "an account that predates v381 must land approved, or taking this revision "
        "locks out the entire deployment behind an empty approval queue"
    )
    db_session.rollback()


def test_the_check_refuses_an_unknown_status(db_session):
    """The constraint is what keeps the fail-safe read in approval.py sound."""
    from sqlalchemy.exc import IntegrityError

    conn = db_session.connection()
    user_id = _insert_user(conn)
    with pytest.raises(IntegrityError):
        conn.execute(
            text('UPDATE "user" SET approval_status = :s WHERE id = :i'),
            {"s": "maybe", "i": user_id},
        )
    db_session.rollback()


@pytest.mark.parametrize("state", ["pending", "approved", "rejected"])
def test_every_documented_state_can_actually_be_written(db_session, state):
    """Asserting the constraint text is not enough — v380 learned that the hard way."""
    conn = db_session.connection()
    user_id = _insert_user(conn)
    conn.execute(
        text('UPDATE "user" SET approval_status = :s WHERE id = :i'), {"s": state, "i": user_id}
    )
    assert (
        conn.execute(
            text('SELECT approval_status FROM "user" WHERE id = :i'), {"i": user_id}
        ).scalar()
        == state
    )
    db_session.rollback()


def test_the_application_constant_matches_the_check():
    """A value the code can write but the DB rejects is a 500 on an admin click."""
    from app.auth.approval import VALID_APPROVAL_STATUSES

    module = _revision_module()
    allowed = {part.strip().strip("'") for part in module.VALID_APPROVAL_STATUSES_SQL.split(",")}
    assert set(VALID_APPROVAL_STATUSES) == allowed


def test_rerunning_the_upgrade_is_a_no_op(db_session):
    """The startup runner stamps by fingerprint, so a revision re-runs routinely."""
    module = _revision_module()
    conn = db_session.connection()

    conn.execute(text(module.UPGRADE_SQL))
    conn.execute(text(module.CONSTRAINT_SQL))
    conn.execute(text(module.UPGRADE_SQL))
    conn.execute(text(module.CONSTRAINT_SQL))

    assert (
        conn.execute(
            text(
                "SELECT count(*) FROM pg_constraint WHERE conname = 'ck_user_approval_status_valid'"
            )
        ).scalar()
        == 1
    )
    db_session.rollback()


def test_the_index_backs_the_pending_queue(db_session):
    """``GET /admin/user-approvals`` filters on a column that is almost all one value."""
    conn = db_session.connection()
    indexes = inspect(conn).get_indexes("user")
    assert [ix for ix in indexes if ix["column_names"] == ["approval_status"]]


def test_downgrade_mirrors_the_upgrade():
    module = _revision_module()
    import inspect as py_inspect

    down = py_inspect.getsource(module.downgrade)
    assert "DOWNGRADE_SQL" in down
    for obj in (
        "approval_status",
        "approved_at",
        "approved_by",
        "ck_user_approval_status_valid",
        "fk_user_approved_by",
        "ix_user_approval_status",
    ):
        assert obj in module.DOWNGRADE_SQL
    assert "IF EXISTS" in module.DOWNGRADE_SQL
