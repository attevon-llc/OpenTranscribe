"""
Tests for admin endpoint security.

Tests verify:
- Password reset uses request body (not query params) - security fix
- Super admin access to admin endpoints
- Account lock/unlock functionality
- Session termination
- Role-based access control

Security tests ensure sensitive data is not exposed in URLs/logs.

NOTE: These tests are for advanced admin security features planned in the FedRAMP
compliance plan. They are currently skipped until the features are fully implemented.
"""

import os
import uuid
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from unittest.mock import patch

import pytest

# Skip all tests - advanced admin security features in development
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_ADVANCED_ADMIN_TESTS", "false").lower() != "true",
    reason="Advanced admin security features in development (set RUN_ADVANCED_ADMIN_TESTS=true to run)",
)

from app.core.security import get_password_hash
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.user import AdminPasswordResetRequest


class TestPasswordResetSecurity:
    """Test that password reset uses request body for security."""

    def test_password_reset_accepts_body_params(
        self, client, db_session, super_admin_token_headers
    ):
        """The endpoint accepts the new password in the request body, not the query string.

        Uses ``super_admin_token_headers`` deliberately. This asserted
        ``status_code in [200, 403]`` with a plain-admin token, which could only ever be 403 —
        ``test_regular_admin_cannot_reset_password`` below pins that — so it never once
        exercised the body-parsing path it exists to cover, and it duplicated that test
        instead (issue #431). Only a super_admin gets far enough for the body to matter.
        """
        # Create a target user to reset
        target_user = User(
            email="target_reset@example.com",
            full_name="Target Reset User",
            hashed_password=get_password_hash("oldpassword123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        # Make request with password in body (secure method)
        response = client.post(
            f"/api/admin/users/{target_user.uuid}/reset-password",
            headers=super_admin_token_headers,
            json={"new_password": "NewSecurePassword123!", "force_change": True},
        )

        assert response.status_code == 200, response.text
        assert response.json()["success"] is True

    def test_password_reset_rejects_query_params(
        self, client, db_session, super_admin_token_headers
    ):
        """Test that password reset does NOT accept password in query params."""
        # Create a target user
        target_user = User(
            email="target_query@example.com",
            full_name="Target Query User",
            hashed_password=get_password_hash("oldpassword123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        # Try to reset with password in query params (INSECURE - should fail).
        # Super admin passes the auth gate, so the rejection is purely the
        # missing JSON body — proving query params are never accepted.
        response = client.post(
            f"/api/admin/users/{target_user.uuid}/reset-password",
            headers=super_admin_token_headers,
            params={"new_password": "InsecureQueryPassword123!"},
        )

        # Should fail with 422 (validation error) since body is required
        assert response.status_code == 422

    def test_password_reset_schema_requires_body(self):
        """Test that AdminPasswordResetRequest schema enforces body params."""
        # Valid request with required field
        request = AdminPasswordResetRequest(new_password="ValidPassword123!")
        assert request.new_password == "ValidPassword123!"
        assert request.force_change is True  # Default value

        # With custom force_change
        request2 = AdminPasswordResetRequest(new_password="AnotherPass456!", force_change=False)
        assert request2.force_change is False

    def test_password_reset_schema_validates_min_length(self):
        """Test that password schema enforces minimum length."""
        with pytest.raises(ValueError):
            AdminPasswordResetRequest(new_password="short")


class TestSuperAdminAccess:
    """Test super admin access to privileged endpoints."""

    @pytest.fixture
    def super_admin_user(self, db_session):
        """Create a super admin user."""
        user = User(
            email="superadmin@example.com",
            full_name="Super Admin",
            hashed_password=get_password_hash("superadminpass"),
            is_active=True,
            is_superuser=True,
            role="super_admin",
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    @pytest.fixture
    def super_admin_token_headers(self, client, super_admin_user):
        """Get auth headers for super admin user."""
        response = client.post(
            "/api/auth/token",
            data={"username": super_admin_user.email, "password": "superadminpass"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        tokens = response.json()
        access_token = tokens["access_token"]
        return {"Authorization": f"Bearer {access_token}"}

    def test_super_admin_can_reset_password(self, client, db_session, super_admin_token_headers):
        """Test that super_admin can reset user passwords."""
        target_user = User(
            email="password_target@example.com",
            full_name="Password Target",
            hashed_password=get_password_hash("oldpass123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/reset-password",
            headers=super_admin_token_headers,
            json={"new_password": "NewSuperSecure123!", "force_change": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_regular_admin_cannot_reset_password(self, client, db_session, admin_token_headers):
        """Test that regular admin cannot reset passwords (requires super_admin)."""
        target_user = User(
            email="admin_target@example.com",
            full_name="Admin Target",
            hashed_password=get_password_hash("oldpass123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/reset-password",
            headers=admin_token_headers,
            json={"new_password": "NewPassword123!", "force_change": True},
        )

        # Regular admin should be forbidden
        assert response.status_code == 403

    def test_super_admin_can_change_user_role(self, client, db_session, super_admin_token_headers):
        """Test that super_admin can change user roles."""
        target_user = User(
            email="role_target@example.com",
            full_name="Role Target",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.put(
            f"/api/admin/users/{target_user.uuid}/role",
            headers=super_admin_token_headers,
            params={"new_role": "admin"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["old_role"] == "user"
        assert data["new_role"] == "admin"

    def test_super_admin_can_reset_mfa(self, client, db_session, super_admin_token_headers):
        """Test that super_admin can reset user MFA."""
        target_user = User(
            email="mfa_target@example.com",
            full_name="MFA Target",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/mfa/reset",
            headers=super_admin_token_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestAccountLockUnlock:
    """Test account lock and unlock functionality."""

    def test_admin_can_lock_account(self, client, db_session, admin_token_headers):
        """Test that admin can lock a user account."""
        target_user = User(
            email="lock_target@example.com",
            full_name="Lock Target",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/lock",
            headers=admin_token_headers,
            params={"reason": "Security investigation"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify user is now inactive
        db_session.refresh(target_user)
        assert target_user.is_active is False

    def test_admin_can_unlock_account(self, client, db_session, admin_token_headers):
        """Test that admin can unlock a user account."""
        target_user = User(
            email="unlock_target@example.com",
            full_name="Unlock Target",
            hashed_password=get_password_hash("password123"),
            is_active=False,  # Start locked
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/unlock",
            headers=admin_token_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_lock_nonexistent_user_returns_404(self, client, admin_token_headers):
        """Test that locking a non-existent user returns 404."""
        fake_uuid = str(uuid.uuid4())
        response = client.post(
            f"/api/admin/users/{fake_uuid}/lock",
            headers=admin_token_headers,
            params={"reason": "Test reason"},
        )

        assert response.status_code == 404

    def test_unlock_nonexistent_user_returns_404(self, client, admin_token_headers):
        """Test that unlocking a non-existent user returns 404."""
        fake_uuid = str(uuid.uuid4())
        response = client.post(
            f"/api/admin/users/{fake_uuid}/unlock",
            headers=admin_token_headers,
        )

        assert response.status_code == 404

    def test_regular_user_cannot_lock_accounts(
        self, client, db_session, user_token_headers, admin_user
    ):
        """Test that regular users cannot lock accounts."""
        response = client.post(
            f"/api/admin/users/{admin_user.uuid}/lock",
            headers=user_token_headers,
            params={"reason": "Attempted by regular user"},
        )

        assert response.status_code == 403


class TestSessionTermination:
    """Test session termination functionality."""

    def test_admin_can_view_user_sessions(self, client, db_session, admin_token_headers):
        """Test that admin can view user sessions."""
        target_user = User(
            email="session_view@example.com",
            full_name="Session View User",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.get(
            f"/api/admin/users/{target_user.uuid}/sessions",
            headers=admin_token_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)

    def test_admin_can_terminate_user_sessions(self, client, db_session, admin_token_headers):
        """Test that admin can terminate all user sessions."""
        # Create target user
        target_user = User(
            email="session_terminate@example.com",
            full_name="Session Terminate User",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        # Create some refresh tokens for the user
        for i in range(3):
            token = RefreshToken(
                user_id=target_user.id,
                token_hash=f"testhash{i}{target_user.id}",
                jti=str(uuid.uuid4()),
                expires_at=datetime.now(UTC) + timedelta(days=7),
            )
            db_session.add(token)
        db_session.commit()

        response = client.delete(
            f"/api/admin/users/{target_user.uuid}/sessions",
            headers=admin_token_headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["sessions_terminated"] == 3

        # Verify tokens are revoked
        tokens = db_session.query(RefreshToken).filter(RefreshToken.user_id == target_user.id).all()
        for token in tokens:
            assert token.revoked_at is not None

    def test_terminate_sessions_nonexistent_user_returns_404(self, client, admin_token_headers):
        """Test that terminating sessions for non-existent user returns 404."""
        fake_uuid = str(uuid.uuid4())
        response = client.delete(
            f"/api/admin/users/{fake_uuid}/sessions",
            headers=admin_token_headers,
        )

        assert response.status_code == 404

    def test_regular_user_cannot_terminate_sessions(
        self, client, db_session, user_token_headers, admin_user
    ):
        """Test that regular users cannot terminate sessions."""
        response = client.delete(
            f"/api/admin/users/{admin_user.uuid}/sessions",
            headers=user_token_headers,
        )

        assert response.status_code == 403


class TestRoleBasedAccess:
    """Test role-based access control for admin endpoints."""

    def test_user_role_cannot_access_admin_stats(self, client, user_token_headers):
        """Test that user role cannot access admin statistics."""
        response = client.get("/api/admin/stats", headers=user_token_headers)
        assert response.status_code == 403

    def test_admin_role_can_access_admin_stats(self, client, admin_token_headers):
        """Test that admin role can access admin statistics."""
        response = client.get("/api/admin/stats", headers=admin_token_headers)
        assert response.status_code == 200

    def test_user_role_cannot_list_users(self, client, user_token_headers):
        """Test that user role cannot list all users."""
        response = client.get("/api/admin/users", headers=user_token_headers)
        assert response.status_code == 403

    def test_admin_role_can_list_users(self, client, admin_token_headers):
        """Test that admin role can list all users."""
        response = client.get("/api/admin/users", headers=admin_token_headers)
        assert response.status_code == 200

    def test_unauthenticated_access_denied(self, client):
        """Test that unauthenticated requests are denied."""
        endpoints = [
            "/api/admin/stats",
            "/api/admin/users",
            "/api/admin/reports/account-status",
        ]

        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == 401, f"Endpoint {endpoint} should require auth"


class TestAccountStatusReport:
    """Test account status reporting for compliance."""

    def test_admin_can_get_account_status_report(self, client, admin_token_headers):
        """Test that admin can get account status report."""
        response = client.get(
            "/api/admin/reports/account-status",
            headers=admin_token_headers,
        )

        assert response.status_code == 200
        data = response.json()

        # Verify expected fields exist
        assert "total_users" in data
        assert "active_users" in data
        assert "inactive_users" in data
        assert "mfa_enabled_users" in data
        assert "password_expired_users" in data

    def test_regular_user_cannot_access_account_status(self, client, user_token_headers):
        """Test that regular users cannot access account status report."""
        response = client.get(
            "/api/admin/reports/account-status",
            headers=user_token_headers,
        )

        assert response.status_code == 403


class TestUserSearchEndpoint:
    """Test admin user search functionality."""

    @pytest.fixture
    def super_admin_user(self, db_session):
        """Create a super admin user."""
        user = User(
            email="search_superadmin@example.com",
            full_name="Search Super Admin",
            hashed_password=get_password_hash("superadminpass"),
            is_active=True,
            is_superuser=True,
            role="super_admin",
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    @pytest.fixture
    def super_admin_token_headers(self, client, super_admin_user):
        """Get auth headers for super admin user."""
        response = client.post(
            "/api/auth/token",
            data={"username": super_admin_user.email, "password": "superadminpass"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        tokens = response.json()
        access_token = tokens["access_token"]
        return {"Authorization": f"Bearer {access_token}"}

    def test_admin_can_search_users(self, client, db_session, admin_token_headers):
        """Test that admin can search users."""
        # Create some test users
        for i in range(5):
            user = User(
                email=f"searchtest{i}@example.com",
                full_name=f"Search Test User {i}",
                hashed_password=get_password_hash("password123"),
                is_active=True,
                is_superuser=False,
                role="user",
            )
            db_session.add(user)
        db_session.commit()

        response = client.get(
            "/api/admin/users/search",
            headers=admin_token_headers,
            params={"query": "searchtest"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data
        assert "total" in data

    def test_search_with_role_filter(self, client, db_session, admin_token_headers):
        """Test user search with role filter."""
        response = client.get(
            "/api/admin/users/search",
            headers=admin_token_headers,
            params={"role": "admin"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data

    def test_search_with_active_filter(self, client, admin_token_headers):
        """Test user search with active status filter."""
        response = client.get(
            "/api/admin/users/search",
            headers=admin_token_headers,
            params={"is_active": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert "users" in data


class TestLockoutIntegration:
    """Test lockout module integration with admin endpoints."""

    def test_unlock_clears_lockout_state(self, client, db_session, admin_token_headers):
        """Test that admin unlock clears lockout state."""
        target_user = User(
            email="lockout_test@example.com",
            full_name="Lockout Test User",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        # Simulate some failed login attempts to trigger lockout
        # (In a real scenario, the lockout module would track this)

        # Admin unlocks the account
        response = client.post(
            f"/api/admin/users/{target_user.uuid}/unlock",
            headers=admin_token_headers,
        )

        assert response.status_code == 200


class TestAuditLogging:
    """Test that admin actions are properly audit logged."""

    @pytest.fixture
    def super_admin_user(self, db_session):
        """Create a super admin user."""
        user = User(
            email="audit_superadmin@example.com",
            full_name="Audit Super Admin",
            hashed_password=get_password_hash("superadminpass"),
            is_active=True,
            is_superuser=True,
            role="super_admin",
        )
        db_session.add(user)
        db_session.commit()
        db_session.refresh(user)
        return user

    @pytest.fixture
    def super_admin_token_headers(self, client, super_admin_user):
        """Get auth headers for super admin user."""
        response = client.post(
            "/api/auth/token",
            data={"username": super_admin_user.email, "password": "superadminpass"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        tokens = response.json()
        access_token = tokens["access_token"]
        return {"Authorization": f"Bearer {access_token}"}

    @patch("app.services.account_security_service.audit_logger")
    def test_password_reset_is_audited(
        self, mock_audit, client, db_session, super_admin_token_headers
    ):
        """Test that password reset actions are audit logged.

        The admin reset-password endpoint delegates to
        ``account_security_service.audit_password_change``, which resolves its own
        ``audit_logger`` binding (imported at module scope in that file) rather than
        the one imported into ``api.endpoints.admin`` -- so that is the name that
        must be patched for the mock to observe the call.
        """
        target_user = User(
            email="audit_target@example.com",
            full_name="Audit Target",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/reset-password",
            headers=super_admin_token_headers,
            json={"new_password": "Correct-Horse-Battery-9!", "force_change": True},
        )

        assert response.status_code == 200
        # Verify audit logger was called
        mock_audit.log.assert_called()

    @patch("app.api.endpoints.admin.audit_logger")
    def test_account_lock_is_audited(self, mock_audit, client, db_session, admin_token_headers):
        """Test that account lock actions are audit logged."""
        target_user = User(
            email="lock_audit@example.com",
            full_name="Lock Audit User",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.post(
            f"/api/admin/users/{target_user.uuid}/lock",
            headers=admin_token_headers,
            params={"reason": "Audit test"},
        )

        assert response.status_code == 200
        mock_audit.log.assert_called()

    @patch("app.api.endpoints.admin.audit_logger")
    def test_session_termination_is_audited(
        self, mock_audit, client, db_session, admin_token_headers
    ):
        """Test that session termination is audit logged."""
        target_user = User(
            email="session_audit@example.com",
            full_name="Session Audit User",
            hashed_password=get_password_hash("password123"),
            is_active=True,
            is_superuser=False,
            role="user",
        )
        db_session.add(target_user)
        db_session.commit()
        db_session.refresh(target_user)

        response = client.delete(
            f"/api/admin/users/{target_user.uuid}/sessions",
            headers=admin_token_headers,
        )

        assert response.status_code == 200
        mock_audit.log.assert_called()


class TestAdminPasswordResetRequest:
    """Test AdminPasswordResetRequest schema validation."""

    def test_valid_password_request(self):
        """Test creating a valid password reset request."""
        request = AdminPasswordResetRequest(
            new_password="ValidPassword123!",
            force_change=True,
        )
        assert request.new_password == "ValidPassword123!"
        assert request.force_change is True

    def test_default_force_change(self):
        """Test that force_change defaults to True."""
        request = AdminPasswordResetRequest(new_password="ValidPassword123!")
        assert request.force_change is True

    def test_password_minimum_length_enforced(self):
        """Test that password minimum length is enforced."""
        with pytest.raises(ValueError):
            AdminPasswordResetRequest(new_password="short")

    def test_exactly_minimum_length_allowed(self):
        """Test that exactly 8 characters is allowed."""
        request = AdminPasswordResetRequest(new_password="12345678")
        assert len(request.new_password) == 8


# Run with: pytest tests/test_admin_security.py -v
