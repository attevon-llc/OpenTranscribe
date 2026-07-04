"""Tests for the cloud-edition seams (open-core extension points).

Covers the auth provider registry, generic external JIT user sync, the
get_current_user external-verifier branch, /auth/methods discovery, and the
new organization / usage_event models. The community edition registers no
verifiers, so every test restores the empty-registry state on exit.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi import Request

from app.auth.constants import CLOUD_SEAM_VERSION
from app.auth.constants import EXTERNAL_AUTH_NO_PASSWORD
from app.auth.constants import VALID_AUTH_TYPES
from app.auth.external_sync import sync_external_user_to_db
from app.auth.provider_registry import ExternalIdentity
from app.auth.provider_registry import get_registered_providers
from app.auth.provider_registry import has_verifiers
from app.auth.provider_registry import register_verifier
from app.auth.provider_registry import unregister_verifier
from app.auth.provider_registry import verify_external_token
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.usage_event import UsageEvent
from app.models.user import User

# Provider name used by the generic-registry seam tests. The registry + JIT sync
# are vendor-neutral and map ANY provider onto the generic external_id /
# external_org_id columns. We use a core-permitted auth_type here ("keycloak")
# because core's user.auth_type CHECK only allows the built-in types — the cloud
# edition widens that CHECK for its own managed-IdP provider in its own migration
# and owns the external-provider persistence tests for it.
EXTERNAL_PROVIDER = "keycloak"


class FakeVerifier:
    """Test double matching the TokenVerifier protocol."""

    def __init__(self, accept_token: str, identity: ExternalIdentity | None):
        self.accept_token = accept_token
        self.identity = identity
        self.calls = 0

    def verify(self, token: str, request: Request) -> ExternalIdentity | None:
        self.calls += 1
        return self.identity if token == self.accept_token else None


class CrashingVerifier:
    def verify(self, token: str, request: Request):
        raise RuntimeError("boom")


def _identity(**overrides: Any) -> ExternalIdentity:
    defaults: dict[str, Any] = {
        "provider": EXTERNAL_PROVIDER,
        "external_id": f"user_{uuid.uuid4().hex[:12]}",
        "email": f"seam-test-{uuid.uuid4().hex[:8]}@example.com",
        "full_name": "Seam Tester",
        "org_id": "org_abc123",
        "org_role": "org:member",
    }
    defaults.update(overrides)
    return ExternalIdentity(**defaults)


def _fake_request() -> Request:
    # get_current_user only touches request.state on the external path; a
    # lightweight stand-in keeps these tests free of ASGI plumbing.
    return SimpleNamespace(state=SimpleNamespace())  # type: ignore[return-value]


@pytest.fixture(autouse=True)
def clean_registry():
    """Every test starts and ends with an empty verifier registry."""
    unregister_verifier(EXTERNAL_PROVIDER)
    unregister_verifier("other")
    yield
    unregister_verifier(EXTERNAL_PROVIDER)
    unregister_verifier("other")


class TestConstants:
    def test_core_auth_types_are_vendor_neutral(self):
        # Core enumerates only its built-in auth types. Registry-based external
        # providers (cloud edition) are NOT enumerated here — they map onto the
        # generic external_id/external_org_id columns instead, and the cloud
        # layer widens the auth_type CHECK for its own provider.
        assert VALID_AUTH_TYPES == ["local", "ldap", "keycloak", "pki"]
        assert "clerk" not in VALID_AUTH_TYPES

    def test_seam_version_present(self):
        assert isinstance(CLOUD_SEAM_VERSION, int)
        assert CLOUD_SEAM_VERSION >= 1


class TestProviderRegistry:
    def test_empty_by_default(self):
        assert not has_verifiers()
        assert get_registered_providers() == []
        assert verify_external_token("anything", _fake_request()) is None

    def test_register_and_match(self):
        ident = _identity()
        verifier = FakeVerifier("good-token", ident)
        register_verifier(EXTERNAL_PROVIDER, verifier)

        assert has_verifiers()
        assert get_registered_providers() == [EXTERNAL_PROVIDER]
        assert verify_external_token("good-token", _fake_request()) is ident
        assert verify_external_token("bad-token", _fake_request()) is None

    def test_unregister(self):
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("t", _identity()))
        unregister_verifier(EXTERNAL_PROVIDER)
        assert not has_verifiers()

    def test_crashing_verifier_is_contained(self):
        """A broken cloud layer must never take down authentication."""
        ident = _identity()
        register_verifier("other", CrashingVerifier())
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("tok", ident))
        assert verify_external_token("tok", _fake_request()) is ident


class TestExternalSync:
    def test_creates_new_user(self, db_session):
        ident = _identity()
        user = sync_external_user_to_db(db_session, ident)

        assert user.external_id == ident.external_id
        assert user.email == ident.email
        assert user.auth_type == EXTERNAL_PROVIDER
        assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD
        assert user.external_org_id == "org_abc123"
        # org:member / org:admin are tenant capabilities, never platform roles
        assert user.role == "user"
        assert user.is_superuser is False

    def test_resync_updates_profile(self, db_session):
        ident = _identity()
        sync_external_user_to_db(db_session, ident)
        updated = ExternalIdentity(
            provider=ident.provider,
            external_id=ident.external_id,
            email=ident.email,
            full_name="Renamed Person",
            org_id="org_new",
            org_role="org:admin",
        )
        user = sync_external_user_to_db(db_session, updated)
        assert user.full_name == "Renamed Person"
        assert user.external_org_id == "org_new"
        # Still not a platform admin even as org:admin
        assert user.role == "user"

    def test_converts_local_user_by_verified_email(self, db_session):
        email = f"seam-local-{uuid.uuid4().hex[:8]}@example.com"
        local = User(
            email=email,
            full_name="Local Person",
            hashed_password="$2b$12$fakehashfakehashfakehash",
            auth_type="local",
            is_active=True,
        )
        db_session.add(local)
        db_session.commit()

        # Email-match conversion requires the IdP to assert the address verified.
        ident = _identity(email=email, email_verified=True)
        user = sync_external_user_to_db(db_session, ident)

        assert user.id == local.id
        assert user.external_id == ident.external_id
        assert user.auth_type == EXTERNAL_PROVIDER
        assert user.hashed_password == EXTERNAL_AUTH_NO_PASSWORD  # one-way conversion

    def test_unverified_email_match_is_refused(self, db_session):
        """Anyone can register a victim's address at a self-serve IdP; without a
        verified-email assertion the email match must NOT link/convert the
        existing local account (account-takeover vector)."""
        email = f"seam-victim-{uuid.uuid4().hex[:8]}@example.com"
        local = User(
            email=email,
            full_name="Victim",
            hashed_password="$2b$12$fakehashfakehashfakehash",
            auth_type="local",
            is_active=True,
        )
        db_session.add(local)
        db_session.commit()

        ident = _identity(email=email)  # email_verified defaults to False
        with pytest.raises(PermissionError):
            sync_external_user_to_db(db_session, ident)

        db_session.refresh(local)
        assert local.auth_type == "local"
        assert local.hashed_password != EXTERNAL_AUTH_NO_PASSWORD
        assert local.external_id is None

    def test_super_admin_never_linked_by_email(self, db_session):
        """Platform-owner accounts are never JIT-linked by email match, even
        with a verified address — linking them is a deliberate local action."""
        email = f"seam-owner-{uuid.uuid4().hex[:8]}@example.com"
        owner = User(
            email=email,
            full_name="Owner",
            hashed_password="$2b$12$fakehashfakehashfakehash",
            auth_type="local",
            role="super_admin",
            is_superuser=True,
            is_active=True,
        )
        db_session.add(owner)
        db_session.commit()

        ident = _identity(email=email, email_verified=True)
        with pytest.raises(PermissionError):
            sync_external_user_to_db(db_session, ident)

        db_session.refresh(owner)
        assert owner.auth_type == "local"
        assert owner.role == "super_admin"

    def test_external_id_match_needs_no_email_verification(self, db_session):
        """An already-linked identity (external_id match) re-syncs regardless of
        the email_verified flag — the link was established previously."""
        ident = _identity(email_verified=True)
        created = sync_external_user_to_db(db_session, ident)

        resync = _identity(
            external_id=ident.external_id,
            email=ident.email,
            full_name="Renamed",
            email_verified=False,
        )
        user = sync_external_user_to_db(db_session, resync)
        assert user.id == created.id
        assert user.full_name == "Renamed"

    def test_platform_admin_only_when_flagged(self, db_session):
        user = sync_external_user_to_db(db_session, _identity(is_admin=True))
        # External IdPs grant at most 'admin'; super_admin is local-only and
        # is_superuser mirrors (role == super_admin), so it stays False.
        assert user.role == "admin"
        assert user.is_superuser is False

    def test_provider_agnostic_generic_column_mapping(self, db_session):
        # Core is vendor-neutral: every registry provider maps onto the same
        # generic external_id/external_org_id columns — there is no per-provider
        # column-mapping table to maintain. (The auth_type itself is gated by
        # core's CHECK; the cloud layer widens it for its own provider.)
        from app.auth.external_sync import EXTERNAL_ID_COLUMN
        from app.auth.external_sync import EXTERNAL_ORG_ID_COLUMN

        assert EXTERNAL_ID_COLUMN == "external_id"
        assert EXTERNAL_ORG_ID_COLUMN == "external_org_id"

        ident = _identity()
        user = sync_external_user_to_db(db_session, ident)
        assert getattr(user, EXTERNAL_ID_COLUMN) == ident.external_id
        assert getattr(user, EXTERNAL_ORG_ID_COLUMN) == ident.org_id


class TestGetCurrentUserExternalBranch:
    def test_external_token_returns_jit_user(self, db_session):
        from app.api.endpoints.auth import get_current_user

        ident = _identity()
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("ext-token", ident))
        request = _fake_request()

        user = get_current_user(request=request, token="ext-token", db=db_session)

        assert user.external_id == ident.external_id
        assert request.state.external_identity is ident

    def test_non_matching_token_falls_through_to_local(self, db_session):
        from app.api.endpoints.auth import get_current_user

        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("only-this", _identity()))

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_fake_request(), token="not-a-valid-jwt", db=db_session)
        assert exc.value.status_code == 401

    def test_inactive_external_user_rejected(self, db_session):
        from app.api.endpoints.auth import get_current_user

        ident = _identity()
        user = sync_external_user_to_db(db_session, ident)
        user.is_active = False
        db_session.commit()
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("tok", ident))

        with pytest.raises(HTTPException) as exc:
            get_current_user(request=_fake_request(), token="tok", db=db_session)
        assert exc.value.status_code == 400


class TestGetOptionalCurrentUserExternalBranch:
    """get_optional_current_user mirrors the external-verifier branch but keeps
    optional semantics: it returns the user on a valid external token, None
    otherwise (never raises)."""

    def _request_with_token(self, token: str) -> Request:
        # get_optional_current_user reads the token from the Authorization
        # header / cookie and touches request.state on the external path.
        return SimpleNamespace(  # type: ignore[return-value]
            headers={"Authorization": f"Bearer {token}"},
            cookies={},
            state=SimpleNamespace(),
        )

    def test_external_token_returns_jit_user_and_stashes_identity(self, db_session):
        from app.api.endpoints.auth import get_optional_current_user

        ident = _identity()
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("ext-token", ident))
        request = self._request_with_token("ext-token")

        user = get_optional_current_user(request=request, db=db_session)

        assert user is not None
        assert user.external_id == ident.external_id
        # Identity MUST be stashed so resolve_org_context()/thumbnail org
        # resolution works for external users on optional-auth routes.
        assert request.state.external_identity is ident
        assert request.state.org_id == ident.org_id

    def test_no_token_returns_none(self, db_session):
        from app.api.endpoints.auth import get_optional_current_user

        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("ext-token", _identity()))
        request = SimpleNamespace(headers={}, cookies={}, state=SimpleNamespace())

        assert get_optional_current_user(request=request, db=db_session) is None  # type: ignore[arg-type]

    def test_non_matching_token_falls_through_to_none(self, db_session):
        from app.api.endpoints.auth import get_optional_current_user

        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("only-this", _identity()))
        request = self._request_with_token("not-a-valid-jwt")

        # No external match and not a valid local JWT -> None (never raises).
        assert get_optional_current_user(request=request, db=db_session) is None

    def test_inactive_external_user_returns_none(self, db_session):
        from app.api.endpoints.auth import get_optional_current_user

        ident = _identity()
        user = sync_external_user_to_db(db_session, ident)
        user.is_active = False
        db_session.commit()
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("tok", ident))
        request = self._request_with_token("tok")

        # Optional semantics: inactive user returns None rather than raising.
        assert get_optional_current_user(request=request, db=db_session) is None


class TestWebSocketAuthExternalBranch:
    """_try_authenticate_token authenticates a WS socket with an external token
    via the registered verifier; the community path stays local-JWT only."""

    def test_external_token_authenticates_socket(self, db_session):
        from app.api.websockets import _try_authenticate_token

        ident = _identity()
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("ws-ext-token", ident))

        user = _try_authenticate_token("ws-ext-token", db_session, websocket=None)

        assert user is not None
        assert user.external_id == ident.external_id

    def test_non_matching_token_returns_none(self, db_session):
        from app.api.websockets import _try_authenticate_token

        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("only-this", _identity()))

        # Not an external match and not a valid local JWT -> None (caller closes 4003).
        assert _try_authenticate_token("garbage-token", db_session, websocket=None) is None

    def test_inactive_external_user_returns_none(self, db_session):
        from app.api.websockets import _try_authenticate_token

        ident = _identity()
        user = sync_external_user_to_db(db_session, ident)
        user.is_active = False
        db_session.commit()
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("tok", ident))

        assert _try_authenticate_token("tok", db_session, websocket=None) is None

    def test_community_path_no_verifier_local_only(self, db_session):
        from app.api.websockets import _try_authenticate_token

        # No verifier registered (community edition): an external-looking token
        # that isn't a valid local JWT yields None.
        assert not has_verifiers()
        assert _try_authenticate_token("ws-ext-token", db_session, websocket=None) is None


class TestAuthMethodsDiscovery:
    def test_external_providers_empty_by_default(self, client):
        body = client.get("/api/auth/methods").json()
        assert body["external_providers"] == []
        assert EXTERNAL_PROVIDER not in body["methods"]
        # Reported generically — no per-vendor flag key.
        assert "clerk_enabled" not in body

    def test_external_provider_reported_when_registered(self, client):
        register_verifier(EXTERNAL_PROVIDER, FakeVerifier("t", _identity()))
        body = client.get("/api/auth/methods").json()
        assert body["external_providers"] == [EXTERNAL_PROVIDER]
        assert EXTERNAL_PROVIDER in body["methods"]


class TestOrganizationModels:
    def test_org_membership_and_usage_event_roundtrip(self, db_session):
        org = Organization(external_org_id=f"org_{uuid.uuid4().hex[:10]}", name="Acme Inc")
        db_session.add(org)
        db_session.commit()
        assert org.id is not None
        assert org.is_active is True

        user = sync_external_user_to_db(db_session, _identity())
        member = OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:admin")
        db_session.add(member)
        db_session.commit()
        assert org.memberships[0].user.id == user.id
        assert user.org_memberships[0].role == "org:admin"

    def test_usage_event_idempotency_key_unique(self, db_session):
        from sqlalchemy.exc import IntegrityError

        key = f"file:{uuid.uuid4().hex[:8]}:run1"
        db_session.add(
            UsageEvent(event_type="transcription.hours", quantity=1.5, idempotency_key=key)
        )
        db_session.commit()

        db_session.add(
            UsageEvent(event_type="transcription.hours", quantity=1.5, idempotency_key=key)
        )
        with pytest.raises(IntegrityError):
            db_session.commit()
        db_session.rollback()
