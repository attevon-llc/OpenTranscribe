"""
Tests for advanced admin API endpoints.

NOTE: Basic admin tests (stats, list users, create user, delete user) are in
tests/api/endpoints/test_admin.py. This file covers the advanced admin features
added for FedRAMP compliance: password reset, role management, user search,
and audit log access.
"""


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
        assert response.status_code in [401, 403]

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
        assert response.status_code in [401, 403]


class TestAdminAuditLog:
    """Test admin audit log access (super admin only)."""

    def test_super_admin_can_view_audit_logs(self, client, super_admin_token_headers):
        """Test that a super admin can view audit logs."""
        response = client.get("/api/admin/audit-logs", headers=super_admin_token_headers)
        assert response.status_code == 200
        data = response.json()
        assert "logs" in data

    def test_super_admin_can_export_audit_logs(self, client, super_admin_token_headers):
        """Test that a super admin can export audit logs."""
        response = client.get(
            "/api/admin/audit-logs/export",
            headers=super_admin_token_headers,
            params={"export_format": "csv"},
        )
        assert response.status_code in [200, 400]

    def test_admin_cannot_view_audit_logs(self, client, admin_token_headers):
        """Audit logs are super-admin only — a plain admin is forbidden."""
        response = client.get("/api/admin/audit-logs", headers=admin_token_headers)
        assert response.status_code == 403
