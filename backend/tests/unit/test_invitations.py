"""Admin invitations + email verification (v375), pinned with fakes.

Two features that only exist because "disable self-registration" was otherwise
unusable: ``POST /api/admin/users`` could not set ``auth_type``, so every
admin-created account was ``local`` and could not log in at all on a deployment
whose IdP owns identity — and ``require_email_verification`` was a declared
auth-config key with no reader anywhere.

The session fake below evaluates real SQLAlchemy criteria (``col == v``,
``col.is_(None)``, ``col > v``) against in-memory rows, so the tests exercise the
actual predicates the services issue rather than a stubbed lookup.
"""

# mypy: disable-error-code="arg-type,no-any-return,union-attr"
# This suite passes structural stand-ins (fake sessions, fake users, namespace
# requests) to signatures that declare Session/User/Request, and indexes
# HTTPException.detail, which is typed str while every lifecycle gate raises an
# object. Declared once here rather than as a cast at every call site — casts
# bury the assertion, and widening a production signature to suit a test is worse.
from __future__ import annotations

import operator as _op
import uuid as uuid_pkg
from collections import defaultdict
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import Response
from sqlalchemy.sql import operators as sa_ops

from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.utils import local_password_allowed
from app.models.invitation import EmailVerificationToken
from app.models.user import User

STRONG_PASSWORD = "Correct-Horse-9Battery!"  # noqa: S105 - test fixture, not a credential


def _matches(row, criterion) -> bool:
    """Evaluate one SQLAlchemy binary criterion against an in-memory object."""
    key = criterion.left.key
    actual = getattr(row, key)
    expected = getattr(criterion.right, "value", None)
    op = criterion.operator
    if op is sa_ops.is_:
        return actual is None
    if op is sa_ops.is_not:
        return actual is not None
    if op is _op.eq:
        return actual == expected
    if op is _op.gt:
        return actual is not None and actual > expected
    raise AssertionError(f"FakeQuery does not support operator {op!r}")


class FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *criteria):
        return FakeQuery([r for r in self._rows if all(_matches(r, c) for c in criteria)])

    def order_by(self, *_):
        return self

    def limit(self, n):
        return FakeQuery(self._rows[:n])

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)

    def update(self, values):
        for row in self._rows:
            for key, value in values.items():
                setattr(row, key, value)
        return len(self._rows)


class FakeSession:
    """Minimal Session stand-in: per-model row lists plus id assignment."""

    def __init__(self):
        self.rows = defaultdict(list)
        self.commits = 0
        self._next_id = 1

    def query(self, model):
        return FakeQuery(self.rows[model])

    def add(self, obj):
        if obj not in self.rows[type(obj)]:
            self.rows[type(obj)].append(obj)
        self.flush()

    def flush(self):
        for rows in self.rows.values():
            for row in rows:
                if getattr(row, "id", None) is None:
                    row.id = self._next_id
                    self._next_id += 1

    def commit(self):
        self.flush()
        self.commits += 1

    def refresh(self, _obj):
        return None

    def rollback(self):
        return None


class RecordingMailer:
    """Captures what would have been emailed, including the raw token URL."""

    def __init__(self):
        self.invitations = []
        self.verifications = []

    def send_invitation(self, to_email, accept_url, inviter, expires_in_hours, requires_password):
        self.invitations.append(
            {
                "to": to_email,
                "url": accept_url,
                "inviter": inviter,
                "requires_password": requires_password,
            }
        )

    def send_email_verification(self, to_email, verify_url, expires_in_hours):
        self.verifications.append({"to": to_email, "url": verify_url})


@pytest.fixture
def db():
    return FakeSession()


@pytest.fixture
def mailer(monkeypatch):
    from app.auth import email_verification as ev_module
    from app.auth import invitations as inv_module

    recorder = RecordingMailer()
    monkeypatch.setattr(inv_module, "email_service", recorder)
    monkeypatch.setattr(ev_module, "email_service", recorder)
    # The real helper drives queries this fake session does not model; the
    # invitation flow only cares THAT a history row is written.
    monkeypatch.setattr(inv_module, "add_password_to_history", lambda *a, **k: None)
    return recorder


@pytest.fixture
def admin():
    return User(id=1, email="admin@example.com", hashed_password="x", role="super_admin")


def _invite(db, mailer, admin, **overrides):
    """Create an invitation and return (invitation, raw_token)."""
    from app.auth.invitations import create_invitation

    params = {
        "email": f"invitee_{uuid_pkg.uuid4().hex[:8]}@example.com",
        "full_name": "New Person",
        "role": "user",
        "auth_type": "local",
        "expires_in_hours": 72,
        "created_by": admin,
        "ip_address": "10.0.0.1",
    }
    params.update(overrides)
    invitation, error = create_invitation(db, **params)
    assert error is None
    token = mailer.invitations[-1]["url"].split("token=")[1]
    return invitation, token


class TestInvitationIssuance:
    def test_only_the_hash_is_stored(self, db, mailer, admin):
        import hashlib

        invitation, token = _invite(db, mailer, admin)
        assert invitation.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(invitation.__dict__)

    def test_reinviting_revokes_the_previous_link(self, db, mailer, admin):
        from app.auth.invitations import accept_invitation

        first, first_token = _invite(db, mailer, admin, email="dup@example.com")
        _invite(db, mailer, admin, email="dup@example.com")

        assert first.revoked_at is not None
        user, error = accept_invitation(db, first_token, STRONG_PASSWORD, None)
        assert user is None and error is not None

    def test_existing_account_is_refused(self, db, mailer, admin):
        from app.auth.invitations import create_invitation

        db.add(User(email="taken@example.com", hashed_password="x", role="user"))
        invitation, error = create_invitation(
            db,
            email="taken@example.com",
            full_name=None,
            role="user",
            auth_type="local",
            expires_in_hours=24,
            created_by=admin,
            ip_address="10.0.0.1",
        )
        assert invitation is None
        assert error is not None


class TestInvitationAcceptance:
    def test_creates_an_active_account_with_the_invited_role_and_auth_type(self, db, mailer, admin):
        from app.auth.invitations import accept_invitation

        _, token = _invite(db, mailer, admin, role="admin", auth_type="local")
        user, error = accept_invitation(db, token, STRONG_PASSWORD, None)

        assert error is None
        assert user.is_active is True
        assert user.role == "admin"
        assert user.is_superuser is False  # derived mirror: admin != super_admin
        assert user.auth_type == "local"
        assert user.password_changed_at is not None
        # Redeeming a link mailed to the address IS proof of control.
        assert user.email_verified is True

    def test_token_is_single_use(self, db, mailer, admin):
        from app.auth.invitations import GENERIC_INVALID
        from app.auth.invitations import accept_invitation

        invitation, token = _invite(db, mailer, admin)
        user, _ = accept_invitation(db, token, STRONG_PASSWORD, None)
        assert user is not None
        assert invitation.used_at is not None
        assert invitation.created_user_id == user.id

        again, error = accept_invitation(db, token, STRONG_PASSWORD, None)
        assert again is None
        assert error == GENERIC_INVALID

    def test_expired_invitation_is_refused(self, db, mailer, admin):
        from app.auth.invitations import accept_invitation

        invitation, token = _invite(db, mailer, admin)
        invitation.expires_at = datetime.now(UTC) - timedelta(minutes=1)

        user, error = accept_invitation(db, token, STRONG_PASSWORD, None)
        assert user is None and error is not None

    def test_revoked_invitation_is_refused(self, db, mailer, admin):
        from app.auth.invitations import accept_invitation
        from app.auth.invitations import revoke_invitation

        invitation, token = _invite(db, mailer, admin)
        revoke_invitation(db, invitation)

        user, error = accept_invitation(db, token, STRONG_PASSWORD, None)
        assert user is None and error is not None

    def test_every_bad_token_state_is_indistinguishable(self, db, mailer, admin):
        """Unknown, expired, revoked and used must return one identical error.

        Any variation tells the holder of a guessed token which guesses were
        closer — and tells an attacker that a token exists at all.
        """
        from app.auth.invitations import accept_invitation
        from app.auth.invitations import revoke_invitation

        expired, expired_token = _invite(db, mailer, admin)
        expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        revoked, revoked_token = _invite(db, mailer, admin)
        revoke_invitation(db, revoked)
        used, used_token = _invite(db, mailer, admin)
        accept_invitation(db, used_token, STRONG_PASSWORD, None)

        errors = {
            accept_invitation(db, tok, STRONG_PASSWORD, None)[1]
            for tok in ("no-such-token", "", expired_token, revoked_token, used_token)
        }
        assert len(errors) == 1

    def test_a_weak_password_does_not_burn_the_invitation(self, db, mailer, admin):
        from app.auth.invitations import accept_invitation

        invitation, token = _invite(db, mailer, admin)
        user, error = accept_invitation(db, token, "short", None)

        assert user is None and error is not None
        assert invitation.used_at is None, "policy rejection must not consume the link"

        user, error = accept_invitation(db, token, STRONG_PASSWORD, None)
        assert error is None and user is not None


class TestExternalAuthTypeInvitations:
    @pytest.mark.parametrize("auth_type", ["ldap", "oidc", "pki"])
    def test_no_local_password_is_stored(self, db, mailer, admin, auth_type):
        from app.auth.invitations import accept_invitation

        _, token = _invite(db, mailer, admin, auth_type=auth_type)
        user, error = accept_invitation(db, token, None, None)

        assert error is None
        assert user.auth_type == auth_type
        assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD
        assert user.password_changed_at is None
        assert user.allow_local_fallback is False
        allowed, _reason = local_password_allowed(user.auth_type, user.allow_local_fallback)
        assert allowed is False

    def test_a_supplied_password_is_refused_rather_than_ignored(self, db, mailer, admin):
        """Silently dropping it would leave the invitee believing it was set."""
        from app.auth.invitations import accept_invitation

        _, token = _invite(db, mailer, admin, auth_type="ldap")
        user, error = accept_invitation(db, token, STRONG_PASSWORD, None)
        assert user is None and error is not None

    def test_the_invite_email_says_no_password_is_needed(self, db, mailer, admin):
        _invite(db, mailer, admin, auth_type="oidc")
        assert mailer.invitations[-1]["requires_password"] is False


class TestEmailVerification:
    def _user(self, db, **overrides):
        params = {
            "email": "u@example.com",
            "hashed_password": "x",
            "role": "user",
            "auth_type": "local",
            "is_active": True,
            "email_verified": False,
            "uuid": uuid_pkg.uuid4(),
        }
        params.update(overrides)
        user = User(**params)
        db.add(user)
        return user

    def _require(self, monkeypatch, required: bool):
        from app.auth import email_verification as ev

        monkeypatch.setattr(ev, "email_verification_required", lambda _db: required)

    def test_verification_token_marks_the_user_and_is_single_use(self, db, mailer):
        from app.auth.email_verification import issue_verification_token
        from app.auth.email_verification import verify_email

        user = self._user(db)
        issue_verification_token(db, user, "10.0.0.1")
        token = mailer.verifications[-1]["url"].split("token=")[1]

        ok, error = verify_email(db, token)
        assert ok and error is None
        assert user.email_verified is True
        assert user.email_verified_at is not None

        ok, error = verify_email(db, token)
        assert ok is False and error is not None

    def test_unknown_and_used_tokens_share_one_message(self, db, mailer):
        from app.auth.email_verification import GENERIC_INVALID
        from app.auth.email_verification import issue_verification_token
        from app.auth.email_verification import verify_email

        user = self._user(db)
        issue_verification_token(db, user)
        token = mailer.verifications[-1]["url"].split("token=")[1]
        verify_email(db, token)

        assert verify_email(db, token)[1] == GENERIC_INVALID
        assert verify_email(db, "nope")[1] == GENERIC_INVALID
        assert verify_email(db, "")[1] == GENERIC_INVALID

    def test_expired_token_is_refused(self, db, mailer):
        from app.auth.email_verification import issue_verification_token
        from app.auth.email_verification import verify_email

        user = self._user(db)
        issue_verification_token(db, user)
        record = db.rows[EmailVerificationToken][-1]
        record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        token = mailer.verifications[-1]["url"].split("token=")[1]

        assert verify_email(db, token)[0] is False
        assert user.email_verified is False

    def test_local_login_is_blocked_when_verification_is_required(self, db, mailer, monkeypatch):
        from fastapi import HTTPException

        from app.auth.email_verification import assert_email_verified_for_local_login

        user = self._user(db)
        self._require(monkeypatch, True)

        with pytest.raises(HTTPException) as exc:
            assert_email_verified_for_local_login(db, str(user.uuid))
        assert exc.value.status_code == 403
        # The blocked attempt hands the user a way forward without an admin.
        assert mailer.verifications, "a fresh verification link must be sent"

    def test_verified_account_logs_in(self, db, mailer, monkeypatch):
        from app.auth.email_verification import assert_email_verified_for_local_login

        user = self._user(db, email_verified=True)
        self._require(monkeypatch, True)
        assert_email_verified_for_local_login(db, str(user.uuid))

    def test_setting_off_means_no_gate(self, db, mailer, monkeypatch):
        from app.auth.email_verification import assert_email_verified_for_local_login

        user = self._user(db)
        self._require(monkeypatch, False)
        assert_email_verified_for_local_login(db, str(user.uuid))

    @pytest.mark.parametrize("auth_type", ["ldap", "oidc", "pki"])
    def test_external_accounts_are_unaffected(self, db, mailer, monkeypatch, auth_type):
        """Their address is the IdP's assertion, not ours to re-verify."""
        from app.auth.email_verification import assert_email_verified_for_local_login

        user = self._user(db, auth_type=auth_type)
        self._require(monkeypatch, True)
        assert_email_verified_for_local_login(db, str(user.uuid))

    def test_super_admin_is_exempt(self, db, mailer, monkeypatch):
        """Same break-glass rule as ``local_enabled``: auth config is theirs."""
        from app.auth.email_verification import assert_email_verified_for_local_login

        user = self._user(db, role="super_admin", is_superuser=True)
        self._require(monkeypatch, True)
        assert_email_verified_for_local_login(db, str(user.uuid))

    def test_resend_never_reveals_whether_the_address_exists(self, db, mailer):
        from app.auth.email_verification import resend_verification

        resend_verification(db, "stranger@example.com", "10.0.0.1")
        assert mailer.verifications == []


class TestAdminDirectCreation:
    """``users.create_user`` — the path the audit found unusable."""

    def _payload(self, monkeypatch, **overrides):
        from app.schemas.user import UserCreate

        params = {"email": "made@example.com", "password": STRONG_PASSWORD}
        params.update(overrides)
        return UserCreate(**params)

    def test_auth_type_is_settable_and_stores_no_local_password(self, db, monkeypatch):
        from app.api.endpoints import users as users_module

        monkeypatch.setattr(users_module, "add_password_to_history", lambda *a, **k: None)
        payload = self._payload(monkeypatch, auth_type="ldap", password=None)
        user = users_module.create_user(payload, db)

        assert user.auth_type == "ldap"
        assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD
        assert user.must_change_password is False

    def test_local_account_is_forced_to_change_the_admin_chosen_password(self, db, monkeypatch):
        from app.api.endpoints import users as users_module

        recorded = []
        monkeypatch.setattr(
            users_module, "add_password_to_history", lambda _db, uid, h: recorded.append(uid)
        )
        user = users_module.create_user(self._payload(monkeypatch), db)

        assert user.auth_type == "local"
        assert user.must_change_password is True
        assert user.password_changed_at is not None
        assert recorded, "the initial password must enter the reuse history"

    def test_a_local_account_without_a_password_is_rejected(self):
        from pydantic import ValidationError

        from app.schemas.user import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(email="x@example.com", auth_type="local")

    def test_an_external_account_with_a_password_is_rejected(self):
        from pydantic import ValidationError

        from app.schemas.user import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(email="x@example.com", auth_type="ldap", password=STRONG_PASSWORD)

    def test_an_unknown_auth_type_is_rejected(self):
        from pydantic import ValidationError

        from app.schemas.user import UserCreate

        with pytest.raises(ValidationError):
            UserCreate(email="x@example.com", auth_type="carrier-pigeon")


def test_invitation_uuid_is_the_only_public_identifier():
    """Hybrid-ID rule: the API never exposes the integer PK."""
    from app.schemas.invitation import InvitationResponse

    fields = set(InvitationResponse.model_fields)
    assert "uuid" in fields
    assert "id" not in fields
    assert not any(f.endswith("_id") for f in fields)


def test_accept_response_tells_the_spa_which_credential_to_use():
    from app.schemas.invitation import InvitationAcceptResponse

    assert "can_login_with_password" in InvitationAcceptResponse.model_fields


def test_accept_endpoint_answers_every_bad_token_identically(db, mailer, admin):
    """The HTTP surface, not just the service: same status AND same detail.

    ``__wrapped__`` peels the slowapi rate-limit decorator, which needs live
    application state; the limit itself is asserted by the route-tier suite.
    """
    from fastapi import HTTPException

    from app.api.endpoints.auth.invitations import accept_invitation_endpoint
    from app.auth.invitations import accept_invitation
    from app.auth.invitations import revoke_invitation
    from app.schemas.invitation import InvitationAcceptRequest

    handler = getattr(accept_invitation_endpoint, "__wrapped__", accept_invitation_endpoint)
    request = SimpleNamespace(
        client=SimpleNamespace(host="10.0.0.1"), headers={}, state=SimpleNamespace()
    )

    expired, expired_token = _invite(db, mailer, admin)
    expired.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    revoked, revoked_token = _invite(db, mailer, admin)
    revoke_invitation(db, revoked)
    _used, used_token = _invite(db, mailer, admin)
    accept_invitation(db, used_token, STRONG_PASSWORD, None)

    outcomes = set()
    for token in ("no-such-token", "", expired_token, revoked_token, used_token):
        with pytest.raises(HTTPException) as exc:
            handler(
                request=request,
                response=Response(),
                body=InvitationAcceptRequest(token=token),
                db=db,
            )
        outcomes.add((exc.value.status_code, exc.value.detail))

    assert len(outcomes) == 1, f"token state leaked through the response: {outcomes}"
