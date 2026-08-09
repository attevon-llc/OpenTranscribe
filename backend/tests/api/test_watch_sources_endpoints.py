"""Functional characterization tests for the watch-sources endpoints.

Covers ``watch_sources.py`` (``/api/watch-sources``): source CRUD with the
write-only encrypted-credential contract (S3 secret / SMB password are accepted
on write but NEVER echoed back), the ``has_*`` boolean flags, folder-browse
validation, the connection-test error envelope (against an unreachable target —
no real S3/SMB connection is made), and admin-only global settings / email
configs.

The 403-other_user + admin-bypass authz snapshots for the GET/PUT paths are
already pinned in ``test_ownership_contracts.py``; these add functional
coverage around them. All rows live on the savepoint-isolated ``db_session``.
"""

from __future__ import annotations

import uuid

from fastapi import status

from app.models.watch_source import WatchSource
from app.utils.encryption import encrypt_api_key


def _make_source(
    db_session,
    owner,
    *,
    source_type: str = "s3",
    is_enabled: bool = True,
    with_secret: bool = False,
) -> WatchSource:
    ws = WatchSource(
        uuid=uuid.uuid4(),
        user_id=owner.id,
        created_by=owner.id,
        name=f"watch-{uuid.uuid4().hex[:8]}",
        source_type=source_type,
        is_enabled=is_enabled,
        s3_endpoint_url="https://s3.invalid.example.com",
        s3_bucket_name="bucket",
        s3_access_key_id="AKIATEST",
    )
    if with_secret:
        ws.encrypted_s3_secret_key = encrypt_api_key("super-secret-value")
    db_session.add(ws)
    db_session.commit()
    db_session.refresh(ws)
    return ws


# ---------------------------------------------------------------------------
# GET /api/watch-sources  (list, scoped to own)
# ---------------------------------------------------------------------------


def test_list_sources_unauthorized(client):
    response = client.get("/api/watch-sources")
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


def test_list_sources_returns_own(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user)
    response = client.get("/api/watch-sources", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {s["uuid"] for s in response.json()["sources"]}
    assert str(ws.uuid) in uuids


def test_list_sources_excludes_other_users(
    client, other_user_auth_headers, normal_user, db_session
):
    ws = _make_source(db_session, normal_user)
    response = client.get("/api/watch-sources", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_200_OK
    uuids = {s["uuid"] for s in response.json()["sources"]}
    assert str(ws.uuid) not in uuids


def test_list_sources_scope_all_is_admin_only(
    client, other_user_auth_headers, normal_user, db_session
):
    """A non-admin requesting scope=all is silently scoped back to own."""
    ws = _make_source(db_session, normal_user)
    response = client.get(
        "/api/watch-sources", headers=other_user_auth_headers, params={"scope": "all"}
    )
    assert response.status_code == status.HTTP_200_OK
    uuids = {s["uuid"] for s in response.json()["sources"]}
    assert str(ws.uuid) not in uuids


def test_list_sources_scope_all_admin_sees_all(
    client, super_admin_token_headers, normal_user, db_session
):
    ws = _make_source(db_session, normal_user)
    response = client.get(
        "/api/watch-sources", headers=super_admin_token_headers, params={"scope": "all"}
    )
    assert response.status_code == status.HTTP_200_OK
    uuids = {s["uuid"] for s in response.json()["sources"]}
    assert str(ws.uuid) in uuids


# ---------------------------------------------------------------------------
# POST /api/watch-sources  (create) + write-only secret
# ---------------------------------------------------------------------------


def test_create_s3_source_happy(client, user_token_headers):
    response = client.post(
        "/api/watch-sources",
        headers=user_token_headers,
        json={
            "name": f"s3-{uuid.uuid4().hex[:6]}",
            "source_type": "s3",
            "s3_bucket_name": "mybucket",
            "s3_access_key_id": "AKIATEST",
            "s3_secret_key": "creation-secret",
            "s3_endpoint_url": "https://s3.invalid.example.com",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["source_type"] == "s3"
    assert body["is_own"] is True
    # The secret was supplied → flag is true, but the value is never echoed.
    assert body["has_s3_secret_key"] is True
    assert "creation-secret" not in response.text


def test_create_source_stamps_request_org(client, user_token_headers, normal_user, db_session):
    """Issue #262c: the source captures the CREATING request's tenant so every
    background import is stamped with it (never a first-membership guess)."""
    import uuid as uuid_pkg

    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.main import app
    from app.models.organization import Organization

    org = Organization(
        external_org_id=f"org_{uuid_pkg.uuid4().hex[:8]}", name="Watch Org", is_active=True
    )
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)

    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=normal_user, org_id=org.id, org_role="org:member"
    )
    try:
        response = client.post(
            "/api/watch-sources",
            headers=user_token_headers,
            json={
                "name": f"s3org-{uuid.uuid4().hex[:6]}",
                "source_type": "s3",
                "s3_bucket_name": "mybucket",
                "s3_access_key_id": "AKIATEST",
                "s3_secret_key": "org-test-secret",
            },
        )
    finally:
        app.dependency_overrides.pop(get_current_context, None)

    assert response.status_code == status.HTTP_200_OK
    row = db_session.query(WatchSource).filter(WatchSource.uuid == response.json()["uuid"]).first()
    assert row is not None
    assert row.organization_id == org.id


def test_create_source_personal_scope_unstamped(client, user_token_headers, db_session):
    """Community invariance: no org context -> organization_id stays NULL."""
    response = client.post(
        "/api/watch-sources",
        headers=user_token_headers,
        json={
            "name": f"s3pers-{uuid.uuid4().hex[:6]}",
            "source_type": "s3",
            "s3_bucket_name": "mybucket",
            "s3_access_key_id": "AKIATEST",
            "s3_secret_key": "personal-test-secret",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    row = db_session.query(WatchSource).filter(WatchSource.uuid == response.json()["uuid"]).first()
    assert row is not None
    assert row.organization_id is None


def test_create_source_with_secret_never_echoed(client, user_token_headers):
    """The plaintext S3 secret is accepted but never returned; has_* flips true."""
    response = client.post(
        "/api/watch-sources",
        headers=user_token_headers,
        json={
            "name": f"s3sec-{uuid.uuid4().hex[:6]}",
            "source_type": "s3",
            "s3_bucket_name": "mybucket",
            "s3_access_key_id": "AKIATEST",
            "s3_secret_key": "PLAINTEXT-SECRET-DO-NOT-ECHO",
        },
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["has_s3_secret_key"] is True
    # The secret and its encrypted column are never present in the response.
    serialized = response.text
    assert "PLAINTEXT-SECRET-DO-NOT-ECHO" not in serialized
    assert "s3_secret_key" not in body
    assert "encrypted_s3_secret_key" not in body


def test_create_source_invalid_type_422(client, user_token_headers):
    response = client.post(
        "/api/watch-sources",
        headers=user_token_headers,
        json={"name": "bad", "source_type": "ftp"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_source_empty_name_422(client, user_token_headers):
    """name has min_length=1."""
    response = client.post(
        "/api/watch-sources",
        headers=user_token_headers,
        json={"name": "", "source_type": "s3"},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_create_source_unauthorized(client):
    response = client.post("/api/watch-sources", json={"name": "x", "source_type": "s3"})
    assert response.status_code == status.HTTP_401_UNAUTHORIZED


# ---------------------------------------------------------------------------
# GET / PUT / DELETE /api/watch-sources/{uuid}
# ---------------------------------------------------------------------------


def test_get_source_owner(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user, with_secret=True)
    response = client.get(f"/api/watch-sources/{ws.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["uuid"] == str(ws.uuid)
    # Stored secret is flagged present but never serialized.
    assert body["has_s3_secret_key"] is True
    assert "super-secret-value" not in response.text


def test_get_source_nonexistent_404(client, user_token_headers):
    response = client.get(f"/api/watch-sources/{uuid.uuid4()}", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Watch source not found"


def test_update_source_owner(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user)
    response = client.put(
        f"/api/watch-sources/{ws.uuid}",
        headers=user_token_headers,
        json={"name": "renamed-source"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["name"] == "renamed-source"


def test_update_source_secret_not_echoed(client, user_token_headers, normal_user, db_session):
    """Updating with a new secret flips the flag but never returns it."""
    ws = _make_source(db_session, normal_user)
    response = client.put(
        f"/api/watch-sources/{ws.uuid}",
        headers=user_token_headers,
        json={"s3_secret_key": "NEW-PLAINTEXT-SECRET"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["has_s3_secret_key"] is True
    assert "NEW-PLAINTEXT-SECRET" not in response.text


def test_delete_source_owner(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user)
    response = client.delete(f"/api/watch-sources/{ws.uuid}", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["success"] is True
    follow_up = client.get(f"/api/watch-sources/{ws.uuid}", headers=user_token_headers)
    assert follow_up.status_code == status.HTTP_404_NOT_FOUND


def test_delete_source_other_user_403(client, other_user_auth_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user)
    response = client.delete(f"/api/watch-sources/{ws.uuid}", headers=other_user_auth_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN
    assert response.json()["detail"] == "Not authorized for this watch source"


# ---------------------------------------------------------------------------
# Scan + connection test
# ---------------------------------------------------------------------------


def test_scan_disabled_source_400(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user, is_enabled=False)
    response = client.post(f"/api/watch-sources/{ws.uuid}/scan", headers=user_token_headers)
    assert response.status_code == status.HTTP_400_BAD_REQUEST
    assert response.json()["detail"] == "Enable the source before scanning"


def test_scan_enabled_source_dispatches(client, user_token_headers, normal_user, db_session):
    """Celery dispatch is no-opped by conftest; the endpoint still returns started."""
    ws = _make_source(db_session, normal_user, is_enabled=True)
    response = client.post(f"/api/watch-sources/{ws.uuid}/scan", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["status"] == "started"


def test_connection_test_unreachable_returns_error_envelope(
    client, user_token_headers, normal_user, db_session
):
    """Testing an S3 source against an unreachable endpoint returns a structured
    failure envelope (success=False + message), NOT a 5xx — no real connection
    is established because the host is invalid."""
    ws = _make_source(db_session, normal_user, with_secret=True)
    response = client.post(f"/api/watch-sources/{ws.uuid}/test", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["success"] is False
    assert isinstance(body["message"], str) and body["message"]


# ---------------------------------------------------------------------------
# File history
# ---------------------------------------------------------------------------


def test_list_source_files_empty_envelope(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user)
    response = client.get(f"/api/watch-sources/{ws.uuid}/files", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 0
    assert body["files"] == []
    assert body["page"] == 1


def test_list_source_files_page_size_over_max_422(
    client, user_token_headers, normal_user, db_session
):
    ws = _make_source(db_session, normal_user)
    response = client.get(
        f"/api/watch-sources/{ws.uuid}/files",
        headers=user_token_headers,
        params={"page_size": 9999},
    )
    assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


def test_source_file_stats(client, user_token_headers, normal_user, db_session):
    ws = _make_source(db_session, normal_user)
    response = client.get(f"/api/watch-sources/{ws.uuid}/files/stats", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["total"] == 0
    assert body["imported"] == 0


# ---------------------------------------------------------------------------
# Folder browse validation
# ---------------------------------------------------------------------------


def test_browse_not_configured_404(client, user_token_headers):
    """With no WATCH_FOLDER_PATH configured (default test env), browse is 404."""
    from app.core.config import settings

    if settings.WATCH_FOLDER_PATH:
        import pytest

        pytest.skip("WATCH_FOLDER_PATH is configured in this environment")
    response = client.get("/api/watch-sources/browse", headers=user_token_headers)
    assert response.status_code == status.HTTP_404_NOT_FOUND
    assert response.json()["detail"] == "Local watch folder is not configured"


# ---------------------------------------------------------------------------
# Multipart regex test (pure, no DB)
# ---------------------------------------------------------------------------


def test_multipart_regex_match(client, user_token_headers):
    response = client.post(
        "/api/watch-sources/test-multipart-regex",
        headers=user_token_headers,
        json={"regex": r"(?P<base>.+)_P(?P<part>\d+)(?P<ext>\.\w+)$", "filename": "show_P001.mp4"},
    )
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    assert body["matched"] is True
    assert body["part_number"] == 1


def test_multipart_regex_no_match(client, user_token_headers):
    response = client.post(
        "/api/watch-sources/test-multipart-regex",
        headers=user_token_headers,
        json={"regex": r"(?P<base>.+)_P(?P<part>\d+)(?P<ext>\.\w+)$", "filename": "plain.mp4"},
    )
    assert response.status_code == status.HTTP_200_OK
    assert response.json()["matched"] is False


# ---------------------------------------------------------------------------
# Capabilities
# ---------------------------------------------------------------------------


def test_capabilities(client, user_token_headers):
    response = client.get("/api/watch-sources/capabilities", headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in ("watch_source_enabled", "local_enabled", "fs_events_enabled", "fs_events_mode"):
        assert key in body
    # The UI switches on this to explain which observer a source can get (#294).
    assert body["fs_events_mode"] in ("auto", "native", "polling", "off")


# ---------------------------------------------------------------------------
# Admin-only global settings + email configs
# ---------------------------------------------------------------------------


def test_global_settings_non_admin_403(client, user_token_headers):
    response = client.get("/api/watch-sources/settings", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_global_settings_admin_200(client, super_admin_token_headers):
    response = client.get("/api/watch-sources/settings", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    body = response.json()
    for key in (
        "enabled",
        "file_stability_seconds",
        "max_imports_per_scan",
        "fs_events_enabled",
        "fs_events_mode",
        "fs_events_poll_seconds",
    ):
        assert key in body
    assert body["fs_events_mode"] in ("auto", "native", "polling", "off")
    assert body["fs_events_poll_seconds"] >= 1


def test_list_email_configs_non_admin_403(client, user_token_headers):
    response = client.get("/api/watch-sources/email-configs", headers=user_token_headers)
    assert response.status_code == status.HTTP_403_FORBIDDEN


def test_list_email_configs_admin_200(client, super_admin_token_headers):
    response = client.get("/api/watch-sources/email-configs", headers=super_admin_token_headers)
    assert response.status_code == status.HTTP_200_OK
    assert "configs" in response.json()
