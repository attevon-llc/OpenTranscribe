"""Branch pins for the tenant and share gates of ``GET /api/files/{uuid}/thumbnail``.

The thumbnail route is optional-auth and resolves the caller's tenant itself
(``resolve_org_context``), so it decides tenancy in its own body rather than through the
permission helper the other file routes share. ``test_files_streaming.py`` pins the
unauthenticated, quarantine and plain cross-user branches; this file pins the two tenant
branches and the share branch:

* the DIRECT OWNER is still tenant-gated: an owner's org-stamped file is refused in
  personal scope, and an owner's personal file is refused in org scope;
* a NON-OWNER is allowed by a share only inside the caller's active tenant.

``thumbnail_path=None`` is the discriminator, as in ``test_files_streaming.py``: the
"Thumbnail not available for this file" 404 is reached only after every gate passed, so it
means "authorized", without any object in storage.
"""

from __future__ import annotations

import uuid

import pytest

from app.api import deps_context
from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.media import MediaFile
from app.models.organization import Organization
from app.models.sharing import CollectionShare

_AUTHORIZED = "Thumbnail not available for this file"
_DENIED = "Access denied to this file"


def _org(db_session) -> Organization:
    org = Organization(name=f"thumb-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    db_session.refresh(org)
    return org


def _file(db_session, owner, organization_id: int | None) -> MediaFile:
    file_uuid = uuid.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        user_id=owner.id,
        organization_id=organization_id,
        filename=f"thumb_{file_uuid.hex[:8]}.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=1024,
        status="completed",
        is_public=False,
        thumbnail_path=None,
    )
    db_session.add(media_file)
    db_session.commit()
    db_session.refresh(media_file)
    return media_file


def _share(db_session, media_file: MediaFile, sharer, recipient) -> None:
    collection = Collection(
        uuid=uuid.uuid4(),
        user_id=sharer.id,
        name=f"thumb-share-{uuid.uuid4().hex[:8]}",
        organization_id=media_file.organization_id,
    )
    db_session.add(collection)
    db_session.flush()
    db_session.add(
        CollectionMember(
            uuid=uuid.uuid4(), collection_id=collection.id, media_file_id=media_file.id
        )
    )
    db_session.add(
        CollectionShare(
            uuid=uuid.uuid4(),
            collection_id=collection.id,
            shared_by_id=sharer.id,
            target_type="user",
            target_user_id=recipient.id,
            permission="viewer",
        )
    )
    db_session.commit()


@pytest.fixture
def in_org(monkeypatch):
    """Put every thumbnail caller in ``org`` scope: the cloud token path, without a token."""

    def _enter(org: Organization) -> None:
        monkeypatch.setattr(
            deps_context, "resolve_org_context", lambda request, db, user: (org.id, "member")
        )

    return _enter


def _get(client, media_file: MediaFile, headers):
    return client.get(f"/api/files/{media_file.uuid}/thumbnail", headers=headers)


def test_an_owner_is_refused_their_org_file_in_personal_scope(
    client, user_token_headers, normal_user, db_session
):
    media_file = _file(db_session, normal_user, _org(db_session).id)
    response = _get(client, media_file, user_token_headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == _DENIED


def test_an_owner_is_refused_their_personal_file_in_org_scope(
    client, user_token_headers, normal_user, db_session, in_org
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, None)
    in_org(org)
    response = _get(client, media_file, user_token_headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == _DENIED


def test_an_owner_reads_their_org_file_in_that_org(
    client, user_token_headers, normal_user, db_session, in_org
):
    """The control for the two refusals above: the gate is on the scope, not the file."""
    org = _org(db_session)
    media_file = _file(db_session, normal_user, org.id)
    in_org(org)
    response = _get(client, media_file, user_token_headers)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == _AUTHORIZED


def test_a_share_authorizes_a_non_owner_inside_the_files_tenant(
    client, other_user_auth_headers, normal_user, other_user, db_session, in_org
):
    org = _org(db_session)
    media_file = _file(db_session, normal_user, org.id)
    _share(db_session, media_file, normal_user, other_user)
    in_org(org)
    response = _get(client, media_file, other_user_auth_headers)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == _AUTHORIZED


def test_a_share_does_not_authorize_from_another_tenant(
    client, other_user_auth_headers, normal_user, other_user, db_session, in_org
):
    media_file = _file(db_session, normal_user, _org(db_session).id)
    _share(db_session, media_file, normal_user, other_user)
    in_org(_org(db_session))
    response = _get(client, media_file, other_user_auth_headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == _DENIED


def test_a_personal_share_authorizes_a_non_owner_in_personal_scope(
    client, other_user_auth_headers, normal_user, other_user, db_session
):
    media_file = _file(db_session, normal_user, None)
    _share(db_session, media_file, normal_user, other_user)
    response = _get(client, media_file, other_user_auth_headers)
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == _AUTHORIZED
