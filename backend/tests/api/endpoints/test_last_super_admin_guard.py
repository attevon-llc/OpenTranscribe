"""The deployment must never lose its last ``super_admin``.

Auth configuration, role changes, the audit log and SCIM tokens are all
super_admin-gated, so an install with zero super_admins is locked out of them
permanently — there is no recovery short of editing the database by hand.

``assert_not_last_super_admin`` existed and was wired into exactly two of the four
routes that can remove one (issue #431):

===============================================  ============  ==================
route                                            self-target   last super_admin
===============================================  ============  ==================
``PUT /api/admin/users/{uuid}/role``             n/a           refused
``DELETE /api/users/{uuid}``                     refused       refused
``DELETE /api/admin/users/{uuid}``               refused       **was allowed**
``POST /api/admin/gdpr/erase-user/{uuid}``       **allowed**   **was allowed**
===============================================  ============  ==================

So the deployment refused to *demote* its last super_admin while permitting the same
account to be *deleted*, and the GDPR route — which cascades object storage,
OpenSearch voiceprints and the relational rows before dropping the account — carried
neither guard. Reversible operation blocked, irreversible one open.

Each test below states the route it pins, because the guards live in three different
places and a future route that removes an account needs to appear here.
"""

from __future__ import annotations

import threading
import uuid as uuid_pkg

import pytest
from fastapi import status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.core.security import get_password_hash
from app.models.user import User
from tests.conftest import engine
from tests.db_locks import acquire_ddl_lock_shared

# One worker for every test that depends on how many active super_admins exist: the
# concurrency test below COMMITS two, and a "sole super_admin" test elsewhere in the
# same group would otherwise see them mid-run (test_account_lifecycle_service.py too).
pytestmark = pytest.mark.xdist_group("last_super_admin")


def _make_super_admin(db_session) -> User:
    """A second super_admin, so the deployment has more than one."""
    user = User(
        email=f"extra-sa-{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name="Extra Super Admin",
        hashed_password=get_password_hash("extrapass123"),
        is_active=True,
        is_superuser=True,
        role=ROLE_SUPER_ADMIN,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _super_admin_count(db_session) -> int:
    return int(db_session.query(User).filter(User.role == ROLE_SUPER_ADMIN).count())


def _neutralize_other_super_admins(db_session, keep: User) -> None:
    """Demote every OTHER active super_admin so ``keep`` is provably the sole one.

    This runs against the live dev database (see ``backend/tests/CLAUDE.md``), which
    already carries real super_admin accounts (e.g. the seeded admin). Without this,
    "the last super_admin" tests below would silently pass or fail depending on how
    many real accounts happen to exist, rather than on the guard.
    """
    others = db_session.query(User).filter(User.role == ROLE_SUPER_ADMIN, User.id != keep.id).all()
    for other in others:
        other.role = "user"
        other.is_superuser = False
    db_session.commit()


class TestGdprEraseUserGuards:
    """``POST /api/admin/gdpr/erase-user/{uuid}`` — the most destructive route."""

    def test_a_super_admin_cannot_erase_their_own_account(
        self, client, super_admin_token_headers, super_admin_user
    ):
        """Self-erasure would delete the caller mid-request, cascade and all."""
        response = client.post(
            f"/api/admin/gdpr/erase-user/{super_admin_user.uuid}",
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "own account" in response.json()["detail"]

    def test_erasing_a_non_last_super_admin_is_permitted(
        self, client, super_admin_token_headers, db_session
    ):
        """The positive control.

        Without it, "refuse everything" would satisfy the two refusal tests and the
        route would be unusable rather than merely safe.
        """
        victim = _make_super_admin(db_session)
        assert _super_admin_count(db_session) >= 2

        response = client.post(
            f"/api/admin/gdpr/erase-user/{victim.uuid}",
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_200_OK

    def test_an_unknown_uuid_is_404_not_500(self, client, super_admin_token_headers):
        response = client.post(
            f"/api/admin/gdpr/erase-user/{uuid_pkg.uuid4()}",
            headers=super_admin_token_headers,
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestAdminDeleteUserCompositionKeepsOneSuperAdmin:
    """``DELETE /api/admin/users/{uuid}`` — safe by COMPOSITION, not by the last-one guard.

    Worth stating plainly, because the first version of this class asserted something it
    could not reach. The two pre-existing guards already make losing the last super_admin
    impossible:

    * deleting a super_admin requires **being** one (403 otherwise), and
    * you cannot target yourself (400).

    So the caller is always a surviving super_admin, and ``assert_not_last_super_admin``
    on this route can never fire. It is kept as a backstop for a future change to either
    premise (see the comment at its call site), but a test named "deleting the last
    super_admin is refused" would really be asserting the self-target guard while
    appearing to cover the other one — and it passed against the unfixed code, which is
    how this was caught. These tests pin the two premises the safety actually rests on.
    """

    def test_a_plain_admin_cannot_delete_a_super_admin(
        self, client, admin_token_headers, db_session
    ):
        """Premise 1. Lose this 403 and a plain admin can delete every super_admin."""
        victim = _make_super_admin(db_session)

        response = client.delete(f"/api/admin/users/{victim.uuid}", headers=admin_token_headers)

        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert _super_admin_count(db_session) >= 1

    def test_a_super_admin_cannot_delete_their_own_account(
        self, client, super_admin_token_headers, super_admin_user
    ):
        """Premise 2. Together with premise 1, this is what guarantees a survivor."""
        response = client.delete(
            f"/api/admin/users/{super_admin_user.uuid}",
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST

    def test_a_super_admin_can_delete_another_super_admin(
        self, client, super_admin_token_headers, db_session
    ):
        """The positive control: the route must still work for its real use.

        Without it, tightening either premise into "refuse everything" would satisfy both
        tests above.
        """
        victim = _make_super_admin(db_session)

        response = client.delete(
            f"/api/admin/users/{victim.uuid}", headers=super_admin_token_headers
        )

        assert response.status_code == status.HTTP_200_OK


class TestUpdateUserDeactivationGuard:
    """``PUT /api/users/{uuid}`` with ``is_active: false`` — the fifth route.

    The role-change and delete guards were already wired; this route wrote
    ``is_active=False`` unguarded, so deactivating the last super_admin locked the
    deployment out exactly as demoting it would, with no HTTPException in between.
    """

    def test_deactivating_the_last_super_admin_is_refused(
        self, client, super_admin_token_headers, super_admin_user, db_session
    ):
        _neutralize_other_super_admins(db_session, super_admin_user)

        response = client.put(
            f"/api/users/{super_admin_user.uuid}",
            json={"is_active": False},
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert "last super_admin" in response.json()["detail"]

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.id == super_admin_user.id).one()
        assert refreshed.is_active is True

    def test_deactivating_a_non_last_super_admin_is_permitted(
        self, client, super_admin_token_headers, db_session
    ):
        """The positive control, so "refuse everything" cannot satisfy the guard alone."""
        victim = _make_super_admin(db_session)

        response = client.put(
            f"/api/users/{victim.uuid}",
            json={"is_active": False},
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_200_OK

        db_session.expire_all()
        refreshed = db_session.query(User).filter(User.id == victim.id).one()
        assert refreshed.is_active is False


class TestRefusalWireShape:
    """The exact status and JSON body of both last-super_admin refusals.

    The guards live below the API layer, so they raise a ``DomainError`` rather than
    an ``HTTPException``. ``main.py`` renders one through FastAPI's own handler, and
    these pins are what show a client cannot tell the difference.
    """

    @pytest.mark.parametrize(
        ("payload", "detail"),
        [
            (
                {"role": "user"},
                "Cannot demote the last super_admin — promote another account first.",
            ),
            (
                {"is_active": False},
                "Cannot deactivate the last super_admin — promote another account first.",
            ),
        ],
        ids=["demote", "deactivate"],
    )
    def test_the_refusal_body_is_exactly_the_detail(
        self, client, super_admin_token_headers, super_admin_user, db_session, payload, detail
    ):
        _neutralize_other_super_admins(db_session, super_admin_user)

        response = client.put(
            f"/api/users/{super_admin_user.uuid}",
            json=payload,
            headers=super_admin_token_headers,
        )

        assert response.status_code == status.HTTP_400_BAD_REQUEST
        assert response.headers["content-type"] == "application/json"
        assert response.json() == {"detail": detail}


class TestConcurrentGuardsSerialize:
    """Two transactions demoting different super_admins must not both pass the guard.

    Under READ COMMITTED an unlocked count lets Tx1 (demoting B) and Tx2 (demoting A)
    each count the other as a surviving super_admin, both commit, and a deployment
    whose only super_admins were A and B is left with none. The guard must make Tx2
    wait for Tx1 and then count B as already demoted.

    The rows have to be COMMITTED for a second connection to see them, so this test
    cannot use the savepoint-isolated ``db_session``; it creates and deletes its own.
    """

    def test_the_second_guard_waits_and_then_does_not_count_the_demoted_row(self):
        tag = uuid_pkg.uuid4().hex[:8]
        with engine.begin() as setup:
            acquire_ddl_lock_shared(setup)
            seeded = Session(bind=setup)
            a = User(
                email=f"race-a-{tag}@example.com",
                full_name="Race A",
                hashed_password=get_password_hash("racepass123"),
                is_active=True,
                is_superuser=True,
                role=ROLE_SUPER_ADMIN,
            )
            b = User(
                email=f"race-b-{tag}@example.com",
                full_name="Race B",
                hashed_password=get_password_hash("racepass123"),
                is_active=True,
                is_superuser=True,
                role=ROLE_SUPER_ADMIN,
            )
            seeded.add_all([a, b])
            seeded.flush()
            a_id, b_id = int(a.id), int(b.id)
            seeded.close()

        from app.services.account_security_service import assert_not_last_super_admin
        from app.services.account_security_service import count_other_active_super_admins

        with Session(engine) as probe:
            others_of_a_before = count_other_active_super_admins(probe, probe.get_one(User, a_id))
            probe.rollback()

        s1 = Session(engine)
        s2 = Session(engine)
        try:
            acquire_ddl_lock_shared(s1.connection())
            acquire_ddl_lock_shared(s2.connection())
            # A wedged wait must fail the test, not hang the worker.
            s2.execute(text("SET LOCAL lock_timeout = '10s'"))

            # Tx1: the guard passes for B (A survives), and B is demoted, uncommitted.
            b1 = s1.get_one(User, b_id)
            assert_not_last_super_admin(s1, b1, ROLE_USER)
            b1.role = ROLE_USER
            b1.is_superuser = False
            s1.flush()

            # Tx2: the guard for A, from another thread so a wait is observable.
            counted: dict[str, int] = {}

            def _tx2() -> None:
                counted["others"] = count_other_active_super_admins(s2, s2.get_one(User, a_id))

            worker = threading.Thread(target=_tx2)
            worker.start()
            worker.join(timeout=1.0)
            waited_for_tx1 = worker.is_alive()
            s1.commit()
            worker.join(timeout=15.0)

            assert not worker.is_alive(), "Tx2's guard never returned"
            assert waited_for_tx1, "Tx2 counted while Tx1 still held B's demotion"
            assert counted["others"] == others_of_a_before - 1, (
                "Tx2 still counted B, whose demotion had committed: with A and B the only "
                "super_admins, both demotions would pass and leave none"
            )
        finally:
            s2.rollback()
            s2.close()
            s1.rollback()
            s1.close()
            with engine.begin() as cleanup:
                cleanup.execute(
                    text('DELETE FROM "user" WHERE id IN (:a, :b)'), {"a": a_id, "b": b_id}
                )


class TestTheGuardItself:
    """``assert_not_last_super_admin`` in isolation, since three routes rely on it."""

    def test_it_refuses_when_the_target_is_the_only_super_admin(self, db_session):
        from app.auth.roles import ROLE_USER
        from app.core.exceptions import InvalidInputError
        from app.services.account_security_service import assert_not_last_super_admin

        sole = _make_super_admin(db_session)
        for other in (
            db_session.query(User).filter(User.role == ROLE_SUPER_ADMIN, User.id != sole.id).all()
        ):
            other.role = "user"
            other.is_superuser = False
        db_session.commit()
        assert _super_admin_count(db_session) == 1

        with pytest.raises(InvalidInputError) as exc:
            assert_not_last_super_admin(db_session, sole, ROLE_USER)

        assert exc.value.status_code == status.HTTP_400_BAD_REQUEST

    def test_it_permits_the_change_when_another_super_admin_remains(self, db_session):
        """Positive control: the guard must not refuse unconditionally."""
        from app.auth.roles import ROLE_USER
        from app.services.account_security_service import assert_not_last_super_admin
        from tests.helpers import does_not_raise

        victim = _make_super_admin(db_session)
        _make_super_admin(db_session)
        assert _super_admin_count(db_session) >= 2

        with does_not_raise("demoting one of several super_admins must be allowed"):
            assert_not_last_super_admin(db_session, victim, ROLE_USER)

    def test_promoting_to_super_admin_is_never_refused(self, db_session):
        """The `new_role == ROLE_SUPER_ADMIN` early return.

        Re-affirming an existing super_admin's role must not trip the guard, or an
        idempotent role write would fail on a single-super_admin deployment.
        """
        from app.services.account_security_service import assert_not_last_super_admin
        from tests.helpers import does_not_raise

        sole = _make_super_admin(db_session)

        with does_not_raise("re-affirming super_admin must not trip the last-one guard"):
            assert_not_last_super_admin(db_session, sole, ROLE_SUPER_ADMIN)
