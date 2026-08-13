"""``_authenticate_external_token`` — the containment around the cloud JIT seam.

``get_current_user`` offers every bearer token to the registered external
verifiers before the local-JWT path. The community edition registers none, so the
branch is a no-op here; the *failure* handling is what this suite pins, because
each of its three failure modes is silent when it is wrong:

* a **refused link** (``PermissionError`` — unverified email match, or a
  ``super_admin`` target) must be a clean 401. Anything else turns
  ``account_linking``'s one unconditional rule into a 500 an operator reads as an
  outage rather than as a refusal;
* **any other sync failure** must be a clean 401 *and* must roll the session back.
  A half-applied JIT write left in the request's session poisons everything the
  request touches afterwards, and the 500 surfaces nowhere near the cause;
* an **inactive** external account must be the same 400 a local deactivated
  account gets, not a 401 — the credential was fine, the account is not.

``tests/test_cloud_seams.py`` covers the happy path and the "not my token" fall
through. This suite is the complement: the two exception branches, the
``request.state`` contract the observability layer reads, and the fact that a
verifier is only consulted when one is registered.

The registry is real (``register_verifier`` / ``verify_external_token``), the JIT
sync is real, and the users are real rows. Only the two failure injections replace
``sync_external_user_to_db``, because a sync that fails is precisely what cannot be
produced by driving the real one.
"""

# mypy: disable-error-code="arg-type"
# ``Request``/``Session`` parameters are declared for the production call sites;
# these are real Requests built from an ASGI scope and a real savepointed Session,
# handed in positionally. Declared once rather than as a cast per call.
from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.endpoints.auth import dependencies as deps_module
from app.auth import external_sync
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import has_verifiers
from app.auth.provider_registry import register_verifier
from app.auth.provider_registry import unregister_verifier
from app.core.security import get_password_hash
from app.models.user import User

_authenticate = deps_module._authenticate_external_token

#: A core-permitted ``auth_type``. The registry and the JIT sync are
#: vendor-neutral, but ``user.auth_type`` is CHECK-constrained to the built-in set
#: — the managed edition widens that CHECK in its own migration.
PROVIDER = "oidc"
TOKEN = "an-external-token"  # noqa: S105 # nosec B105 — an opaque test string


@pytest.fixture(autouse=True)
def _empty_registry():
    """The community edition registers nothing; restore that either way."""
    unregister_verifier(PROVIDER)
    assert not has_verifiers(), "a previous test leaked a verifier"
    yield
    unregister_verifier(PROVIDER)


class _Verifier:
    """A verifier that claims exactly one token, as the contract requires."""

    def __init__(self, accepts: str, identity: ExternalIdentity | None):
        self._accepts = accepts
        self._identity = identity
        self.calls: list[str] = []

    def verify(self, token: str, request: Request) -> ExternalIdentity | None:
        self.calls.append(token)
        return self._identity if token == self._accepts else None


def _identity(**overrides: Any) -> ExternalIdentity:
    unique = uuid_pkg.uuid4().hex[:10]
    defaults: dict[str, Any] = {
        "provider": PROVIDER,
        "external_id": f"ext_{unique}",
        "email": f"external-{unique}@example.com",
        "full_name": "External Person",
        "org_id": f"org_{unique}",
        "email_verified": True,
    }
    defaults.update(overrides)
    return ExternalIdentity(**defaults)


def _request() -> Request:
    """A real ``Request`` — ``request.state`` is part of this function's contract."""
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "server": ("backend", 8080),
            "root_path": "",
            "path": "/api/files",
            "query_string": b"",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("10.0.0.7", 40000),
        }
    )


class TestTheSeamIsOnlyConsultedWhenItIsWiredUp:
    def test_no_registered_verifier_means_no_external_path(self, db_session):
        """The community edition must pay nothing and decide nothing here."""
        assert _authenticate(_request(), TOKEN, db_session) is None

    def test_a_registered_verifier_is_offered_the_token(self, db_session):
        verifier = _Verifier(TOKEN, _identity())
        register_verifier(PROVIDER, verifier)

        user = _authenticate(_request(), TOKEN, db_session)

        assert user is not None
        assert verifier.calls == [TOKEN]

    def test_a_token_no_verifier_claims_falls_through(self, db_session):
        """``None`` means "not mine" — the local-JWT path must still get a turn."""
        register_verifier(PROVIDER, _Verifier("some-other-token", _identity()))

        assert _authenticate(_request(), TOKEN, db_session) is None

    def test_a_crashing_verifier_is_contained(self, db_session):
        """A broken cloud layer may not take local authentication down with it."""

        class _Crashing:
            def verify(self, token: str, request: Request) -> ExternalIdentity | None:
                raise RuntimeError("the managed IdP is down")

        register_verifier(PROVIDER, _Crashing())

        assert _authenticate(_request(), TOKEN, db_session) is None


class TestASuccessfulExternalAuthentication:
    @pytest.fixture
    def identity(self) -> ExternalIdentity:
        return _identity()

    @pytest.fixture
    def authenticated(self, db_session, identity) -> tuple[Request, User]:
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))
        request = _request()
        user = _authenticate(request, TOKEN, db_session)
        assert user is not None
        return request, user

    def test_the_identity_is_provisioned(self, authenticated, identity):
        _request_obj, user = authenticated

        assert user.external_id == identity.external_id
        assert user.email == identity.email
        assert bool(user.is_active) is True

    def test_the_verified_identity_is_stashed_for_org_scoping(self, authenticated, identity):
        """``get_current_context`` reads it rather than verifying the token twice."""
        request, _user = authenticated

        assert request.state.external_identity is identity

    def test_the_access_log_contract_is_stamped(self, authenticated, identity):
        """``request.state.user_id``/``org_id`` are what the observability layer
        reads off the request. Left unset, every external request is logged
        unattributed — invisible, because nothing raises."""
        request, user = authenticated

        assert request.state.user_id == user.id
        assert request.state.org_id == identity.org_id

    def test_an_identity_with_no_org_stamps_none_rather_than_omitting_the_field(self, db_session):
        identity = _identity(org_id=None)
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))
        request = _request()

        _authenticate(request, TOKEN, db_session)

        assert request.state.org_id is None


class TestARefusedLinkIsACleanRefusal:
    """``PermissionError`` is ``account_linking``'s refusal, not a fault."""

    @pytest.fixture
    def existing_local_account(self, db_session) -> User:
        user = User(
            email=f"victim-{uuid_pkg.uuid4().hex[:8]}@example.com",
            full_name="Local Person",
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

    def test_an_unverified_email_match_is_a_401(self, db_session, existing_local_account):
        """The real refusal, driven through the real ``sync_external_user_to_db``:
        an IdP that does not assert the address as verified may not take over an
        account that already exists at that address."""
        identity = _identity(email=str(existing_local_account.email), email_verified=False)
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))

        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.status_code == 401

    def test_the_refused_account_is_not_converted(self, db_session, existing_local_account):
        """A refusal that still rewrote the row would be the takeover it refused."""
        identity = _identity(email=str(existing_local_account.email), email_verified=False)
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))

        with pytest.raises(HTTPException):
            _authenticate(_request(), TOKEN, db_session)

        db_session.refresh(existing_local_account)
        assert str(existing_local_account.auth_type) == "local"
        assert existing_local_account.external_id is None

    def test_the_refusal_challenges_with_bearer(self, db_session, existing_local_account):
        """A 401 without ``WWW-Authenticate`` is not a challenge an API client can act on."""
        identity = _identity(email=str(existing_local_account.email), email_verified=False)
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))

        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.headers == {"WWW-Authenticate": "Bearer"}

    def test_the_reason_reaches_the_client(self, db_session, monkeypatch):
        """The refusal message is the caller's only clue, so it is passed through
        rather than replaced by the generic provisioning failure."""

        def _refuse(_db, _identity):
            raise PermissionError("External identity email is not verified")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _refuse)
        register_verifier(PROVIDER, _Verifier(TOKEN, _identity()))

        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.detail == "External identity email is not verified"


class TestAnyOtherSyncFailureIsContained:
    @pytest.fixture
    def broken_sync(self, monkeypatch) -> list[str]:
        """Make the JIT write fail the way a database error would."""
        calls: list[str] = []

        def _explode(_db, _identity):
            calls.append("sync")
            raise RuntimeError("could not write the user row")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _explode)
        register_verifier(PROVIDER, _Verifier(TOKEN, _identity()))
        return calls

    def test_it_is_a_401_not_a_500(self, db_session, broken_sync):
        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.status_code == 401
        assert broken_sync == ["sync"]

    def test_the_detail_does_not_leak_the_internal_error(self, db_session, broken_sync):
        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.detail == "Could not provision external identity"

    def test_the_session_is_rolled_back(self, db_session, monkeypatch):
        """A half-applied JIT write left in the request's session poisons every
        later query on it, and the resulting 500 surfaces nowhere near the cause."""
        rollbacks: list[int] = []
        monkeypatch.setattr(db_session, "rollback", lambda: rollbacks.append(1))

        def _explode(_db, _identity):
            raise RuntimeError("could not write the user row")

        monkeypatch.setattr(external_sync, "sync_external_user_to_db", _explode)
        register_verifier(PROVIDER, _Verifier(TOKEN, _identity()))

        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.status_code == 401
        assert rollbacks == [1]

    def test_a_successful_sync_rolls_nothing_back(self, db_session, monkeypatch):
        """Positive control: the rollback belongs to the failure branch only."""
        rollbacks: list[int] = []
        monkeypatch.setattr(db_session, "rollback", lambda: rollbacks.append(1))
        register_verifier(PROVIDER, _Verifier(TOKEN, _identity()))

        user = _authenticate(_request(), TOKEN, db_session)

        assert user is not None
        assert rollbacks == []


class TestADeactivatedExternalAccountIsRefusedAsDeactivated:
    def test_it_is_the_same_400_a_local_account_gets(self, db_session):
        """400, not 401: the credential verified, the account is unusable.

        The SPA and the API clients distinguish the two — a 401 tells a caller to
        re-authenticate, which for a deactivated account is an infinite loop.
        """
        identity = _identity()
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))
        user = external_sync.sync_external_user_to_db(db_session, identity)
        user.is_active = False
        db_session.commit()

        with pytest.raises(HTTPException) as exc:
            _authenticate(_request(), TOKEN, db_session)

        assert exc.value.status_code == 400
        assert exc.value.detail == "Inactive user"

    def test_an_active_account_is_returned(self, db_session):
        """Positive control for the line above."""
        identity = _identity()
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))
        external_sync.sync_external_user_to_db(db_session, identity)

        user = _authenticate(_request(), TOKEN, db_session)

        assert user is not None
        assert bool(user.is_active) is True

    def test_a_deactivated_account_is_not_reactivated_by_the_attempt(self, db_session):
        identity = _identity()
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))
        user = external_sync.sync_external_user_to_db(db_session, identity)
        user.is_active = False
        db_session.commit()

        with pytest.raises(HTTPException):
            _authenticate(_request(), TOKEN, db_session)

        db_session.refresh(user)
        assert bool(user.is_active) is False


class TestGetCurrentUserRoutesThroughTheSeam:
    """A seam nothing calls is not a seam."""

    def test_an_external_token_authenticates_without_a_local_jwt(self, db_session):
        identity = _identity()
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))
        request = _request()

        user = deps_module.get_current_user(request=request, token=TOKEN, db=db_session)

        assert user.external_id == identity.external_id
        assert request.state.user_id == user.id

    def test_the_seam_runs_before_local_validation(self, db_session):
        """``TOKEN`` is not a JWT at all: reaching the local path would 401."""
        register_verifier(PROVIDER, _Verifier(TOKEN, _identity()))

        user = deps_module.get_current_user(request=_request(), token=TOKEN, db=db_session)

        assert str(user.auth_type) == PROVIDER

    def test_a_refused_link_surfaces_from_the_dependency(self, db_session):
        existing = User(
            email=f"victim-{uuid_pkg.uuid4().hex[:8]}@example.com",
            full_name="Local Person",
            hashed_password=get_password_hash("irrelevant-Passphrase99!"),
            role="user",
            auth_type="local",
            is_active=True,
            is_superuser=False,
        )
        db_session.add(existing)
        db_session.commit()
        identity = _identity(email=str(existing.email), email_verified=False)
        register_verifier(PROVIDER, _Verifier(TOKEN, identity))

        with pytest.raises(HTTPException) as exc:
            deps_module.get_current_user(request=_request(), token=TOKEN, db=db_session)

        assert exc.value.status_code == 401


def test_the_provider_name_used_here_is_one_core_permits():
    """Guards the fixtures above: a provider outside the CHECK set would make
    every JIT write in this file fail on the constraint rather than on the rule
    under test."""
    assert PROVIDER in VALID_AUTH_TYPES
