"""Who may trash, restore, list and delete forever (issue #1101, plan section 3.6).

| Action | Personal scope | Organization scope | Platform role |
|---|---|---|---|
| Trash, restore | owner | owner, or ``org:admin`` of the file's org | write reach |
| List | own | own; ``org:admin`` the whole org trash | ``sees_all_in_scope`` |
| Delete forever, empty | owner | ``org:admin`` only | write reach |

Sharing reaches nothing in the trash: an editor share can neither trash (issue #1103) nor see,
restore or purge a trashed file. In multi-tenant mode a platform admin reaches a tenant's
trash only through a support grant, and only a WRITE grant restores or purges.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.enums import FileStatus
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.sharing import CollectionShare
from app.services import trash_service

TRASH = "/api/files/trash"


@pytest.fixture(autouse=True)
def _no_external_destroy(monkeypatch):
    monkeypatch.setattr(
        "app.services.file_cleanup_service.delete_file_storage_artifacts", lambda *_a, **_k: True
    )
    monkeypatch.setattr(
        "app.services.file_cleanup_service._cleanup_opensearch_for_file", lambda *_a: []
    )


def _org(db) -> Organization:
    org = Organization(name=f"trash-perm-{uuid.uuid4().hex[:8]}")
    db.add(org)
    db.commit()
    return org


def _file(db, owner, organization_id=None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"trash_perm_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status=FileStatus.COMPLETED,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _share(db, media_file, owner, grantee, permission="editor") -> None:
    collection = Collection(
        user_id=owner.id,
        organization_id=media_file.organization_id,
        name=f"shared-{uuid.uuid4().hex[:8]}",
    )
    db.add(collection)
    db.commit()
    db.add(CollectionMember(collection_id=collection.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=grantee.id,
            permission=permission,
        )
    )
    db.commit()


def _trashed(db, owner, organization_id=None) -> MediaFile:
    media_file = _file(db, owner, organization_id)
    trash_service.trash_files(db, [media_file], actor=owner, request=None, bypass=None)
    return media_file


def _listed(client, headers) -> set[str]:
    response = client.get(TRASH, headers=headers)
    assert response.status_code == 200, response.text
    return {item["uuid"] for item in response.json()["items"]}


def _is_trashed(db, media_file) -> bool:
    db.expire_all()
    row = db.get(MediaFile, media_file.id)
    return row is not None and row.deleted_at is not None


# --------------------------------------------------------------------------- personal scope


def test_personal_owner_may_do_everything(client, user_token_headers, normal_user, db_session):
    media_file = _file(db_session, normal_user)
    uuid_ = media_file.uuid

    assert client.delete(f"/api/files/{uuid_}", headers=user_token_headers).status_code == 204
    assert str(uuid_) in _listed(client, user_token_headers)
    assert client.post(f"/api/files/{uuid_}/restore", headers=user_token_headers).status_code == 200
    client.delete(f"/api/files/{uuid_}", headers=user_token_headers)
    assert client.delete(f"/api/files/{uuid_}/purge", headers=user_token_headers).status_code == 202


def test_an_editor_share_reaches_nothing_in_the_trash(
    client, other_user_auth_headers, other_user, normal_user, db_session
):
    live = _file(db_session, normal_user)
    _share(db_session, live, normal_user, other_user)
    trashed = _trashed(db_session, normal_user)
    _share(db_session, trashed, normal_user, other_user)

    assert (
        client.delete(f"/api/files/{live.uuid}", headers=other_user_auth_headers).status_code == 403
    )
    assert str(trashed.uuid) not in _listed(client, other_user_auth_headers)
    restore = client.post(f"/api/files/{trashed.uuid}/restore", headers=other_user_auth_headers)
    purge = client.delete(f"/api/files/{trashed.uuid}/purge", headers=other_user_auth_headers)
    assert (restore.status_code, purge.status_code) == (404, 404)
    assert _is_trashed(db_session, trashed)


def test_a_stranger_cannot_purge_a_live_file_by_guessing(
    client, other_user_auth_headers, normal_user, db_session
):
    """A live file the caller may not delete answers 404, not FILE_NOT_IN_TRASH."""
    media_file = _file(db_session, normal_user)

    response = client.delete(f"/api/files/{media_file.uuid}/purge", headers=other_user_auth_headers)

    assert response.status_code == 404


# --------------------------------------------------------------------------- organization scope


def test_an_org_member_trashes_and_restores_their_own_file_but_cannot_purge_it(
    client, user_token_headers, normal_user, db_session, org_context
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, org.id)
    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)

    assert (
        client.delete(f"/api/files/{media_file.uuid}", headers=user_token_headers).status_code
        == 204
    )
    assert str(media_file.uuid) in _listed(client, user_token_headers)
    purge = client.delete(f"/api/files/{media_file.uuid}/purge", headers=user_token_headers)
    assert purge.status_code == 403
    assert client.post("/api/files/trash/empty", headers=user_token_headers).status_code == 403
    assert _is_trashed(db_session, media_file), "a member's empty-trash purged org content"
    row = next(
        i
        for i in client.get(TRASH, headers=user_token_headers).json()["items"]
        if i["uuid"] == str(media_file.uuid)
    )
    assert (row["can_restore"], row["can_purge"]) == (True, False)
    restore = client.post(f"/api/files/{media_file.uuid}/restore", headers=user_token_headers)
    assert restore.status_code == 200


def test_an_org_member_does_not_see_another_members_trash(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org = _org(db_session)
    media_file = _trashed(db_session, normal_user, org.id)
    org_context(org_id=org.id, org_role="org:member", only_for=other_user.id)

    assert str(media_file.uuid) not in _listed(client, other_user_auth_headers)
    restore = client.post(f"/api/files/{media_file.uuid}/restore", headers=other_user_auth_headers)
    assert restore.status_code == 404


def test_an_org_admin_manages_the_whole_org_trash(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org = _org(db_session)
    to_restore = _file(db_session, normal_user, org.id)
    to_purge = _trashed(db_session, normal_user, org.id)
    to_empty = _trashed(db_session, normal_user, org.id)
    org_context(org_id=org.id, org_role="org:admin", only_for=other_user.id)

    trash = client.delete(f"/api/files/{to_restore.uuid}", headers=other_user_auth_headers)
    assert trash.status_code == 204
    assert {str(to_restore.uuid), str(to_purge.uuid), str(to_empty.uuid)} <= _listed(
        client, other_user_auth_headers
    )
    restore = client.post(f"/api/files/{to_restore.uuid}/restore", headers=other_user_auth_headers)
    assert restore.status_code == 200
    purge = client.delete(f"/api/files/{to_purge.uuid}/purge", headers=other_user_auth_headers)
    assert purge.status_code == 202
    empty = client.post("/api/files/trash/empty", headers=other_user_auth_headers)
    # to_purge is already due but not yet destroyed (no worker here), so it is queued again.
    assert (empty.status_code, empty.json()) == (202, {"queued": 2})


def test_an_org_admin_of_another_org_reaches_nothing(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org, other_org = _org(db_session), _org(db_session)
    live = _file(db_session, normal_user, org.id)
    trashed = _trashed(db_session, normal_user, org.id)
    org_context(org_id=other_org.id, org_role="org:admin", only_for=other_user.id)

    assert (
        client.delete(f"/api/files/{live.uuid}", headers=other_user_auth_headers).status_code == 403
    )
    assert str(trashed.uuid) not in _listed(client, other_user_auth_headers)
    restore = client.post(f"/api/files/{trashed.uuid}/restore", headers=other_user_auth_headers)
    purge = client.delete(f"/api/files/{trashed.uuid}/purge", headers=other_user_auth_headers)
    assert (restore.status_code, purge.status_code) == (404, 404)


def test_an_org_admin_cannot_reach_a_members_personal_trash(
    client, other_user_auth_headers, other_user, normal_user, db_session, org_context
):
    org = _org(db_session)
    personal = _trashed(db_session, normal_user, None)
    org_context(org_id=org.id, org_role="org:admin", only_for=other_user.id)

    restore = client.post(f"/api/files/{personal.uuid}/restore", headers=other_user_auth_headers)
    assert restore.status_code == 404


def test_the_bulk_trash_verbs_report_per_file(
    client, user_token_headers, normal_user, db_session, org_context
):
    org = _org(db_session)
    own_personal = _trashed(db_session, normal_user, None)
    own_org = _trashed(db_session, normal_user, org.id)

    personal = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"action": "purge", "file_uuids": [str(own_personal.uuid)]},
    )
    assert [r["success"] for r in personal.json()] == [True]

    org_context(org_id=org.id, org_role="org:member", only_for=normal_user.id)
    bulk = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"action": "purge", "file_uuids": [str(own_org.uuid), str(uuid.uuid4())]},
    )
    assert bulk.status_code == 200
    assert [(r["success"], r["error"]) for r in bulk.json()] == [
        (False, "FORBIDDEN"),
        (False, "NOT_FOUND"),
    ]
    restore = client.post(
        "/api/files/management/bulk-action",
        headers=user_token_headers,
        json={"action": "restore", "file_uuids": [str(own_org.uuid)]},
    )
    assert [r["success"] for r in restore.json()] == [True]


# --------------------------------------------------------------------------- platform roles


def test_a_single_tenant_platform_admin_reaches_every_trash(
    client, admin_token_headers, normal_user, db_session
):
    trashed = _trashed(db_session, normal_user)

    assert str(trashed.uuid) in _listed(client, admin_token_headers)
    restore = client.post(f"/api/files/{trashed.uuid}/restore", headers=admin_token_headers)
    assert restore.status_code == 200
    client.delete(f"/api/files/{trashed.uuid}", headers=admin_token_headers)
    purge = client.delete(f"/api/files/{trashed.uuid}/purge", headers=admin_token_headers)
    assert purge.status_code == 202


def test_a_multi_tenant_admin_without_a_grant_reaches_no_tenant_trash(support_world):
    w = support_world
    trash_service.trash_files(w.db, [w.file], actor=w.owner, request=None, bypass=None)

    assert str(w.file.uuid) not in _listed(w.client, w.admin_headers)
    restore = w.client.post(f"/api/files/{w.file.uuid}/restore", headers=w.admin_headers)
    purge = w.client.delete(f"/api/files/{w.file.uuid}/purge", headers=w.admin_headers)
    assert (restore.status_code, purge.status_code) == (404, 404)
    assert _is_trashed(w.db, w.file)


def test_a_read_grant_lists_the_tenant_trash_but_cannot_restore_or_purge(support_world):
    w = support_world
    trash_service.trash_files(w.db, [w.file], actor=w.owner, request=None, bypass=None)
    headers = w.with_grant(w.active_grant(level="read"))

    assert str(w.file.uuid) in _listed(w.client, headers)
    restore = w.client.post(f"/api/files/{w.file.uuid}/restore", headers=headers)
    purge = w.client.delete(f"/api/files/{w.file.uuid}/purge", headers=headers)
    assert (restore.status_code, purge.status_code) == (403, 403)
    assert _is_trashed(w.db, w.file)


def test_a_write_grant_restores_and_purges_inside_its_tenant_only(support_world):
    w = support_world
    for f in (w.file, w.other_org_file):
        trash_service.trash_files(w.db, [f], actor=w.owner, request=None, bypass=None)
    headers = w.with_grant(w.active_grant(level="write"))

    listed = _listed(w.client, headers)
    assert str(w.file.uuid) in listed
    assert str(w.other_org_file.uuid) not in listed
    other = w.client.post(f"/api/files/{w.other_org_file.uuid}/restore", headers=headers)
    assert other.status_code == 404
    restore = w.client.post(f"/api/files/{w.file.uuid}/restore", headers=headers)
    assert restore.status_code == 200, restore.text
    w.client.delete(f"/api/files/{w.file.uuid}", headers=headers)
    purge = w.client.delete(f"/api/files/{w.file.uuid}/purge", headers=headers)
    assert purge.status_code == 202, purge.text
