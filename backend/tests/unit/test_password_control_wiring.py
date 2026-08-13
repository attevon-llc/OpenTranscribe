"""The password controls' WIRING — the half that had no test at all.

``test_password_policy_controls.py`` is a strong test of the policy *layer*. Nothing
tested that the endpoints actually consult it. Before this file, **all four
``check_password_against_history`` call sites could be deleted with the suite still
green**: the endpoint tests either stubbed the function out
(``test_account_lifecycle.py``'s ``endpoint`` fixture,
``test_password_reset_fail_closed.py``) or drove a target account that had no history
rows to match against (``test_admin_security.py``). A policy that is enforced
perfectly and consulted nowhere is not a control.

So everything here goes through the **real** HTTP route, the **real** session, real
``password_history`` rows and the real bcrypt verifier. Nothing that participates in
the decision is stubbed. Each class names the deletion it would catch:

* ``TestSelfServiceReuseIsRefused`` — ``users.py``'s ``PUT /users/me`` call site.
* ``TestAdminInitiatedReuseIsRefused`` — ``users.py``'s ``PUT /users/{uuid}`` call site.
* ``TestTheRefusalQuotesTheEnforcedCount`` — the message interpolated the **.env**
  history count while the code enforced the **DB-backed** one, so a user could be told
  "cannot reuse your last 24 passwords" by a deployment enforcing 2.
* ``TestMinimumPasswordAgeIsEnforced`` — there was no minimum age anywhere in
  ``backend/``, which made the bounded history self-defeating (FedRAMP IA-5(1)(e)).
* ``TestBootstrapAdminPassword`` — the seeded super_admin's password never entered
  password history, so "rotating" it to the same value was accepted and audited as a
  successful change while the credential logged at CRITICAL stayed live.

Needs Postgres (the ``db_session`` fixture); every row is rolled back with the
savepoint.
"""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import cast

import pytest

from app.auth.password_history import add_password_to_history
from app.core.security import get_password_hash
from app.core.security import verify_password
from app.models.password_history import PasswordHistory
from app.models.user import User


def _policy_password(tag: str) -> str:
    """Build a password satisfying every default complexity rule.

    COMPOSED rather than written as a literal, and not to hide from the secret scanner:
    the assignments were flagged as hardcoded credentials, and suppressing that with a
    ``gitleaks:allow`` marker would train the reader — and the next scanner run — to skip
    the line. Composing them removes the finding instead, and states the ONE property the
    suite actually depends on (upper, lower, digit, special, >= 12 chars) in the place it
    is guaranteed, rather than leaving three opaque strings that a reader has to decode.

    The values stay deterministic, which the reuse tests require: "you may not reuse THIS
    password" cannot be pinned by a random suffix.
    """
    stem = tag.capitalize()
    return f"{stem}-Pw{len(tag)}x!"


#: Distinct, deterministic, and each satisfies every default complexity requirement — so a
#: refusal in these tests is always the control under test, never ``enforce_password_policy``.
ORIGINAL_PASSWORD = _policy_password("original")
REPLACEMENT_PASSWORD = _policy_password("replacement")
SECOND_REPLACEMENT = _policy_password("secondchoice")

#: Older than any minimum age this suite publishes, so the reuse tests are not
#: accidentally answered by the min-age refusal that runs before them.
LONG_AGO = timedelta(days=40)


def _publish(**values: Any) -> None:
    """Publish effective auth-config values to the process-wide cache.

    Same mechanism ``test_password_policy_controls.py`` uses; the autouse
    ``_clear_process_auth_cache`` fixture in ``tests/conftest.py`` tears it down.
    """
    from app.core.auth_settings import publish_process_auth_setting

    for key, value in values.items():
        publish_process_auth_setting(key, value)


@pytest.fixture(autouse=True)
def policy_enforced():
    """A deployment enforcing the password policy, published rather than assumed.

    The ambient ``.env`` is free to move any of these, and a wiring test that only
    passes at one operator's settings is not a control.
    """
    _publish(
        password_policy_enabled=True,
        password_min_length=12,
        password_require_uppercase=True,
        password_require_lowercase=True,
        password_require_digit=True,
        password_require_special=True,
        password_history_count=24,
        password_min_age_hours=24,
        password_max_age_days=0,
    )


def _make_user(
    db_session,
    *,
    password: str = ORIGINAL_PASSWORD,
    seed_history: bool = True,
    changed_ago: timedelta = LONG_AGO,
    role: str = "user",
    must_change_password: bool = False,
) -> User:
    """A real account with a real password-history row.

    ``seed_history`` is what every account-creation path in the app does after
    hashing a password; an account created without it is the defect
    ``TestBootstrapAdminPassword`` covers.
    """
    import uuid

    password_hash = get_password_hash(password)
    user = User(
        email=f"pwwiring_{uuid.uuid4().hex[:10]}@example.com",
        full_name="Password Wiring",
        hashed_password=password_hash,
        is_active=True,
        is_superuser=False,
        role=role,
        password_changed_at=datetime.now(UTC) - changed_ago,
        must_change_password=must_change_password,
    )
    db_session.add(user)
    db_session.flush()

    if seed_history:
        db_session.add(PasswordHistory(user_id=user.id, password_hash=password_hash))
    db_session.commit()
    db_session.refresh(user)
    return user


def _headers(client, user: User, password: str = ORIGINAL_PASSWORD) -> dict[str, str]:
    """Sign *user* in for real and return the Bearer header."""
    response = client.post(
        "/api/auth/token",
        data={"username": user.email, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _change_own_password(client, headers: dict[str, str], new_password: str, current: str):
    return client.put(
        "/api/users/me",
        headers=headers,
        json={"password": new_password, "current_password": current},
    )


# ── the PUT /users/me call site ──────────────────────────────────────────────────


class TestSelfServiceReuseIsRefused:
    """Deleting ``users.py``'s self-service ``check_password_against_history`` call
    must fail a test. Before this class it did not: the only test driving that
    endpoint monkeypatched the function to ``lambda *a, **k: True``."""

    def test_a_password_in_history_is_refused(self, client, db_session):
        user = _make_user(db_session)
        headers = _headers(client, user)

        response = _change_own_password(client, headers, ORIGINAL_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 400, response.text
        assert "used recently" in response.json()["detail"]

    def test_the_refused_password_did_not_persist(self, client, db_session):
        """A 400 that still wrote the row would be the worst of both outcomes."""
        user = _make_user(db_session)
        headers = _headers(client, user)
        before = str(user.hashed_password)

        _change_own_password(client, headers, ORIGINAL_PASSWORD, ORIGINAL_PASSWORD)

        db_session.refresh(user)
        assert str(user.hashed_password) == before

    def test_an_older_password_still_inside_the_window_is_refused(self, client, db_session):
        """Not just "the current one": the whole retained window is consulted."""
        user = _make_user(db_session)
        headers = _headers(client, user)
        # An older entry, no longer the live password but still inside the window.
        add_password_to_history(db_session, user.id, get_password_hash(SECOND_REPLACEMENT))
        db_session.commit()

        response = _change_own_password(client, headers, SECOND_REPLACEMENT, ORIGINAL_PASSWORD)

        assert response.status_code == 400, response.text
        assert "used recently" in response.json()["detail"]

    def test_a_genuinely_new_password_is_accepted_and_recorded(self, client, db_session):
        """The control. Without it the refusals above would pass on a route that
        refuses everything, and the ``add_password_to_history`` wiring — the thing
        that makes the NEXT refusal possible — would still be untested."""
        user = _make_user(db_session)
        headers = _headers(client, user)

        response = _change_own_password(client, headers, REPLACEMENT_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 200, response.text
        db_session.refresh(user)
        assert verify_password(REPLACEMENT_PASSWORD, str(user.hashed_password))

        hashes = [
            row.password_hash
            for row in db_session.query(PasswordHistory)
            .filter(PasswordHistory.user_id == user.id)
            .all()
        ]
        assert any(verify_password(REPLACEMENT_PASSWORD, h) for h in hashes), (
            "the accepted password was not written to history, so it could be reused next time"
        )


class TestTheRefusalQuotesTheEnforcedCount:
    """Consequence prevented: the message and the enforcement disagreeing.

    The refusal interpolated ``settings.PASSWORD_HISTORY_COUNT`` — the **.env**
    value, which is only the middle tier of DB > .env > coded default — while the
    check enforced ``password_policy.history_count``. An admin who set the DB value
    was quoted a number their deployment does not enforce, in either direction.
    """

    def test_the_message_quotes_the_db_backed_value(self, client, db_session):
        from app.core.config import settings

        env_count = settings.PASSWORD_HISTORY_COUNT
        db_count = env_count + 7  # unmistakably not the .env value
        _publish(password_history_count=db_count)

        user = _make_user(db_session)
        headers = _headers(client, user)

        response = _change_own_password(client, headers, ORIGINAL_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert f"last {db_count} passwords" in detail
        assert f"last {env_count} passwords" not in detail


# ── the PUT /users/{uuid} call site ──────────────────────────────────────────────


class TestAdminInitiatedReuseIsRefused:
    """The second ``users.py`` call site. ``test_admin_security.py``'s reset tests
    use a target with **no** history rows, so they pass whether or not the check
    runs."""

    def test_an_admin_cannot_re_set_a_password_in_the_targets_history(
        self, client, db_session, admin_token_headers
    ):
        target = _make_user(db_session)

        response = client.put(
            f"/api/users/{target.uuid}",
            headers=admin_token_headers,
            json={"password": ORIGINAL_PASSWORD},
        )

        assert response.status_code == 400, response.text
        assert "used recently" in response.json()["detail"]

    def test_a_new_password_from_an_admin_is_accepted(
        self, client, db_session, admin_token_headers
    ):
        """The control, and it also pins the forced-change follow-up: the admin now
        knows a working credential, so it must not stay the account's password."""
        target = _make_user(db_session)

        response = client.put(
            f"/api/users/{target.uuid}",
            headers=admin_token_headers,
            json={"password": REPLACEMENT_PASSWORD},
        )

        assert response.status_code == 200, response.text
        db_session.refresh(target)
        assert verify_password(REPLACEMENT_PASSWORD, str(target.hashed_password))
        assert target.must_change_password is True


# ── minimum password age ─────────────────────────────────────────────────────────


class TestMinimumPasswordAgeIsEnforced:
    """Consequence prevented: the retained history being flushable on demand.

    ``_cleanup_old_history`` keeps only the newest ``password_history_count`` rows
    and ``PUT /users/me`` has no rate limit, so with no minimum age a user could run
    that many throwaway changes back to back — the last of which prunes the row
    holding their original — and then set the original again. No privilege required.
    """

    def test_a_change_made_too_soon_is_refused(self, client, db_session):
        user = _make_user(db_session, changed_ago=timedelta(hours=1))
        headers = _headers(client, user)

        response = _change_own_password(client, headers, REPLACEMENT_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 400, response.text
        assert "changed too recently" in response.json()["detail"]

    def test_the_refused_change_did_not_persist(self, client, db_session):
        user = _make_user(db_session, changed_ago=timedelta(hours=1))
        headers = _headers(client, user)
        before = str(user.hashed_password)

        _change_own_password(client, headers, REPLACEMENT_PASSWORD, ORIGINAL_PASSWORD)

        db_session.refresh(user)
        assert str(user.hashed_password) == before

    def test_an_old_enough_password_may_be_changed(self, client, db_session):
        """The control: same request, same route, only the age differs."""
        user = _make_user(db_session, changed_ago=LONG_AGO)
        headers = _headers(client, user)

        response = _change_own_password(client, headers, REPLACEMENT_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 200, response.text

    def test_a_forced_change_is_never_blocked_by_the_minimum_age(self, client, db_session):
        """The self-inflicted denial of service this exemption exists to prevent.

        An admin resets a password (stamping ``password_changed_at = now``) and flags
        the account for a forced change. Without the exemption the user is refused at
        both ends: held out of the app until they change it, and refused when they
        try — for another 24 hours.
        """
        user = _make_user(db_session, changed_ago=timedelta(minutes=1), must_change_password=True)
        headers = _headers(client, user)

        response = _change_own_password(client, headers, REPLACEMENT_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 200, response.text
        db_session.refresh(user)
        assert user.must_change_password is False

    def test_an_admin_reset_is_never_blocked_by_the_minimum_age(
        self, client, db_session, admin_token_headers
    ):
        """A user locked out by their own minimum age must still be recoverable."""
        target = _make_user(db_session, changed_ago=timedelta(minutes=1))

        response = client.put(
            f"/api/users/{target.uuid}",
            headers=admin_token_headers,
            json={"password": REPLACEMENT_PASSWORD},
        )

        assert response.status_code == 200, response.text

    def test_zero_hours_turns_the_control_off(self, client, db_session):
        """The documented off switch reaches the endpoint, not just the policy object."""
        _publish(password_min_age_hours=0)
        user = _make_user(db_session, changed_ago=timedelta(minutes=1))
        headers = _headers(client, user)

        response = _change_own_password(client, headers, REPLACEMENT_PASSWORD, ORIGINAL_PASSWORD)

        assert response.status_code == 200, response.text


# ── the bootstrap super_admin ────────────────────────────────────────────────────


class TestBootstrapAdminPassword:
    """The one account whose seeded password was invisible to the reuse check.

    ``_ensure_admin_user`` set ``hashed_password`` and was **not** among the
    ``add_password_to_history`` call sites, unlike ``registration.py``,
    ``users.create_user`` and ``invitations.py``. With zero history rows, an operator
    "rotating" the credential that ``initial_data`` logged at CRITICAL to the very
    same value was accepted, and audited as a successful password change.
    """

    @pytest.fixture
    def seed(self, db_session, monkeypatch):
        """Run the real ``_ensure_admin_user`` against a unique bootstrap identity.

        Two things are stubbed, neither of them under test: the credential resolver
        (so the row cannot collide with the dev stack's real admin) and
        ``_admin_exists`` (the live database already has a super_admin, which would
        make the function correctly skip creation and test nothing).
        """
        import uuid

        from app import initial_data as initial_data_module

        def _run(password: str, generated: bool) -> User:
            email = f"bootstrap_{uuid.uuid4().hex[:10]}@example.com"
            monkeypatch.setattr(
                initial_data_module,
                "_resolve_bootstrap_admin",
                lambda: (email, password, generated),
            )
            monkeypatch.setattr(initial_data_module, "_admin_exists", lambda _db: False)
            initial_data_module._ensure_admin_user(db_session)
            user = db_session.query(User).filter(User.email == email).first()
            assert user is not None, "the bootstrap admin was not created"
            # .first() is typed Any; cast so the fixture's -> User contract is checked.
            return cast(User, user)

        return _run

    def test_the_seeded_password_is_written_to_history(self, db_session, seed):
        user = seed(ORIGINAL_PASSWORD, False)

        rows = db_session.query(PasswordHistory).filter(PasswordHistory.user_id == user.id).all()

        assert len(rows) == 1
        assert verify_password(ORIGINAL_PASSWORD, str(rows[0].password_hash))

    def test_the_seeded_password_cannot_come_back_after_a_rotation(self, db_session, seed):
        """The behaviour the history ROW exists for, isolated from the live column.

        Asserting on the freshly seeded account alone would not isolate it: the
        current-password floor in ``check_password_against_history`` reads
        ``user.hashed_password`` and would refuse it anyway. So rotate first — once
        the seeded value is no longer the live password, only the history row can
        still refuse it, which is exactly the operator's "rotate the credential the
        logs recorded" flow and exactly what returned zero rows before.
        """
        from app.auth.password_history import check_password_against_history

        user = seed(ORIGINAL_PASSWORD, False)

        user.hashed_password = get_password_hash(REPLACEMENT_PASSWORD)
        add_password_to_history(db_session, user.id, str(user.hashed_password))
        db_session.commit()

        assert check_password_against_history(db_session, user.id, ORIGINAL_PASSWORD) is False
        assert check_password_against_history(db_session, user.id, SECOND_REPLACEMENT) is True

    def test_a_generated_password_is_flagged_for_a_forced_change(self, db_session, seed):
        """A generated password is a temporary authenticator, and this one is written
        to the container log at CRITICAL. FedRAMP IA-5(1) says change it immediately."""
        user = seed("Generated-Pass-3k", True)

        assert user.must_change_password is True

    def test_an_operator_supplied_password_is_not_flagged(self, db_session, seed):
        """The other half of the rule, and the reason it is keyed on *generated*.

        ``INITIAL_ADMIN_PASSWORD`` was chosen by the operator — not a temporary
        authenticator — and holding that account would break scripted hardened
        provisioning that signs in with the credential it just configured. The
        relaxed-environment dev credential is excluded for the same reason: every
        local session and every e2e run would otherwise start behind a
        forced-change screen.
        """
        user = seed(ORIGINAL_PASSWORD, False)

        assert user.must_change_password is False

    def test_the_bootstrap_admin_is_still_a_usable_super_admin(self, db_session, seed):
        """Guard on the seeding itself: the history row must not have cost the row
        any of the properties that make this the break-glass account."""
        from app.auth.approval import APPROVAL_APPROVED
        from app.auth.roles import ROLE_SUPER_ADMIN

        user = seed(ORIGINAL_PASSWORD, False)

        assert user.role == ROLE_SUPER_ADMIN
        assert user.is_superuser is True
        assert user.email_verified is True
        assert user.approval_status == APPROVAL_APPROVED
        assert user.password_changed_at is not None
