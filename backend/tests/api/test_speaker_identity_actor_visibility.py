"""Who may learn WHICH PERSON made a speaker's identity, over the real routes (#1280).

``identity.by`` names a person (name or email). It is shown to the file's owner, to an
administrator of the file's organization, and to a platform administrator only where the
platform role reaches the file: any administrator in single-tenant mode, and in
multi-tenant mode only under a support-access grant for the file's tenant, which records
the use like every other platform access. A viewer who reached the file by an ordinary
share gets the source and the time but not the person. Every case runs through the route
that serves speakers (file detail, the segments page, the speaker list) so the viewer is
built from the real request context, never by hand.
"""

from __future__ import annotations

import uuid as uuid_pkg
from datetime import UTC
from datetime import datetime
from datetime import timedelta

import pytest

from app.core.config import settings
from app.models.erasure import ErasureLedgerEntry
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import TranscriptSegment
from app.models.sharing import CollectionShare
from app.models.support_access import SupportAccessUse
from app.services.platform_access import reset_tenancy_mode_cache
from tests.fixtures.speaker_cleanup_seams import make_speaker

_AT = datetime(2026, 1, 2, tzinfo=UTC)


def _person_made(db, owner, file, actor, **extra):
    """A speaker a person named, with a transcript segment so every route serves it."""
    speaker = make_speaker(
        db,
        owner,
        file,
        display_name="Zed",
        verified=True,
        identity_source="human",
        identity_source_user_id=actor.id,
        identity_source_at=_AT,
        identity_source_detail={"origin": "typed"},
        **extra,
    )
    db.add(
        TranscriptSegment(
            media_file_id=file.id, speaker_id=speaker.id, start_time=0.0, end_time=1.0, text="hi"
        )
    )
    db.commit()
    return speaker


def _actor(user) -> dict:
    return {"uuid": str(user.uuid), "name": user.full_name or user.email}


def _who(by) -> dict | None:
    """The person a ``by`` names (its uuid and name), without the erasure flag."""
    return None if by is None else {"uuid": by["uuid"], "name": by["name"]}


def _file_identity(client, file, **kw):
    response = client.get(f"/api/files/{file.uuid}", **kw)
    assert response.status_code == 200, response.text
    (speaker,) = response.json()["speakers"]
    return speaker["identity"]


def _segment_identity(client, file, **kw):
    response = client.get(f"/api/files/{file.uuid}/segments", **kw)
    assert response.status_code == 200, response.text
    (segment,) = response.json()["transcript_segments"]
    return segment["speaker"]["identity"]


def _list_identity(client, file, **kw):
    response = client.get("/api/speakers", params={"file_uuid": str(file.uuid)}, **kw)
    assert response.status_code == 200, response.text
    (speaker,) = response.json()
    return speaker["identity"]


_SURFACES = [_file_identity, _segment_identity, _list_identity]
_SURFACE_IDS = ["file-detail", "segments", "speaker-list"]


# --------------------------------------------------------------------------- #
# Organization: the admin of the file's organization, and nobody else in it
# --------------------------------------------------------------------------- #


@pytest.fixture()
def org_file(client, gov):
    """Alice's file in an organization collection every member of the organization can read."""
    media_file = gov.file(gov.alice)
    gov.put(gov.collection(gov.alice), media_file)
    _person_made(gov.db, gov.alice, media_file, gov.alice)
    return media_file


@pytest.mark.parametrize("surface", _SURFACES, ids=_SURFACE_IDS)
def test_an_org_admin_is_told_who_made_the_identity_and_a_plain_member_is_not(
    client, gov, org_file, surface
):
    with gov.acting_as(gov.admin):
        by_admin = surface(client, org_file)
    with gov.acting_as(gov.bob):
        by_member = surface(client, org_file)

    assert _who(by_admin["by"]) == _actor(gov.alice)
    assert by_member is not None and by_member["source"] == "human", "control: the identity shows"
    assert by_member["by"] is None


@pytest.mark.parametrize("surface", _SURFACES, ids=_SURFACE_IDS)
def test_the_owner_is_told_who_made_the_identity(client, gov, org_file, surface):
    with gov.acting_as(gov.alice):
        by_owner = surface(client, org_file)

    assert _who(by_owner["by"]) == _actor(gov.alice)


# --------------------------------------------------------------------------- #
# Platform administrator: only where the platform role reaches the file
# --------------------------------------------------------------------------- #


def _share_personal_file_with(w, target):
    holder = Collection(name=f"sa {uuid_pkg.uuid4().hex[:6]}", user_id=w.owner.id)
    w.db.add(holder)
    w.db.flush()
    w.db.add(CollectionMember(collection_id=holder.id, media_file_id=w.personal_file.id))
    w.db.add(
        CollectionShare(
            collection_id=holder.id,
            shared_by_id=w.owner.id,
            target_type="user",
            target_user_id=target.id,
            permission="viewer",
        )
    )
    w.db.commit()


@pytest.fixture()
def shared_world(support_world):
    """The owner's personal file, a person-made speaker on it, shared with the platform admin."""
    w = support_world
    _person_made(w.db, w.owner, w.personal_file, w.owner)
    _share_personal_file_with(w, w.admin)
    return w


@pytest.mark.parametrize("surface", _SURFACES, ids=_SURFACE_IDS)
def test_a_platform_admin_with_no_grant_gets_no_name_in_a_multi_tenant_deployment(
    shared_world, surface
):
    w = shared_world

    identity = surface(w.client, w.personal_file, headers=w.admin_headers)
    owner_sees = surface(w.client, w.personal_file, headers=w.owner_headers)

    assert identity is not None and identity["source"] == "human", "control: reached by the share"
    assert identity["by"] is None
    assert _who(owner_sees["by"]) == _actor(w.owner), "control: the owner is told"


def test_a_platform_admin_with_no_grant_leaves_no_use_record_and_learns_nothing(shared_world):
    w = shared_world

    _file_identity(w.client, w.personal_file, headers=w.admin_headers)

    assert w.db.query(SupportAccessUse).count() == 0


@pytest.mark.parametrize("surface", _SURFACES, ids=_SURFACE_IDS)
def test_under_a_grant_the_platform_admin_is_told_and_the_use_is_recorded(shared_world, surface):
    w = shared_world
    grant = w.request(subject_user_uuid=str(w.owner.uuid))
    approved = w.client.post(
        f"/api/users/me/support-access/{grant['uuid']}/approve", headers=w.owner_headers, json={}
    )
    assert approved.status_code == 200, approved.text

    identity = surface(w.client, w.personal_file, headers=w.with_grant(grant["uuid"]))

    assert _who(identity["by"]) == _actor(w.owner)
    uses = [u for u in w.db.query(SupportAccessUse).all() if u.resource_type == "media_file"]
    assert [str(u.resource_uuid) for u in uses] == [str(w.personal_file.uuid)]


@pytest.mark.parametrize("surface", _SURFACES, ids=_SURFACE_IDS)
def test_in_a_single_tenant_deployment_an_admin_is_told(shared_world, monkeypatch, surface):
    w = shared_world
    monkeypatch.setattr(settings, "TENANCY_MODE", "single")
    reset_tenancy_mode_cache()

    identity = surface(w.client, w.personal_file, headers=w.admin_headers)

    assert _who(identity["by"]) == _actor(w.owner)


# --------------------------------------------------------------------------- #
# A person whose account is being erased
# --------------------------------------------------------------------------- #


def _erasure_request(db, user, *, user_uuid=None) -> None:
    db.add(
        ErasureLedgerEntry(
            subject_type="user",
            subject_user_id=user.id,
            subject_user_uuid=user_uuid or user.uuid,
            status="pending",
            sla_due_at=datetime.now(UTC) + timedelta(days=30),
        )
    )
    db.commit()


@pytest.mark.parametrize("surface", _SURFACES, ids=_SURFACE_IDS)
def test_an_account_under_erasure_is_an_erased_marker_with_no_name_or_email(client, gov, surface):
    gov.join(gov.outsider)
    media_file = gov.file(gov.alice)
    _person_made(gov.db, gov.alice, media_file, gov.outsider)
    with gov.acting_as(gov.alice):
        named = surface(client, media_file)["by"]
    _erasure_request(gov.db, gov.outsider)

    with gov.acting_as(gov.alice):
        erased = surface(client, media_file)["by"]

    assert _who(named) == _actor(gov.outsider), "control: named before the request"
    assert erased == {"uuid": None, "name": None, "erased": True}
    assert gov.outsider.email not in str(erased)


def test_an_erasure_entry_for_a_different_account_with_the_same_id_is_not_the_account(client, gov):
    """The ledger names an account by id AND uuid: a reissued id is somebody else."""
    media_file = gov.file(gov.alice)
    _person_made(gov.db, gov.alice, media_file, gov.bob)
    _erasure_request(gov.db, gov.bob, user_uuid=uuid_pkg.uuid4())

    with gov.acting_as(gov.alice):
        by = _file_identity(client, media_file)["by"]

    assert _who(by) == _actor(gov.bob)
