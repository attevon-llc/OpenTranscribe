"""Characterization tests for ``app.api.endpoints.auth_config``.

Every route here is gated to the ``super_admin`` role. These tests pin the
current authorization behavior (401 anonymous, 403 for non-super-admin with the
exact ``detail``) and the super-admin happy/validation paths, so the dedup and
typed-model refactors can be proven behavior-neutral.

All mutating calls run inside the savepoint-isolated ``db_session`` and never
persist to dev data.

Routes are mounted at ``/api/admin/auth-config``.

Run: ``venv/bin/pytest tests/api/test_auth_config_endpoints.py -v -n0``
"""

_BASE = "/api/admin/auth-config"


# --------------------------------------------------------------------------- #
# Authorization gate — super_admin only
# --------------------------------------------------------------------------- #
class TestSuperAdminGate:
    def test_get_all_unauthenticated_401(self, client):
        resp = client.get(_BASE)
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Could not validate credentials"

    def test_get_all_normal_user_403(self, client, user_token_headers):
        resp = client.get(_BASE, headers=user_token_headers)
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]

    def test_get_all_regular_admin_403(self, client, admin_token_headers):
        """A plain ``admin`` (not ``super_admin``) is still forbidden."""
        resp = client.get(_BASE, headers=admin_token_headers)
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]

    def test_status_normal_user_403(self, client, user_token_headers):
        resp = client.get(f"{_BASE}/status", headers=user_token_headers)
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]

    def test_get_category_regular_admin_403(self, client, admin_token_headers):
        resp = client.get(f"{_BASE}/ldap", headers=admin_token_headers)
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]

    def test_put_category_normal_user_403(self, client, user_token_headers):
        resp = client.put(f"{_BASE}/mfa", headers=user_token_headers, json={"mfa_enabled": False})
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]

    def test_test_connection_normal_user_403(self, client, user_token_headers):
        resp = client.post(f"{_BASE}/ldap/test", headers=user_token_headers, json={})
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]

    def test_migrate_normal_user_403(self, client, user_token_headers):
        resp = client.post(f"{_BASE}/migrate", headers=user_token_headers)
        assert resp.status_code == 403
        assert "super_admin" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Super-admin reads
# --------------------------------------------------------------------------- #
class TestSuperAdminReads:
    def test_get_all_returns_category_map(self, client, super_admin_token_headers):
        resp = client.get(_BASE, headers=super_admin_token_headers)
        assert resp.status_code == 200
        body = resp.json()
        # Response groups configs by category; the documented categories are keys.
        for category in (
            "local",
            "ldap",
            "oidc",
            "pki",
            "password_policy",
            "mfa",
            "session",
            "banner",
            "lockout",
        ):
            assert category in body
            assert isinstance(body[category], list)

    def test_get_status_shape(self, client, super_admin_token_headers):
        resp = client.get(f"{_BASE}/status", headers=super_admin_token_headers)
        assert resp.status_code == 200
        body = resp.json()
        for key in (
            "ldap_enabled",
            "oidc_enabled",
            "pki_enabled",
            "mfa_enabled",
            "password_policy_enabled",
            "login_banner_enabled",
        ):
            assert key in body
            assert isinstance(body[key], bool)

    def test_get_category_valid(self, client, super_admin_token_headers):
        resp = client.get(f"{_BASE}/mfa", headers=super_admin_token_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), dict)

    def test_get_category_invalid_400(self, client, super_admin_token_headers):
        resp = client.get(f"{_BASE}/not-a-category", headers=super_admin_token_headers)
        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("Invalid category. Must be one of:")

    def test_get_audit_log(self, client, super_admin_token_headers):
        resp = client.get(f"{_BASE}/audit/mfa", headers=super_admin_token_headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)


# --------------------------------------------------------------------------- #
# Super-admin writes / validation (savepoint-isolated)
# --------------------------------------------------------------------------- #
class TestSuperAdminWrites:
    def test_put_category_happy(self, client, super_admin_token_headers, db_session):
        resp = client.put(
            f"{_BASE}/mfa",
            headers=super_admin_token_headers,
            json={"mfa_issuer_name": "OpenTranscribe-Test"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["message"] == "mfa configuration updated"
        assert body["updated_count"] == 1
        assert body["updated_keys"] == ["mfa_issuer_name"]

    def test_put_category_invalid_400(self, client, super_admin_token_headers):
        resp = client.put(
            f"{_BASE}/bogus",
            headers=super_admin_token_headers,
            json={"foo": "bar"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"].startswith("Invalid category. Must be one of:")

    def test_test_connection_unsupported_category_400(self, client, super_admin_token_headers):
        """``/test`` only supports ldap/oidc — anything else is a 400."""
        resp = client.post(f"{_BASE}/mfa/test", headers=super_admin_token_headers, json={})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Connection test not supported for category: mfa"

    def test_test_ldap_connection_missing_server(self, client, super_admin_token_headers):
        """LDAP test with no server address returns success=False (not an HTTP error)."""
        resp = client.post(f"{_BASE}/ldap/test", headers=super_admin_token_headers, json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["message"] == "LDAP server address is required"
