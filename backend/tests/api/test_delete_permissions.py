"""Permanent-delete permission matrix (issue #1103).

Delete is its own right, not "editor": the file's owner, an ``org:admin`` of the
file's organization, or a platform admin. Before the fix every delete path
resolved the file with ``min_permission="editor"``, so an editor share grant let
a non-owner destroy the file, and an org admin could not delete a member's file.

Covers the single route (``DELETE /api/files/{uuid}``) and the bulk route
(``POST /api/files/management/bulk-action`` with ``action=delete``), which must
report a per-file ``FORBIDDEN`` result instead of aborting the batch.
"""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.core.enums import FileStatus
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.sharing import CollectionShare

BULK = "/api/files/management/bulk-action"


def _org(db_session) -> Organization:
    org = Organization(name=f"delete-perm-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def _file(db_session, owner, organization_id: int | None = None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"delete_perm_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _share(db_session, media_file, owner, grantee, *, permission: str) -> None:
    collection = Collection(
        user_id=owner.id,
        organization_id=media_file.organization_id,
        name=f"shared-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(collection)
    db_session.commit()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db_session.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=grantee.id,
            permission=permission,
        )
    )
    db_session.commit()


def _exists(db_session, media_file) -> bool:
    db_session.expire_all()
    return db_session.get(MediaFile, media_file.id) is not None


def _bulk_delete(client, headers, *uuids):
    return client.post(
        BULK,
        headers=headers,
        json={"action": "delete", "file_uuids": [str(u) for u in uuids]},
    )


# ---------------------------------------------------------------------------
# Single delete
# ---------------------------------------------------------------------------


def test_owner_can_delete_own_file(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == 204
    assert not _exists(db_session, media_file)


def test_editor_share_cannot_delete(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    media_file = _file(db_session, normal_user)
    _share(db_session, media_file, normal_user, other_user, permission="editor")

    with patch("app.auth.audit.audit_logger.log") as audit:
        response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 403
    assert "owner" in response.json()["detail"]
    assert _exists(db_session, media_file)
    events = [c.args[0] for c in audit.call_args_list]
    assert "file.delete.denied" in events


def test_editor_share_keeps_edit_rights(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    """Losing delete must not cost the editor anything else."""
    media_file = _file(db_session, normal_user)
    _share(db_session, media_file, normal_user, other_user, permission="editor")

    response = client.put(
        f"/api/files/{media_file.uuid}",
        headers=other_user_auth_headers,
        json={"title": "edited by editor"},
    )

    assert response.status_code == 200


def test_org_member_cannot_delete_another_members_file(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=org.id)
    _share(db_session, media_file, normal_user, other_user, permission="editor")
    org_context(org_id=org.id, org_role="org:member", only_for=other_user.id)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 403
    assert _exists(db_session, media_file)


def test_org_member_can_delete_own_org_file(
    client, user_token_headers, normal_user, db_session, org_context
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=org.id)
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers)

    assert response.status_code == 204
    assert not _exists(db_session, media_file)


def test_org_admin_can_delete_members_file(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    """No share needed: an org admin manages the organization's files."""
    org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=org.id)
    org_context(org_id=org.id, org_role="org:admin", only_for=other_user.id)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 204
    assert not _exists(db_session, media_file)


def test_org_admin_of_other_org_cannot_delete(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org = _org(db_session)
    other_org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=org.id)
    org_context(org_id=other_org.id, org_role="org:admin", only_for=other_user.id)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 403
    assert _exists(db_session, media_file)


def test_org_admin_cannot_delete_personal_file(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    """Personal (org-less) scope: owner only, even for an org admin with a share."""
    org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=None)
    _share(db_session, media_file, normal_user, other_user, permission="editor")
    org_context(org_id=org.id, org_role="org:admin", only_for=other_user.id)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=other_user_auth_headers)

    assert response.status_code == 403
    assert _exists(db_session, media_file)


def test_platform_admin_can_delete(client, admin_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)

    response = client.delete(f"/api/files/{media_file.uuid}", headers=admin_token_headers)

    assert response.status_code == 204
    assert not _exists(db_session, media_file)


# ---------------------------------------------------------------------------
# Bulk delete
# ---------------------------------------------------------------------------


def test_bulk_delete_reports_forbidden_per_file(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    """Mixed batch: own file deleted, editor-shared file refused, batch not aborted."""
    own = _file(db_session, other_user)
    shared = _file(db_session, normal_user)
    _share(db_session, shared, normal_user, other_user, permission="editor")

    response = _bulk_delete(client, other_user_auth_headers, shared.uuid, own.uuid)

    assert response.status_code == 200
    results = {r["file_uuid"]: r for r in response.json()}
    assert results[str(shared.uuid)]["success"] is False
    assert results[str(shared.uuid)]["error"] == "FORBIDDEN"
    assert results[str(own.uuid)]["success"] is True
    assert _exists(db_session, shared)
    assert not _exists(db_session, own)


def test_bulk_delete_org_admin_can_delete_members_file(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=org.id)
    org_context(org_id=org.id, org_role="org:admin", only_for=other_user.id)

    response = _bulk_delete(client, other_user_auth_headers, media_file.uuid)

    assert response.status_code == 200
    assert response.json()[0]["success"] is True, response.json()
    assert not _exists(db_session, media_file)


@pytest.mark.parametrize("org_role", ["org:member", None])
def test_bulk_delete_non_admin_member_forbidden(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context, org_role
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, organization_id=org.id)
    _share(db_session, media_file, normal_user, other_user, permission="editor")
    org_context(org_id=org.id, org_role=org_role, only_for=other_user.id)

    response = _bulk_delete(client, other_user_auth_headers, media_file.uuid)

    assert response.status_code == 200
    assert response.json()[0]["success"] is False
    assert response.json()[0]["error"] == "FORBIDDEN"
    assert _exists(db_session, media_file)


# ---------------------------------------------------------------------------
# Gallery hint
# ---------------------------------------------------------------------------


def test_gallery_list_reports_can_delete(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    own = _file(db_session, other_user)
    shared = _file(db_session, normal_user)
    _share(db_session, shared, normal_user, other_user, permission="editor")

    response = client.get("/api/files?ownership=all&page_size=100", headers=other_user_auth_headers)

    assert response.status_code == 200
    by_uuid = {item["uuid"]: item for item in response.json()["items"]}
    assert by_uuid[str(own.uuid)]["can_delete"] is True
    assert by_uuid[str(shared.uuid)]["can_delete"] is False
