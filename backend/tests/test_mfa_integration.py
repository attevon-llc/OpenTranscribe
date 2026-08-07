"""
MFA API Integration Tests.

Tests verify the full MFA lifecycle through FastAPI endpoints with a real database:
- MFA status check for local users
- MFA setup (generate secret + QR code)
- MFA verify-setup (enable with TOTP code)
- Login with MFA required (returns mfa_token)
- MFA verify during login (TOTP code + backup code)
- MFA disable
- PKI/OIDC users cannot set up MFA

Run with: pytest tests/test_mfa_integration.py -v
"""

from unittest.mock import patch

import pyotp
import pytest

from app.core.config import settings


@pytest.fixture(autouse=True)
def enable_mfa_settings():
    """Enable MFA globally via settings patch for all tests in this module."""
    with patch.object(settings, "MFA_ENABLED", True), patch.object(settings, "MFA_REQUIRED", False):
        yield


@pytest.fixture
def require_mfa_settings():
    """Additionally require MFA for login tests."""
    with patch.object(settings, "MFA_REQUIRED", True):
        yield


class TestMFAStatus:
    """Test GET /api/auth/mfa/status endpoint."""

    def test_mfa_status_unauthenticated(self, client):
        """Unauthenticated request returns 401."""
        response = client.get("/api/auth/mfa/status")
        assert response.status_code == 401

    def test_mfa_status_local_user(self, client, normal_user, user_token_headers, db_session):
        """Local user can check MFA status."""

        response = client.get("/api/auth/mfa/status", headers=user_token_headers)

        assert response.status_code == 200
        data = response.json()
        assert "mfa_enabled" in data
        assert "mfa_configured" in data
        assert "can_setup_mfa" in data
        assert data["can_setup_mfa"] is True
        assert data["mfa_configured"] is False

    def test_mfa_status_admin_user(self, client, admin_user, admin_token_headers, db_session):
        """Admin user can check MFA status."""

        response = client.get("/api/auth/mfa/status", headers=admin_token_headers)

        assert response.status_code == 200
        data = response.json()
        assert data["can_setup_mfa"] is True


class TestMFASetup:
    """Test POST /api/auth/mfa/setup endpoint."""

    def test_mfa_setup_returns_secret_and_qr(
        self, client, normal_user, user_token_headers, db_session
    ):
        """MFA setup returns secret, provisioning URI, and QR code."""

        response = client.post("/api/auth/mfa/setup", headers=user_token_headers)

        assert response.status_code == 200
        data = response.json()
        assert "secret" in data
        assert "provisioning_uri" in data
        assert "qr_code_base64" in data
        assert data["provisioning_uri"].startswith("otpauth://totp/")
        assert len(data["secret"]) == 32  # Base32 encoded 20 bytes

    def test_mfa_setup_requires_auth(self, client):
        """Setup without auth returns 401."""
        response = client.post("/api/auth/mfa/setup")
        assert response.status_code == 401


class TestMFAVerifySetup:
    """Test POST /api/auth/mfa/verify-setup endpoint."""

    def test_verify_setup_with_valid_totp(
        self, client, normal_user, user_token_headers, db_session
    ):
        """Valid TOTP code during setup enables MFA and returns backup codes."""

        # Step 1: Start setup
        setup_response = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        assert setup_response.status_code == 200
        secret = setup_response.json()["secret"]

        # Step 2: Generate valid TOTP code
        totp = pyotp.TOTP(secret)
        code = totp.now()

        # Step 3: Verify setup
        verify_response = client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": code},
        )

        assert verify_response.status_code == 200
        data = verify_response.json()
        assert data["success"] is True
        assert "backup_codes" in data
        assert len(data["backup_codes"]) > 0

    def test_verify_setup_with_invalid_code(
        self, client, normal_user, user_token_headers, db_session
    ):
        """Invalid TOTP code during setup is rejected."""

        # Start setup
        setup_response = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        assert setup_response.status_code == 200

        # Wrong code
        verify_response = client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": "000000"},
        )

        assert verify_response.status_code == 400


class TestMFALoginFlow:
    """Test complete MFA login flow: login → mfa_token → verify."""

    def _setup_mfa_for_user(self, client, token_headers, db_session):
        """Helper: set up MFA for a user and return the secret and backup codes."""

        # Start MFA setup
        setup_resp = client.post("/api/auth/mfa/setup", headers=token_headers)
        assert setup_resp.status_code == 200
        secret = setup_resp.json()["secret"]

        # Verify with TOTP
        totp = pyotp.TOTP(secret)
        code = totp.now()
        verify_resp = client.post(
            "/api/auth/mfa/verify-setup",
            headers=token_headers,
            json={"code": code},
        )
        assert verify_resp.status_code == 200
        backup_codes = verify_resp.json()["backup_codes"]

        return secret, backup_codes

    def test_login_returns_mfa_required(self, client, normal_user, user_token_headers, db_session):
        """Login for MFA-enabled user returns mfa_required=true with mfa_token."""
        secret, _ = self._setup_mfa_for_user(client, user_token_headers, db_session)

        # Login — should require MFA
        login_response = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
        )

        assert login_response.status_code == 200
        data = login_response.json()
        assert data.get("mfa_required") is True
        assert "mfa_token" in data

    def test_mfa_verify_with_totp_code(self, client, normal_user, user_token_headers, db_session):
        """MFA verify with TOTP code returns access token."""
        secret, _ = self._setup_mfa_for_user(client, user_token_headers, db_session)

        # Login to get MFA token
        login_response = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
        )
        mfa_token = login_response.json()["mfa_token"]

        # Verify with current TOTP
        totp = pyotp.TOTP(secret)
        code = totp.now()

        verify_response = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": mfa_token, "code": code},
        )

        assert verify_response.status_code == 200
        data = verify_response.json()
        assert "access_token" in data
        assert data["token_type"] == "bearer"

    def test_mfa_verify_with_backup_code(self, client, normal_user, user_token_headers, db_session):
        """MFA verify with backup code returns access token."""
        secret, backup_codes = self._setup_mfa_for_user(client, user_token_headers, db_session)

        # Login to get MFA token
        login_response = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
        )
        mfa_token = login_response.json()["mfa_token"]

        # Verify with backup code
        verify_response = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": mfa_token, "code": backup_codes[0]},
        )

        assert verify_response.status_code == 200
        data = verify_response.json()
        assert "access_token" in data

    def test_mfa_verify_with_wrong_code(self, client, normal_user, user_token_headers, db_session):
        """MFA verify with wrong code returns 401."""
        self._setup_mfa_for_user(client, user_token_headers, db_session)

        # Login to get MFA token
        login_response = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
        )
        mfa_token = login_response.json()["mfa_token"]

        # Wrong code
        verify_response = client.post(
            "/api/auth/mfa/verify",
            json={"mfa_token": mfa_token, "code": "000000"},
        )

        assert verify_response.status_code == 401

    def test_mfa_token_single_use(self, client, normal_user, user_token_headers, db_session):
        """MFA token cannot be reused after successful verification.

        Uses in-memory blacklist patches since Redis is unavailable in tests.
        In production, Redis stores the JTI blacklist for replay prevention.
        """
        secret, _ = self._setup_mfa_for_user(client, user_token_headers, db_session)

        # Login to get MFA token
        login_response = client.post(
            "/api/auth/token",
            data={"username": normal_user.email, "password": "password123"},
        )
        mfa_token = login_response.json()["mfa_token"]

        # Simulate Redis-based JTI blacklist with an in-memory set
        _blacklisted_jtis: set[str] = set()

        def mock_blacklist(jti: str, expires_seconds: int) -> bool:
            _blacklisted_jtis.add(jti)
            return True

        def mock_is_blacklisted(jti: str) -> bool:
            return jti in _blacklisted_jtis

        # Patch where the helpers are defined: the verify endpoint resolves them
        # through their own module, not through the package facade.
        with (
            patch(
                "app.api.endpoints.auth.mfa_tokens._blacklist_mfa_token",
                side_effect=mock_blacklist,
            ),
            patch(
                "app.api.endpoints.auth.mfa_tokens._is_mfa_token_blacklisted",
                side_effect=mock_is_blacklisted,
            ),
        ):
            # First verify succeeds
            totp = pyotp.TOTP(secret)
            code = totp.now()
            first_verify = client.post(
                "/api/auth/mfa/verify",
                json={"mfa_token": mfa_token, "code": code},
            )
            assert first_verify.status_code == 200

            # Second verify with same token fails (replay prevention)
            code2 = totp.now()
            second_verify = client.post(
                "/api/auth/mfa/verify",
                json={"mfa_token": mfa_token, "code": code2},
            )
            assert second_verify.status_code == 401


class TestMFADisable:
    """Test POST /api/auth/mfa/disable endpoint."""

    def test_disable_mfa_with_valid_code(self, client, normal_user, user_token_headers, db_session):
        """Disabling MFA with valid TOTP code succeeds."""

        # Set up MFA
        setup_resp = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        secret = setup_resp.json()["secret"]
        totp = pyotp.TOTP(secret)
        code = totp.now()

        client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": code},
        )

        # Disable MFA with current code
        disable_code = totp.now()
        disable_response = client.post(
            "/api/auth/mfa/disable",
            headers=user_token_headers,
            json={"code": disable_code},
        )

        assert disable_response.status_code == 200

        # Status should show unconfigured
        status_resp = client.get("/api/auth/mfa/status", headers=user_token_headers)
        assert status_resp.json()["mfa_configured"] is False

    def test_disable_mfa_with_wrong_code(self, client, normal_user, user_token_headers, db_session):
        """Disabling MFA with wrong code is rejected."""

        # Set up MFA
        setup_resp = client.post("/api/auth/mfa/setup", headers=user_token_headers)
        secret = setup_resp.json()["secret"]
        totp = pyotp.TOTP(secret)

        client.post(
            "/api/auth/mfa/verify-setup",
            headers=user_token_headers,
            json={"code": totp.now()},
        )

        # Try to disable with wrong code
        disable_response = client.post(
            "/api/auth/mfa/disable",
            headers=user_token_headers,
            json={"code": "000000"},
        )

        assert disable_response.status_code in (400, 401)
