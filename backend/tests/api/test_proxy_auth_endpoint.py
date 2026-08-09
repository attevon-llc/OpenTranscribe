"""``POST /api/auth/proxy/authenticate`` and the per-request consistency check.

The unit suite (``tests/unit/test_proxy_header_auth.py``) covers the trust rules in
isolation. This one exercises them through the real router, because two of the
feature's properties only exist at that level:

* the endpoint mints an **ordinary session** — a ``refresh_token`` row — so every
  existing session control applies to a proxy login with no special case;
* a session that no longer matches the asserted identity is **revoked**, not just
  refused, which is the bug Open WebUI had to retrofit after #14406.

Starlette's ``TestClient`` reports a peer of ``"testclient"``, which is not an
address and is therefore untrusted by construction — correct, and exactly what makes
the negative cases below real. The positive cases build their own client with a
routable peer address.
"""

# mypy: disable-error-code="arg-type"
# Payloads and ORM rows are handed to signatures typed for the real schemas;
# declared once here rather than as a cast at every call site.
from __future__ import annotations

import uuid as uuid_pkg

import pytest
from fastapi.testclient import TestClient

from app.core.security import get_password_hash
from app.db.base import get_db
from app.main import app
from app.models.user import User
from app.services.auth_config_service import AuthConfigService

pytestmark = pytest.mark.xdist_group("proxy_auth")

ENDPOINT = "/api/auth/proxy/authenticate"
PROXY_PEER = "10.0.0.7"
EMAIL_HEADER = "X-Forwarded-Email"


@pytest.fixture
def proxy_enabled(db_session, super_admin_user):
    """Turn the feature on in the DB, as the admin UI would."""
    AuthConfigService.bulk_update_category(
        db_session,
        "proxy",
        {
            "proxy_enabled": True,
            "proxy_trusted_proxies": "10.0.0.0/8",
            "proxy_email_header": EMAIL_HEADER,
        },
        user_id=int(super_admin_user.id),
    )
    yield


@pytest.fixture
def proxy_client(db_session):
    """A TestClient whose socket peer is inside the configured allowlist."""

    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, client=(PROXY_PEER, 40000)) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


class TestTheEndpointRefusesWhenItShould:
    def test_disabled_by_default(self, client):
        """No configuration, no feature: a 400, not a session."""
        response = client.post(ENDPOINT, headers={EMAIL_HEADER: "ada@example.com"})
        assert response.status_code == 400

    def test_an_untrusted_peer_is_refused(self, client, proxy_enabled):
        """TestClient's peer is 'testclient' — not in 10.0.0.0/8, and not an IP."""
        response = client.post(ENDPOINT, headers={EMAIL_HEADER: "ada@example.com"})
        assert response.status_code == 401
        assert "access_token" not in response.json()

    def test_no_header_from_a_trusted_peer_is_still_a_refusal(self, proxy_client, proxy_enabled):
        """Nothing asserted is not a login; it is just nothing."""
        assert proxy_client.post(ENDPOINT).status_code == 401


class TestASuccessfulProxyLogin:
    def test_it_provisions_and_mints_a_real_session(self, proxy_client, db_session, proxy_enabled):
        email = f"proxy-{uuid_pkg.uuid4().hex[:8]}@example.com"

        response = proxy_client.post(
            ENDPOINT, headers={EMAIL_HEADER: email, "X-Forwarded-User": "Ada Lovelace"}
        )

        assert response.status_code == 200, response.text
        assert response.json()["access_token"]

        created = db_session.query(User).filter(User.email == email).first()
        assert created is not None
        assert str(created.auth_type) == "proxy"
        assert str(created.role) == "user"
        assert bool(created.is_superuser) is False

        # A session IS a refresh_token row — that is what makes idle/absolute
        # timeouts, the concurrent cap and the sessions UI apply here unchanged.
        from app.models.refresh_token import RefreshToken

        sessions = db_session.query(RefreshToken).filter(RefreshToken.user_id == created.id).count()
        assert sessions == 1

    def test_a_super_admin_account_is_never_taken_over(
        self, proxy_client, super_admin_user, proxy_enabled
    ):
        """The one unconditional rule in ``auth/account_linking.py``."""
        response = proxy_client.post(ENDPOINT, headers={EMAIL_HEADER: str(super_admin_user.email)})
        assert response.status_code == 401

    def test_jit_off_refuses_an_unknown_identity(
        self, proxy_client, db_session, super_admin_user, proxy_enabled
    ):
        AuthConfigService.bulk_update_category(
            db_session,
            "proxy",
            {"proxy_jit_provisioning": False},
            user_id=int(super_admin_user.id),
        )
        email = f"unknown-{uuid_pkg.uuid4().hex[:8]}@example.com"

        response = proxy_client.post(ENDPOINT, headers={EMAIL_HEADER: email})

        assert response.status_code == 401
        assert db_session.query(User).filter(User.email == email).first() is None

    def test_an_unadmitted_domain_is_refused(
        self, proxy_client, db_session, super_admin_user, proxy_enabled
    ):
        AuthConfigService.bulk_update_category(
            db_session,
            "proxy",
            {"proxy_allowed_domains": "corp.example"},
            user_id=int(super_admin_user.id),
        )
        response = proxy_client.post(
            ENDPOINT, headers={EMAIL_HEADER: f"nope-{uuid_pkg.uuid4().hex[:6]}@other.test"}
        )
        assert response.status_code == 401


class TestTheRoleHeaderIsCapped:
    def test_super_admin_in_the_header_grants_nothing(
        self, proxy_client, db_session, super_admin_user, proxy_enabled
    ):
        AuthConfigService.bulk_update_category(
            db_session,
            "proxy",
            {"proxy_role_header": "X-Forwarded-Role"},
            user_id=int(super_admin_user.id),
        )
        email = f"escalate-{uuid_pkg.uuid4().hex[:8]}@example.com"

        response = proxy_client.post(
            ENDPOINT, headers={EMAIL_HEADER: email, "X-Forwarded-Role": "super_admin"}
        )

        assert response.status_code == 200
        created = db_session.query(User).filter(User.email == email).first()
        assert str(created.role) == "user"
        assert bool(created.is_superuser) is False

    def test_admin_in_the_header_is_honoured(
        self, proxy_client, db_session, super_admin_user, proxy_enabled
    ):
        AuthConfigService.bulk_update_category(
            db_session,
            "proxy",
            {"proxy_role_header": "X-Forwarded-Role"},
            user_id=int(super_admin_user.id),
        )
        email = f"promote-{uuid_pkg.uuid4().hex[:8]}@example.com"

        response = proxy_client.post(
            ENDPOINT, headers={EMAIL_HEADER: email, "X-Forwarded-Role": "admin"}
        )

        assert response.status_code == 200
        created = db_session.query(User).filter(User.email == email).first()
        assert str(created.role) == "admin"
        assert bool(created.is_superuser) is False


class TestPerRequestConsistency:
    """The #14406 fix, written up front rather than retrofitted."""

    @pytest.fixture
    def proxy_user(self, db_session) -> User:
        user = User(
            email=f"consistent-{uuid_pkg.uuid4().hex[:8]}@example.com",
            full_name="Proxy Person",
            hashed_password=get_password_hash("irrelevant-Passphrase99!"),
            role="user",
            auth_type="proxy",
            is_active=True,
            is_superuser=False,
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    def _session_headers(self, proxy_client, proxy_user) -> dict[str, str]:
        response = proxy_client.post(ENDPOINT, headers={EMAIL_HEADER: str(proxy_user.email)})
        assert response.status_code == 200, response.text
        return {"Authorization": f"Bearer {response.json()['access_token']}"}

    # /api/users/me, not /api/auth/me: the consistency check lives in
    # get_current_active_user alongside the other account-lifecycle gates, and
    # /api/auth/me deliberately depends on the narrower get_current_user.
    def test_a_matching_assertion_passes_through(self, proxy_client, proxy_user, proxy_enabled):
        headers = self._session_headers(proxy_client, proxy_user)
        response = proxy_client.get(
            "/api/users/me", headers={**headers, EMAIL_HEADER: str(proxy_user.email)}
        )
        assert response.status_code == 200

    def test_an_absent_header_is_not_treated_as_a_mismatch(
        self, proxy_client, proxy_user, proxy_enabled
    ):
        """A request that did not traverse the proxy asserts nothing."""
        headers = self._session_headers(proxy_client, proxy_user)
        assert proxy_client.get("/api/users/me", headers=headers).status_code == 200

    def test_a_different_identity_revokes_the_session(
        self, proxy_client, db_session, proxy_user, proxy_enabled
    ):
        headers = self._session_headers(proxy_client, proxy_user)

        response = proxy_client.get(
            "/api/users/me", headers={**headers, EMAIL_HEADER: "someone.else@example.com"}
        )

        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "proxy_identity_mismatch"

        from app.models.refresh_token import RefreshToken

        live = (
            db_session.query(RefreshToken)
            .filter(RefreshToken.user_id == proxy_user.id, RefreshToken.revoked_at.is_(None))
            .count()
        )
        assert live == 0, "a mismatch must revoke, not merely refuse"

    def test_an_untrusted_peer_cannot_log_anyone_out(
        self, client, proxy_client, proxy_user, proxy_enabled
    ):
        """Otherwise the control is a denial of service anyone can trigger."""
        headers = self._session_headers(proxy_client, proxy_user)

        response = client.get(
            "/api/users/me", headers={**headers, EMAIL_HEADER: "someone.else@example.com"}
        )
        assert response.status_code == 200
