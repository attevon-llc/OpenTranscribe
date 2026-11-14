"""Shared speaker-profile ids come from a semi-join, not a DISTINCT over a 4-table join (#1337, D2)."""

from __future__ import annotations

import re

import pytest

from app.core.tenancy import UNSCOPED
from app.services.permission_service import PermissionService
from tests.query_budget import count_statements
from tests.unit.test_speaker_profiles_list_query_budget import _shared_profile

pytestmark = pytest.mark.unit


def test_shared_profiles_are_found_without_distinct_or_media_file_join(db_session, normal_user):
    _shared_profile(db_session, normal_user, 3)
    with count_statements(db_session.connection()) as log:
        ids = PermissionService.get_accessible_profile_ids(
            db_session, normal_user.id, organization_id=UNSCOPED
        )
    assert len(ids) == 3  # control: every shared profile is still reachable
    shared = log.matching(r"collection_share")
    assert shared, log.describe()
    assert not [s for s in shared if "DISTINCT" in s or "JOIN media_file" in s], shared
    with_source = PermissionService.get_accessible_profile_ids_with_source(
        db_session, normal_user.id, organization_id=UNSCOPED
    )
    assert {pid for pid, owned in with_source if not owned} == ids


def test_shared_profile_query_has_no_or_across_share_columns(db_session, normal_user):
    """OR over target_user_id/target_group_id defeats the share indexes (LOADRUN4 EXPLAIN)."""
    _shared_profile(db_session, normal_user, 2)
    with count_statements(db_session.connection()) as log:
        PermissionService.get_accessible_profile_ids(
            db_session, normal_user.id, organization_id=UNSCOPED
        )
    shared = log.matching(r"collection_share")
    assert shared, log.describe()
    assert not [
        s
        for s in shared
        if "target_user_id" in s and "target_group_id" in s and re.search(r"\bOR\b", s)
    ], shared
    assert any("UNION" in s for s in shared), shared


def _share_profile(db, *, to_user=None, to_group=None):
    """One owner, one profile reachable through a collection shared to a user and/or group."""
    import datetime as dt
    import uuid as uuid_pkg

    from app.core.security import get_password_hash
    from app.models.collection import Collection
    from app.models.collection import CollectionMember
    from app.models.media import MediaFile
    from app.models.sharing import CollectionShare
    from app.models.speaker import Speaker
    from app.models.speaker import SpeakerProfile
    from app.models.user import User

    owner = User(
        email=f"o_{uuid_pkg.uuid4().hex[:10]}@example.com",
        full_name="Owner",
        hashed_password=get_password_hash("x" * 12),
        is_active=True,
        role="user",
    )
    db.add(owner)
    db.flush()
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=owner.id, name="P")
    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=owner.id,
        filename="f.mp4",
        title="f",
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    collection = Collection(user_id=owner.id, name="c", description="")
    db.add_all([profile, media, collection])
    db.flush()
    db.add_all(
        [
            Speaker(
                uuid=uuid_pkg.uuid4(),
                user_id=owner.id,
                media_file_id=media.id,
                name="SPEAKER_00",
                profile_id=profile.id,
                verified=True,
            ),
            CollectionMember(collection_id=collection.id, media_file_id=media.id),
        ]
    )
    if to_user is not None:
        db.add(
            CollectionShare(
                collection_id=collection.id,
                shared_by_id=owner.id,
                target_type="user",
                target_user_id=to_user.id,
                permission="viewer",
            )
        )
    if to_group is not None:
        db.add(
            CollectionShare(
                collection_id=collection.id,
                shared_by_id=owner.id,
                target_type="group",
                target_group_id=to_group.id,
                permission="viewer",
            )
        )
    db.flush()
    return profile.id


def test_shared_profile_ids_user_group_both_and_neither(db_session, normal_user):
    """The UNION split returns exactly what the OR did, for each way a share can reach a user."""
    from app.models.group import UserGroup
    from app.models.group import UserGroupMember

    group = UserGroup(name="g", owner_id=normal_user.id)
    db_session.add(group)
    db_session.flush()
    db_session.add(UserGroupMember(group_id=group.id, user_id=normal_user.id, role="member"))
    db_session.flush()

    via_user = _share_profile(db_session, to_user=normal_user)
    via_group = _share_profile(db_session, to_group=group)
    via_both = _share_profile(db_session, to_user=normal_user, to_group=group)
    unrelated = _share_profile(db_session)  # control: not shared with anyone

    got = PermissionService.get_accessible_profile_ids(
        db_session, normal_user.id, organization_id=UNSCOPED
    )
    assert {via_user, via_group, via_both} <= got
    assert unrelated not in got

    # neither: a user with no share and no group membership
    from app.models.user import User

    stranger = User(
        email="stranger_lfk@example.com",
        full_name="S",
        hashed_password="x",
        is_active=True,
        role="user",
    )
    db_session.add(stranger)
    db_session.flush()
    assert (
        PermissionService.get_accessible_profile_ids(
            db_session, stranger.id, organization_id=UNSCOPED
        )
        == set()
    )
