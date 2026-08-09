"""MFA enforcement, single-use half-tokens, and login failure handling.

Each test here pins one defect found in the auth audit:

* ``MFA_REQUIRED`` was reported by ``/mfa/status`` but never enforced at ``/token`` —
  an unenrolled user on an MFA-required deployment received a full session, so
  enrolment was only ever enforced by the SPA.
* ``/mfa/disable`` verified a backup code without consuming it.
* ``/mfa/verify`` never checked ``is_active``, so a deactivated account could finish
  logging in and get a persisted refresh token.
* The half-token's single-use check was read-then-write, so two concurrent
  verifications of one jti could both mint a session.
* Login audit records derived ``auth_method`` from ``LDAP_ENABLED`` instead of the
  method actually used.
* An inactive account answered 400 "Inactive user account" — an enumeration oracle
  that also skipped lockout recording entirely.
* The super-admin lockout exemption was computed only on success, i.e. never for the
  failed attempts that actually lock an account.

Everything runs against fakes: no Postgres, no Redis, no HTTP client.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from typing import cast
from uuid import UUID

import pyotp
import pytest
from fastapi import HTTPException
from fastapi import Response

from app.api.endpoints.auth import login as login_module
from app.api.endpoints.auth import mfa as mfa_module
from app.api.endpoints.auth import mfa_enrollment as mfa_enrollment_module
from app.api.endpoints.auth import mfa_tokens as mfa_tokens_module
from app.api.endpoints.auth.dependencies import get_current_user
from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_ENROLL
from app.api.endpoints.auth.mfa_tokens import MFA_SCOPE_VERIFY
from app.auth.constants import TOKEN_TYPE_MFA
from app.auth.mfa import MFAService
from app.core.config import settings
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.schemas.user import MFADisableRequest
from app.schemas.user import MFAVerifySetupRequest
from tests.jwt_compat import jwt

USER_UUID = "019ec90a-1b2c-7def-8000-0000000000aa"


# ── fakes ────────────────────────────────────────────────────────────────────────


class _FakeQuery:
    """Returns one canned row regardless of the filter — the filters are SQL, not logic."""

    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._result


class _FakeDB:
    """Minimal ``Session`` stand-in keyed by model class."""

    def __init__(self, rows: dict | None = None):
        self.rows = rows or {}
        self.commits = 0
        self.deleted: list = []
        self.added: list = []

    def query(self, model):
        return _FakeQuery(self.rows.get(model))

    def commit(self):
        self.commits += 1

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)


class _FakeRedis:
    """Records ``SET NX`` the way Redis would."""

    def __init__(self):
        self.keys: dict[str, str] = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def exists(self, key):
        return 1 if key in self.keys else 0


def _db(rows: dict | None = None) -> Any:
    """A ``Session`` stand-in.

    Typed ``Any`` so it can be handed to signatures declaring ``Session``
    without a cast at every call site.
    """
    return _FakeDB(rows)


def _user(**overrides: Any) -> Any:
    """A ``User`` stand-in with the attributes the auth paths read."""
    attrs = {
        "id": 1,
        "uuid": UUID(USER_UUID),
        "email": "person@example.com",
        "role": "user",
        "auth_type": "local",
        "is_active": True,
        "allow_local_fallback": False,
    }
    attrs.update(overrides)
    return SimpleNamespace(**attrs)


def _body(response) -> dict:
    return cast(dict, json.loads(response.body))


def _unwrap(func):
    """Strip the slowapi rate-limit wrapper so the endpoint can be called directly."""
    while hasattr(func, "__wrapped__"):
        func = func.__wrapped__
    return func


def _decode(token: str) -> dict:
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


@pytest.fixture
def fake_redis(monkeypatch) -> _FakeRedis:
    fake = _FakeRedis()
    monkeypatch.setattr(mfa_tokens_module, "get_redis_client", lambda: fake)
    return fake


@pytest.fixture
def mfa_required(monkeypatch):
    """Deployment-wide MFA requirement, as the admin UI / .env would set it."""
    monkeypatch.setattr(login_module, "_is_mfa_enabled", lambda db: True)
    monkeypatch.setattr(login_module, "_is_mfa_required", lambda db: True)


# ── MFA_REQUIRED is enforced at login, not just reported ─────────────────────────


class TestEnrollmentIsEnforcedAtLogin:
    def test_unenrolled_user_gets_no_access_token(self, mfa_required):
        db = _db({UserMFA: None})

        response = login_module._check_mfa_requirement(db, _user(), USER_UUID, "user")

        assert response is not None, "an unenrolled user must not get a full session"
        body = _body(response)
        assert body["mfa_required"] is True
        assert body["mfa_enrollment_required"] is True
        assert "access_token" not in body
        assert "refresh_token" not in body

    def test_enrollment_challenge_carries_a_usable_half_token(self, mfa_required):
        db = _db({UserMFA: None})

        body = _body(login_module._check_mfa_requirement(db, _user(), USER_UUID, "user"))

        payload = _decode(body["mfa_token"])
        assert payload["sub"] == USER_UUID
        assert payload["jti"]

    def test_enrolled_user_is_challenged_to_verify_not_enroll(self, mfa_required):
        db = _db({UserMFA: SimpleNamespace(totp_enabled=True)})

        body = _body(login_module._check_mfa_requirement(db, _user(), USER_UUID, "user"))

        assert body["mfa_required"] is True
        assert "mfa_enrollment_required" not in body

    def test_unenrolled_user_passes_when_mfa_is_optional(self, monkeypatch):
        """MFA enabled but not required stays opt-in — the pre-existing behaviour."""
        monkeypatch.setattr(login_module, "_is_mfa_enabled", lambda db: True)
        monkeypatch.setattr(login_module, "_is_mfa_required", lambda db: False)

        assert (
            login_module._check_mfa_requirement(_db({UserMFA: None}), _user(), USER_UUID, "user")
            is None
        )

    def test_pki_user_on_native_auth_still_bypasses_local_mfa(self, mfa_required):
        """Smart card is already two-factor; requiring a local TOTP would lock them out."""
        db = _db({UserMFA: None})

        assert (
            login_module._check_mfa_requirement(
                db, _user(auth_type="pki"), USER_UUID, "user", actual_auth_method="pki"
            )
            is None
        )

    def test_pki_user_on_password_fallback_must_enroll(self, mfa_required):
        db = _db({UserMFA: None})

        response = login_module._check_mfa_requirement(
            db, _user(auth_type="pki"), USER_UUID, "user", actual_auth_method="local"
        )

        assert _body(response)["mfa_enrollment_required"] is True

    def test_login_endpoint_returns_the_challenge_instead_of_tokens(
        self, monkeypatch, mfa_required
    ):
        """End to end through /token: the defect was a full session for an API client."""
        user = _user()
        db = _db({User: user, UserMFA: None})
        monkeypatch.setattr(
            login_module, "_perform_authentication", lambda db, u, p: (True, USER_UUID, {}, "local")
        )
        monkeypatch.setattr(
            login_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest")
        )
        monkeypatch.setattr(login_module, "check_and_record_attempt", lambda *a, **k: (False, None))
        monkeypatch.setattr(login_module, "_get_user_role", lambda db, uuid_str, data: "user")
        monkeypatch.setattr(settings, "MAX_CONCURRENT_SESSIONS", 0)

        response = _unwrap(login_module.login_for_access_token)(
            response=Response(),
            request=cast(Any, None),
            form_data=SimpleNamespace(username="person@example.com", password="pw"),
            db=db,
        )

        body = _body(response)
        assert "access_token" not in body
        assert body["mfa_enrollment_required"] is True
        assert response.headers.get("set-cookie") is None


class TestEnrollmentTokenIsNotAnAccessToken:
    def test_token_carries_the_mfa_type_claim(self, mfa_required):
        body = _body(
            login_module._check_mfa_requirement(_db({UserMFA: None}), _user(), USER_UUID, "user")
        )

        assert _decode(body["mfa_token"])["type"] == TOKEN_TYPE_MFA

    def test_token_is_rejected_by_get_current_user(self, mfa_required):
        body = _body(
            login_module._check_mfa_requirement(_db({UserMFA: None}), _user(), USER_UUID, "user")
        )
        request: Any = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=request, token=body["mfa_token"], db=cast(Any, None))

        assert exc.value.status_code == 401


# ── the enrolment half-token authorizes enrolment, and only enrolment ────────────


def _enroll_token(role: str = "user") -> str:
    return mfa_tokens_module._create_mfa_token(USER_UUID, role, scope=MFA_SCOPE_ENROLL)


def _verify_token(role: str = "user") -> str:
    return mfa_tokens_module._create_mfa_token(USER_UUID, role, scope=MFA_SCOPE_VERIFY)


class TestEnrollmentTokenScope:
    """A verify token must not reach /mfa/setup: that endpoint RESETS the second factor."""

    def test_login_mints_an_enroll_scoped_token(self, mfa_required):
        body = _body(
            login_module._check_mfa_requirement(_db({UserMFA: None}), _user(), USER_UUID, "user")
        )

        assert _decode(body["mfa_token"])["mfa_scope"] == MFA_SCOPE_ENROLL

    def test_login_mints_a_verify_scoped_token_for_an_enrolled_user(self, mfa_required):
        body = _body(
            login_module._check_mfa_requirement(
                _db({UserMFA: SimpleNamespace(totp_enabled=True)}),
                _user(),
                USER_UUID,
                "user",
            )
        )

        assert _decode(body["mfa_token"])["mfa_scope"] == MFA_SCOPE_VERIFY

    def test_verify_token_is_refused_at_enrollment(self, fake_redis):
        db = _db({User: _user()})

        with pytest.raises(HTTPException) as exc:
            mfa_enrollment_module.get_user_for_enrollment(
                request=cast(Any, None), token=_verify_token(), db=db
            )

        assert exc.value.status_code == 401
        assert exc.value.detail == "Invalid MFA token"

    def test_enroll_token_is_refused_at_verification(self, fake_redis):
        """The mirror guard: an enrolment token must not complete a login."""
        db = _db({User: _user(), UserMFA: SimpleNamespace(totp_enabled=True)})

        with pytest.raises(HTTPException) as exc:
            mfa_tokens_module._get_user_for_mfa(db, _enroll_token())

        assert exc.value.status_code == 401

    def test_legacy_unscoped_token_still_verifies(self, fake_redis):
        """A half-token minted before scopes existed keeps working across a deploy."""
        from datetime import UTC
        from datetime import datetime
        from datetime import timedelta

        now = datetime.now(UTC)
        legacy = jwt.encode(
            {
                "sub": USER_UUID,
                "role": "user",
                "type": TOKEN_TYPE_MFA,
                "jti": "legacy-jti",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        uuid_str, role, jti = mfa_tokens_module._verify_mfa_token(legacy)

        assert (uuid_str, role, jti) == (USER_UUID, "user", "legacy-jti")

    def test_legacy_unscoped_token_cannot_enroll(self, fake_redis):
        """…but it is not silently promoted to the new, more powerful scope."""
        from datetime import UTC
        from datetime import datetime
        from datetime import timedelta

        now = datetime.now(UTC)
        legacy = jwt.encode(
            {
                "sub": USER_UUID,
                "role": "user",
                "type": TOKEN_TYPE_MFA,
                "jti": "legacy-jti-2",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )

        with pytest.raises(HTTPException) as exc:
            mfa_tokens_module._verify_mfa_token(legacy, expected_scope=MFA_SCOPE_ENROLL)

        assert exc.value.status_code == 401

    def test_enroll_token_is_still_not_a_session(self, fake_redis):
        request: Any = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=request, token=_enroll_token(), db=cast(Any, None))

        assert exc.value.status_code == 401

    def test_inactive_user_cannot_enroll(self, fake_redis):
        db = _db({User: _user(is_active=False)})

        with pytest.raises(HTTPException) as exc:
            mfa_enrollment_module.get_user_for_enrollment(
                request=cast(Any, None), token=_enroll_token(), db=db
            )

        assert exc.value.status_code == 401
        assert "Inactive" in exc.value.detail

    def test_used_enroll_token_is_refused(self, fake_redis):
        token = _enroll_token()
        mfa_tokens_module._claim_mfa_token(_decode(token)["jti"], 300)

        with pytest.raises(HTTPException) as exc:
            mfa_enrollment_module.get_user_for_enrollment(
                request=cast(Any, None), token=token, db=_db({User: _user()})
            )

        assert exc.value.status_code == 401
        assert "already been used" in exc.value.detail

    def test_a_real_session_still_authorizes_enrollment(self, monkeypatch, fake_redis):
        """The voluntary path: a logged-in user adding MFA gets no jti to burn."""
        user = _user()
        monkeypatch.setattr(
            mfa_enrollment_module, "get_current_user", lambda request, token, db: user
        )

        ctx = mfa_enrollment_module.get_user_for_enrollment(
            request=cast(Any, None), token=None, db=_db()
        )

        assert ctx.user is user
        assert ctx.mfa_jti is None


class TestForcedEnrollmentFlow:
    """/mfa/setup then /mfa/verify-setup, driven entirely by the enrolment half-token."""

    @pytest.fixture
    def enrollment_env(self, monkeypatch, fake_redis):
        """Patch out audit/IO so the two endpoints can be called directly."""
        monkeypatch.setattr(mfa_module, "_is_mfa_enabled", lambda db: True)
        monkeypatch.setattr(mfa_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest"))
        monkeypatch.setattr(mfa_module.audit_logger, "log_mfa_event", lambda **kwargs: None)
        monkeypatch.setattr(settings, "MFA_BACKUP_CODE_COUNT", 2)
        monkeypatch.setattr(
            mfa_enrollment_module.token_service,
            "create_refresh_token",
            lambda **kwargs: ("refresh-token", None),
        )
        # TOTP single-use claims go to the same fake Redis as the half-token claims.
        import app.core.redis as redis_module

        monkeypatch.setattr(redis_module, "get_redis", lambda: fake_redis)
        return fake_redis

    def _context(self, token: str, db) -> mfa_enrollment_module.EnrollmentContext:
        return mfa_enrollment_module.get_user_for_enrollment(
            request=cast(Any, None), token=token, db=db
        )

    def test_enrollment_token_reaches_mfa_setup(self, enrollment_env):
        db = _db({User: _user(), UserMFA: None})
        ctx = self._context(_enroll_token(), db)

        response = _unwrap(mfa_module.setup_mfa)(
            response=Response(), request=cast(Any, None), enrollment=ctx, db=db
        )

        assert response.secret
        assert response.provisioning_uri.startswith("otpauth://totp/")
        assert db.added, "a UserMFA row should have been staged"

    def test_setup_does_not_burn_the_token(self, enrollment_env):
        """The user may re-open the QR page; only verify-setup spends the token."""
        db = _db({User: _user(), UserMFA: None})
        token = _enroll_token()

        _unwrap(mfa_module.setup_mfa)(
            response=Response(), request=cast(Any, None), enrollment=self._context(token, db), db=db
        )

        # Still resolvable → still unspent.
        assert self._context(token, db).mfa_jti == _decode(token)["jti"]

    def _pending_enrollment(self):
        """A UserMFA row mid-setup: secret stored, not yet enabled."""
        secret = MFAService.generate_totp_secret()
        user_mfa: Any = SimpleNamespace(
            user_id=1,
            totp_secret=MFAService.encrypt_totp_secret(secret),
            totp_enabled=False,
            backup_codes=[],
        )
        return secret, user_mfa

    def test_verify_setup_enables_mfa_and_returns_a_session(self, enrollment_env):
        secret, user_mfa = self._pending_enrollment()
        db = _db({User: _user(), UserMFA: user_mfa})
        ctx = self._context(_enroll_token(), db)

        response = _unwrap(mfa_module.verify_mfa_setup)(
            response=Response(),
            request=cast(Any, None),
            request_body=MFAVerifySetupRequest(code=pyotp.TOTP(secret).now()),
            enrollment=ctx,
            db=db,
        )

        body = _body(response)
        assert user_mfa.totp_enabled is True
        assert body["success"] is True
        assert len(body["backup_codes"]) == 2
        assert body["access_token"]
        assert body["refresh_token"] == "refresh-token"
        assert response.headers.get("set-cookie") is not None

    def test_the_issued_token_is_a_real_access_token(self, enrollment_env):
        secret, user_mfa = self._pending_enrollment()
        db = _db({User: _user(), UserMFA: user_mfa})

        response = _unwrap(mfa_module.verify_mfa_setup)(
            response=Response(),
            request=cast(Any, None),
            request_body=MFAVerifySetupRequest(code=pyotp.TOTP(secret).now()),
            enrollment=self._context(_enroll_token(), db),
            db=db,
        )

        payload = _decode(_body(response)["access_token"])
        assert payload["type"] == "access"
        assert payload["sub"] == USER_UUID

    def test_verify_setup_burns_the_enrollment_token(self, enrollment_env):
        """Replay after enrolment: a second /mfa/setup must not let the token run again."""
        secret, user_mfa = self._pending_enrollment()
        db = _db({User: _user(), UserMFA: user_mfa})
        token = _enroll_token()

        _unwrap(mfa_module.verify_mfa_setup)(
            response=Response(),
            request=cast(Any, None),
            request_body=MFAVerifySetupRequest(code=pyotp.TOTP(secret).now()),
            enrollment=self._context(token, db),
            db=db,
        )

        with pytest.raises(HTTPException) as exc:
            self._context(token, db)

        assert exc.value.status_code == 401
        assert "already been used" in exc.value.detail

    def test_wrong_code_does_not_burn_the_token(self, enrollment_env):
        """A typo must not cost the user their only route out of the login screen."""
        _secret, user_mfa = self._pending_enrollment()
        db = _db({User: _user(), UserMFA: user_mfa})
        token = _enroll_token()

        with pytest.raises(HTTPException) as exc:
            _unwrap(mfa_module.verify_mfa_setup)(
                response=Response(),
                request=cast(Any, None),
                request_body=MFAVerifySetupRequest(code="000000"),
                enrollment=self._context(token, db),
                db=db,
            )

        assert exc.value.status_code == 400
        assert user_mfa.totp_enabled is False
        assert self._context(token, db).mfa_jti  # still spendable

    def test_voluntary_enrollment_gets_no_tokens(self, enrollment_env, monkeypatch):
        """An already-logged-in user keeps their session; no second one is minted."""
        secret, user_mfa = self._pending_enrollment()
        db = _db({User: _user(), UserMFA: user_mfa})
        ctx = mfa_enrollment_module.EnrollmentContext(user=_user(), user_role="user")

        response = _unwrap(mfa_module.verify_mfa_setup)(
            response=Response(),
            request=cast(Any, None),
            request_body=MFAVerifySetupRequest(code=pyotp.TOTP(secret).now()),
            enrollment=ctx,
            db=db,
        )

        body = _body(response)
        assert body["success"] is True
        assert "access_token" not in body
        assert response.headers.get("set-cookie") is None


# ── /mfa/disable consumes the backup code it accepts ─────────────────────────────


class TestDisableConsumesBackupCode:
    @pytest.fixture
    def disable_setup(self, monkeypatch):
        codes = MFAService.generate_backup_codes(2)
        hashed = MFAService.hash_backup_codes(codes)
        user_mfa: Any = SimpleNamespace(
            user_id=1,
            totp_secret="encrypted",
            totp_enabled=True,
            backup_codes=list(hashed),
        )
        db = _db({UserMFA: user_mfa})

        monkeypatch.setattr(mfa_module, "_is_mfa_enabled", lambda db: True)
        monkeypatch.setattr(mfa_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest"))
        monkeypatch.setattr(mfa_module.audit_logger, "log_mfa_event", lambda **kwargs: None)
        monkeypatch.setattr(
            MFAService, "decrypt_totp_secret", staticmethod(lambda secret: "JBSWY3DPEHPK3PXP")
        )
        return codes, hashed, user_mfa, db

    def _disable(self, code: str, db, user=None):
        return _unwrap(mfa_module.disable_mfa)(
            response=Response(),
            request=cast(Any, None),
            request_body=MFADisableRequest(code=code),
            current_user=user or _user(),
            db=db,
        )

    def test_used_backup_code_is_removed(self, disable_setup):
        codes, hashed, user_mfa, db = disable_setup

        self._disable(codes[0], db)

        assert hashed[0] not in user_mfa.backup_codes
        assert len(user_mfa.backup_codes) == 1

    def test_used_backup_code_no_longer_verifies(self, disable_setup):
        codes, _hashed, user_mfa, db = disable_setup

        self._disable(codes[0], db)
        still_valid, _ = MFAService.verify_backup_code(codes[0], list(user_mfa.backup_codes))

        assert still_valid is False

    def test_unused_backup_code_survives(self, disable_setup):
        codes, hashed, user_mfa, db = disable_setup

        self._disable(codes[0], db)

        assert hashed[1] in user_mfa.backup_codes

    def test_wrong_code_is_rejected_and_consumes_nothing(self, disable_setup):
        _codes, hashed, user_mfa, db = disable_setup

        with pytest.raises(HTTPException) as exc:
            self._disable("ZZZZ-9999", db)

        assert exc.value.status_code == 401
        assert list(user_mfa.backup_codes) == hashed


# ── /mfa/verify refuses a deactivated account ────────────────────────────────────


class TestInactiveUserCannotCompleteMFA:
    def test_deactivated_account_is_refused(self, fake_redis):
        token = mfa_tokens_module._create_mfa_token(USER_UUID, "user")
        db = _db(
            {
                User: _user(is_active=False),
                UserMFA: SimpleNamespace(totp_enabled=True, totp_secret="encrypted"),
            }
        )

        with pytest.raises(HTTPException) as exc:
            mfa_tokens_module._get_user_for_mfa(db, token)

        assert exc.value.status_code == 401
        assert "Inactive" in exc.value.detail

    def test_active_account_still_passes(self, fake_redis):
        token = mfa_tokens_module._create_mfa_token(USER_UUID, "user")
        user_mfa: Any = SimpleNamespace(totp_enabled=True, totp_secret="encrypted")
        db = _db({User: _user(), UserMFA: user_mfa})

        resolved_user, resolved_mfa, uuid_str, role, jti = mfa_tokens_module._get_user_for_mfa(
            db, token
        )

        assert resolved_user.is_active is True
        assert resolved_mfa is user_mfa
        assert uuid_str == USER_UUID
        assert role == "user"
        assert jti


# ── one half-token, one session ──────────────────────────────────────────────────


class TestHalfTokenIsSingleUse:
    def test_claim_is_atomic(self, fake_redis):
        assert mfa_tokens_module._claim_mfa_token("jti-1", 300) is True
        assert mfa_tokens_module._claim_mfa_token("jti-1", 300) is False

    def test_claim_is_scoped_per_jti(self, fake_redis):
        assert mfa_tokens_module._claim_mfa_token("jti-1", 300) is True
        assert mfa_tokens_module._claim_mfa_token("jti-2", 300) is True

    def test_claim_uses_set_nx_not_read_then_write(self, fake_redis, monkeypatch):
        """A plain SET would let both racers through; the NX flag is the whole fix."""
        captured = {}

        def capture(key, value, nx=False, ex=None):
            captured.update({"key": key, "nx": nx, "ex": ex})
            return True

        monkeypatch.setattr(fake_redis, "set", capture)
        mfa_tokens_module._claim_mfa_token("jti-3", 300)

        assert captured["nx"] is True
        assert captured["ex"] == 300
        assert captured["key"].endswith("jti-3")

    def test_claim_fails_closed_without_redis(self, monkeypatch):
        monkeypatch.setattr(mfa_tokens_module, "get_redis_client", lambda: None)
        monkeypatch.setattr(settings, "MFA_REQUIRE_REDIS", True)

        with pytest.raises(HTTPException) as exc:
            mfa_tokens_module._claim_mfa_token("jti-4", 300)

        assert exc.value.status_code == 503

    def test_claim_without_redis_still_consults_the_blacklist(self, monkeypatch):
        """Fail-open must not mean "skip the check" — the non-atomic pair still runs."""
        used: set[str] = set()
        monkeypatch.setattr(mfa_tokens_module, "get_redis_client", lambda: None)
        monkeypatch.setattr(settings, "MFA_REQUIRE_REDIS", False)
        monkeypatch.setattr(mfa_tokens_module, "_is_mfa_token_blacklisted", lambda jti: jti in used)

        def _record(jti: str, ttl: int) -> bool:
            used.add(jti)
            return True

        monkeypatch.setattr(mfa_tokens_module, "_blacklist_mfa_token", _record)

        assert mfa_tokens_module._claim_mfa_token("jti-5", 300) is True
        assert mfa_tokens_module._claim_mfa_token("jti-5", 300) is False

    def test_concurrent_verification_yields_exactly_one_session(self, fake_redis, monkeypatch):
        user = _user()
        user_mfa: Any = SimpleNamespace(totp_enabled=True, last_verified_at=None)
        db = _db()
        monkeypatch.setattr(mfa_enrollment_module.audit_logger, "log_mfa_event", lambda **kw: None)
        monkeypatch.setattr(
            mfa_enrollment_module.token_service,
            "create_refresh_token",
            lambda **kwargs: ("refresh-token", None),
        )

        def verify():
            return mfa_enrollment_module._complete_mfa_verification(
                db, user, user_mfa, USER_UUID, "user", "shared-jti", False, "10.0.0.1", "pytest"
            )

        first = verify()
        with pytest.raises(HTTPException) as exc:
            verify()

        assert _body(first)["access_token"]
        assert exc.value.status_code == 401
        assert "already been used" in exc.value.detail

    def test_replay_is_refused_before_anything_is_written(self, fake_redis, monkeypatch):
        """The claim must precede the DB write, or a losing racer still mutates state."""
        user_mfa: Any = SimpleNamespace(totp_enabled=True, last_verified_at=None)
        db = _db()
        monkeypatch.setattr(mfa_enrollment_module.audit_logger, "log_mfa_event", lambda **kw: None)
        mfa_tokens_module._claim_mfa_token("burned-jti", 300)

        with pytest.raises(HTTPException):
            mfa_enrollment_module._complete_mfa_verification(
                db, _user(), user_mfa, USER_UUID, "user", "burned-jti", False, "10.0.0.1", "pytest"
            )

        assert db.commits == 0
        assert user_mfa.last_verified_at is None


# ── the login audit records the method actually used ─────────────────────────────


def _lockout_policy():
    """The resolved (DB > .env) lockout policy the audit record must quote."""
    return SimpleNamespace(account_lockout_threshold=5, account_lockout_duration_minutes=15)


class TestAuditRecordsRealAuthMethod:
    @pytest.fixture
    def ldap_deployment(self, monkeypatch):
        monkeypatch.setattr(settings, "LDAP_ENABLED", True)
        monkeypatch.setattr(login_module, "check_and_record_attempt", lambda *a, **k: (False, None))
        monkeypatch.setattr(
            login_module, "get_lockout_info", lambda u: {"failed_attempts": 0, "lockout_count": 0}
        )

    def test_local_password_failure_is_not_audited_as_ldap(self, monkeypatch, ldap_deployment):
        captured = {}
        monkeypatch.setattr(
            login_module.audit_logger, "log_login_failure", lambda **kw: captured.update(kw)
        )

        # Second argument is the canonical lockout bucket; for a submission that IS
        # the account's email the two coincide.
        login_module._handle_lockout_check(
            "person@example.com",
            "person@example.com",
            False,
            "10.0.0.1",
            "pytest",
            _lockout_policy(),
        )

        assert captured["auth_method"] != "ldap"
        assert captured["auth_method"] == "unknown"

    def test_ldap_failure_is_audited_as_ldap(self, monkeypatch, ldap_deployment):
        captured = {}
        monkeypatch.setattr(
            login_module.audit_logger, "log_login_failure", lambda **kw: captured.update(kw)
        )

        login_module._handle_lockout_check(
            "person@example.com",
            "person@example.com",
            False,
            "10.0.0.1",
            "pytest",
            _lockout_policy(),
            auth_method="ldap",
        )

        assert captured["auth_method"] == "ldap"

    def test_fallback_success_is_audited_as_local(self, monkeypatch):
        """A PKI user who used the local password must not be audited as an LDAP login."""
        monkeypatch.setattr(settings, "LDAP_ENABLED", True)
        captured = {}
        monkeypatch.setattr(
            login_module.audit_logger, "log_login_success", lambda **kw: captured.update(kw)
        )
        monkeypatch.setattr(
            login_module.token_service,
            "create_refresh_token",
            lambda **kwargs: ("refresh-token", None),
        )

        login_module._generate_login_tokens(
            _db(),
            _user(auth_type="pki"),
            USER_UUID,
            "user",
            "pytest",
            "10.0.0.1",
            auth_method="local",
        )

        assert captured["auth_method"] == "local"


# ── an inactive account is an ordinary failed attempt ────────────────────────────


class TestInactiveAccountLogin:
    def test_inactive_account_does_not_raise_its_own_status(self, monkeypatch):
        def _inactive(db, username, password):
            raise HTTPException(status_code=400, detail="Inactive user account")

        monkeypatch.setenv("TESTING", "false")
        monkeypatch.setattr(login_module, "_authenticate_production_user", _inactive)

        success, uuid_str, data, method = login_module._perform_authentication(
            _db(), "person@example.com", "pw"
        )

        assert success is False
        assert (uuid_str, data, method) == ("", {}, "")

    def test_unexpected_errors_still_propagate(self, monkeypatch):
        def _boom(db, username, password):
            raise HTTPException(status_code=503, detail="Directory unavailable")

        monkeypatch.setenv("TESTING", "false")
        monkeypatch.setattr(login_module, "_authenticate_production_user", _boom)

        with pytest.raises(HTTPException) as exc:
            login_module._perform_authentication(_db(), "person@example.com", "pw")

        assert exc.value.status_code == 503


class TestLockoutRecording:
    @pytest.fixture
    def recorded(self, monkeypatch):
        """Capture every check_and_record_attempt call the login endpoint makes."""
        calls: list[dict] = []

        def record(identifier, success, exempt_from_lockout=False):
            calls.append(
                {
                    "identifier": identifier,
                    "success": success,
                    "exempt_from_lockout": exempt_from_lockout,
                }
            )
            return False, None

        monkeypatch.setattr(login_module, "check_and_record_attempt", record)
        monkeypatch.setattr(
            login_module, "get_lockout_info", lambda u: {"failed_attempts": 0, "lockout_count": 0}
        )
        monkeypatch.setattr(login_module.audit_logger, "log_login_failure", lambda **kw: None)
        monkeypatch.setattr(
            login_module, "_get_client_info", lambda request: ("10.0.0.1", "pytest")
        )
        monkeypatch.setattr(
            login_module, "_perform_authentication", lambda db, u, p: (False, "", {}, "")
        )
        return calls

    def _login(self, db):
        return _unwrap(login_module.login_for_access_token)(
            response=Response(),
            request=cast(Any, None),
            form_data=SimpleNamespace(username="person@example.com", password="pw"),
            db=db,
        )

    def test_failed_login_returns_the_uniform_401(self, recorded):
        with pytest.raises(HTTPException) as exc:
            self._login(_db({User: None}))

        assert exc.value.status_code == 401
        assert exc.value.detail == "Incorrect username or password"

    def test_failed_login_records_the_attempt(self, recorded):
        with pytest.raises(HTTPException):
            self._login(_db({User: None}))

        assert recorded == [
            {"identifier": "person@example.com", "success": False, "exempt_from_lockout": False}
        ]

    def test_super_admin_exemption_applies_to_failures(self, recorded):
        """The exemption was computed inside `if auth_success:`, so it never fired."""
        db = _db({User: _user(role="super_admin", allow_local_fallback=True)})

        with pytest.raises(HTTPException):
            self._login(db)

        assert recorded[0]["exempt_from_lockout"] is True

    def test_ordinary_account_is_not_exempt(self, recorded):
        db = _db({User: _user(role="admin", allow_local_fallback=True)})

        with pytest.raises(HTTPException):
            self._login(db)

        assert recorded[0]["exempt_from_lockout"] is False
