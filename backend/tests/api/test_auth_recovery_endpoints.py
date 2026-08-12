"""The unauthenticated account-recovery + creation surface, over HTTP.

Nine routes — password reset request/confirm, email verify/resend, invitation
lookup/accept, and the admin invitation create/list/revoke — had **zero**
behavioural test. What existed tested something adjacent to each of them:

* ``unit/test_invitations.py`` drives the *services* (and one handler function via
  ``__wrapped__``), never the router.
* ``unit/test_csrf_and_docs_gating.py`` asserts these paths appear in the CSRF
  exempt list — as strings.
* ``unit/test_password_reset_fail_closed.py`` covers the session-revocation helper,
  never the endpoint.
* ``unit/test_route_privilege_tiers.py`` lists them in ``KNOWN_PUBLIC``, which
  asserts they are reachable without a session — the opposite of asserting what
  they do with a bad token.

So the properties actually protecting these routes were unpinned: single-use
tokens, expiry, and above all the anti-enumeration contract that
``test_route_privilege_tiers.py`` states in prose — "every bad token gets one
identical error" and "Resend is deliberately answer-identical so it is not an
account-existence oracle". A response that varies by token state or by whether
an address exists is an oracle that needs no credential at all, and none of it
was asserted anywhere.

Nonexistent-account discipline: every negative path here uses a fresh
uuid-suffixed address that no account holds. Lockout is keyed on the resolved
account and escalates, so a negative test against a shared account poisons every
later login (see ``backend/tests/CLAUDE.md``).
"""

from __future__ import annotations

import hashlib
import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.models.invitation import UserInvitation
from app.models.password_reset import PasswordResetToken

#: Passes the default policy (length 12, upper/lower/digit/special) and shares no
#: substring with the fixture users' emails or names, which the policy also checks.
STRONG_PASSWORD = "Correct-Horse-9Battery!"  # noqa: S105 - test fixture, not a credential
OTHER_STRONG_PASSWORD = "Vivid-Zebra-4Marmalade!"  # noqa: S105 - test fixture, not a credential

RESET_REQUEST_PATH = "/api/auth/password-reset/request"
RESET_CONFIRM_PATH = "/api/auth/password-reset/confirm"
VERIFY_EMAIL_PATH = "/api/auth/verify-email"
RESEND_VERIFY_PATH = "/api/auth/verify-email/resend"
INVITATIONS_PATH = "/api/auth/invitations"
INVITE_LOOKUP_PATH = "/api/auth/invitations/lookup"
INVITE_ACCEPT_PATH = "/api/auth/invitations/accept"


def _nobody_email() -> str:
    """An address no account holds, freshly generated so lockout can't accrue."""
    return f"nosuchuser-{uuid_pkg.uuid4().hex[:12]}@example.com"


def _csrf(client, headers: dict | None = None) -> dict:
    """Merge the double-submit CSRF header a browser would send.

    The ``*_token_headers`` fixtures authenticate by POSTing to /api/auth/token, which
    sets the ``csrf_token`` cookie on the shared TestClient jar. Once a jar exists the
    client LOOKS like a browser, so every non-exempt state-changing request must echo
    that cookie in ``X-CSRF-Token`` or the middleware answers 403 — regardless of the
    Bearer header. The invitation routes are authenticated admin routes and therefore
    NOT in the exempt set (unlike password-reset / verify-email, which must work with
    no session at all). Same helper shape as tests/api/test_auth_endpoints.py:47.
    """
    merged = dict(headers or {})
    token = client.cookies.get("csrf_token")
    if token:
        merged["X-CSRF-Token"] = token
    return merged


class RecordingMailer:
    """Captures what would have been mailed, including the raw token URL.

    The raw token exists only inside the email body — only its SHA-256 hash is
    stored — so capturing the mail is the only way to drive the real
    request-then-redeem round trip instead of hand-forging a token row.
    """

    def __init__(self) -> None:
        self.resets: list[str] = []
        self.invitations: list[dict] = []
        self.verifications: list[str] = []

    def send_password_reset(self, to_email: str, reset_url: str) -> None:
        self.resets.append(reset_url)

    def send_invitation(self, to_email, accept_url, inviter, expires_in_hours, requires_password):
        self.invitations.append({"to": to_email, "url": accept_url})

    def send_email_verification(self, to_email: str, verify_url: str, expires_in_hours) -> None:
        self.verifications.append(verify_url)

    @staticmethod
    def token_of(url: str) -> str:
        return url.split("token=")[1]


@pytest.fixture
def mailer(monkeypatch) -> RecordingMailer:
    """Replace the mail transport in all three recovery services.

    Not merely convenience: with no transport configured ``send_invitation``
    raises ``EmailDeliveryError``, which ``main.py`` maps to 503 — so the create
    route is untestable without this, and the reset route would take its silent
    ``DELIVERY_FAILED`` branch and never produce a token URL to redeem.
    """
    recorder = RecordingMailer()
    monkeypatch.setattr("app.auth.password_reset.email_service", recorder)
    monkeypatch.setattr("app.auth.invitations.email_service", recorder)
    monkeypatch.setattr("app.auth.email_verification.email_service", recorder)
    return recorder


def _issue_reset_token(db_session, user, *, used: bool = False, expired: bool = False) -> str:
    """Create a reset token row in a chosen state and return the raw token.

    Forged directly rather than round-tripped because the states under test —
    already consumed, already expired — are not reachable through the request
    endpoint within one test.
    """
    raw = f"forged-{uuid_pkg.uuid4().hex}"
    now = datetime.now(UTC)
    db_session.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=(now - timedelta(minutes=5)) if expired else (now + timedelta(hours=1)),
            used_at=now if used else None,
            ip_address="10.0.0.1",
        )
    )
    db_session.commit()
    return raw


# --------------------------------------------------------------------------- #
# POST /auth/password-reset/request                                            #
# --------------------------------------------------------------------------- #
class TestPasswordResetRequest:
    def test_a_real_local_account_is_mailed_a_link(self, client, normal_user, mailer):
        response = client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        assert response.status_code == 200, response.text
        assert len(mailer.resets) == 1

    def test_an_unknown_address_is_mailed_nothing(self, client, mailer):
        response = client.post(RESET_REQUEST_PATH, json={"email": _nobody_email()})
        assert response.status_code == 200, response.text
        assert mailer.resets == []

    def test_known_and_unknown_addresses_are_answer_identical(self, client, normal_user, mailer):
        """The anti-enumeration contract. Any difference — status, body, or an
        error escaping from the mail transport — turns this into an
        account-existence oracle that needs no credential."""
        known = client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        unknown = client.post(RESET_REQUEST_PATH, json={"email": _nobody_email()})
        assert (known.status_code, known.json()) == (unknown.status_code, unknown.json())

    def test_an_inactive_account_is_answer_identical_and_gets_no_token(
        self, client, normal_user, mailer, db_session
    ):
        """Deactivated accounts must not be resettable, and must not be
        distinguishable from active ones in the response."""
        normal_user.is_active = False
        db_session.commit()
        response = client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        assert response.status_code == 200, response.text
        assert mailer.resets == []
        assert (
            db_session.query(PasswordResetToken)
            .filter(PasswordResetToken.user_id == normal_user.id)
            .count()
            == 0
        )

    def test_an_empty_body_is_answer_identical(self, client, mailer):
        """``email`` defaults to ``""`` rather than being required, so a bodyless
        probe must take the same silent path as an unknown address."""
        response = client.post(RESET_REQUEST_PATH, json={})
        assert response.status_code == 200, response.text
        assert mailer.resets == []


# --------------------------------------------------------------------------- #
# POST /auth/password-reset/confirm                                            #
# --------------------------------------------------------------------------- #
class TestPasswordResetConfirm:
    def test_round_trip_sets_the_new_password(self, client, normal_user, mailer):
        """End to end through both endpoints: the mailed token is redeemable and
        the new credential actually authenticates."""
        client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        token = RecordingMailer.token_of(mailer.resets[0])

        response = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert response.status_code == 200, response.text

        login = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": STRONG_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert login.status_code == 200, login.text

    def test_a_consumed_token_is_refused(self, client, normal_user, db_session):
        """Single use. Without it, a reset link recovered from a mailbox months
        later is a permanent account-takeover credential."""
        token = _issue_reset_token(db_session, normal_user, used=True)
        before = str(normal_user.hashed_password)

        response = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert response.status_code == 400, response.text
        db_session.refresh(normal_user)
        assert str(normal_user.hashed_password) == before

    def test_an_expired_token_is_refused(self, client, normal_user, db_session):
        token = _issue_reset_token(db_session, normal_user, expired=True)
        before = str(normal_user.hashed_password)

        response = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert response.status_code == 400, response.text
        db_session.refresh(normal_user)
        assert str(normal_user.hashed_password) == before

    def test_reusing_a_token_after_a_successful_reset_is_refused(self, client, normal_user, mailer):
        client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        token = RecordingMailer.token_of(mailer.resets[0])
        first = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert first.status_code == 200, first.text

        again = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": OTHER_STRONG_PASSWORD}
        )
        assert again.status_code == 400, again.text

    def test_every_bad_token_state_is_answer_identical(self, client, normal_user, db_session):
        """Unknown, expired and consumed must be indistinguishable — status AND
        detail. A distinct message for "expired" confirms that a guessed token
        once existed, and tells a holder of a stale link which guesses were
        closer."""
        outcomes = {
            (
                response.status_code,
                response.json().get("detail"),
            )
            for response in (
                client.post(
                    RESET_CONFIRM_PATH,
                    json={"token": tok, "new_password": STRONG_PASSWORD},
                )
                for tok in (
                    "no-such-token-at-all",
                    "",
                    _issue_reset_token(db_session, normal_user, expired=True),
                    _issue_reset_token(db_session, normal_user, used=True),
                )
            )
        }
        assert len(outcomes) == 1, f"token state leaked through the response: {outcomes}"

    def test_a_policy_rejected_password_does_not_burn_the_link(self, client, normal_user, mailer):
        """A weak first attempt must not consume the token — otherwise the user's
        only recovery path is spent on a typo."""
        client.post(RESET_REQUEST_PATH, json={"email": normal_user.email})
        token = RecordingMailer.token_of(mailer.resets[0])

        weak = client.post(RESET_CONFIRM_PATH, json={"token": token, "new_password": "short"})
        assert weak.status_code == 400, weak.text

        retry = client.post(
            RESET_CONFIRM_PATH, json={"token": token, "new_password": STRONG_PASSWORD}
        )
        assert retry.status_code == 200, retry.text


# --------------------------------------------------------------------------- #
# POST /auth/verify-email  ·  POST /auth/verify-email/resend                    #
# --------------------------------------------------------------------------- #
class TestEmailVerification:
    def _issue(self, db_session, user, mailer) -> str:
        from app.auth.email_verification import issue_verification_token

        issue_verification_token(db_session, user, "10.0.0.1")
        return RecordingMailer.token_of(mailer.verifications[-1])

    def test_a_valid_token_marks_the_address_verified(
        self, client, normal_user, mailer, db_session
    ):
        token = self._issue(db_session, normal_user, mailer)
        response = client.post(VERIFY_EMAIL_PATH, json={"token": token})
        assert response.status_code == 200, response.text
        db_session.refresh(normal_user)
        assert normal_user.email_verified is True

    def test_a_consumed_token_is_refused(self, client, normal_user, mailer, db_session):
        token = self._issue(db_session, normal_user, mailer)
        first = client.post(VERIFY_EMAIL_PATH, json={"token": token})
        assert first.status_code == 200, first.text

        again = client.post(VERIFY_EMAIL_PATH, json={"token": token})
        assert again.status_code == 400, again.text

    def test_unknown_and_consumed_tokens_are_answer_identical(
        self, client, normal_user, mailer, db_session
    ):
        token = self._issue(db_session, normal_user, mailer)
        client.post(VERIFY_EMAIL_PATH, json={"token": token})

        outcomes = {
            (response.status_code, response.json().get("detail"))
            for response in (
                client.post(VERIFY_EMAIL_PATH, json={"token": tok})
                for tok in (token, "no-such-token-at-all", "")
            )
        }
        assert len(outcomes) == 1, f"token state leaked through the response: {outcomes}"

    def test_resend_is_answer_identical_for_known_unknown_and_verified(
        self, client, normal_user, mailer, db_session
    ):
        """The property the route table documents in prose. Any variation makes an
        unauthenticated caller able to test whether an address has an account."""
        unknown = client.post(RESEND_VERIFY_PATH, json={"email": _nobody_email()})
        unverified = client.post(RESEND_VERIFY_PATH, json={"email": normal_user.email})

        normal_user.email_verified = True
        db_session.commit()
        verified = client.post(RESEND_VERIFY_PATH, json={"email": normal_user.email})

        answers = [(r.status_code, r.json()) for r in (unknown, unverified, verified)]
        assert answers[0] == answers[1] == answers[2], (
            f"resend distinguishes account states: {answers}"
        )

    def test_resend_mails_nothing_for_an_unknown_address(self, client, mailer):
        response = client.post(RESEND_VERIFY_PATH, json={"email": _nobody_email()})
        assert response.status_code == 200, response.text
        assert mailer.verifications == []

    def test_resend_mails_nothing_for_an_already_verified_address(
        self, client, normal_user, mailer, db_session
    ):
        normal_user.email_verified = True
        db_session.commit()
        response = client.post(RESEND_VERIFY_PATH, json={"email": normal_user.email})
        assert response.status_code == 200, response.text
        assert mailer.verifications == []


# --------------------------------------------------------------------------- #
# Admin invitation surface                                                     #
# --------------------------------------------------------------------------- #
def _invite_payload(**overrides) -> dict:
    payload = {
        "email": f"invitee-{uuid_pkg.uuid4().hex[:12]}@example.com",
        "full_name": "Invited Person",
        "role": "user",
        "auth_type": "local",
    }
    payload.update(overrides)
    return payload


class TestInvitationAdminGate:
    def test_create_is_401_unauthenticated(self, client):
        response = client.post(INVITATIONS_PATH, json=_invite_payload())
        assert response.status_code == 401, response.text

    def test_create_is_403_for_a_plain_user(self, client, user_token_headers, mailer):
        response = client.post(INVITATIONS_PATH, headers=user_token_headers, json=_invite_payload())
        assert response.status_code == 403, response.text
        assert mailer.invitations == []

    def test_a_plain_admin_cannot_invite_an_admin(self, client, admin_token_headers, mailer):
        """An invitation is a deferred account creation, so minting an elevated
        role inherits ``POST /api/admin/users``' super_admin gate. Without it,
        privilege escalation is one invite away."""
        response = client.post(
            INVITATIONS_PATH, headers=admin_token_headers, json=_invite_payload(role="admin")
        )
        assert response.status_code == 403, response.text
        assert mailer.invitations == []

    def test_a_plain_admin_cannot_invite_a_super_admin(self, client, admin_token_headers, mailer):
        response = client.post(
            INVITATIONS_PATH,
            headers=admin_token_headers,
            json=_invite_payload(role="super_admin"),
        )
        assert response.status_code == 403, response.text
        assert mailer.invitations == []

    def test_a_super_admin_can_invite_an_admin(self, client, super_admin_token_headers, mailer):
        response = client.post(
            INVITATIONS_PATH,
            headers=super_admin_token_headers,
            json=_invite_payload(role="admin"),
        )
        assert response.status_code == 201, response.text
        assert response.json()["role"] == "admin"

    def test_an_unknown_role_is_422(self, client, admin_token_headers, mailer):
        response = client.post(
            INVITATIONS_PATH,
            headers=admin_token_headers,
            json=_invite_payload(role="root"),
        )
        assert response.status_code == 422, response.text

    def test_list_is_403_for_a_plain_user(self, client, user_token_headers):
        response = client.get(INVITATIONS_PATH, headers=user_token_headers)
        assert response.status_code == 403, response.text

    def test_revoke_is_403_for_a_plain_user(self, client, user_token_headers, admin_user):
        response = client.delete(
            f"{INVITATIONS_PATH}/{uuid_pkg.uuid4()}", headers=user_token_headers
        )
        assert response.status_code == 403, response.text


class TestInvitationLifecycleOverHttp:
    @pytest.fixture
    def invitation(self, client, admin_token_headers, mailer) -> dict:
        """One pending local invitation, plus the raw token from its email."""
        payload = _invite_payload()
        response = client.post(
            INVITATIONS_PATH, headers=_csrf(client, admin_token_headers), json=payload
        )
        assert response.status_code == 201, response.text
        return {
            "body": response.json(),
            "email": payload["email"],
            "token": RecordingMailer.token_of(mailer.invitations[-1]["url"]),
        }

    def test_the_created_body_never_carries_the_token(self, invitation):
        """Only the hash is stored, so a token in the response would mean a second
        copy of a live credential in the admin UI's memory and logs."""
        assert "token" not in invitation["body"]

    def test_the_invitation_is_listed_as_pending(self, client, admin_token_headers, invitation):
        response = client.get(INVITATIONS_PATH, headers=admin_token_headers)
        assert response.status_code == 200, response.text
        listed = {row["email"]: row["status"] for row in response.json()}
        assert listed.get(invitation["email"]) == "pending"

    def test_lookup_returns_the_invited_address_to_the_token_holder(self, client, invitation):
        response = client.post(
            INVITE_LOOKUP_PATH, headers=_csrf(client), json={"token": invitation["token"]}
        )
        assert response.status_code == 200, response.text
        assert response.json()["email"] == invitation["email"]
        assert response.json()["requires_password"] is True

    def test_accept_creates_an_account_that_can_sign_in(self, client, invitation):
        response = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": STRONG_PASSWORD},
        )
        assert response.status_code == 200, response.text
        assert response.json()["can_login_with_password"] is True

        login = client.post(
            "/api/auth/token",
            data={"username": invitation["email"], "password": STRONG_PASSWORD},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert login.status_code == 200, login.text

    def test_an_invitation_redeemed_twice_is_refused(self, client, invitation):
        """Single use. A replayable invite is an unlimited account-creation
        credential for anyone who reads the mailbox."""
        first = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": STRONG_PASSWORD},
        )
        assert first.status_code == 200, first.text

        again = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": OTHER_STRONG_PASSWORD},
        )
        assert again.status_code == 400, again.text

    def test_the_second_redemption_creates_no_second_account(self, client, invitation, db_session):
        from app.models.user import User

        client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": STRONG_PASSWORD},
        )
        client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": OTHER_STRONG_PASSWORD},
        )
        assert db_session.query(User).filter(User.email == invitation["email"]).count() == 1

    def test_revoke_is_204_and_burns_the_link(self, client, admin_token_headers, invitation):
        response = client.delete(
            f"{INVITATIONS_PATH}/{invitation['body']['uuid']}", headers=admin_token_headers
        )
        assert response.status_code == 204, response.text

        accepted = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": STRONG_PASSWORD},
        )
        assert accepted.status_code == 400, accepted.text

    def test_revoke_of_an_unknown_uuid_is_404(self, client, admin_token_headers):
        response = client.delete(
            f"{INVITATIONS_PATH}/{uuid_pkg.uuid4()}", headers=admin_token_headers
        )
        assert response.status_code == 404, response.text

    def test_revoke_of_a_malformed_uuid_is_400(self, client, admin_token_headers):
        response = client.delete(f"{INVITATIONS_PATH}/not-a-uuid", headers=admin_token_headers)
        assert response.status_code == 400, response.text

    def test_a_weak_password_does_not_burn_the_invitation(self, client, invitation):
        weak = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": "short"},
        )
        assert weak.status_code == 400, weak.text

        retry = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": invitation["token"], "password": STRONG_PASSWORD},
        )
        assert retry.status_code == 200, retry.text


class TestInvitationTokenStatesAreIndistinguishable:
    """Unknown, expired, revoked and already-used must be one identical answer on
    BOTH public routes. ``unit/test_invitations.py`` asserts this for the accept
    *handler*; nothing asserted it for the HTTP responses, and nothing at all
    asserted it for lookup — where the reply otherwise discloses an address."""

    @pytest.fixture
    def token_states(self, client, admin_token_headers, mailer, db_session) -> list[str]:
        tokens = ["no-such-token-at-all", ""]

        def _create() -> tuple[str, str]:
            payload = _invite_payload()
            response = client.post(
                INVITATIONS_PATH, headers=_csrf(client, admin_token_headers), json=payload
            )
            assert response.status_code == 201, response.text
            return (
                response.json()["uuid"],
                RecordingMailer.token_of(mailer.invitations[-1]["url"]),
            )

        expired_uuid, expired_token = _create()
        db_session.query(UserInvitation).filter(UserInvitation.uuid == expired_uuid).update(
            {"expires_at": datetime.now(UTC) - timedelta(minutes=1)}
        )
        db_session.commit()
        tokens.append(expired_token)

        revoked_uuid, revoked_token = _create()
        revoke = client.delete(
            f"{INVITATIONS_PATH}/{revoked_uuid}", headers=_csrf(client, admin_token_headers)
        )
        assert revoke.status_code == 204, revoke.text
        tokens.append(revoked_token)

        _used_uuid, used_token = _create()
        accepted = client.post(
            INVITE_ACCEPT_PATH,
            headers=_csrf(client),
            json={"token": used_token, "password": STRONG_PASSWORD},
        )
        assert accepted.status_code == 200, accepted.text
        tokens.append(used_token)

        return tokens

    def test_lookup_answers_every_bad_token_identically(self, client, token_states):
        outcomes = {
            (response.status_code, response.json().get("detail"))
            for response in (
                client.post(INVITE_LOOKUP_PATH, headers=_csrf(client), json={"token": tok})
                for tok in token_states
            )
        }
        assert len(outcomes) == 1, f"token state leaked through lookup: {outcomes}"

    def test_accept_answers_every_bad_token_identically(self, client, token_states):
        outcomes = {
            (response.status_code, response.json().get("detail"))
            for response in (
                client.post(
                    INVITE_ACCEPT_PATH,
                    headers=_csrf(client),
                    json={"token": tok, "password": STRONG_PASSWORD},
                )
                for tok in token_states
            )
        }
        assert len(outcomes) == 1, f"token state leaked through accept: {outcomes}"
