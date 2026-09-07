"""Tests for per-user media source CRUD and sharing.

Covers the /api/user-settings/media-sources endpoints:
- Create, read, update, delete media sources
- Sharing toggle and visibility
- Credential security (passwords never exposed to non-owners)
- Hostname validation (SSRF protection)
- Owner-only edit/delete enforcement
"""

import uuid

import pytest

from app.models.user_media_source import UserMediaSource
from app.utils.encryption import encrypt_api_key
from tests.helpers import stub_public_dns


@pytest.fixture(autouse=True)
def _public_media_host_dns(monkeypatch):
    """Let ``*.example.com`` resolve to a public address for this module.

    Since audit finding A1, storing a media source resolves the hostname and refuses
    non-public addresses — the old regex-and-blocklist validator accepted
    ``169.254.169.254``. RFC 2606 reserves ``example.com`` but its subdomains NXDOMAIN,
    so every hostname in this suite would otherwise be refused for "cannot resolve",
    which is not what any of these tests are about.

    Deliberately scoped to that one domain: ``internalhost`` and ``redis.internal.svc``
    below must keep failing to resolve, or the rejection tests become no-ops.
    """
    stub_public_dns(monkeypatch)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_media_source(
    db,
    user,
    *,
    hostname="media.example.com",
    provider_type="mediacms",
    username="testuser",
    password="testpass",
    verify_ssl=True,
    label="",
    shared=False,
):
    """Create a UserMediaSource directly in the DB."""
    encrypted_pw = encrypt_api_key(password) if password else None
    source = UserMediaSource(
        uuid=uuid.uuid4(),
        user_id=user.id,
        hostname=hostname,
        provider_type=provider_type,
        username=username,
        password=encrypted_pw,
        verify_ssl=verify_ssl,
        label=label or f"test-{uuid.uuid4().hex[:6]}",
        is_active=True,
        is_shared=shared,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


# ===================================================================
# CRUD — Create
# ===================================================================


class TestMediaSourceCreate:
    """POST /user-settings/media-sources"""

    def test_create_basic_source(self, client, normal_user, user_token_headers):
        """Create a simple media source with hostname and credentials."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={
                "hostname": "videos.example.com",
                "provider_type": "mediacms",
                "username": "alice",
                "password": "secret123",
                "label": "My Videos",
            },
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["hostname"] == "videos.example.com"
        assert data["provider_type"] == "mediacms"
        assert data["username"] == "alice"
        assert data["label"] == "My Videos"
        assert data["has_credentials"] is True
        assert data["is_own"] is True
        assert data["is_shared"] is False
        assert "uuid" in data

    def test_create_source_without_credentials(self, client, normal_user, user_token_headers):
        """Create a source without username/password."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "public.example.com"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_credentials"] is False

    def test_create_source_with_port(self, client, normal_user, user_token_headers):
        """Hostname with port should be accepted."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "media.example.com:8443", "username": "u", "password": "p"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["hostname"] == "media.example.com:8443"

    def test_create_duplicate_hostname_rejected(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Same user cannot add two sources with the same hostname."""
        _create_media_source(db_session, normal_user, hostname="dup.example.com")
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "dup.example.com"},
            headers=user_token_headers,
        )
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    def test_create_requires_auth(self, client):
        """Unauthenticated request should be rejected."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "media.example.com"},
        )
        assert resp.status_code == 401

    def test_create_source_ssl_disabled(self, client, normal_user, user_token_headers):
        """Create a source with SSL verification disabled."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={
                "hostname": "selfcert.example.com",
                "verify_ssl": False,
                "username": "u",
                "password": "p",
            },
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["verify_ssl"] is False


# ===================================================================
# CRUD — Read / List
# ===================================================================


class TestMediaSourceList:
    """GET /user-settings/media-sources"""

    def test_list_own_sources(self, client, db_session, normal_user, user_token_headers):
        """User sees their own sources in the 'sources' list."""
        _create_media_source(db_session, normal_user, hostname="mine.example.com")

        resp = client.get("/api/user-settings/media-sources", headers=user_token_headers)
        assert resp.status_code == 200
        data = resp.json()
        hostnames = [s["hostname"] for s in data["sources"]]
        assert "mine.example.com" in hostnames

    def test_list_excludes_other_users_private(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Other user's private sources should NOT appear."""
        _create_media_source(db_session, normal_user, hostname="private.example.com", shared=False)

        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        data = resp.json()
        all_hostnames = [s["hostname"] for s in data["sources"] + data["shared_sources"]]
        assert "private.example.com" not in all_hostnames

    def test_list_shows_shared_sources(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared sources from other users appear in 'shared_sources'."""
        _create_media_source(db_session, normal_user, hostname="shared.example.com", shared=True)

        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        data = resp.json()
        shared_hostnames = [s["hostname"] for s in data["shared_sources"]]
        assert "shared.example.com" in shared_hostnames

    def test_own_shared_source_in_own_list(
        self, client, db_session, normal_user, user_token_headers
    ):
        """Owner's shared source appears in 'sources', not 'shared_sources'."""
        _create_media_source(db_session, normal_user, hostname="myshared.example.com", shared=True)

        resp = client.get("/api/user-settings/media-sources", headers=user_token_headers)
        data = resp.json()
        own_hosts = [s["hostname"] for s in data["sources"]]
        shared_hosts = [s["hostname"] for s in data["shared_sources"]]
        assert "myshared.example.com" in own_hosts
        assert "myshared.example.com" not in shared_hosts

    def test_list_requires_auth(self, client):
        """Unauthenticated request returns 401."""
        resp = client.get("/api/user-settings/media-sources")
        assert resp.status_code == 401


# ===================================================================
# CRUD — Update
# ===================================================================


class TestMediaSourceUpdate:
    """PUT /user-settings/media-sources/{uuid}"""

    def test_update_label(self, client, db_session, normal_user, user_token_headers):
        """Owner can update their source's label."""
        src = _create_media_source(db_session, normal_user, hostname="edit.example.com")
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"label": "Updated Label"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["label"] == "Updated Label"

    def test_update_credentials(self, client, db_session, normal_user, user_token_headers):
        """Owner can update credentials."""
        src = _create_media_source(db_session, normal_user, hostname="cred.example.com")
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"username": "newuser", "password": "newpass"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["username"] == "newuser"
        assert resp.json()["has_credentials"] is True

    def test_update_clear_password(self, client, db_session, normal_user, user_token_headers):
        """Owner can clear credentials by sending empty password."""
        src = _create_media_source(db_session, normal_user, hostname="clear.example.com")
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"password": ""},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["has_credentials"] is False

    def test_update_ssl_setting(self, client, db_session, normal_user, user_token_headers):
        """Owner can toggle SSL verification."""
        src = _create_media_source(db_session, normal_user, hostname="ssl.example.com")
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"verify_ssl": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["verify_ssl"] is False

    def test_non_owner_cannot_update(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner gets 404 (source not found for their user_id)."""
        src = _create_media_source(db_session, normal_user, hostname="notyours.example.com")
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"label": "Hacked"},
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 404

    def test_update_nonexistent_returns_404(self, client, normal_user, user_token_headers):
        """Updating a non-existent UUID returns 404."""
        resp = client.put(
            f"/api/user-settings/media-sources/{uuid.uuid4()}",
            json={"label": "Ghost"},
            headers=user_token_headers,
        )
        assert resp.status_code == 404


# ===================================================================
# CRUD — Delete
# ===================================================================


class TestMediaSourceDelete:
    """DELETE /user-settings/media-sources/{uuid}"""

    def test_owner_can_delete(self, client, db_session, normal_user, user_token_headers):
        """Owner can delete their own source."""
        src = _create_media_source(db_session, normal_user, hostname="delete.example.com")
        resp = client.delete(
            f"/api/user-settings/media-sources/{src.uuid}",
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["success"] is True

        # Verify it's gone
        list_resp = client.get("/api/user-settings/media-sources", headers=user_token_headers)
        hostnames = [s["hostname"] for s in list_resp.json()["sources"]]
        assert "delete.example.com" not in hostnames

    def test_non_owner_cannot_delete(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner gets 404 when trying to delete another's source."""
        src = _create_media_source(db_session, normal_user, hostname="nodelete.example.com")
        resp = client.delete(
            f"/api/user-settings/media-sources/{src.uuid}",
            headers=other_user_auth_headers,
        )
        assert resp.status_code == 404

    def test_delete_nonexistent_returns_404(self, client, normal_user, user_token_headers):
        """Deleting a non-existent UUID returns 404."""
        resp = client.delete(
            f"/api/user-settings/media-sources/{uuid.uuid4()}",
            headers=user_token_headers,
        )
        assert resp.status_code == 404


# ===================================================================
# Sharing — Toggle & Visibility
# ===================================================================


class TestMediaSourceSharing:
    """Sharing toggle and cross-user visibility."""

    def test_share_via_update(self, client, db_session, normal_user, user_token_headers):
        """Owner can share a source by setting is_shared=True."""
        src = _create_media_source(db_session, normal_user, hostname="toshare.example.com")
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"is_shared": True},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is True
        assert resp.json()["shared_at"] is not None

    def test_unshare_via_update(self, client, db_session, normal_user, user_token_headers):
        """Owner can unshare by setting is_shared=False."""
        src = _create_media_source(
            db_session, normal_user, hostname="unshare.example.com", shared=True
        )
        resp = client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"is_shared": False},
            headers=user_token_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["is_shared"] is False
        assert resp.json()["shared_at"] is None

    def test_shared_source_has_owner_attribution(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Shared sources include owner name and role for non-owners."""
        _create_media_source(
            db_session, normal_user, hostname="attributed.example.com", shared=True
        )
        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        shared = resp.json()["shared_sources"]
        src = next(s for s in shared if s["hostname"] == "attributed.example.com")
        assert src["owner_name"] == normal_user.full_name
        assert src["is_own"] is False

    def test_shared_source_disappears_after_unshare(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """After unsharing, source disappears from other user's list."""
        src = _create_media_source(
            db_session, normal_user, hostname="vanish.example.com", shared=True
        )
        # Other user sees it
        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        shared_hosts = [s["hostname"] for s in resp.json()["shared_sources"]]
        assert "vanish.example.com" in shared_hosts

        # Owner unshares
        client.put(
            f"/api/user-settings/media-sources/{src.uuid}",
            json={"is_shared": False},
            headers=user_token_headers,
        )

        # Other user no longer sees it
        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        shared_hosts = [s["hostname"] for s in resp.json()["shared_sources"]]
        assert "vanish.example.com" not in shared_hosts


# ===================================================================
# Credential Security
# ===================================================================


class TestMediaSourceCredentialSecurity:
    """Passwords must NEVER be exposed to non-owners."""

    def test_own_source_shows_username(self, client, db_session, normal_user, user_token_headers):
        """Owner sees their own username in the response."""
        _create_media_source(
            db_session, normal_user, hostname="owncreds.example.com", username="myuser"
        )
        resp = client.get("/api/user-settings/media-sources", headers=user_token_headers)
        src = next(s for s in resp.json()["sources"] if s["hostname"] == "owncreds.example.com")
        assert src["username"] == "myuser"

    def test_shared_source_hides_username(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Non-owner sees empty username for shared sources."""
        _create_media_source(
            db_session,
            normal_user,
            hostname="hidecreds.example.com",
            username="secretuser",
            shared=True,
        )
        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        src = next(
            s for s in resp.json()["shared_sources"] if s["hostname"] == "hidecreds.example.com"
        )
        assert src["username"] == ""
        assert src["has_credentials"] is True

    def test_password_never_in_response(self, client, db_session, normal_user, user_token_headers):
        """Password should never appear in any API response, even for the owner."""
        _create_media_source(db_session, normal_user, hostname="nopw.example.com")
        resp = client.get("/api/user-settings/media-sources", headers=user_token_headers)
        src = next(s for s in resp.json()["sources"] if s["hostname"] == "nopw.example.com")
        assert "password" not in src


# ===================================================================
# Hostname Validation (SSRF Protection)
# ===================================================================


class TestMediaSourceHostnameValidation:
    """Hostname validation prevents SSRF at the endpoint.

    Since audit finding A1 the judgement is made by ``utils/url_validation``, which
    resolves the name and inspects every address. So the names below are refused for
    resolving to a private address (in a container or cluster) or for not resolving at
    all, rather than by the first-label blocklist that used to sit in the schema and
    let ``169.254.169.254`` straight through. The address-class cases live in
    ``unit/test_media_source_ssrf.py``; these pin the HTTP contract.
    """

    def test_bare_hostname_rejected(self, client, normal_user, user_token_headers):
        """Single-label service names must not become an allowed host."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "internalhost"},
            headers=user_token_headers,
        )
        assert resp.status_code == 422

    def test_localhost_rejected(self, client, normal_user, user_token_headers):
        """'localhost' should be rejected."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "localhost"},
            headers=user_token_headers,
        )
        assert resp.status_code == 422

    def test_reserved_internal_name_rejected(self, client, normal_user, user_token_headers):
        """Kubernetes/Docker service names must not become an allowed host.

        In a cluster these resolve to a private address; outside one they do not resolve
        at all. Both are refusals, which is why this holds without the hardcoded name
        list it used to depend on.
        """
        for reserved in ["redis.internal.svc", "postgres.default.svc", "minio.cluster.local"]:
            resp = client.post(
                "/api/user-settings/media-sources",
                json={"hostname": reserved},
                headers=user_token_headers,
            )
            assert resp.status_code == 422, f"Expected 422 for {reserved}"

    def test_valid_fqdn_accepted(self, client, normal_user, user_token_headers):
        """Valid fully-qualified domain names should be accepted."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "media.corp.example.com"},
            headers=user_token_headers,
        )
        assert resp.status_code == 200

    def test_invalid_hostname_chars_rejected(self, client, normal_user, user_token_headers):
        """Hostnames with invalid characters should be rejected."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "media.example.com/<script>"},
            headers=user_token_headers,
        )
        assert resp.status_code == 422

    def test_invalid_port_rejected(self, client, normal_user, user_token_headers):
        """Hostnames with invalid port numbers should be rejected."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "media.example.com:99999"},
            headers=user_token_headers,
        )
        assert resp.status_code == 422

    def test_invalid_provider_type_rejected(self, client, normal_user, user_token_headers):
        """Unsupported provider types should be rejected."""
        resp = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "media.example.com", "provider_type": "dropbox"},
            headers=user_token_headers,
        )
        assert resp.status_code == 422


# ===================================================================
# Edge Cases
# ===================================================================


class TestMediaSourceEdgeCases:
    """Edge cases and limits."""

    def test_different_users_same_hostname(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """Two different users can have sources with the same hostname."""
        # First user creates
        resp1 = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "shared-host.example.com", "username": "user1", "password": "pass1"},
            headers=user_token_headers,
        )
        assert resp1.status_code == 200

        # Second user creates same hostname
        resp2 = client.post(
            "/api/user-settings/media-sources",
            json={"hostname": "shared-host.example.com", "username": "user2", "password": "pass2"},
            headers=other_user_auth_headers,
        )
        assert resp2.status_code == 200

    def test_deleted_source_no_longer_shared(
        self,
        client,
        db_session,
        normal_user,
        other_user,
        user_token_headers,
        other_user_auth_headers,
    ):
        """After owner deletes a shared source, it disappears for other users."""
        src = _create_media_source(
            db_session, normal_user, hostname="delsrc.example.com", shared=True
        )

        # Verify other user sees it
        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        shared_hosts = [s["hostname"] for s in resp.json()["shared_sources"]]
        assert "delsrc.example.com" in shared_hosts

        # Owner deletes
        client.delete(
            f"/api/user-settings/media-sources/{src.uuid}",
            headers=user_token_headers,
        )

        # Other user no longer sees it
        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        shared_hosts = [s["hostname"] for s in resp.json()["shared_sources"]]
        assert "delsrc.example.com" not in shared_hosts

    def test_inactive_user_sources_not_shared(
        self, client, db_session, normal_user, other_user, other_user_auth_headers
    ):
        """Sources from deactivated users should not appear in shared list."""
        _create_media_source(db_session, normal_user, hostname="inactive.example.com", shared=True)
        # Deactivate the owner
        normal_user.is_active = False
        db_session.commit()

        resp = client.get("/api/user-settings/media-sources", headers=other_user_auth_headers)
        shared_hosts = [s["hostname"] for s in resp.json()["shared_sources"]]
        assert "inactive.example.com" not in shared_hosts
