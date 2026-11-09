"""``collection_queries`` against the real database: names, counts, membership and shares."""

import uuid as uuid_pkg

import pytest

from app.models.collection import Collection
from app.models.collection import CollectionMember
from app.models.group import UserGroup
from app.models.group import UserGroupMember
from app.models.media import FileStatus
from app.models.media import MediaFile
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services import collection_queries as cq


def _user(db, label):
    user = User(
        email=f"{label}_{uuid_pkg.uuid4().hex[:8]}@example.com",
        full_name=label,
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db.add(user)
    db.flush()
    return user


def _file(db, user, name="f.mp4"):
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=name,
        content_type="video/mp4",
        file_size=1,
        storage_path=f"test/{uuid_pkg.uuid4().hex}.mp4",
        status=FileStatus.COMPLETED,
    )
    db.add(media)
    db.flush()
    return media


def _collection(db, user, name):
    collection = Collection(name=name, user_id=user.id)
    db.add(collection)
    db.flush()
    return collection


@pytest.fixture
def world(db_session):
    owner = _user(db_session, "owner")
    other = _user(db_session, "other")
    files = [_file(db_session, owner, f"f{i}.mp4") for i in range(3)]
    coll = _collection(db_session, owner, f"c-{uuid_pkg.uuid4().hex[:6]}")
    for media in files[:2]:
        db_session.add(CollectionMember(collection_id=coll.id, media_file_id=media.id))
    db_session.flush()
    return owner, other, files, coll


def test_name_checks_distinguish_creator_tenant_and_self(db_session, world):
    owner, other, _, coll = world
    assert cq.collection_name_taken_for_creator(db_session, owner.id, None, coll.name)
    assert not cq.collection_name_taken_for_creator(db_session, other.id, None, coll.name)
    assert not cq.collection_name_taken_in_tenant_of(db_session, coll, coll.name)
    twin = _collection(db_session, owner, "twin")
    assert cq.collection_name_taken_in_tenant_of(db_session, twin, coll.name)


def test_member_helpers_report_exactly_the_members(db_session, world):
    _, _, files, coll = world
    ids = [f.id for f in files]
    assert set(cq.collection_member_file_ids(db_session, coll.id)) == set(ids[:2])
    assert cq.existing_member_ids(db_session, coll.id, ids) == set(ids[:2])
    assert cq.remove_members(db_session, coll.id, [ids[0], ids[2]]) == 1
    assert cq.collection_member_file_ids(db_session, coll.id) == [ids[1]]


def test_resolve_file_ids_restricts_to_owner_only_when_asked(db_session, world):
    owner, other, files, _ = world
    theirs = _file(db_session, other, "theirs.mp4")
    uuids = [files[0].uuid, theirs.uuid]
    assert {r.id for r in cq.resolve_file_ids(db_session, uuids, owner_user_id=None)} == {
        files[0].id,
        theirs.id,
    }
    assert {r.id for r in cq.resolve_file_ids(db_session, uuids, owner_user_id=owner.id)} == {
        files[0].id
    }
    assert cq.files_in_scope(db_session, uuids, MediaFile.user_id == other.id) == [theirs]


def test_visible_media_counts_counts_members_and_skips_empty_input(db_session, world):
    _, _, _, coll = world
    assert cq.visible_media_counts(db_session, [], include_quarantined=False) == {}
    counts = cq.visible_media_counts(db_session, [coll.id], include_quarantined=False)
    assert counts == {coll.id: 2}


def test_shares_and_their_lookups(db_session, world):
    owner, other, _, coll = world
    group = UserGroup(uuid=uuid_pkg.uuid4(), name="g", owner_id=other.id)
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=owner.id, role="member"))
    to_user = CollectionShare(
        collection_id=coll.id,
        shared_by_id=owner.id,
        target_type="user",
        target_user_id=other.id,
        permission="viewer",
    )
    to_group = CollectionShare(
        collection_id=coll.id,
        shared_by_id=other.id,
        target_type="group",
        target_group_id=group.id,
        permission="editor",
    )
    db_session.add_all([to_user, to_group])
    db_session.flush()

    assert cq.collection_has_shares(db_session, coll.id)
    assert cq.share_to_user_exists(db_session, coll.id, other.id)
    assert not cq.share_to_user_exists(db_session, coll.id, owner.id)
    assert cq.share_to_group_exists(db_session, coll.id, group.id)
    assert cq.share_counts(db_session, [coll.id]) == {coll.id: 2}
    loaded = cq.load_share(db_session, to_user.id)
    assert loaded is not None
    assert loaded.target_user is not None
    assert loaded.target_user.id == other.id
    assert {s.id for s in cq.list_shares(db_session, coll.id, skip=0, limit=10)} == {
        to_user.id,
        to_group.id,
    }
    assert {s.id for s in cq.shares_reaching_user(db_session, [coll.id], other.id)} == {to_user.id}
    assert {s.id for s in cq.shares_reaching_user(db_session, [coll.id], owner.id)} == {to_group.id}
    assert cq.share_target_user_ids(db_session, to_user, None) == [other.id]
    assert cq.share_target_user_ids(db_session, to_group, None) == [owner.id]
    assert cq.group_member_user_ids(db_session, group.id) == {owner.id}
