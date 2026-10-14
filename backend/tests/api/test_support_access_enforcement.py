"""What a support-access grant lets a request do, and what it never does (issue #1122).

A grant is carried per request in ``X-Support-Access-Grant``. It is re-read every time,
bound to its grantee, scoped to one tenant, limited to its access level, recorded in the
use log before anything is served, and refused outright on the surfaces that create
content, feed an LLM or leave the system as a file.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.models.support_access import SupportAccessUse
from app.services.platform_access import reset_tenancy_mode_cache
from app.services.platform_bypass import SupportGrantView
from app.services.platform_bypass import build_bypass
from app.services.redaction.config import EffectiveRedactionConfig


def _code(response) -> str:
    return str(response.json()["detail"]["code"])


def _uses(w, grant_uuid: str) -> list[SupportAccessUse]:
    row = w.row(grant_uuid)
    rows: list[SupportAccessUse] = (
        w.db.query(SupportAccessUse)
        .filter(SupportAccessUse.grant_id == row.id)
        .order_by(SupportAccessUse.id)
        .all()
    )
    return rows


# ---------------------------------------------------------------------------
# A grant must be active, the caller's own, and for that tenant
# ---------------------------------------------------------------------------


def test_a_pending_grant_does_not_authorize(support_world):
    w = support_world
    grant = w.request()
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant["uuid"]))
    assert response.status_code == 403
    assert _code(response) == "support_grant_not_active"


def test_without_a_grant_the_admin_still_cannot_read_the_tenants_file(support_world):
    """The control: the same request minus the header is refused."""
    w = support_world
    assert w.client.get(f"/api/files/{w.file.uuid}", headers=w.admin_headers).status_code == 404


def test_an_approved_grant_reads_the_file_and_records_the_use(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    with patch("app.auth.audit.audit_logger.log") as audit:
        response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    assert response.status_code == 200
    assert response.json()["uuid"] == str(w.file.uuid)

    uses = _uses(w, grant_uuid)
    request_level = [u for u in uses if u.resource_type is None]
    resource_level = [u for u in uses if u.resource_type == "media_file"]
    assert len(request_level) == 1
    assert request_level[0].method == "GET"
    assert request_level[0].route == "/api/files/{file_uuid}"
    assert len(resource_level) == 1
    assert str(resource_level[0].resource_uuid) == str(w.file.uuid)
    assert resource_level[0].organization_id == w.org.id
    assert resource_level[0].owner_user_id == w.owner.id
    assert resource_level[0].need == "read"

    used = [c for c in audit.call_args_list if c.args[0] == "support_access.used"]
    assert len(used) == 2
    assert w.file.filename not in str(used)


def test_the_use_log_holds_ids_and_never_a_filename(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    listing = w.client.get(
        f"/api/org-admin/support-access/{grant_uuid}/uses", headers=w.org_admin_headers
    )
    assert listing.status_code == 200
    assert listing.json()["items"]
    assert w.file.filename not in listing.text


def test_a_grant_does_not_cross_to_another_org(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    response = w.client.get(
        f"/api/files/{w.other_org_file.uuid}/stream-url", headers=w.with_grant(grant_uuid)
    )
    assert response.status_code == 403
    assert "support_grant" not in response.text
    assert not [u for u in _uses(w, grant_uuid) if u.resource_type == "media_file"]


def test_a_grant_for_one_tenant_does_not_reach_a_personal_workspace(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    response = w.client.get(
        f"/api/files/{w.personal_file.uuid}/stream-url", headers=w.with_grant(grant_uuid)
    )
    assert response.status_code == 403


def test_a_personal_grant_reaches_only_that_persons_workspace(support_world):
    w = support_world
    grant = w.request(subject_user_uuid=str(w.owner.uuid))
    w.client.post(
        f"/api/users/me/support-access/{grant['uuid']}/approve", headers=w.owner_headers, json={}
    )
    headers = w.with_grant(grant["uuid"])
    assert w.client.get(f"/api/files/{w.personal_file.uuid}", headers=headers).status_code == 200
    assert w.client.get(f"/api/files/{w.file.uuid}/stream-url", headers=headers).status_code == 403


def test_the_grant_uuid_of_another_admin_is_refused(support_world):
    from tests.fixtures.support_world import login_headers
    from tests.fixtures.support_world import make_user

    w = support_world
    grant_uuid = w.active_grant()
    thief = make_user(w.db, role="admin", label="thief")
    response = w.client.get(
        f"/api/files/{w.file.uuid}",
        headers=w.with_grant(grant_uuid, login_headers(w.client, thief)),
    )
    assert response.status_code == 403
    assert _code(response) == "support_grant_invalid"


@pytest.mark.parametrize("value", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_an_unknown_grant_is_invalid(support_world, value):
    w = support_world
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(value))
    assert response.status_code == 403
    assert _code(response) == "support_grant_invalid"


def test_an_expired_grant_is_refused_with_its_code(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.set_times(grant_uuid, starts_at=-2, expires_at=-1)
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    assert response.status_code == 403
    assert _code(response) == "support_grant_expired"


def test_a_revoked_grant_is_refused_with_its_code(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.client.post(f"/api/support-access/grants/{grant_uuid}/revoke", headers=w.org_admin_headers)
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    assert response.status_code == 403
    assert _code(response) == "support_grant_revoked"


def test_a_demoted_admins_grant_stops_working(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.admin.role = "user"
    w.db.commit()
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    assert response.status_code == 403
    assert _code(response) == "support_grant_invalid"


def test_a_deactivated_target_organization_kills_the_grant(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.org.is_active = False
    w.db.commit()
    response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    assert response.status_code == 403
    assert _code(response) == "support_grant_invalid"


def test_the_header_in_single_tenant_mode_is_a_400(client, admin_token_headers, monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "community")
    reset_tenancy_mode_cache()
    try:
        response = client.get(
            "/api/files",
            headers={
                **admin_token_headers,
                "X-Support-Access-Grant": "00000000-0000-0000-0000-000000000000",
            },
        )
    finally:
        reset_tenancy_mode_cache()
    assert response.status_code == 400
    assert _code(response) == "support_access_unavailable"


# ---------------------------------------------------------------------------
# Access level
# ---------------------------------------------------------------------------


def test_a_read_grant_cannot_write_or_delete(support_world):
    w = support_world
    grant_uuid = w.active_grant(level="read")
    headers = w.with_grant(grant_uuid)
    put = w.client.put(f"/api/files/{w.file.uuid}", headers=headers, json={"title": "edited"})
    delete = w.client.delete(f"/api/files/{w.file.uuid}", headers=headers)
    # a route that never asks the bypass anything: the method alone is refused
    tag = w.client.post("/api/tags", headers=headers, json={"name": "sa-tag"})
    for response in (put, delete, tag):
        assert response.status_code == 403
        assert _code(response) == "support_grant_write_required"
    w.db.refresh(w.file)
    assert w.file.title != "edited"


def test_a_write_grant_can_edit_inside_its_tenant_only(support_world):
    w = support_world
    grant_uuid = w.active_grant(level="write")
    headers = w.with_grant(grant_uuid)
    ok = w.client.put(
        f"/api/files/{w.file.uuid}", headers=headers, json={"title": "fixed-by-support"}
    )
    assert ok.status_code == 200
    w.db.refresh(w.file)
    assert w.file.title == "fixed-by-support"
    assert [u.need for u in _uses(w, grant_uuid) if u.resource_type == "media_file"] == ["write"]

    other = w.client.put(
        f"/api/files/{w.other_org_file.uuid}", headers=headers, json={"title": "x"}
    )
    assert other.status_code == 403
    assert "support_grant" not in other.text


# ---------------------------------------------------------------------------
# Surfaces a grant never reaches
# ---------------------------------------------------------------------------

_REFUSED: list[tuple[str, str, dict[str, Any] | None]] = [
    ("get", "/api/search", None),
    ("post", "/api/chat/conversations", {}),
    ("post", "/api/files/prepare", {}),
    ("post", "/api/files/complete", {}),
    ("post", "/api/files/process-url", {}),
    ("post", "/api/collections", {}),
    ("post", "/api/tags", {}),
    ("post", "/api/watch-sources", {}),
    ("get", "/api/files/{file}/export", None),
    ("get", "/api/files/{file}/subtitles", None),
    ("post", "/api/files/{file}/prepare-download", {}),
    ("post", "/api/files/bulk-export/prepare", {}),
    ("get", "/api/files/{file}/summary/export", None),
    ("get", "/api/admin/stats", None),
]


@pytest.mark.parametrize(
    ("method", "path", "body"), _REFUSED, ids=[f"{m} {p}" for m, p, _ in _REFUSED]
)
def test_a_grant_is_refused_on_chat_search_upload_export_and_admin(
    support_world, method, path, body
):
    w = support_world
    headers = w.with_grant(w.active_grant(level="write"))
    kwargs = {"json": body} if body is not None else {}
    response = getattr(w.client, method)(
        path.replace("{file}", str(w.file.uuid)), headers=headers, **kwargs
    )
    assert response.status_code == 403
    assert _code(response) == "support_grant_action_not_permitted"


def test_the_refused_attempt_is_still_recorded(support_world):
    w = support_world
    grant_uuid = w.active_grant(level="write")
    w.client.get("/api/search", headers=w.with_grant(grant_uuid))
    assert any(u.route == "/api/search" for u in _uses(w, grant_uuid))


# ---------------------------------------------------------------------------
# Stale or unrelated headers must never block the lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_routes_ignore_a_stale_header(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.set_times(grant_uuid, starts_at=-2, expires_at=-1)
    stale = w.with_grant(grant_uuid)
    assert w.client.get("/api/auth/session", headers=stale).status_code == 200
    assert w.client.get("/api/system/capabilities", headers=stale).status_code == 200
    assert (
        w.client.get(f"/api/support-access/grants/{grant_uuid}", headers=stale).status_code == 200
    )
    revoked = w.client.post(f"/api/support-access/grants/{grant_uuid}/revoke", headers=stale)
    assert revoked.status_code == 200


# ---------------------------------------------------------------------------
# Fail closed, and bearer tokens
# ---------------------------------------------------------------------------


def test_when_the_use_row_cannot_be_written_nothing_is_served(support_world):
    w = support_world
    grant_uuid = w.active_grant()

    def _broken():
        raise RuntimeError("database unavailable")

    with patch("app.db.session_utils.session_scope", side_effect=_broken):
        response = w.client.get(f"/api/files/{w.file.uuid}", headers=w.with_grant(grant_uuid))
    assert response.status_code == 503
    assert _code(response) == "support_access_audit_unavailable"
    assert w.file.filename not in response.text


def test_presigned_urls_are_capped_under_a_grant(support_world):
    w = support_world
    grant_uuid = w.active_grant()
    w.roles[w.owner.id] = (w.org.id, "org:member")
    url = f"/api/files/{w.file.uuid}/stream-url"

    under = w.client.get(url, headers=w.with_grant(grant_uuid), params={"media_type": "video"})
    owner = w.client.get(url, headers=w.owner_headers, params={"media_type": "video"})

    assert under.status_code == owner.status_code == 200
    assert 60 <= under.json()["expires_in"] <= 300
    assert owner.json()["expires_in"] == settings.MEDIA_URL_EXPIRE_SECONDS
    assert owner.json()["expires_in"] > 300


def test_redaction_is_resolved_for_the_file_owner_under_a_grant(support_world):
    """Support sees what the tenant's policy shows, not the staff member's looser one."""
    from app.api.endpoints.files.crud import _resolve_redaction_for_request

    w = support_world
    grant = SupportGrantView(
        id=1,
        uuid="00000000-0000-0000-0000-000000000001",
        organization_id=w.org.id,
        subject_user_id=None,
        access_level="read",
        grant_mode="approved",
        expires_at=w.row(w.active_grant()).expires_at,
    )
    bypass = build_bypass(w.db, w.admin, w.org.id, grant=grant)

    def _policy_of(_db, user_id, organization_id=None):
        # the owner's tenant masks PII; the staff member's own preferences mask nothing
        if user_id == w.owner.id and organization_id == w.org.id:
            return EffectiveRedactionConfig(enabled=True, enabled_categories={"pii"})
        return EffectiveRedactionConfig(enabled=False)

    with patch("app.services.redaction.config.resolve_effective_config", side_effect=_policy_of):
        cfg, reveal = _resolve_redaction_for_request(
            w.db, w.file, w.admin, bypass=bypass, redact=False, organization_id=w.org.id
        )

    assert cfg.enabled is True
    assert cfg.enabled_categories == {"pii"}
    assert reveal == set()

    # the control: without the grant the staff member's own policy applies
    plain = build_bypass(w.db, w.admin, w.org.id)
    with patch("app.services.redaction.config.resolve_effective_config", side_effect=_policy_of):
        own_cfg, _ = _resolve_redaction_for_request(
            w.db, w.file, w.admin, bypass=plain, redact=False, organization_id=w.org.id
        )
    assert own_cfg.enabled is False
