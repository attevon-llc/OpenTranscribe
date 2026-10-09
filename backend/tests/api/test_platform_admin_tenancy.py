"""A platform admin holds no implicit content access in a multi-tenant deployment (#1122).

``User.is_admin`` used to be consulted in front of every tenant gate, so one admin
credential read, edited and deleted any tenant's content by UUID. In multi-tenant mode
(any active ``Organization`` row, under ``TESTING`` where the mode cache is bypassed) the
admin's content rights are now whatever their membership gives them, exactly like anyone
else. Single-tenant (community) behaviour is pinned by the control tests at the end.

Status codes pin each helper's existing answer: file and collection helpers answer 403 for
an out-of-tenant row, speaker and profile helpers 404 (plan decision D4).
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from app.core.config import settings
from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import SpeakerProfile
from app.models.media import Task
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.services.platform_access import reset_tenancy_mode_cache


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setattr(settings, "TENANCY_MODE", "auto")
    monkeypatch.setattr(settings, "DEPLOYMENT_EDITION", "community")
    reset_tenancy_mode_cache()
    yield
    reset_tenancy_mode_cache()


def _org(db_session) -> Organization:
    org = Organization(name=f"padmin-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def _file(db_session, owner, organization_id: int | None = None, **kwargs) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"padmin_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
        **kwargs,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _exists(db_session, media_file) -> bool:
    db_session.expire_all()
    return db_session.get(MediaFile, media_file.id) is not None


@pytest.fixture
def org_file(db_session, normal_user):
    """A file owned by ``normal_user`` inside a freshly created organization."""
    org = _org(db_session)
    return _file(db_session, normal_user, organization_id=org.id)


@pytest.fixture
def personal_file(db_session, normal_user):
    """``normal_user``'s personal file, in a deployment that has an organization (MULTI)."""
    _org(db_session)
    return _file(db_session, normal_user)


# ---------------------------------------------------------------------------
# Reads, by the three routes that used to reach the tenant gate differently
# ---------------------------------------------------------------------------


def test_admin_cannot_read_other_org_file_without_grant(client, admin_token_headers, org_file):
    """The detail route answers a file the caller may not see like a missing one (404)."""
    response = client.get(f"/api/files/{org_file.uuid}", headers=admin_token_headers)
    assert response.status_code == 404


def test_admin_cannot_read_other_users_personal_file_in_multi_mode(
    client, admin_token_headers, personal_file
):
    response = client.get(f"/api/files/{personal_file.uuid}", headers=admin_token_headers)
    assert response.status_code == 404


def test_admin_stream_url_is_tenant_gated(client, admin_token_headers, org_file):
    """``/stream-url`` went through a bare lookup that never reached the tenant gate."""
    response = client.get(f"/api/files/{org_file.uuid}/stream-url", headers=admin_token_headers)
    assert response.status_code == 403


def test_admin_thumbnail_is_tenant_gated(client, admin_token_headers, org_file):
    """The thumbnail route resolves org scope itself and skipped both tenant branches."""
    response = client.get(f"/api/files/{org_file.uuid}/thumbnail", headers=admin_token_headers)
    assert response.status_code == 403


def test_owner_still_reads_their_own_file_in_multi_mode(client, user_token_headers, personal_file):
    """The control that keeps the tests above honest: the gate is on the admin, not the file."""
    response = client.get(f"/api/files/{personal_file.uuid}", headers=user_token_headers)
    assert response.status_code == 200


def test_admin_file_list_excludes_other_personal_workspaces(
    client, admin_token_headers, personal_file
):
    response = client.get("/api/files", headers=admin_token_headers)
    assert response.status_code == 200
    assert str(personal_file.uuid) not in response.text


def test_admin_owner_facet_excludes_other_users_in_multi_mode(
    client, admin_token_headers, normal_user, personal_file
):
    response = client.get("/api/files/owners", headers=admin_token_headers)
    assert response.status_code == 200
    assert str(normal_user.uuid) not in response.text


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------


def test_admin_cannot_delete_other_tenant_file(client, admin_token_headers, db_session, org_file):
    response = client.delete(f"/api/files/{org_file.uuid}", headers=admin_token_headers)
    assert response.status_code == 403
    assert _exists(db_session, org_file)


def test_admin_cannot_update_other_tenant_file(client, admin_token_headers, db_session, org_file):
    response = client.put(
        f"/api/files/{org_file.uuid}", headers=admin_token_headers, json={"title": "hijacked"}
    )
    assert response.status_code == 403
    db_session.refresh(org_file)
    assert org_file.title != "hijacked"


def test_admin_reprocess_other_tenant_refused(client, admin_token_headers, org_file):
    """Reprocess dispatches up to eight Celery tasks, LLM-backed ones included."""
    with (
        patch("app.api.endpoints.files.reprocess.dispatch_selective_tasks") as dispatch,
        patch("app.api.endpoints.files.reprocess.clear_selective_data") as clear,
    ):
        response = client.post(
            f"/api/files/{org_file.uuid}/reprocess",
            headers=admin_token_headers,
            json={"stages": ["summarization"]},
        )
    assert response.status_code == 403
    dispatch.assert_not_called()
    clear.assert_not_called()


# ---------------------------------------------------------------------------
# The reveal of unredacted PII is owner-only in multi-tenant mode
# ---------------------------------------------------------------------------


def _fake_cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.export_locked = False
    cfg.reveal_categories.side_effect = lambda requested, is_owner: (
        {"pii"} if (requested and is_owner) else set()
    )
    return cfg


def _reveal_via_detail(db_session, media_file, user, bypass):
    from app.api.endpoints.files.crud import _resolve_redaction_for_request

    return _resolve_redaction_for_request(
        db_session, media_file, user, bypass=bypass, redact=False
    )[1]


def _reveal_via_subtitles(db_session, media_file, user, bypass):
    from app.api.endpoints.files.subtitles import _resolve_subtitle_redaction

    return _resolve_subtitle_redaction(db_session, media_file, user, False, bypass=bypass)[1]


def _reveal_via_export(db_session, media_file, user, bypass):
    from app.api.endpoints.files.transcript_export import _resolve_export_redaction

    return _resolve_export_redaction(db_session, media_file, user, False, bypass=bypass)[1]


@pytest.mark.parametrize(
    "reveal",
    [_reveal_via_detail, _reveal_via_subtitles, _reveal_via_export],
    ids=lambda f: f.__name__,
)
def test_admin_cannot_reveal_unredacted_other_tenant(db_session, admin_user, org_file, reveal):
    from app.services.platform_bypass import build_bypass

    bypass = build_bypass(db_session, admin_user, None)
    with patch("app.services.redaction.config.resolve_effective_config", return_value=_fake_cfg()):
        assert reveal(db_session, org_file, admin_user, bypass) == set()


@pytest.mark.parametrize(
    "reveal",
    [_reveal_via_detail, _reveal_via_subtitles, _reveal_via_export],
    ids=lambda f: f.__name__,
)
def test_owner_can_still_reveal_in_multi_mode(db_session, normal_user, org_file, reveal):
    from app.services.platform_bypass import build_bypass

    bypass = build_bypass(db_session, normal_user, None)
    with patch("app.services.redaction.config.resolve_effective_config", return_value=_fake_cfg()):
        assert reveal(db_session, org_file, normal_user, bypass) == {"pii"}


# ---------------------------------------------------------------------------
# Lists, tasks and watch sources that ignored the tenant for an admin
# ---------------------------------------------------------------------------


def test_admin_profile_list_excludes_other_tenants(
    client, admin_token_headers, db_session, normal_user
):
    org = _org(db_session)
    names = {}
    for label, org_id in (("org", org.id), ("personal", None)):
        profile = SpeakerProfile(
            user_id=normal_user.id,
            organization_id=org_id,
            name=f"padmin-{label}-{uuid.uuid4().hex[:6]}",
        )
        db_session.add(profile)
        names[label] = profile.name
    db_session.commit()

    response = client.get("/api/speaker-profiles/profiles", headers=admin_token_headers)

    assert response.status_code == 200
    assert names["org"] not in response.text
    assert names["personal"] not in response.text


def _task_for(db_session, media_file) -> Task:
    task = Task(
        id=f"padmin-{uuid.uuid4().hex}",
        user_id=media_file.user_id,
        media_file_id=media_file.id,
        task_type="transcription",
        status="in_progress",
    )
    db_session.add(task)
    db_session.commit()
    return task


def test_admin_task_lookup_is_tenant_scoped(client, admin_token_headers, db_session, org_file):
    """Another tenant's task answers exactly like an id that does not exist."""
    task = _task_for(db_session, org_file)
    response = client.get(f"/api/tasks/{task.id}", headers=admin_token_headers)
    unknown = client.get(f"/api/tasks/padmin-{uuid.uuid4().hex}", headers=admin_token_headers)
    assert response.status_code == unknown.status_code == 400
    assert task.id not in response.text


def test_admin_task_retry_is_tenant_scoped(client, admin_token_headers, db_session, org_file):
    org_file.status = FileStatus.ERROR
    db_session.commit()
    response = client.post(f"/api/tasks/retry/{org_file.uuid}", headers=admin_token_headers)
    assert response.status_code == 403
    db_session.refresh(org_file)
    assert org_file.status == FileStatus.ERROR


def test_watch_source_assign_to_user_of_other_tenant_refused(
    client,
    admin_token_headers,
    admin_user,
    db_session,
    normal_user,
    org_context,
    monkeypatch,
    tmp_path,
):
    """An admin could assign a source to ANY user with no membership check."""
    monkeypatch.setattr(settings, "WATCH_FOLDER_PATH", str(tmp_path))
    org = _org(db_session)
    db_session.add(OrganizationMembership(organization_id=org.id, user_id=admin_user.id))
    db_session.commit()
    org_context(org_id=org.id, org_role="org:member", only_for=admin_user.id)

    response = client.post(
        "/api/watch-sources",
        headers=admin_token_headers,
        json={
            "name": "assign-test",
            "source_type": "local",
            "local_path": "inbox",
            "assign_to_user_uuid": str(normal_user.uuid),
        },
    )

    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Community control: with no organization rows nothing about the admin changes
# ---------------------------------------------------------------------------


def test_community_admin_bypass_unchanged(client, admin_token_headers, db_session, normal_user):
    """Green before and after: a single-tenant admin keeps instance-wide access."""
    media_file = _file(db_session, normal_user)

    assert (
        client.get(f"/api/files/{media_file.uuid}", headers=admin_token_headers).status_code == 200
    )
    listing = client.get("/api/files", headers=admin_token_headers)
    assert listing.status_code == 200
    assert str(media_file.uuid) in listing.text
    deleted = client.delete(f"/api/files/{media_file.uuid}", headers=admin_token_headers)
    assert deleted.status_code == 204
    assert not _exists(db_session, media_file)


def _metadata_events(audit) -> list:
    return [c for c in audit.call_args_list if c.args[0] == "platform_admin.metadata.access"]


def test_quarantine_list_is_audited_in_multi_mode(client, admin_token_headers, org_file):
    """Platform routes that list other tenants' filenames leave a trace (plan 3.3, D3)."""
    with patch("app.auth.audit.audit_logger.log") as audit:
        response = client.get("/api/admin/files/quarantined", headers=admin_token_headers)
    assert response.status_code == 200
    events = _metadata_events(audit)
    assert len(events) == 1
    assert events[0].kwargs["details"]["route"] == "/api/admin/files/quarantined"
    assert org_file.filename not in str(events[0])


def test_retention_preview_is_audited_in_multi_mode(client, admin_token_headers, org_file):
    with patch("app.auth.audit.audit_logger.log") as audit:
        response = client.get(
            "/api/admin/settings/retention-config/preview",
            headers=admin_token_headers,
            params={"retention_days": 1},
        )
    assert response.status_code == 200
    events = _metadata_events(audit)
    assert len(events) == 1
    assert events[0].kwargs["details"]["route"] == "/api/admin/settings/retention-config/preview"


def test_community_platform_listings_emit_no_new_audit_event(
    client, admin_token_headers, db_session, normal_user
):
    _file(db_session, normal_user)
    with patch("app.auth.audit.audit_logger.log") as audit:
        quarantine = client.get("/api/admin/files/quarantined", headers=admin_token_headers)
        retention = client.get(
            "/api/admin/settings/retention-config/preview",
            headers=admin_token_headers,
            params={"retention_days": 1},
        )
    assert quarantine.status_code == retention.status_code == 200
    assert _metadata_events(audit) == []


def test_community_admin_sees_other_users_task_and_profiles(
    client, admin_token_headers, db_session, normal_user
):
    media_file = _file(db_session, normal_user)
    task = _task_for(db_session, media_file)
    profile = SpeakerProfile(user_id=normal_user.id, name=f"padmin-c-{uuid.uuid4().hex[:6]}")
    db_session.add(profile)
    db_session.commit()

    assert client.get(f"/api/tasks/{task.id}", headers=admin_token_headers).status_code == 200
    profiles = client.get("/api/speaker-profiles/profiles", headers=admin_token_headers)
    assert profiles.status_code == 200
    assert profile.name in profiles.text


def test_forced_multi_removes_the_bypass_without_any_organization(
    client, admin_token_headers, db_session, normal_user, monkeypatch
):
    """TENANCY_MODE=multi on a community install: personal workspaces become tenants."""
    monkeypatch.setattr(settings, "TENANCY_MODE", "multi")
    media_file = _file(db_session, normal_user)
    response = client.get(f"/api/files/{media_file.uuid}", headers=admin_token_headers)
    assert response.status_code == 404
