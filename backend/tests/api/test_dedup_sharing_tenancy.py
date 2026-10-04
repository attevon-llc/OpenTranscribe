"""Content dedup, watch sources, share notifications and tag shares stay inside a tenant.

Tenant rule: in an organization only that organization's rows; in a personal
workspace only org-less rows. A community install has no organizations and no
membership rows, so every user is in the same personal tenant there and
behaviour must be unchanged (instance-wide watch-source dedup included).

Relations exercised:

* ``org_a``/``org_b``: two organizations; alice is in both, bob only in A.
* ``carol``/``dave``: belong to no organization (the community-install shape).
* "owner left": a membership row deleted after the resource was created.

Requests run as a user in a tenant by overriding the two auth dependencies,
the shape ``test_collection_tenancy.py`` uses.
"""

from __future__ import annotations

import shutil
import uuid as uuid_pkg
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import Collection
from app.models.media import MediaFile
from app.models.media import Tag
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.sharing import TagShare
from app.models.user import User
from app.models.watch_source import WatchSource
from app.models.watch_source import WatchSourceFile
from app.services.imohash_service import compute_from_path
from app.services.watch_sources import processing

_SAMPLE_AUDIO = Path(__file__).resolve().parents[1] / "fixtures" / "media" / "sample_short.wav"


# --------------------------------------------------------------------------- #
# Fixtures / builders                                                          #
# --------------------------------------------------------------------------- #


def _mk_user(db, label: str) -> User:
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=f"{label} user",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _mk_org(db, label: str) -> Organization:
    org = Organization(
        external_org_id=f"org_{label}_{uuid_pkg.uuid4().hex[:8]}",
        name=f"{label} Org",
        is_active=True,
    )
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


def _join(db, org: Organization, user: User, role: str = "org:member") -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role=role))
    db.commit()


def _leave(db, org: Organization, user: User) -> None:
    db.query(OrganizationMembership).filter(
        OrganizationMembership.organization_id == org.id,
        OrganizationMembership.user_id == user.id,
    ).delete()
    db.commit()


def _mk_file(db, user: User, org_id: int | None, **extra) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    extra.setdefault("status", "completed")
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"dedup_tenancy_{str(fuuid)[:8]}.wav",
        storage_path=f"dedup_tenancy/{fuuid}.wav",
        content_type="audio/wav",
        file_size=1,
        user_id=user.id,
        organization_id=org_id,
        **extra,
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _mk_source(db, owner: User, org_id: int | None) -> WatchSource:
    source = WatchSource(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        created_by=owner.id,
        organization_id=org_id,
        name=f"watch-{uuid_pkg.uuid4().hex[:8]}",
        source_type="local",
        is_enabled=True,
        local_path=".",
        auto_transcribe=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    return source


def _mk_row(db, source: WatchSource, **overrides) -> WatchSourceFile:
    values = {
        "uuid": uuid_pkg.uuid4(),
        "watch_source_id": source.id,
        "remote_path": f"/watch/{uuid_pkg.uuid4().hex}.wav",
        "filename": "recording.wav",
        "status": "importing",
    }
    values.update(overrides)
    row = WatchSourceFile(**values)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _stage_audio(tmp_path: Path) -> Path:
    """Real WAVE bytes with a unique tail, so the fingerprint is fresh per test."""
    dest = tmp_path / f"{uuid_pkg.uuid4().hex}.wav"
    shutil.copyfile(_SAMPLE_AUDIO, dest)
    with dest.open("ab") as fp:
        fp.write(b"\x00" * 64 + uuid_pkg.uuid4().bytes)
    return dest


@contextmanager
def _acting_as(user: User, org_id: int | None, org_role: str | None = "org:member"):
    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role=org_role if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


@pytest.fixture()
def world(db_session):
    db = db_session
    org_a = _mk_org(db, "A")
    org_b = _mk_org(db, "B")
    alice = _mk_user(db, "alice")
    bob = _mk_user(db, "bob")
    carol = _mk_user(db, "carol")
    dave = _mk_user(db, "dave")
    _join(db, org_a, alice)
    _join(db, org_a, bob)
    _join(db, org_b, alice)
    return type(
        "World",
        (),
        {
            "db": db,
            "org_a": org_a,
            "org_b": org_b,
            "alice": alice,
            "bob": bob,
            "carol": carol,
            "dave": dave,
        },
    )


def _org(w, name: str | None) -> int | None:
    return None if name is None else getattr(w, name).id


# --------------------------------------------------------------------------- #
# Watch-source import dedup                                                    #
# --------------------------------------------------------------------------- #

# (existing owner, existing tenant, importer owner, importer tenant, deduped?)
_DEDUP_CASES = [
    pytest.param("bob", "org_a", "alice", "org_a", True, id="same-org"),
    pytest.param("bob", "org_a", "alice", "org_b", False, id="other-org"),
    pytest.param("bob", "org_a", "carol", None, False, id="org-vs-personal-no-org"),
    pytest.param("bob", None, "alice", None, False, id="personal-vs-personal-of-org-members"),
    pytest.param("carol", None, "alice", "org_a", False, id="personal-vs-org"),
    pytest.param("carol", None, "alice", None, False, id="community-personal-vs-member-personal"),
    pytest.param("carol", None, "dave", None, True, id="community-personal-vs-personal"),
    pytest.param("alice", None, "alice", None, True, id="own-personal-vs-own-personal"),
]


@pytest.mark.parametrize(
    ("owner", "owner_org", "importer", "importer_org", "deduped"), _DEDUP_CASES
)
def test_watch_import_dedups_against_existing_media_only_in_tenant(
    world, tmp_path, owner, owner_org, importer, importer_org, deduped
):
    w = world
    audio = _stage_audio(tmp_path)
    imohash = compute_from_path(str(audio))
    assert imohash
    existing = _mk_file(w.db, getattr(w, owner), _org(w, owner_org), imohash=imohash)

    source = _mk_source(w.db, getattr(w, importer), _org(w, importer_org))
    row = _mk_row(w.db, source)

    with patch.object(processing, "_finalize_media_ingest", return_value=row) as finalize:
        result = processing.ingest_prepared_file(
            w.db, source, str(audio), filename="rec.wav", row=row, size=audio.stat().st_size
        )

    if deduped:
        assert result.status == "skipped_duplicate"
        assert result.skip_reason == "duplicate_existing"
        assert result.media_file_id == existing.id
        finalize.assert_not_called()
    else:
        assert result.media_file_id != existing.id, "linked to another tenant's file"
        assert result.skip_reason is None
        finalize.assert_called_once()


@pytest.mark.parametrize(
    ("owner", "owner_org", "importer", "importer_org", "deduped"), _DEDUP_CASES
)
def test_watch_import_dedups_against_other_sources_only_in_tenant(
    world, tmp_path, owner, owner_org, importer, importer_org, deduped
):
    w = world
    audio = _stage_audio(tmp_path)
    imohash = compute_from_path(str(audio))
    other_source = _mk_source(w.db, getattr(w, owner), _org(w, owner_org))
    other_media = _mk_file(w.db, getattr(w, owner), _org(w, owner_org))
    _mk_row(w.db, other_source, status="imported", imohash=imohash, media_file_id=other_media.id)

    source = _mk_source(w.db, getattr(w, importer), _org(w, importer_org))
    row = _mk_row(w.db, source)

    with patch.object(processing, "_finalize_media_ingest", return_value=row) as finalize:
        result = processing.ingest_prepared_file(
            w.db, source, str(audio), filename="rec.wav", row=row, size=audio.stat().st_size
        )

    if deduped:
        assert result.skip_reason == "duplicate_other_source"
        assert result.media_file_id == other_media.id
        finalize.assert_not_called()
    else:
        assert result.media_file_id != other_media.id, "linked to another tenant's file"
        finalize.assert_called_once()


def test_watch_import_is_refused_and_source_disabled_after_owner_leaves_org(world, tmp_path):
    w = world
    source = _mk_source(w.db, w.bob, w.org_a.id)
    row = _mk_row(w.db, source)
    _leave(w.db, w.org_a, w.bob)
    audio = _stage_audio(tmp_path)

    with patch.object(processing, "_finalize_media_ingest", return_value=row) as finalize:
        result = processing.ingest_prepared_file(
            w.db, source, str(audio), filename="rec.wav", row=row, size=audio.stat().st_size
        )

    finalize.assert_not_called()
    assert result.status == "error"
    assert result.error_message == processing.OWNER_LEFT_ORG_MESSAGE
    w.db.refresh(source)
    assert source.is_enabled is False


def test_scan_plan_disables_source_whose_owner_left_org(world):
    from app.tasks import watch_source_tasks

    w = world
    source = _mk_source(w.db, w.bob, w.org_a.id)
    _leave(w.db, w.org_a, w.bob)

    @contextmanager
    def _same_session():
        yield w.db

    with (
        patch.object(watch_source_tasks, "session_scope", _same_session),
        patch.object(watch_source_tasks, "create_client") as create_client,
    ):
        assert watch_source_tasks._load_scan_plan(source.id) is None
    create_client.assert_not_called()
    w.db.refresh(source)
    assert source.is_enabled is False


@pytest.mark.parametrize("owner_org", ["org_a", None], ids=["org-member", "personal"])
def test_member_source_keeps_scanning(world, owner_org):
    w = world
    source = _mk_source(w.db, w.bob, _org(w, owner_org))
    assert processing.disable_if_owner_left_org(w.db, source) is False
    assert source.is_enabled is True


# --------------------------------------------------------------------------- #
# Watch-source endpoints                                                       #
# --------------------------------------------------------------------------- #

# (source tenant, caller tenant, visible?)
_SOURCE_CASES = [
    pytest.param("org_a", "org_a", True, id="same-org"),
    pytest.param("org_a", "org_b", False, id="other-org"),
    pytest.param("org_a", None, False, id="org-source-from-personal"),
    pytest.param(None, None, True, id="personal"),
    pytest.param(None, "org_a", False, id="personal-source-from-org"),
]


@pytest.mark.parametrize(("source_org", "caller_org", "visible"), _SOURCE_CASES)
def test_watch_source_detail_is_tenant_scoped(client, world, source_org, caller_org, visible):
    w = world
    source = _mk_source(w.db, w.alice, _org(w, source_org))
    with _acting_as(w.alice, _org(w, caller_org)):
        detail = client.get(f"/api/watch-sources/{source.uuid}")
        files = client.get(f"/api/watch-sources/{source.uuid}/files")
    assert detail.status_code == (200 if visible else 404), detail.text
    assert files.status_code == (200 if visible else 404), files.text


@pytest.mark.parametrize(("source_org", "caller_org", "visible"), _SOURCE_CASES)
def test_watch_source_listing_is_tenant_scoped(client, world, source_org, caller_org, visible):
    w = world
    source = _mk_source(w.db, w.alice, _org(w, source_org))
    with _acting_as(w.alice, _org(w, caller_org)):
        listed = client.get("/api/watch-sources")
    assert listed.status_code == 200, listed.text
    uuids = {s["uuid"] for s in listed.json()["sources"]}
    assert (str(source.uuid) in uuids) is visible


def test_watch_source_listing_community_unchanged(client, world):
    w = world
    source = _mk_source(w.db, w.carol, None)
    with _acting_as(w.carol, None):
        listed = client.get("/api/watch-sources")
        assert client.get(f"/api/watch-sources/{source.uuid}").status_code == 200
    assert str(source.uuid) in {s["uuid"] for s in listed.json()["sources"]}


# --------------------------------------------------------------------------- #
# Upload / URL dedup                                                           #
# --------------------------------------------------------------------------- #

# (file tenant, caller tenant, duplicate?)
_UPLOAD_CASES = [
    pytest.param("org_a", "org_a", True, id="same-org"),
    pytest.param("org_a", "org_b", False, id="other-org"),
    pytest.param("org_a", None, False, id="org-file-from-personal"),
    pytest.param(None, None, True, id="personal"),
    pytest.param(None, "org_a", False, id="personal-file-from-org"),
]


@pytest.mark.parametrize(("file_org", "caller_org", "duplicate"), _UPLOAD_CASES)
def test_prepare_upload_duplicate_is_tenant_scoped(client, world, file_org, caller_org, duplicate):
    w = world
    digest = uuid_pkg.uuid4().hex
    existing = _mk_file(w.db, w.alice, _org(w, file_org), file_hash=digest)
    with _acting_as(w.alice, _org(w, caller_org)):
        response = client.post(
            "/api/files/prepare",
            json={
                "filename": "copy.wav",
                "file_size": 1024,
                "content_type": "audio/wav",
                "file_hash": digest,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    if duplicate:
        assert body["is_duplicate"] == 1
        assert body["file_id"] == str(existing.uuid)
    else:
        assert body["is_duplicate"] == 0
        assert body["file_id"] != str(existing.uuid)


def test_failed_duplicate_cleanup_only_touches_the_active_tenant(world):
    from app.utils.file_hash import cleanup_failed_duplicates

    w = world
    digest = uuid_pkg.uuid4().hex
    other_org_failed = _mk_file(w.db, w.alice, w.org_b.id, file_hash=digest, status="error")
    same_org_failed = _mk_file(w.db, w.alice, w.org_a.id, file_hash=digest, status="error")
    other_id, same_id = other_org_failed.id, same_org_failed.id

    with patch("app.services.minio_service.delete_file"):
        cleaned = cleanup_failed_duplicates(w.db, digest, w.alice.id, organization_id=w.org_a.id)

    assert cleaned == 1
    assert w.db.get(MediaFile, same_id) is None
    assert w.db.get(MediaFile, other_id) is not None


@pytest.mark.parametrize(("file_org", "caller_org", "duplicate"), _UPLOAD_CASES)
def test_url_duplicate_is_tenant_scoped(world, file_org, caller_org, duplicate):
    from fastapi import HTTPException

    from app.api.endpoints.files.url_processing import _check_duplicate_video

    w = world
    url = f"https://www.youtube.com/watch?v={uuid_pkg.uuid4().hex[:11]}"
    _mk_file(w.db, w.alice, _org(w, file_org), source_url=url)
    if duplicate:
        with pytest.raises(HTTPException) as exc:
            _check_duplicate_video(w.db, w.alice.id, "vid", url, _org(w, caller_org))
        assert exc.value.status_code == 409
    else:
        _check_duplicate_video(w.db, w.alice.id, "vid", url, _org(w, caller_org))


@pytest.mark.parametrize(("file_org", "caller_org", "duplicate"), _UPLOAD_CASES)
def test_playlist_duplicate_is_tenant_scoped(world, file_org, caller_org, duplicate):
    from app.services.media_download_service import _check_existing_youtube_video

    w = world
    video_id = uuid_pkg.uuid4().hex[:11]
    existing = _mk_file(w.db, w.alice, _org(w, file_org), metadata_raw={"youtube_id": video_id})
    found = _check_existing_youtube_video(w.db, w.alice.id, video_id, _org(w, caller_org))
    assert (found is not None and found.id == existing.id) is duplicate


# --------------------------------------------------------------------------- #
# Collection share notifications                                               #
# --------------------------------------------------------------------------- #


def _mk_group(db, owner: User, members: list[User]) -> UserGroup:
    group = UserGroup(owner_id=owner.id, name=f"grp-{uuid_pkg.uuid4().hex[:8]}")
    db.add(group)
    db.flush()
    db.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="owner"))
    for member in members:
        db.add(UserGroupMember(group_id=group.id, user_id=member.id, role="member"))
    db.commit()
    db.refresh(group)
    return group


def _group_share(db, collection: Collection, group: UserGroup, sharer: User) -> CollectionShare:
    share = CollectionShare(
        collection_id=collection.id,
        shared_by_id=sharer.id,
        target_type="group",
        target_group_id=group.id,
        permission="viewer",
    )
    db.add(share)
    db.commit()
    db.refresh(share)
    return share


def test_org_collection_share_notifies_only_org_members(world):
    from app.api.endpoints.media_collections import _get_share_target_user_ids

    w = world
    group = _mk_group(w.db, w.alice, [w.bob, w.carol])
    collection = Collection(name=f"c-{uuid_pkg.uuid4().hex[:6]}", user_id=w.alice.id)
    collection.organization_id = w.org_a.id
    w.db.add(collection)
    w.db.commit()
    share = _group_share(w.db, collection, group, w.alice)

    assert set(_get_share_target_user_ids(w.db, share, collection)) == {w.alice.id, w.bob.id}

    _leave(w.db, w.org_a, w.bob)
    assert set(_get_share_target_user_ids(w.db, share, collection)) == {w.alice.id}


def test_personal_collection_share_notifies_every_group_member(world):
    from app.api.endpoints.media_collections import _get_share_target_user_ids

    w = world
    group = _mk_group(w.db, w.carol, [w.dave, w.bob])
    collection = Collection(name=f"c-{uuid_pkg.uuid4().hex[:6]}", user_id=w.carol.id)
    w.db.add(collection)
    w.db.commit()
    share = _group_share(w.db, collection, group, w.carol)

    assert set(_get_share_target_user_ids(w.db, share, collection)) == {
        w.carol.id,
        w.dave.id,
        w.bob.id,
    }


def test_revoke_of_org_collection_share_notifies_only_org_members(client, world):
    w = world
    group = _mk_group(w.db, w.alice, [w.bob, w.carol])
    collection = Collection(name=f"c-{uuid_pkg.uuid4().hex[:6]}", user_id=w.alice.id)
    collection.organization_id = w.org_a.id
    w.db.add(collection)
    w.db.commit()
    share = _group_share(w.db, collection, group, w.alice)

    with (
        patch("app.api.endpoints.media_collections.send_ws_event") as send,
        patch("app.api.endpoints.media_collections.update_file_access_index"),
        _acting_as(w.alice, w.org_a.id, "org:admin"),
    ):
        response = client.delete(f"/api/collections/{collection.uuid}/shares/{share.uuid}")
    assert response.status_code == 204, response.text
    notified = {call.args[0] for call in send.call_args_list}
    assert w.carol.id not in notified
    assert w.bob.id in notified


# --------------------------------------------------------------------------- #
# Tag shares                                                                   #
# --------------------------------------------------------------------------- #


def _mk_tag(db, owner: User, org_id: int | None) -> Tag:
    name = f"tag-{uuid_pkg.uuid4().hex[:8]}"
    tag = Tag(
        name=name,
        user_id=owner.id,
        organization_id=org_id,
        source="manual",
        normalized_name=name.lower(),
    )
    db.add(tag)
    db.commit()
    db.refresh(tag)
    return tag


def _share_tag(client, tag: Tag, **target) -> Any:
    return client.post(f"/api/tags/{tag.uuid}/shares", json=target)


@pytest.mark.parametrize(
    ("target", "status_code"),
    [
        pytest.param("bob", 200, id="org-member"),
        pytest.param("carol", 422, id="no-org-user"),
        pytest.param("dave", 422, id="other-no-org-user"),
    ],
)
def test_org_tag_user_share_requires_org_membership(client, world, target, status_code):
    w = world
    tag = _mk_tag(w.db, w.alice, w.org_a.id)
    with _acting_as(w.alice, w.org_a.id):
        response = _share_tag(client, tag, target_user_uuid=str(getattr(w, target).uuid))
    assert response.status_code == status_code, response.text
    shared = w.db.query(TagShare).filter(TagShare.tag_id == tag.id).count()
    assert shared == (1 if status_code == 200 else 0)


def test_org_tag_share_refused_after_target_left_org(client, world):
    w = world
    tag = _mk_tag(w.db, w.alice, w.org_a.id)
    _leave(w.db, w.org_a, w.bob)
    with _acting_as(w.alice, w.org_a.id):
        response = _share_tag(client, tag, target_user_uuid=str(w.bob.uuid))
    assert response.status_code == 422, response.text


def test_personal_tag_share_with_any_user_unchanged(client, world):
    w = world
    tag = _mk_tag(w.db, w.carol, None)
    with _acting_as(w.carol, None):
        response = _share_tag(client, tag, target_user_uuid=str(w.dave.uuid))
    assert response.status_code == 200, response.text


def test_tag_share_with_group_sharer_is_not_in_is_refused(client, world):
    w = world
    tag = _mk_tag(w.db, w.carol, None)
    group = _mk_group(w.db, w.dave, [])
    with _acting_as(w.carol, None):
        response = _share_tag(client, tag, target_group_uuid=str(group.uuid))
    assert response.status_code == 422, response.text
    assert w.db.query(TagShare).filter(TagShare.tag_id == tag.id).count() == 0


@pytest.mark.parametrize(
    ("extra_members", "status_code"),
    [
        pytest.param(["bob"], 200, id="all-org-members"),
        pytest.param(["bob", "carol"], 422, id="one-outsider"),
    ],
)
def test_org_tag_group_share_requires_every_member_in_org(
    client, world, extra_members, status_code
):
    w = world
    tag = _mk_tag(w.db, w.alice, w.org_a.id)
    group = _mk_group(w.db, w.alice, [getattr(w, m) for m in extra_members])
    with _acting_as(w.alice, w.org_a.id):
        response = _share_tag(client, tag, target_group_uuid=str(group.uuid))
    assert response.status_code == status_code, response.text
