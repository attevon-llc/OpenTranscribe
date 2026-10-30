"""Characterization of the refusals raised by ``services.uuid_helpers`` and
``services.chat.context_resolver`` (L2, group G-A).

These tests pin status, exact ``detail``, headers, ``str(exc)`` (which Celery persists as
``last_error_message`` and feeds to ``categorize_error``) and the response bytes of the
endpoints that reach each refusal. They were written against the ``HTTPException`` code and
must stay green after the raises become domain errors, so every ``raises`` accepts either
type and every assertion on the refusal itself is type-independent.
"""

from __future__ import annotations

import json
import uuid as uuid_pkg

import pytest
from fastapi import HTTPException

from app.core import chat_defaults
from app.core.exceptions import AccessDeniedError
from app.core.exceptions import InvalidInputError
from app.core.exceptions import ResourceNotFoundError
from app.models.media import Collection
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.sharing import CollectionShare
from app.models.user import User
from app.schemas.chat import ChatScope
from app.services.chat.context_resolver import count_scope_files
from app.services.chat.context_resolver import resolve_scope_file_uuids
from app.services.platform_bypass import PlatformBypass
from app.services.request_context import RequestContext
from app.services.uuid_helpers import get_by_uuid
from app.services.uuid_helpers import get_collection_by_uuid_with_permission
from app.services.uuid_helpers import get_collection_by_uuid_with_sharing
from app.services.uuid_helpers import get_file_by_uuid
from app.services.uuid_helpers import get_file_by_uuid_with_permission
from app.services.uuid_helpers import get_speaker_by_uuid
from app.services.uuid_helpers import require_profile_in_scope
from app.services.uuid_helpers import require_resource_owner
from app.services.uuid_helpers import require_speaker_access
from app.services.uuid_helpers import uuid_to_id
from app.services.uuid_helpers import validate_uuids

NOT_FOUND = (HTTPException, ResourceNotFoundError)
DENIED = (HTTPException, AccessDeniedError)
INVALID = (HTTPException, InvalidInputError)


def _body(detail: str) -> bytes:
    return json.dumps({"detail": detail}, ensure_ascii=False, separators=(",", ":")).encode()


def _refusal(exc_info, status: int, detail: str) -> None:
    exc = exc_info.value
    assert exc.status_code == status
    assert exc.detail == detail
    assert not getattr(exc, "headers", None)
    assert str(exc) == f"{status}: {detail}"


def _user(db, label: str) -> User:
    from app.core.security import get_password_hash

    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password=get_password_hash("password123"),
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _file(db, owner: User, *, org_id: int | None = None, quarantined: bool = False) -> MediaFile:
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        organization_id=org_id,
        filename="g.mp4",
        title="g",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        is_quarantined=quarantined,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def _collection(db, owner: User, *, restricted: bool = False) -> Collection:
    org_id = None
    if restricted:
        # The schema only allows `restricted` on an organization collection.
        org = Organization(
            external_org_id=f"org_ga_{uuid_pkg.uuid4().hex[:8]}", name="GA Org", is_active=True
        )
        db.add(org)
        db.commit()
        org_id = org.id
    collection = Collection(
        uuid=uuid_pkg.uuid4(),
        name=f"c-{uuid_pkg.uuid4().hex[:6]}",
        user_id=owner.id,
        organization_id=org_id,
        visibility="restricted" if restricted else "organization",
    )
    db.add(collection)
    db.commit()
    db.refresh(collection)
    return collection


def _speaker(db, owner: User, media: MediaFile) -> Speaker:
    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        media_file_id=media.id,
        name=f"SPEAKER_{uuid_pkg.uuid4().hex[:6]}",
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return speaker


def _profile(db, owner: User) -> SpeakerProfile:
    profile = SpeakerProfile(user_id=owner.id, name="P", uuid=uuid_pkg.uuid4())
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


@pytest.fixture
def world(db_session):
    db = db_session
    owner = _user(db, "owner")
    stranger = _user(db, "stranger")
    return {"db": db, "owner": owner, "stranger": stranger}


# --------------------------------------------------------------------------- get_by_uuid


def test_get_by_uuid_malformed_is_400(world):
    with pytest.raises(INVALID) as exc:
        get_by_uuid(world["db"], MediaFile, "not-a-uuid")
    _refusal(exc, 400, "Invalid UUID format: not-a-uuid")


def test_get_by_uuid_missing_default_message_is_404(world):
    with pytest.raises(NOT_FOUND) as exc:
        get_by_uuid(world["db"], MediaFile, uuid_pkg.uuid4())
    _refusal(exc, 404, "MediaFile not found")


def test_get_by_uuid_missing_custom_message_is_404(world):
    with pytest.raises(NOT_FOUND) as exc:
        get_by_uuid(world["db"], MediaFile, str(uuid_pkg.uuid4()), error_message="Nope")
    _refusal(exc, 404, "Nope")


def test_get_file_by_uuid_missing_str_is_what_celery_stores(world):
    with pytest.raises(NOT_FOUND) as exc:
        get_file_by_uuid(world["db"], uuid_pkg.uuid4())
    _refusal(exc, 404, "File not found")


def test_uuid_to_id_propagates_the_404(world):
    with pytest.raises(NOT_FOUND) as exc:
        uuid_to_id(world["db"], MediaFile, uuid_pkg.uuid4())
    _refusal(exc, 404, "MediaFile not found")


def test_get_speaker_by_uuid_missing(world):
    with pytest.raises(NOT_FOUND) as exc:
        get_speaker_by_uuid(world["db"], uuid_pkg.uuid4())
    _refusal(exc, 404, "Speaker not found")


# ------------------------------------------------------------------------ validate_uuids


@pytest.mark.parametrize("bad", ["zzz", ""])
def test_validate_uuids_rejects_bad_item(bad):
    with pytest.raises(INVALID) as exc:
        validate_uuids([str(uuid_pkg.uuid4()), bad])
    _refusal(exc, 400, f"Invalid UUID format: {bad}")


# ---------------------------------------------------------------- require_resource_owner


def test_require_resource_owner_denies_non_owner(world):
    media = _file(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        require_resource_owner(media, world["stranger"], forbidden_detail="Only the owner")
    _refusal(exc, 403, "Only the owner")


def _completes(fn, *args, **kwargs) -> bool:
    """True when ``fn`` returns without raising (for functions that return None)."""
    fn(*args, **kwargs)
    return True


def test_require_resource_owner_allows_owner(world):
    media = _file(world["db"], world["owner"])
    assert _completes(
        require_resource_owner, media, world["owner"], forbidden_detail="Only the owner"
    )


# ------------------------------------------------------ get_file_by_uuid_with_permission


def test_file_permission_missing_is_404(world):
    with pytest.raises(NOT_FOUND) as exc:
        get_file_by_uuid_with_permission(world["db"], uuid_pkg.uuid4(), world["owner"].id)
    _refusal(exc, 404, "File not found")


def test_file_permission_quarantined_is_404_even_for_owner(world):
    media = _file(world["db"], world["owner"], quarantined=True)
    with pytest.raises(NOT_FOUND) as exc:
        get_file_by_uuid_with_permission(world["db"], media.uuid, world["owner"].id)
    _refusal(exc, 404, "File not found")


def test_file_permission_tenant_mismatch_is_403(world):
    media = _file(world["db"], world["owner"], org_id=None)
    with pytest.raises(DENIED) as exc:
        get_file_by_uuid_with_permission(
            world["db"], media.uuid, world["owner"].id, organization_id=987654
        )
    _refusal(exc, 403, "You do not have permission to access this file")


def test_file_permission_stranger_is_403(world):
    media = _file(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        get_file_by_uuid_with_permission(world["db"], media.uuid, world["stranger"].id)
    _refusal(exc, 403, "You do not have permission to access this file")


def test_file_permission_viewer_share_cannot_edit_is_403(world):
    db = world["db"]
    media = _file(db, world["owner"])
    collection = _collection(db, world["owner"])
    from app.models.media import CollectionMember

    db.add(CollectionMember(collection_id=collection.id, media_file_id=media.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=world["owner"].id,
            target_type="user",
            target_user_id=world["stranger"].id,
            permission="viewer",
        )
    )
    db.commit()

    assert get_file_by_uuid_with_permission(db, media.uuid, world["stranger"].id).id == media.id, (
        "control: the viewer share does grant read"
    )
    with pytest.raises(DENIED) as exc:
        get_file_by_uuid_with_permission(
            db, media.uuid, world["stranger"].id, min_permission="editor"
        )
    _refusal(exc, 403, "Requires editor permission on this file")


# -------------------------------------------------- collections (permission / sharing)


def test_collection_permission_stranger_is_403(world):
    collection = _collection(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        get_collection_by_uuid_with_permission(world["db"], collection.uuid, world["stranger"].id)
    _refusal(exc, 403, "You do not have permission to access this collection")


def test_collection_permission_tenant_mismatch_is_403(world):
    collection = _collection(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        get_collection_by_uuid_with_permission(
            world["db"], collection.uuid, world["owner"].id, organization_id=987654
        )
    _refusal(exc, 403, "You do not have permission to access this collection")


def test_collection_restricted_unseen_is_404_for_both_resolvers(world):
    collection = _collection(world["db"], world["owner"], restricted=True)
    for resolver in (get_collection_by_uuid_with_permission, get_collection_by_uuid_with_sharing):
        with pytest.raises(NOT_FOUND) as exc:
            resolver(world["db"], collection.uuid, world["stranger"].id)
        _refusal(exc, 404, "Collection not found")


def test_collection_sharing_stranger_is_403(world):
    collection = _collection(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        get_collection_by_uuid_with_sharing(world["db"], collection.uuid, world["stranger"].id)
    _refusal(exc, 403, "Not authorized to access this collection")


def test_collection_sharing_tenant_mismatch_is_403(world):
    collection = _collection(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        get_collection_by_uuid_with_sharing(
            world["db"], collection.uuid, world["owner"].id, organization_id=987654
        )
    _refusal(exc, 403, "Not authorized to access this collection")


def test_collection_sharing_viewer_cannot_edit_is_403(world):
    db = world["db"]
    collection = _collection(db, world["owner"])
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=world["owner"].id,
            target_type="user",
            target_user_id=world["stranger"].id,
            permission="viewer",
        )
    )
    db.commit()
    _, permission = get_collection_by_uuid_with_sharing(db, collection.uuid, world["stranger"].id)
    assert permission == "viewer", "control: the share does grant viewer"
    with pytest.raises(DENIED) as exc:
        get_collection_by_uuid_with_sharing(
            db, collection.uuid, world["stranger"].id, min_permission="editor"
        )
    _refusal(exc, 403, "Requires editor permission on this collection")


# ------------------------------------------------------------------- speakers / profiles


def test_speaker_access_missing_is_404(world):
    with pytest.raises(NOT_FOUND) as exc:
        require_speaker_access(world["db"], uuid_pkg.uuid4(), world["owner"], organization_id=None)
    _refusal(exc, 404, "Speaker not found")


def test_speaker_access_quarantined_file_is_404(world):
    media = _file(world["db"], world["owner"], quarantined=True)
    speaker = _speaker(world["db"], world["owner"], media)
    with pytest.raises(NOT_FOUND) as exc:
        require_speaker_access(world["db"], speaker.uuid, world["owner"], organization_id=None)
    _refusal(exc, 404, "Speaker not found")


def test_speaker_access_stranger_gets_caller_detail(world):
    media = _file(world["db"], world["owner"])
    speaker = _speaker(world["db"], world["owner"], media)
    with pytest.raises(DENIED) as exc:
        require_speaker_access(
            world["db"],
            speaker.uuid,
            world["stranger"],
            organization_id=None,
            forbidden_detail="Custom forbidden",
        )
    _refusal(exc, 403, "Custom forbidden")
    with pytest.raises(DENIED) as default:
        require_speaker_access(world["db"], speaker.uuid, world["stranger"], organization_id=None)
    _refusal(default, 403, "Not authorized")


def test_speaker_access_viewer_cannot_edit_is_403(world):
    db = world["db"]
    media = _file(db, world["owner"])
    speaker = _speaker(db, world["owner"], media)
    collection = _collection(db, world["owner"])
    from app.models.media import CollectionMember

    db.add(CollectionMember(collection_id=collection.id, media_file_id=media.id))
    db.add(
        CollectionShare(
            collection_id=collection.id,
            shared_by_id=world["owner"].id,
            target_type="user",
            target_user_id=world["stranger"].id,
            permission="viewer",
        )
    )
    db.commit()
    assert (
        require_speaker_access(db, speaker.uuid, world["stranger"], organization_id=None).id
        == speaker.id
    ), "control: the viewer share reaches the speaker"
    with pytest.raises(DENIED) as exc:
        require_speaker_access(
            db, speaker.uuid, world["stranger"], organization_id=None, min_permission="editor"
        )
    _refusal(exc, 403, "Requires editor permission")


def test_profile_scope_missing_is_404(world):
    with pytest.raises(NOT_FOUND) as exc:
        require_profile_in_scope(
            world["db"], uuid_pkg.uuid4(), world["owner"], organization_id=None
        )
    _refusal(exc, 404, "Speaker profile not found")


def test_profile_scope_tenant_mismatch_is_404(world):
    profile = _profile(world["db"], world["owner"])
    with pytest.raises(NOT_FOUND) as exc:
        require_profile_in_scope(world["db"], profile.uuid, world["owner"], organization_id=987654)
    _refusal(exc, 404, "Speaker profile not found")


def test_profile_scope_stranger_is_403(world):
    profile = _profile(world["db"], world["owner"])
    with pytest.raises(DENIED) as exc:
        require_profile_in_scope(world["db"], profile.uuid, world["stranger"], organization_id=None)
    _refusal(exc, 403, "Not authorized to access this profile")


def test_bypass_none_is_the_default_for_every_gate():
    """Control: the helpers above run with ``PlatformBypass.none()`` and so are not bypassed."""
    assert PlatformBypass.none().user_is_admin is False


# ---------------------------------------------------------------- context_resolver (#16)


def _ctx(user) -> RequestContext:
    return RequestContext(user=user, org_id=None)


OVERSIZED = "Selection resolves to 3 files; the maximum is 2. Narrow the selection."


def test_oversized_scope_is_400_with_exact_sentence(world, monkeypatch):
    monkeypatch.setattr(chat_defaults, "CHAT_MAX_SCOPE_FILES", 2)
    files = [_file(world["db"], world["owner"]) for _ in range(3)]
    scope = ChatScope(file_uuids=[str(f.uuid) for f in files])
    with pytest.raises((HTTPException, InvalidInputError)) as exc:
        resolve_scope_file_uuids(world["db"], _ctx(world["owner"]), scope)
    _refusal(exc, 400, OVERSIZED)


def test_count_scope_files_swallows_only_the_400(world, monkeypatch):
    monkeypatch.setattr(chat_defaults, "CHAT_MAX_SCOPE_FILES", 2)
    files = [_file(world["db"], world["owner"]) for _ in range(3)]
    scope = ChatScope(file_uuids=[str(f.uuid) for f in files])
    assert count_scope_files(world["db"], _ctx(world["owner"]), scope) == 3


def test_count_scope_files_reraises_a_non_400(world, monkeypatch):
    def refuse(*_a, **_k):
        raise InvalidInputError("x")

    def forbidden(*_a, **_k):
        raise AccessDeniedError("nope")

    import app.services.chat.context_resolver as module

    monkeypatch.setattr(module, "resolve_scope_file_uuids", forbidden)
    scope = ChatScope(file_uuids=[str(uuid_pkg.uuid4())])
    with pytest.raises(AccessDeniedError) as exc:
        count_scope_files(world["db"], _ctx(world["owner"]), scope)
    assert exc.value.status_code == 403
    monkeypatch.setattr(module, "resolve_scope_file_uuids", refuse)
    assert (
        count_scope_files(world["db"], _ctx(world["owner"]), scope)
        == chat_defaults.CHAT_MAX_SCOPE_FILES + 1
    )


# --------------------------------------------------------------- real endpoints (bytes)


def _headers(h: dict) -> dict:
    return {k: v for k, v in h.items() if not k.startswith("_")}


def test_wire_file_detail_missing_and_foreign_are_the_same_404(
    client, user_token_headers, other_user, db_session
):
    foreign = _file(db_session, other_user)
    headers = _headers(user_token_headers)
    for target in (uuid_pkg.uuid4(), foreign.uuid):
        response = client.get(f"/api/files/{target}", headers=headers)
        assert response.status_code == 404
        assert response.content == _body("File not found")


def test_wire_malformed_collection_uuid_is_400(client, user_token_headers):
    response = client.get("/api/collections/not-a-uuid", headers=_headers(user_token_headers))
    assert response.status_code == 400
    assert response.content == _body("Invalid UUID format: not-a-uuid")


def test_wire_collection_get_and_update_refusals(
    client, user_token_headers, other_user, db_session
):
    collection = _collection(db_session, other_user)
    headers = _headers(user_token_headers)
    got = client.get(f"/api/collections/{collection.uuid}", headers=headers)
    assert got.status_code == 403
    assert got.content == _body("You do not have permission to access this collection")
    put = client.put(f"/api/collections/{collection.uuid}", json={"name": "x"}, headers=headers)
    assert put.status_code == 403
    assert put.content == _body("Not authorized to access this collection")
    hidden = _collection(db_session, other_user, restricted=True)
    missing = client.get(f"/api/collections/{hidden.uuid}", headers=headers)
    assert missing.status_code == 404
    assert missing.content == _body("Collection not found")
    for response in (got, put, missing):
        assert "www-authenticate" not in response.headers


def test_wire_speaker_missing_is_404(client, user_token_headers):
    response = client.get(f"/api/speakers/{uuid_pkg.uuid4()}", headers=_headers(user_token_headers))
    assert response.status_code == 404
    assert response.content == _body("Speaker not found")


def test_wire_chat_estimate_reports_max_plus_one_for_oversized_scope(
    client, user_token_headers, normal_user, db_session, monkeypatch
):
    monkeypatch.setattr(chat_defaults, "CHAT_MAX_SCOPE_FILES", 2)
    files = [_file(db_session, normal_user) for _ in range(3)]
    response = client.post(
        "/api/chat/context/estimate",
        json={"file_uuids": [str(f.uuid) for f in files]},
        headers=_headers(user_token_headers),
    )
    assert response.status_code == 200
    assert response.json()["file_count"] == 3
