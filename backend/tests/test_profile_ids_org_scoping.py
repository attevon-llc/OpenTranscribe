"""``PermissionService.get_accessible_profile_ids`` enforces the tenant in SQL (#1027 B).

This set is handed to the profile kNN as ``accessible_profile_ids``, and the kNN then
drops its ``user_id`` term, so the OpenSearch org clause used to be the only thing
between the caller and another tenant's profile. The SQL set now carries the gate on
both of its branches: profiles the caller owns, and profiles reached through a
collection share.

The world: one caller who owns a profile in org A, org B and personal scope, plus a
second user who owns one profile in each scope and shares all three with the caller
through one collection per scope. Every scope therefore has exactly one owned and one
shared candidate, and removing the gate from either branch leaks the other tenants'.
"""

from __future__ import annotations

import inspect
import uuid as uuid_pkg

import pytest

from app.core.security import get_password_hash
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.sharing import CollectionShare
from app.models.user import User
from app.services.permission_service import PermissionService

SCOPES = ("org_a", "org_b", "personal")


def _user(db, label: str) -> User:
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
    return user


def _file(db, owner: User, org_id: int | None) -> MediaFile:
    file_uuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=file_uuid,
        filename="scope_probe.wav",
        storage_path=f"media/test/{file_uuid}.wav",
        content_type="audio/wav",
        file_size=4096,
        status="completed",
        user_id=owner.id,
        organization_id=org_id,
    )
    db.add(media_file)
    db.flush()
    return media_file


def _profile(db, owner: User, org_id: int | None, tag: str) -> SpeakerProfile:
    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(), user_id=owner.id, name=f"profile {tag}", organization_id=org_id
    )
    db.add(profile)
    db.flush()
    return profile


def _share_profile_via_collection(
    db, sharer: User, recipient: User, profile: SpeakerProfile, org_id: int | None
) -> None:
    """``sharer``'s file in ``org_id`` carries a speaker on ``profile``; its collection is
    shared with ``recipient`` — the chain the shared branch walks."""
    media_file = _file(db, sharer, org_id)
    db.add(
        Speaker(
            uuid=uuid_pkg.uuid4(),
            user_id=sharer.id,
            media_file_id=media_file.id,
            name="SPEAKER_00",
            profile_id=profile.id,
        )
    )
    collection = Collection(
        uuid=uuid_pkg.uuid4(),
        user_id=sharer.id,
        name=f"shared {uuid_pkg.uuid4().hex[:8]}",
        organization_id=org_id,
    )
    db.add(collection)
    db.flush()
    db.add(
        CollectionMember(
            uuid=uuid_pkg.uuid4(), collection_id=collection.id, media_file_id=media_file.id
        )
    )
    db.add(
        CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=collection.id,
            shared_by_id=sharer.id,
            target_type="user",
            target_user_id=recipient.id,
            permission="viewer",
        )
    )
    db.flush()


@pytest.fixture
def world(db_session):
    org_a = Organization(name="tenant-a", slug=f"tenant-a-{uuid_pkg.uuid4().hex[:8]}")
    org_b = Organization(name="tenant-b", slug=f"tenant-b-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add_all([org_a, org_b])
    db_session.flush()
    org_ids = {"org_a": org_a.id, "org_b": org_b.id, "personal": None}

    caller = _user(db_session, "caller")
    sharer = _user(db_session, "sharer")
    owned, shared, files = {}, {}, {}
    for scope, org_id in org_ids.items():
        owned[scope] = _profile(db_session, caller, org_id, f"owned {scope}").id
        shared_profile = _profile(db_session, sharer, org_id, f"shared {scope}")
        _share_profile_via_collection(db_session, sharer, caller, shared_profile, org_id)
        shared[scope] = shared_profile.id
        files[scope] = _file(db_session, caller, org_id).id
    db_session.commit()
    return {"org_ids": org_ids, "caller": caller, "owned": owned, "shared": shared, "files": files}


@pytest.mark.parametrize("scope", SCOPES)
def test_each_scope_gets_exactly_its_owned_and_shared_profiles(scope, db_session, world):
    ids = PermissionService.get_accessible_profile_ids(
        db_session, world["caller"].id, organization_id=world["org_ids"][scope]
    )

    assert ids == {world["owned"][scope], world["shared"][scope]}


@pytest.mark.parametrize("scope", SCOPES)
def test_the_file_scoped_variant_takes_the_tenant_from_the_file_row(scope, db_session, world):
    """The pipeline matches one file; its tenant is the FILE's organization_id."""
    ids = PermissionService.get_accessible_profile_ids_for_file(
        db_session, world["caller"].id, world["files"][scope]
    )

    assert ids == {world["owned"][scope], world["shared"][scope]}


def test_an_explicitly_unscoped_ownership_check_still_spans_every_scope(db_session, world):
    """The escape hatch the ACL call sites use keeps its pre-#1027 meaning."""
    from app.core.tenancy import UNSCOPED

    ids = PermissionService.get_accessible_profile_ids(
        db_session, world["caller"].id, organization_id=UNSCOPED
    )

    assert ids == set(world["owned"].values()) | set(world["shared"].values())


def test_the_tenant_argument_cannot_be_omitted():
    """A new caller must decide its scope; a default would silently pick one for it."""
    parameter = inspect.signature(PermissionService.get_accessible_profile_ids).parameters[
        "organization_id"
    ]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
