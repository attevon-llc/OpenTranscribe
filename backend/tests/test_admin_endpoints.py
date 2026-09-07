"""
Tests for advanced admin API endpoints.

NOTE: Basic admin tests (stats, list users, create user, delete user) are in
tests/api/endpoints/test_admin.py. This file covers the advanced admin features
added for FedRAMP compliance: password reset, role management, user search,
and audit log access.
"""

import pytest


class TestAdminAccountManagement:
    """Test admin account management endpoints."""

    def test_reset_password_requires_admin(self, client, user_token_headers):
        """Test that password reset requires admin privileges."""
        response = client.post(
            "/api/admin/users/some-uuid/reset-password",
            headers=user_token_headers,
            json={"new_password": "NewPassword123!"},
        )
        # Regular user should be forbidden
        assert response.status_code == 403, response.text

    def test_super_admin_can_update_user_role(self, client, super_admin_token_headers, normal_user):
        """Test that a super admin can update user roles (query param API)."""
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/role",
            headers=super_admin_token_headers,
            params={"new_role": "admin"},
        )
        assert response.status_code == 200, response.json()

    def test_admin_cannot_update_user_role(self, client, admin_token_headers, normal_user):
        """Role changes are super-admin only — a plain admin is forbidden."""
        response = client.put(
            f"/api/admin/users/{normal_user.uuid}/role",
            headers=admin_token_headers,
            params={"new_role": "admin"},
        )
        assert response.status_code == 403


class TestAdminUserSearch:
    """Test admin user search endpoints."""

    def test_admin_can_search_users_with_filters(self, client, admin_token_headers):
        """Test that admin can search users with filters."""
        response = client.get(
            "/api/admin/users/search",
            headers=admin_token_headers,
            params={"query": "test", "role": "user"},
        )
        assert response.status_code == 200

    def test_user_cannot_search_users(self, client, user_token_headers):
        """Regular users cannot access the admin search endpoint."""
        response = client.get(
            "/api/admin/users/search",
            headers=user_token_headers,
            params={"query": "test"},
        )
        assert response.status_code == 403, response.text


class TestAdminAuditLog:
    """Test admin audit log access (super admin only)."""

    def test_super_admin_can_view_audit_logs(self, client, super_admin_token_headers):
        """Test that a super admin can view audit logs."""
        response = client.get("/api/admin/audit-logs", headers=super_admin_token_headers)
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data

    def test_super_admin_can_export_audit_logs(self, client, super_admin_token_headers):
        """A super admin can export audit logs — when the OpenSearch audit sink is enabled.

        Was ``in [200, 400]``. The test claims a super admin *can* export, but 400 is exactly
        "could not export", so it passed while asserting the opposite of its own name.
        ``export_audit_logs`` returns 400 when ``AUDIT_LOG_TO_OPENSEARCH`` is false
        (``admin.py:1754``) and conftest forces it false, since savepoint rollback cannot undo
        OpenSearch writes. That precondition is now a visible skip rather than a false pass,
        and the authorization half stays covered unconditionally (issue #431).

        The authorization half used to be a standalone ``not in (401, 403)`` check, which
        also passes on a 500 — but the exact-status assert in whichever branch below
        actually runs already forbids both 401 and 403 (neither is 200 nor 400), so the
        weak check added no coverage beyond them and is removed rather than kept alongside.
        """
        from app.core.config import settings

        response = client.get(
            "/api/admin/audit-logs/export",
            headers=super_admin_token_headers,
            params={"export_format": "csv"},
        )
        if not settings.AUDIT_LOG_TO_OPENSEARCH:
            assert response.status_code == 400, response.text
            pytest.skip("audit export needs AUDIT_LOG_TO_OPENSEARCH=true (conftest forces it off)")
        assert response.status_code == 200, response.text

    def test_admin_cannot_view_audit_logs(self, client, admin_token_headers):
        """Audit logs are super-admin only — a plain admin is forbidden."""
        response = client.get("/api/admin/audit-logs", headers=admin_token_headers)
        assert response.status_code == 403
