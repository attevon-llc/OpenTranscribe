"""PKI HTTP endpoints exercised through the FastAPI TestClient (no RUN_PKI_TESTS gate).

Run with: pytest tests/pki_auth -v
"""

import pytest

from tests.fixtures.pki_auth import generate_test_certificate

# ===================================================================
# HTTP Endpoint Integration Tests (using FastAPI TestClient)
#
# These tests exercise the actual HTTP endpoints and do NOT require
# the RUN_PKI_TESTS=true environment variable. They use the TestClient
# and db_session fixtures from conftest.py, and the auth_config table
# in the test database to enable/disable PKI dynamically.
#
# They all take the `trusted_pki_peer` fixture below: header-sourced PKI
# authentication is refused unless a trusted proxy vouched for the headers,
# so a deployment with no PKI_TRUSTED_PROXIES authenticates nobody. These
# tests model a CORRECTLY configured deployment; the fail-closed behaviour
# when the fixture is absent is covered by TestPKIHeaderSourceFailClosed.
# ===================================================================


@pytest.fixture
def trusted_pki_peer(monkeypatch):
    """Model a deployment whose reverse proxy terminates mTLS and is allow-listed.

    TestClient's peer is the literal string ``"testclient"``, which no IP allowlist can
    ever match, so the allowlist lookup is patched for the duration of the test rather
    than the production trust rule being relaxed. ``app/auth/pki_auth.py`` therefore has
    exactly one rule with no test-only branch in it.

    Two independent call sites gate on that rule (``pki_authenticate``'s
    ``_validate_pki_headers_source``, hit first, via ``header_assertion_permitted``;
    and ``_extract_user_info_from_request``'s own ``_pki_header_source_is_trusted``),
    and each resolves ``header_source_is_trusted`` through its own module's import
    binding — patching one leaves the other running the real, untrusted-peer check.
    Both were introduced together (``_validate_pki_headers_source``, PKI trust
    hardening), but only the second was ever wired into this fixture, so this class's
    whole suite 401'd against a real deployment shape (every DN-header request has
    *some* PKI header, so ``asserted=True`` and the unmocked gate always refuses)
    until a real backend/frontend run against Authentik/LDAP surfaced it (#20/#14).
    """
    from app.auth import header_trust
    from app.auth import pki_auth

    monkeypatch.setattr(
        pki_auth, "_pki_header_source_is_trusted", lambda request, networks=None: True
    )
    monkeypatch.setattr(pki_auth, "header_source_is_trusted", lambda request, networks: True)
    monkeypatch.setattr(header_trust, "header_source_is_trusted", lambda request, networks: True)


@pytest.fixture
def pki_enabled_db(db_session):
    """Enable PKI authentication in the test database.

    Ensures auth_config rows for pki_enabled=true and a test admin DN
    exist. Uses merge (upsert) semantics to handle cases where the rows
    already exist in the database (e.g., from a previous migration or
    manual setup). The test DB session uses savepoints, so changes are
    rolled back after each test.
    """
    from app.models.auth_config import AuthConfig

    admin_dn_value = (
        "emailAddress=pki-admin-test@example.com,"
        "CN=Admin User,OU=Users,"
        "O=OpenTranscribe Admins,L=Arlington,"
        "ST=Virginia,C=US"
    )

    desired = {
        "pki_enabled": ("true", "pki", "bool"),
        "pki_admin_dns": (admin_dn_value, "pki", "string"),
    }

    for key, (value, category, dtype) in desired.items():
        existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == key).first()
        if existing:
            existing.config_value = value
        else:
            db_session.add(
                AuthConfig(
                    config_key=key,
                    config_value=value,
                    category=category,
                    data_type=dtype,
                    is_sensitive=False,
                )
            )
    db_session.commit()
    return db_session


@pytest.fixture
def pki_disabled_db(db_session):
    """Ensure PKI authentication is disabled in the test database.

    Sets pki_enabled=false (or creates the row if it does not exist).
    This is needed because the production database may already have
    pki_enabled=true, which would cause "PKI disabled" tests to fail.
    """
    from app.models.auth_config import AuthConfig

    existing = db_session.query(AuthConfig).filter(AuthConfig.config_key == "pki_enabled").first()
    if existing:
        existing.config_value = "false"
    else:
        db_session.add(
            AuthConfig(
                config_key="pki_enabled",
                config_value="false",
                category="pki",
                data_type="bool",
                is_sensitive=False,
            )
        )
    db_session.commit()
    return db_session


@pytest.fixture
def pki_test_cert():
    """Generate a valid test certificate for endpoint integration tests.

    Returns a tuple of (pem_string, subject_dn_string) for use in
    X-Client-Cert and X-Client-Cert-DN headers.
    """
    pem = generate_test_certificate(
        common_name="Test PKI User",
        email="testpki@example.com",
        organization="OpenTranscribe Test",
        organizational_unit="Engineering",
        days_valid=365,
        days_before=0,
    )
    subject_dn = (
        "emailAddress=testpki@example.com,"
        "CN=Test PKI User,OU=Engineering,"
        "O=OpenTranscribe Test,C=US"
    )
    return pem, subject_dn


@pytest.fixture
def pki_admin_cert():
    """Generate a test certificate whose DN matches the admin DN in pki_enabled_db.

    Returns a tuple of (pem_string, subject_dn_string).
    """
    pem = generate_test_certificate(
        common_name="Admin User",
        email="pki-admin-test@example.com",
        organization="OpenTranscribe Admins",
        organizational_unit="Users",
        days_valid=365,
        days_before=0,
    )
    subject_dn = (
        "emailAddress=pki-admin-test@example.com,"
        "CN=Admin User,OU=Users,"
        "O=OpenTranscribe Admins,L=Arlington,"
        "ST=Virginia,C=US"
    )
    return pem, subject_dn


class TestPKIEndpointIntegration:
    """Integration tests for the PKI authentication HTTP endpoint.

    These tests use the FastAPI TestClient (``client`` fixture) and a real
    test database session with savepoint isolation. PKI is toggled via
    rows in the ``auth_config`` table, matching production behaviour.
    """

    @pytest.fixture(autouse=True)
    def _trusted_peer(self, trusted_pki_peer):
        """Every test here models a deployment with a working mTLS reverse proxy.

        Without it the endpoint refuses every request, which is the point of
        TestPKIHeaderSourceFailClosed — not of these tests.
        """

    def test_pki_authenticate_with_dn_header(self, client, pki_enabled_db, pki_test_cert):
        """POST /api/auth/pki/authenticate with DN header returns 200 and access_token."""
        _pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_pki_authenticate_creates_user(self, client, pki_enabled_db, db_session, pki_test_cert):
        """First PKI login creates a user; second login reuses the same user."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        # First login — user should be created
        response1 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response1.status_code == 200

        # Verify user exists in DB
        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        user_id = user.id

        # Second login — same user should be reused
        response2 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response2.status_code == 200

        user_again = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user_again is not None
        assert user_again.id == user_id

    def test_pki_authenticate_admin_dn(self, client, pki_enabled_db, db_session, pki_admin_cert):
        """Admin DN gives admin or super_admin role with superuser flag."""
        from app.models.user import User

        _pem, subject_dn = pki_admin_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

        # An admin DN grants the 'admin' role. super_admin is local-only and is
        # never granted (nor demoted) by an external IdP; is_superuser mirrors
        # (role == super_admin), so a PKI admin has is_superuser False.
        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        assert user.role in ("admin", "super_admin")
        assert user.is_superuser is (user.role == "super_admin")

    def test_pki_authenticate_regular_user(self, client, pki_enabled_db, db_session, pki_test_cert):
        """Non-admin DN gives regular user role."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response.status_code == 200

        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        assert user.role == "user"
        assert user.is_superuser is False

    def test_pki_authenticate_disabled(self, client, pki_disabled_db):
        """PKI not enabled in DB returns 400 'PKI authentication is not enabled'."""
        response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert-DN": "CN=Test User,O=Test Org,C=US",
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "not enabled" in data["detail"].lower()

    def test_pki_authenticate_no_headers(self, client, pki_enabled_db):
        """No cert/DN headers returns 401."""
        response = client.post(
            "/api/auth/pki/authenticate",
            headers={},
        )

        assert response.status_code == 401
        data = response.json()
        assert "invalid" in data["detail"].lower() or "missing" in data["detail"].lower()

    def test_pki_authenticate_empty_dn(self, client, pki_enabled_db):
        """Empty DN header returns 401."""
        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": ""},
        )

        assert response.status_code == 401

    def test_pki_authenticate_with_full_cert(self, client, pki_enabled_db, pki_test_cert):
        """Full PEM certificate in X-Client-Cert header returns 200."""
        pem, _subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert": pem},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_pki_authenticate_inactive_user(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI user set to inactive returns 400 'Inactive user account'."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        # First login to create the user
        response1 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response1.status_code == 200

        # Deactivate the user
        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        user.is_active = False
        db_session.commit()

        # Second login should fail due to inactive account
        response2 = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )

        assert response2.status_code == 400
        data = response2.json()
        assert "inactive" in data["detail"].lower()

    def test_pki_authenticate_url_encoded_cert(self, client, pki_enabled_db, pki_test_cert):
        """URL-encoded PEM certificate (Nginx format) in X-Client-Cert header returns 200."""
        from urllib.parse import quote

        pem, _subject_dn = pki_test_cert
        encoded_pem = quote(pem)

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert": encoded_pem},
        )

        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        data = response.json()
        assert "access_token" in data

    def test_pki_authenticate_with_both_cert_and_dn(self, client, pki_enabled_db, pki_test_cert):
        """Both X-Client-Cert and X-Client-Cert-DN headers present returns 200."""
        pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert": pem,
                "X-Client-Cert-DN": subject_dn,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data

    def test_pki_authenticate_stores_certificate_metadata(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI login with full cert stores certificate metadata in user record."""
        from app.models.user import User

        pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert": pem,
                "X-Client-Cert-DN": subject_dn,
            },
        )
        assert response.status_code == 200

        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        assert user.pki_common_name == "Test PKI User"
        assert user.pki_organization == "OpenTranscribe Test"
        assert user.pki_organizational_unit == "Engineering"
        assert user.pki_serial_number is not None
        assert user.pki_fingerprint_sha256 is not None
        assert user.pki_not_before is not None
        assert user.pki_not_after is not None

    def test_pki_authenticate_dn_only_no_cert_metadata(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI login with DN only (no full cert) does not populate cert metadata."""
        from app.models.user import User

        _pem, subject_dn = pki_test_cert

        response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert response.status_code == 200

        user = db_session.query(User).filter(User.pki_subject_dn == subject_dn).first()
        assert user is not None
        # Without a full certificate, metadata fields should be None
        assert user.pki_serial_number is None
        assert user.pki_fingerprint_sha256 is None


class TestAuthMethodsEndpoint:
    """Integration tests for the GET /api/auth/methods endpoint.

    Verifies that the auth methods discovery endpoint correctly reflects
    PKI enable/disable state from the database.
    """

    def test_auth_methods_pki_enabled(self, client, pki_enabled_db):
        """With pki_enabled=true in DB, response has pki_enabled: true."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert data["pki_enabled"] is True

    def test_auth_methods_pki_disabled(self, client, pki_disabled_db):
        """pki_enabled=false in DB => pki_enabled: false."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert data["pki_enabled"] is False

    def test_auth_methods_includes_pki_in_methods_list(self, client, pki_enabled_db):
        """When PKI is enabled, 'pki' appears in the methods array."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert "pki" in data["methods"]

    def test_auth_methods_excludes_pki_when_disabled(self, client, pki_disabled_db):
        """When PKI is disabled, 'pki' does not appear in the methods array."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert "pki" not in data["methods"]

    def test_auth_methods_always_includes_local(self, client, pki_disabled_db):
        """The 'local' method is always present regardless of PKI state."""
        response = client.get("/api/auth/methods")

        assert response.status_code == 200
        data = response.json()
        assert "local" in data["methods"]


class TestPKICertificateInfoEndpoint:
    """Integration tests for the GET /api/auth/me/certificate endpoint.

    Verifies that authenticated users can retrieve their certificate
    metadata, and that non-PKI users get has_certificate: false.
    """

    @pytest.fixture(autouse=True)
    def _trusted_peer(self, trusted_pki_peer):
        """The PKI logins these tests perform need a vouching proxy; see the fixture."""

    def test_certificate_info_for_pki_user(self, client, pki_enabled_db, db_session, pki_test_cert):
        """PKI-authenticated user can see their certificate info."""
        pem, subject_dn = pki_test_cert

        # Authenticate via PKI to create user and get token
        auth_response = client.post(
            "/api/auth/pki/authenticate",
            headers={
                "X-Client-Cert": pem,
                "X-Client-Cert-DN": subject_dn,
            },
        )
        assert auth_response.status_code == 200
        access_token = auth_response.json()["access_token"]

        # Fetch certificate info
        cert_response = client.get(
            "/api/auth/me/certificate",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert cert_response.status_code == 200
        data = cert_response.json()
        assert data["has_certificate"] is True
        assert data["subject_dn"] is not None
        assert data["common_name"] == "Test PKI User"
        assert data["organization"] == "OpenTranscribe Test"
        assert data["organizational_unit"] == "Engineering"
        assert data["serial_number"] is not None
        assert data["fingerprint"] is not None
        # Fingerprint should be colon-separated hex
        assert ":" in data["fingerprint"]

    def test_certificate_info_for_local_user(self, client, normal_user, user_token_headers):
        """Local auth user gets has_certificate: false."""
        response = client.get(
            "/api/auth/me/certificate",
            headers=user_token_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["has_certificate"] is False

    def test_certificate_info_requires_auth(self, client):
        """Unauthenticated request to /me/certificate returns 401."""
        response = client.get("/api/auth/me/certificate")

        assert response.status_code == 401

    def test_certificate_info_for_dn_only_pki_user(
        self, client, pki_enabled_db, db_session, pki_test_cert
    ):
        """PKI user authenticated with DN-only still gets has_certificate: true (from DN)."""
        _pem, subject_dn = pki_test_cert

        # Authenticate with DN only (no full cert)
        auth_response = client.post(
            "/api/auth/pki/authenticate",
            headers={"X-Client-Cert-DN": subject_dn},
        )
        assert auth_response.status_code == 200
        access_token = auth_response.json()["access_token"]

        # Fetch certificate info — should have at least subject_dn from user record
        cert_response = client.get(
            "/api/auth/me/certificate",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        assert cert_response.status_code == 200
        data = cert_response.json()
        assert data["has_certificate"] is True
        assert data["subject_dn"] == subject_dn


class TestPKIEndpointThrottling:
    """POST /auth/pki/authenticate must be throttled like every other auth route.

    It mints access AND refresh tokens, so without a limit it was the cheapest
    brute-force surface in the app. No DB is needed: these assert the wiring.
    """

    def test_pki_route_registered_with_limiter(self):
        """The slowapi limiter has a registered limit for the pki_login route."""
        from app.api.endpoints.auth import pki as pki_endpoint  # noqa: F401 - importing registers the route with the limiter
        from app.api.rate_limit import limiter

        route_key = "app.api.endpoints.auth.pki.pki_login"
        assert route_key in limiter._route_limits or route_key in limiter._dynamic_route_limits

    def test_pki_route_source_has_limit_decorator(self):
        """The decorator idiom matches login.py / registration.py."""
        import inspect

        from app.api.endpoints.auth import pki as pki_endpoint

        source = inspect.getsource(pki_endpoint)
        assert "@limiter.limit(get_auth_rate_limit())" in source

    def test_pki_route_records_lockout_attempts(self):
        """Failed PKI attempts are recorded against the subject DN for lockout."""
        import inspect

        from app.api.endpoints.auth import pki as pki_endpoint

        source = inspect.getsource(pki_endpoint)
        assert "check_and_record_attempt(" in source
        assert "lockout_identifier" in source
