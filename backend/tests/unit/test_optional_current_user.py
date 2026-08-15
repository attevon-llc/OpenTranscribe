"""``get_optional_current_user`` — every way a credential can fail to be one.

Optional auth is the quiet one: it answers ``None`` for *every* failure, so a
mistake anywhere in it does not raise, does not log an error, and does not show up
in a status code. The two directions that matter are therefore both silent:

* accepting something it should not (an expired token, a revoked one, a token for
  an account that is deactivated or gone) hands the caller a session on a
  credential nobody would honour at the strict dependency next door;
* refusing something it should accept downgrades an owner to anonymous, which on a
  read surface renders as "this file is not yours" rather than as an error.

So every test here asserts the identity that came back, not merely truthiness — and
each rejection has a positive control that differs in exactly one thing.

``tests/unit/test_token_type_binding.py`` owns the ``type``-claim rejections and
``tests/test_cloud_seams.py`` owns the external-verifier happy path. This suite
covers the rest: where the token is read from, expiry, the revocation gate, the
subject claim, and the account lookup — against a real savepointed session and real
``User`` rows, with tokens minted by the real signer.
"""

# mypy: disable-error-code="arg-type"
# ``request``/``db`` are declared ``Request``/``Session`` for the production call
# sites; these are real Requests built from an ASGI scope and the real savepointed
# session, passed positionally.
from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import pytest
from starlette.requests import Request

from app.api.endpoints.auth.dependencies import get_optional_current_user
from app.auth import token_service as token_service_module
from app.auth.constants import TOKEN_TYPE_ACCESS
from app.auth.cookies import ACCESS_COOKIE
from app.auth.direct_auth import create_access_token
from app.core.config import settings
from app.core.security import get_password_hash
from app.models.user import User


def _request(*, bearer: str | None = None, cookie: str | None = None, raw_auth: str | None = None):
    """A real ``Request``, carrying the credential where the argument says.

    Args:
        bearer: Token to send as ``Authorization: Bearer <token>``.
        cookie: Token to send in the httpOnly ``access_token`` cookie — the browser
            SPA's only channel, since there is no JS-readable token.
        raw_auth: A verbatim ``Authorization`` value, for the malformed-header cases.
    """
    headers: list[tuple[bytes, bytes]] = [(b"user-agent", b"pytest")]
    if bearer is not None:
        headers.append((b"authorization", f"Bearer {bearer}".encode()))
    elif raw_auth is not None:
        headers.append((b"authorization", raw_auth.encode()))
    if cookie is not None:
        headers.append((b"cookie", f"{ACCESS_COOKIE}={cookie}".encode()))

    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": "/api/files/some-uuid",
            "query_string": b"",
            "headers": headers,
            "client": ("10.0.0.7", 40000),
        }
    )


@pytest.fixture
def account(db_session) -> User:
    """A real, ordinary account."""
    unique = uuid_pkg.uuid4().hex[:8]
    user = User(
        email=f"optional-{unique}@example.com",
        full_name="Optional Person",
        hashed_password=get_password_hash("irrelevant-Passphrase99!"),
        role="user",
        auth_type="local",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _token(user: User, **overrides: Any) -> str:
    """An access token for *user*, signed the way the login path signs one."""
    claims: dict[str, Any] = {"sub": str(user.uuid), "role": str(user.role)}
    claims.update(overrides)
    return create_access_token(claims)


def _encode(claims: dict[str, Any], *, key: str | None = None) -> str:
    """Sign *claims* verbatim — no defaults added.

    ``create_access_token`` always stamps ``exp``/``jti``/``type``, which is correct
    for production and useless for asking what happens when one of them is missing.
    """
    from joserfc import jwt as joserfc_jwt
    from joserfc.jwk import OctKey

    payload = {"iat": int(datetime.now(UTC).timestamp()), **claims}
    return joserfc_jwt.encode(
        {"alg": settings.JWT_ALGORITHM},
        payload,
        OctKey.import_key(key or settings.JWT_SECRET_KEY),
        algorithms=[settings.JWT_ALGORITHM],
    )


# ── where the credential is read from ────────────────────────────────────────────


class TestWhereTheTokenComesFrom:
    """Bearer header first (API clients, Swagger), then the httpOnly cookie (SPA)."""

    def test_a_bearer_header_authenticates(self, db_session, account):
        user = get_optional_current_user(_request(bearer=_token(account)), db_session)

        assert user is not None
        assert user.id == account.id

    def test_the_cookie_authenticates_when_there_is_no_header(self, db_session, account):
        """The SPA has no JS-readable token, so this is the browser's only path."""
        user = get_optional_current_user(_request(cookie=_token(account)), db_session)

        assert user is not None
        assert user.id == account.id

    def test_no_credential_at_all_is_anonymous(self, db_session):
        assert get_optional_current_user(_request(), db_session) is None

    def test_a_non_bearer_authorization_scheme_is_not_a_token(self, db_session, account):
        """``Basic`` is somebody else's scheme; it must not be sliced up and tried."""
        assert get_optional_current_user(_request(raw_auth="Basic abc123"), db_session) is None

    def test_a_bare_token_without_the_bearer_prefix_is_not_honoured(self, db_session, account):
        """A valid token in a malformed header is still a malformed header."""
        assert get_optional_current_user(_request(raw_auth=_token(account)), db_session) is None

    def test_a_non_bearer_header_still_falls_back_to_the_cookie(self, db_session, account):
        """Header *shape* decides, not header presence — otherwise a stray
        ``Authorization`` from a proxy would black out the SPA's own cookie."""
        request = _request(raw_auth="Basic abc123", cookie=_token(account))

        user = get_optional_current_user(request, db_session)

        assert user is not None
        assert user.id == account.id

    def test_an_empty_bearer_value_is_anonymous(self, db_session):
        assert get_optional_current_user(_request(bearer=""), db_session) is None


# ── the token itself ─────────────────────────────────────────────────────────────


class TestTheTokenMustBeValid:
    def test_a_forged_signature_is_anonymous(self, db_session, account):
        good = _token(account)
        forged = good[:-4] + ("aaaa" if not good.endswith("aaaa") else "bbbb")

        assert get_optional_current_user(_request(bearer=forged), db_session) is None

    def test_a_token_signed_with_another_key_is_anonymous(self, db_session, account):
        now = datetime.now(UTC)
        foreign = _encode(
            {
                "sub": str(account.uuid),
                "role": "user",
                "jti": str(uuid_pkg.uuid4()),
                "exp": int((now + timedelta(minutes=60)).timestamp()),
                "type": TOKEN_TYPE_ACCESS,
            },
            key="a-completely-different-signing-key-0123456789abcdef",
        )

        assert get_optional_current_user(_request(bearer=foreign), db_session) is None

    def test_an_expired_token_is_anonymous(self, db_session, account):
        """``joserfc`` verifies the signature only — ``exp`` is validated explicitly,
        so dropping that validation would silently make every token immortal."""
        # Minted already expired rather than sleeping: the claim is what is checked.
        expired = create_access_token(
            {"sub": str(account.uuid), "role": "user"}, expires_delta=timedelta(minutes=-5)
        )

        assert get_optional_current_user(_request(bearer=expired), db_session) is None

    def test_a_token_expiring_shortly_is_still_accepted(self, db_session, account):
        """Positive control: only the sign of the offset differs from the line above."""
        fresh = create_access_token(
            {"sub": str(account.uuid), "role": "user"}, expires_delta=timedelta(minutes=5)
        )

        user = get_optional_current_user(_request(bearer=fresh), db_session)

        assert user is not None
        assert user.id == account.id

    def test_a_token_with_no_expiry_at_all_is_anonymous(self, db_session, account):
        """``exp`` is declared **essential**, and that is the half that matters.

        joserfc validates an ``exp`` it finds, so marking the claim non-essential
        changes nothing for an ordinary token — the only observable difference is a
        token carrying no ``exp`` whatsoever, which then becomes **immortal**. Every
        signer in this codebase stamps one, so the day that stops being enforced
        nothing else notices. Both dependencies share the registry, so both are
        checked here.
        """
        from fastapi import HTTPException

        from app.api.endpoints.auth.dependencies import get_current_user

        eternal = _encode({"sub": str(account.uuid), "role": "user", "type": TOKEN_TYPE_ACCESS})

        assert get_optional_current_user(_request(bearer=eternal), db_session) is None
        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_request(bearer=eternal), token=eternal, db=db_session)
        assert exc.value.status_code == 401

    def test_an_otherwise_identical_token_with_an_expiry_is_accepted(self, db_session, account):
        """Positive control: the ``exp`` claim is the only difference."""
        now = datetime.now(UTC)
        mortal = _encode(
            {
                "sub": str(account.uuid),
                "role": "user",
                "type": TOKEN_TYPE_ACCESS,
                "exp": int((now + timedelta(minutes=60)).timestamp()),
            }
        )

        user = get_optional_current_user(_request(bearer=mortal), db_session)

        assert user is not None
        assert user.id == account.id

    def test_a_token_with_no_subject_is_anonymous(self, db_session, account):
        now = datetime.now(UTC)
        subjectless = _encode(
            {
                "role": "user",
                "jti": str(uuid_pkg.uuid4()),
                "exp": int((now + timedelta(minutes=60)).timestamp()),
                "type": TOKEN_TYPE_ACCESS,
            }
        )

        assert get_optional_current_user(_request(bearer=subjectless), db_session) is None

    def test_a_subject_that_is_not_a_uuid_is_anonymous(self, db_session):
        """The column is a UUID; a legacy integer subject must not be coerced."""
        not_a_uuid = create_access_token({"sub": "42", "role": "user"})

        assert get_optional_current_user(_request(bearer=not_a_uuid), db_session) is None


# ── the revocation gate ──────────────────────────────────────────────────────────


class TestTheRevocationGate:
    """AC-12: a revoked credential must stop working on an optional route too."""

    @pytest.fixture
    def revocation_calls(self, monkeypatch) -> list[dict]:
        """Record what the blacklist check was asked, and answer "revoked".

        The blacklist itself is Redis-with-a-database-fallback and has its own
        suite; what is under test here is whether this dependency *consults* it and
        *honours* the answer — and with which arguments, since a check made about
        the wrong token or the wrong user answers about the wrong thing.
        """
        calls: list[dict] = []

        def _revoked(jti, db=None, user_uuid=None, issued_at=None):
            calls.append({"jti": jti, "db": db, "user_uuid": user_uuid})
            return True

        monkeypatch.setattr(token_service_module.token_service, "is_token_revoked", _revoked)
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)
        return calls

    def test_a_revoked_token_is_anonymous(self, db_session, account, revocation_calls):
        assert get_optional_current_user(_request(bearer=_token(account)), db_session) is None
        assert len(revocation_calls) == 1

    def test_the_check_is_asked_about_this_token_and_this_user(
        self, db_session, account, revocation_calls
    ):
        """A check made with the wrong jti or subject would always answer "valid"."""
        token = _token(account)

        assert get_optional_current_user(_request(bearer=token), db_session) is None

        asked = revocation_calls[0]
        assert asked["jti"] == _claims(token)["jti"]
        assert asked["user_uuid"] == str(account.uuid)
        assert asked["db"] is db_session

    def test_a_token_the_blacklist_does_not_know_is_accepted(
        self, db_session, account, monkeypatch
    ):
        """Positive control: same wiring, opposite answer."""
        monkeypatch.setattr(
            token_service_module.token_service, "is_token_revoked", lambda *a, **k: False
        )
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)

        user = get_optional_current_user(_request(bearer=_token(account)), db_session)

        assert user is not None
        assert user.id == account.id

    def test_the_setting_gates_the_check(self, db_session, account, monkeypatch):
        """With revocation disabled the blacklist is not consulted at all — so a
        deployment that turned it off does not pay for it, and cannot be locked out
        by a stale entry."""
        calls: list[str] = []

        def _revoked(jti, **kwargs):
            calls.append(jti)
            return True

        monkeypatch.setattr(token_service_module.token_service, "is_token_revoked", _revoked)
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", False)

        user = get_optional_current_user(_request(bearer=_token(account)), db_session)

        assert user is not None
        assert calls == []

    def test_a_token_with_no_jti_has_no_blacklist_entry_to_check(
        self, db_session, account, monkeypatch
    ):
        """No ``jti`` means no entry could exist, so it is not "revoked" — but it
        must also not be handed to the check as ``None``, which fails closed and
        would refuse a credential the strict dependency accepts."""
        calls: list[Any] = []

        def _revoked(jti, **kwargs):
            calls.append(jti)
            return True

        monkeypatch.setattr(token_service_module.token_service, "is_token_revoked", _revoked)
        monkeypatch.setattr(settings, "TOKEN_REVOCATION_ENABLED", True)

        now = datetime.now(UTC)
        jtiless = _encode(
            {
                "sub": str(account.uuid),
                "role": "user",
                "exp": int((now + timedelta(minutes=60)).timestamp()),
                "type": TOKEN_TYPE_ACCESS,
            }
        )

        user = get_optional_current_user(_request(bearer=jtiless), db_session)

        assert user is not None
        assert calls == []


def _claims(token: str) -> dict:
    """The claims of *token*, read back the way the dependency reads them."""
    from joserfc import jwt as joserfc_jwt
    from joserfc.jwk import OctKey

    return dict(
        joserfc_jwt.decode(
            token, OctKey.import_key(settings.JWT_SECRET_KEY), algorithms=[settings.JWT_ALGORITHM]
        ).claims
    )


# ── the account behind the token ─────────────────────────────────────────────────


class TestTheAccountBehindTheToken:
    def test_the_account_the_subject_names_is_the_one_returned(
        self, db_session, account, other_user
    ):
        """Not "some user": the lookup must key on the token's own subject."""
        user = get_optional_current_user(_request(bearer=_token(account)), db_session)

        assert user is not None
        assert user.id == account.id
        assert user.id != other_user.id

    def test_a_token_for_an_account_that_no_longer_exists_is_anonymous(self, db_session, account):
        """A deleted account's token is still perfectly well signed."""
        token = _token(account)
        db_session.delete(account)
        db_session.commit()

        assert get_optional_current_user(_request(bearer=token), db_session) is None

    def test_a_deactivated_account_is_anonymous(self, db_session, account):
        token = _token(account)
        account.is_active = False
        db_session.commit()

        assert get_optional_current_user(_request(bearer=token), db_session) is None

    def test_an_active_account_is_not(self, db_session, account):
        """Positive control: only ``is_active`` differs from the line above."""
        user = get_optional_current_user(_request(bearer=_token(account)), db_session)

        assert user is not None
        assert user.id == account.id

    def test_a_broken_session_is_anonymous_rather_than_an_error(self, db_session, account):
        """Optional auth's whole contract: it never raises. A public read surface
        must degrade to anonymous when the database is unavailable, not 500."""

        class _BrokenSession:
            def query(self, *_args, **_kwargs):
                raise RuntimeError("database went away")

        assert get_optional_current_user(_request(bearer=_token(account)), _BrokenSession()) is None
