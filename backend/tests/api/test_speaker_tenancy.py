"""Tenant scoping of the speaker plane: speakers, profiles, speaker collections, clusters.

The rule every surface here must follow is the one ``scope_to_context`` states: in an
organization context only rows of that organization are reachable, in the personal
workspace only org-less rows. A speaker has no ACL of its own — it is reached through
its media file — so a speaker is in scope when its file is; profiles, speaker
collections and clusters carry their own ``organization_id``. Out-of-tenant rows answer
404, the same as a missing row.

Writes on a speaker also need **editor** (or owner) on its file: linking a speaker to a
profile feeds its voiceprint into that profile's consolidated embedding, so a shared
``viewer`` must not be able to do it. That part applies to community installs too (no
organizations, every row personal), so the cross-user cases run in both shapes.

Requests run as a given user in a given tenant by overriding the two auth dependencies,
the pattern ``test_collection_tenancy.py`` uses.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCluster
from app.models.media import SpeakerClusterMember
from app.models.media import SpeakerCollection
from app.models.media import SpeakerProfile
from app.models.organization import Organization
from app.models.organization import OrganizationMembership
from app.models.sharing import CollectionShare
from app.models.user import User

SPK = "/api/speakers"
PROF = "/api/speaker-profiles"
CLU = "/api/speaker-clusters"

# --------------------------------------------------------------------------- #
# World                                                                        #
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


def _join(db, org: Organization, user: User) -> None:
    db.add(OrganizationMembership(organization_id=org.id, user_id=user.id, role="org:member"))
    db.commit()


def _mk_file(db, user: User, org_id: int | None) -> MediaFile:
    fuuid = uuid_pkg.uuid4()
    media_file = MediaFile(
        uuid=fuuid,
        filename=f"spk_tenancy_{str(fuuid)[:8]}.mp3",
        storage_path=f"spk_tenancy/{fuuid}.mp3",
        content_type="audio/mpeg",
        file_size=1,
        user_id=user.id,
        organization_id=org_id,
        status="completed",
    )
    db.add(media_file)
    db.commit()
    db.refresh(media_file)
    return media_file


def _mk_speaker(db, media_file: MediaFile, name: str = "SPEAKER_00", **kw: Any) -> Speaker:
    speaker = Speaker(
        user_id=media_file.user_id,
        organization_id=media_file.organization_id,
        media_file_id=media_file.id,
        name=name,
        **kw,
    )
    db.add(speaker)
    db.commit()
    db.refresh(speaker)
    return speaker


def _mk_profile(db, user: User, org_id: int | None, name: str | None = None) -> SpeakerProfile:
    profile = SpeakerProfile(
        user_id=user.id,
        organization_id=org_id,
        name=name or f"Person {uuid_pkg.uuid4().hex[:8]}",
    )
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _mk_cluster(db, user: User, org_id: int | None, speakers: list[Speaker]) -> SpeakerCluster:
    cluster = SpeakerCluster(user_id=user.id, organization_id=org_id, member_count=len(speakers))
    db.add(cluster)
    db.flush()
    for speaker in speakers:
        db.add(SpeakerClusterMember(cluster_id=cluster.id, speaker_id=speaker.id, confidence=0.9))
        speaker.cluster_id = cluster.id
    db.commit()
    db.refresh(cluster)
    return cluster


def _share_file(db, owner: User, media_file: MediaFile, target: User, permission: str) -> None:
    coll = Collection(
        name=f"shared {uuid_pkg.uuid4().hex[:8]}",
        user_id=owner.id,
        organization_id=media_file.organization_id,
    )
    db.add(coll)
    db.flush()
    db.add(CollectionMember(collection_id=coll.id, media_file_id=media_file.id))
    db.add(
        CollectionShare(
            collection_id=coll.id,
            shared_by_id=owner.id,
            target_type="user",
            target_user_id=target.id,
            permission=permission,
        )
    )
    db.commit()


@contextmanager
def _acting_as(user: User, org_id: int | None):
    """Run requests as ``user`` in tenant ``org_id`` (None = personal workspace)."""
    from app.api.deps_context import RequestContext
    from app.api.deps_context import get_current_context
    from app.api.endpoints.auth import get_current_active_user
    from app.main import app

    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_current_context] = lambda: RequestContext(
        user=user, org_id=org_id, org_role="org:member" if org_id is not None else None
    )
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_active_user, None)
        app.dependency_overrides.pop(get_current_context, None)


@dataclass
class World:
    db: Any
    org_a: Organization
    org_b: Organization
    alice: User  # member of A and B, owns the rows under test
    a_file: MediaFile  # alice's file in org A
    a_speaker: Speaker
    a_profile: SpeakerProfile
    p_file: MediaFile  # alice's personal file
    p_speaker: Speaker

    def org(self, name: str | None) -> int | None:
        return {"A": self.org_a.id, "B": self.org_b.id, None: None}[name]


@pytest.fixture()
def world(db_session) -> World:
    db = db_session
    org_a, org_b = _mk_org(db, "A"), _mk_org(db, "B")
    alice = _mk_user(db, "alice")
    _join(db, org_a, alice)
    _join(db, org_b, alice)
    a_file = _mk_file(db, alice, org_a.id)
    p_file = _mk_file(db, alice, None)
    a_profile = _mk_profile(db, alice, org_a.id)
    a_speaker = _mk_speaker(db, a_file, display_name="Org Voice", verified=True)
    p_speaker = _mk_speaker(db, p_file, display_name="Personal Voice", verified=True)
    return World(db, org_a, org_b, alice, a_file, a_speaker, a_profile, p_file, p_speaker)


#: The three ways alice can act on her org-A rows: in org A (in scope), in org B or in
#: her personal workspace (both out of scope -> 404).
TENANTS = [pytest.param("A", True, id="org-A"), pytest.param("B", False, id="org-B")]
TENANTS.append(pytest.param(None, False, id="personal"))


# --------------------------------------------------------------------------- #
# Speakers: reached only through a file of the active tenant                   #
# --------------------------------------------------------------------------- #

#: (method, path, success status). Paths take ``{s}`` = the org-A speaker uuid.
SPEAKER_ROUTES = [
    ("GET", f"{SPK}/{{s}}", 200),
    ("GET", f"{SPK}/{{s}}/cross-media", 200),
    ("POST", f"{SPK}/{{s}}/confirm-gender?gender=male", 200),
    ("POST", f"{SPK}/{{s}}/verify?action=reject", 200),
    ("PUT", f"{SPK}/{{s}}", 200),
    ("DELETE", f"{SPK}/{{s}}", 204),
    ("GET", f"{PROF}/speakers/{{s}}/suggestions", 200),
    ("GET", f"{CLU}/speakers/{{s}}/media-preview", 200),
]


@pytest.mark.parametrize("org_name,in_scope", TENANTS)
@pytest.mark.parametrize(
    "method,path,ok_status", SPEAKER_ROUTES, ids=[f"{m} {p}" for m, p, _ in SPEAKER_ROUTES]
)
def test_speaker_routes_are_confined_to_the_speakers_tenant(
    client, world, org_name, in_scope, method, path, ok_status
):
    w = world
    kwargs: dict[str, Any] = {"json": {"display_name": "Renamed"}} if method == "PUT" else {}
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.request(method, path.format(s=w.a_speaker.uuid), **kwargs)
    if in_scope:
        assert resp.status_code == ok_status, resp.text
    else:
        assert resp.status_code == 404, resp.text
        assert w.db.get(Speaker, w.a_speaker.id) is not None, "an out-of-tenant write landed"


@pytest.mark.parametrize("org_name", ["A", None])
def test_speaker_listing_without_a_file_shows_only_the_active_tenant(client, world, org_name):
    w = world
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.get(SPK)
    assert resp.status_code == 200, resp.text
    listed = {row["uuid"] for row in resp.json()}
    expected, hidden = (w.a_speaker, w.p_speaker) if org_name else (w.p_speaker, w.a_speaker)
    assert str(expected.uuid) in listed
    assert str(hidden.uuid) not in listed


def test_cross_media_occurrences_stay_in_the_active_tenant(client, world):
    w = world
    # A (legacy) personal speaker linked to the org profile must not surface in org A.
    w.a_speaker.profile_id = w.a_profile.id
    w.p_speaker.profile_id = w.a_profile.id
    w.db.commit()
    with _acting_as(w.alice, w.org_a.id):
        resp = client.get(f"{SPK}/{w.a_speaker.uuid}/cross-media")
    assert resp.status_code == 200, resp.text
    assert {row["media_file_id"] for row in resp.json()} == {str(w.a_file.uuid)}


def test_speaker_filter_ignores_a_profile_of_another_tenant(client, world):
    w = world
    personal_profile = _mk_profile(w.db, w.alice, None)
    w.p_speaker.profile_id = personal_profile.id
    w.db.commit()
    with _acting_as(w.alice, w.org_a.id):
        resp = client.get(
            SPK, params={"for_filter": True, "profile_id": str(personal_profile.uuid)}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


def test_merge_refuses_speakers_of_two_tenants(client, world):
    w = world
    with _acting_as(w.alice, w.org_a.id):
        resp = client.post(f"{SPK}/{w.p_speaker.uuid}/merge/{w.a_speaker.uuid}")
    assert resp.status_code == 404, resp.text
    assert w.db.get(Speaker, w.p_speaker.id) is not None


# --------------------------------------------------------------------------- #
# Profiles                                                                     #
# --------------------------------------------------------------------------- #

PROFILE_ROUTES = [
    ("PUT", f"{PROF}/profiles/{{p}}?description=changed", 200),
    ("GET", f"{PROF}/profiles/{{p}}/occurrences", 200),
    ("POST", f"{PROF}/profiles/{{p}}/confirm-gender?gender=female", 200),
    ("DELETE", f"{PROF}/profiles/{{p}}/avatar", 204),
    ("DELETE", f"{PROF}/profiles/{{p}}", 204),
]


@pytest.mark.parametrize("org_name,in_scope", TENANTS)
@pytest.mark.parametrize(
    "method,path,ok_status", PROFILE_ROUTES, ids=[f"{m} {p}" for m, p, _ in PROFILE_ROUTES]
)
def test_profile_routes_are_confined_to_the_profiles_tenant(
    client, world, org_name, in_scope, method, path, ok_status
):
    w = world
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.request(method, path.format(p=w.a_profile.uuid))
    if in_scope:
        assert resp.status_code == ok_status, resp.text
    else:
        assert resp.status_code == 404, resp.text
        w.db.refresh(w.a_profile)
        assert w.a_profile.description is None
        assert w.a_profile.predicted_gender is None


@pytest.mark.parametrize("org_name", ["B", None])
def test_avatar_upload_is_refused_outside_the_profiles_tenant(client, world, org_name):
    w = world
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.post(
            f"{PROF}/profiles/{w.a_profile.uuid}/avatar",
            files={"file": ("a.png", b"\x89PNG\r\n\x1a\n", "image/png")},
        )
    assert resp.status_code == 404, resp.text


@pytest.mark.parametrize("org_name,in_scope", TENANTS)
def test_profile_listing_shows_only_the_active_tenant(client, world, org_name, in_scope):
    w = world
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.get(f"{PROF}/profiles")
    assert resp.status_code == 200, resp.text
    listed = {row["uuid"] for row in resp.json()}
    assert (str(w.a_profile.uuid) in listed) is in_scope


def test_profile_listing_rejects_a_speaker_collection_of_another_tenant(client, world):
    w = world
    coll = SpeakerCollection(name="Board", user_id=w.alice.id, organization_id=w.org_a.id)
    w.db.add(coll)
    w.db.commit()
    _mk_profile(w.db, w.alice, None)  # a caller with no profiles gets [] before any check
    with _acting_as(w.alice, None):
        resp = client.get(f"{PROF}/profiles", params={"collection_uuid": str(coll.uuid)})
    assert resp.status_code == 404, resp.text


def test_profile_occurrences_stay_in_the_active_tenant(client, world):
    w = world
    w.a_speaker.profile_id = w.a_profile.id
    w.p_speaker.profile_id = w.a_profile.id
    w.db.commit()
    with _acting_as(w.alice, w.org_a.id):
        resp = client.get(f"{PROF}/profiles/{w.a_profile.uuid}/occurrences")
    assert resp.status_code == 200, resp.text
    assert [row["media_file_id"] for row in resp.json()] == [w.a_file.id]


def test_speaker_collections_are_stamped_and_listed_per_tenant(client, world):
    w = world
    name = f"Panel {uuid_pkg.uuid4().hex[:8]}"
    with _acting_as(w.alice, w.org_a.id):
        created = client.post(f"{PROF}/collections", params={"name": name})
        assert created.status_code == 200, created.text
        assert name in {c["name"] for c in client.get(f"{PROF}/collections").json()}
    row = w.db.query(SpeakerCollection).filter(SpeakerCollection.name == name).one()
    assert row.organization_id == w.org_a.id
    for other in (w.org_b.id, None):
        with _acting_as(w.alice, other):
            assert name not in {c["name"] for c in client.get(f"{PROF}/collections").json()}


# --------------------------------------------------------------------------- #
# Linking a voiceprint: editor on the speaker's file, profile of its tenant     #
# --------------------------------------------------------------------------- #


@dataclass
class ShareWorld:
    db: Any
    owner: User
    guest: User
    org_id: int | None
    speaker: Speaker
    media_file: MediaFile
    guest_profile: SpeakerProfile


def _share_world(db, shape: str, permission: str) -> ShareWorld:
    """``owner`` shares one file with ``guest`` at ``permission``.

    ``shape="community"``: no organizations, everything personal (a community
    install). ``shape="org"``: both are members of one org and the file is the org's.
    """
    owner, guest = _mk_user(db, "owner"), _mk_user(db, "guest")
    org_id = None
    if shape == "org":
        org = _mk_org(db, "S")
        _join(db, org, owner)
        _join(db, org, guest)
        org_id = org.id
    media_file = _mk_file(db, owner, org_id)
    speaker = _mk_speaker(db, media_file)
    _share_file(db, owner, media_file, guest, permission)
    guest_profile = _mk_profile(db, guest, org_id)
    return ShareWorld(db, owner, guest, org_id, speaker, media_file, guest_profile)


SHAPES = ["community", "org"]
PERMISSIONS = [
    pytest.param("viewer", False, id="viewer"),
    pytest.param("editor", True, id="editor"),
]


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("permission,may_write", PERMISSIONS)
def test_assign_profile_needs_editor_on_the_speakers_file(
    client, db_session, shape, permission, may_write
):
    w = _share_world(db_session, shape, permission)
    with _acting_as(w.guest, w.org_id):
        resp = client.post(
            f"{PROF}/speakers/{w.speaker.uuid}/assign-profile",
            params={"profile_uuid": str(w.guest_profile.uuid)},
        )
    w.db.refresh(w.speaker)
    if may_write:
        assert resp.status_code == 200, resp.text
        assert w.speaker.profile_id == w.guest_profile.id
    else:
        assert resp.status_code == 403, resp.text
        assert w.speaker.profile_id is None, "a viewer linked the owner's voiceprint"


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("permission,may_write", PERMISSIONS)
def test_verify_accept_needs_editor_on_the_speakers_file(
    client, db_session, shape, permission, may_write
):
    w = _share_world(db_session, shape, permission)
    with _acting_as(w.guest, w.org_id):
        resp = client.post(
            f"{SPK}/{w.speaker.uuid}/verify",
            params={"action": "accept", "profile_uuid": str(w.guest_profile.uuid)},
        )
    w.db.refresh(w.speaker)
    if may_write:
        assert resp.status_code == 200, resp.text
        assert w.speaker.profile_id == w.guest_profile.id
    else:
        assert resp.status_code == 403, resp.text
        assert w.speaker.profile_id is None


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("permission,may_write", PERMISSIONS)
def test_create_speaker_needs_editor_and_is_stamped_with_the_files_tenant(
    client, db_session, shape, permission, may_write
):
    w = _share_world(db_session, shape, permission)
    with _acting_as(w.guest, w.org_id):
        resp = client.post(
            SPK,
            params={"media_file_uuid": str(w.media_file.uuid)},
            json={"name": "SPEAKER_09"},
        )
    if may_write:
        assert resp.status_code == 200, resp.text
        created = w.db.query(Speaker).filter(Speaker.uuid == uuid_pkg.UUID(resp.json()["uuid"]))
        assert created.one().organization_id == w.org_id
    else:
        assert resp.status_code == 403, resp.text


def test_assign_profile_refuses_a_profile_of_another_tenant(client, world):
    w = world
    personal_profile = _mk_profile(w.db, w.alice, None)
    for org_id in (w.org_a.id, None):
        with _acting_as(w.alice, org_id):
            resp = client.post(
                f"{PROF}/speakers/{w.a_speaker.uuid}/assign-profile",
                params={"profile_uuid": str(personal_profile.uuid)},
            )
        assert resp.status_code == 404, resp.text
    w.db.refresh(w.a_speaker)
    assert w.a_speaker.profile_id is None


def test_merge_refuses_speakers_of_two_owners_in_different_files(client, db_session):
    w = _share_world(db_session, "community", "editor")
    guest_file = _mk_file(db_session, w.guest, None)
    guest_speaker = _mk_speaker(db_session, guest_file)
    with _acting_as(w.guest, None):
        resp = client.post(f"{SPK}/{w.speaker.uuid}/merge/{guest_speaker.uuid}")
    assert resp.status_code == 400, resp.text
    assert db_session.get(Speaker, w.speaker.id) is not None


def test_profile_gender_confirm_rewrites_only_the_callers_speakers(client, db_session):
    w = _share_world(db_session, "community", "editor")
    own_file = _mk_file(db_session, w.guest, None)
    own_speaker = _mk_speaker(db_session, own_file)
    own_speaker.profile_id = w.guest_profile.id
    w.speaker.profile_id = w.guest_profile.id  # the owner's speaker, linked while shared
    db_session.commit()
    with _acting_as(w.guest, None):
        resp = client.post(
            f"{PROF}/profiles/{w.guest_profile.uuid}/confirm-gender", params={"gender": "male"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 1
    db_session.refresh(own_speaker)
    db_session.refresh(w.speaker)
    assert own_speaker.predicted_gender == "male"
    assert w.speaker.predicted_gender is None


# --------------------------------------------------------------------------- #
# Background paths: auto-profiling, retroactive matching, profile embedding    #
# --------------------------------------------------------------------------- #


def test_auto_profile_does_not_link_a_same_named_profile_of_another_tenant(world, monkeypatch):
    """The speaker gets its own tenant's "Jane Roe" instead (names are per tenant)."""
    from app.api.endpoints import speaker_update
    from app.services import opensearch_service
    from app.services import profile_embedding_service as pes

    w = world
    monkeypatch.setattr(
        pes.ProfileEmbeddingService, "add_speaker_to_profile_embedding", lambda *a, **k: True
    )
    monkeypatch.setattr(opensearch_service, "update_speaker_profile", lambda **_k: True)
    personal = _mk_profile(w.db, w.alice, None, name="Jane Roe")
    assert speaker_update.auto_create_or_assign_profile(w.a_speaker, "Jane Roe", w.db) is True
    w.db.flush()
    linked = w.db.get(SpeakerProfile, w.a_speaker.profile_id)
    assert linked is not None and linked.id != personal.id
    assert (linked.name, linked.organization_id) == ("Jane Roe", w.org_a.id)

    # A second org-A speaker of that name joins the org-A profile, not a third one.
    again = _mk_speaker(w.db, _mk_file(w.db, w.alice, w.org_a.id))
    assert speaker_update.auto_create_or_assign_profile(again, "Jane Roe", w.db) is True
    w.db.flush()
    assert again.profile_id == linked.id


@pytest.mark.parametrize("kind", ["profile", "collection"])
def test_a_name_can_be_reused_in_another_tenant_but_not_twice_in_one(client, world, kind):
    w = world
    name = f"Reused {uuid_pkg.uuid4().hex[:8]}"
    path = f"{PROF}/profiles" if kind == "profile" else f"{PROF}/collections"
    for org_id in (w.org_a.id, w.org_b.id, None):
        with _acting_as(w.alice, org_id):
            first = client.post(path, params={"name": name})
            second = client.post(path, params={"name": name})
        assert first.status_code == 200, first.text
        assert second.status_code == 400, second.text


def test_a_profile_rename_is_checked_only_inside_its_tenant(client, world):
    w = world
    name = f"Renamed {uuid_pkg.uuid4().hex[:8]}"
    _mk_profile(w.db, w.alice, None, name=name)
    with _acting_as(w.alice, w.org_a.id):
        resp = client.put(f"{PROF}/profiles/{w.a_profile.uuid}", params={"name": name})
        assert resp.status_code == 200, resp.text
        clash = _mk_profile(w.db, w.alice, w.org_a.id)
        resp = client.put(f"{PROF}/profiles/{clash.uuid}", params={"name": name})
        assert resp.status_code == 400, resp.text


def test_create_profile_from_speaker_allows_a_name_held_in_another_tenant(client, world):
    w = world
    name = f"Speaker Name {uuid_pkg.uuid4().hex[:8]}"
    _mk_profile(w.db, w.alice, None, name=name)
    with _acting_as(w.alice, w.org_a.id):
        resp = client.post(
            f"{SPK}/{w.a_speaker.uuid}/verify",
            params={"action": "create_profile", "profile_name": name},
        )
    assert resp.status_code == 200, resp.text
    w.db.refresh(w.a_speaker)
    linked = w.db.get(SpeakerProfile, w.a_speaker.profile_id)
    assert (linked.name, linked.organization_id) == (name, w.org_a.id)


def test_retroactive_matching_scores_only_same_tenant_candidates(world, monkeypatch):
    from app.api.endpoints import speaker_update

    w = world
    other_org_speaker = _mk_speaker(w.db, _mk_file(w.db, w.alice, w.org_a.id))
    seen: list[set[int]] = []

    def fake_score(_embedding, candidates, _name):
        seen.append({c["id"] for c in candidates})
        return []

    monkeypatch.setattr(speaker_update, "get_speaker_embedding", lambda _uuid: [1.0, 0.0])
    monkeypatch.setattr(speaker_update, "_score_candidates", fake_score)
    speaker_update.trigger_retroactive_matching(w.a_speaker, w.db)

    assert seen == [{other_org_speaker.id}], "a personal speaker was scored for an org label"


def test_profile_embedding_averages_only_speakers_of_the_profiles_tenant(world, monkeypatch):
    from app.services import profile_embedding_service as pes

    w = world
    w.a_speaker.profile_id = w.a_profile.id
    w.p_speaker.profile_id = w.a_profile.id
    w.db.commit()
    fetched: list[str] = []

    def fake_embedding(speaker_uuid):
        fetched.append(speaker_uuid)
        return [1.0, 0.0]

    monkeypatch.setattr(pes, "get_speaker_embedding", fake_embedding)
    monkeypatch.setattr(pes, "_process_profile_with_embeddings", lambda *a, **k: None)
    assert pes.ProfileEmbeddingService.update_profile_embedding(w.db, w.a_profile.id)
    assert fetched == [str(w.a_speaker.uuid)]


# --------------------------------------------------------------------------- #
# Clusters                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def cluster_world(world) -> tuple[World, SpeakerCluster]:
    # A pair, not a lone speaker: the cluster list shows groups (#1192), and a one-speaker
    # cluster would be absent for the active tenant too, passing the "hidden elsewhere" half
    # of the listing tests for the wrong reason.
    partner = _mk_speaker(world.db, world.a_file, name="SPEAKER_06")
    return world, _mk_cluster(world.db, world.alice, world.org_a.id, [world.a_speaker, partner])


#: (method, path, body, status in the cluster's tenant, status outside it). Outside
#: it a cluster is "not found": 404, or the 400 the service-backed promote/split
#: routes answer for any cluster they cannot resolve.
CLUSTER_ROUTES = [
    ("GET", f"{CLU}/{{c}}", None, 200, 404),
    ("PUT", f"{CLU}/{{c}}", {"label": "Renamed"}, 200, 404),
    ("POST", f"{CLU}/{{c}}/promote", {"name": "Promoted Person"}, 200, 400),
    ("POST", f"{CLU}/{{c}}/analyze-outliers", None, 200, 404),
    ("POST", f"{CLU}/{{c}}/unassign", {"speaker_uuids": ["{s}"], "blacklist": False}, 200, 404),
    ("POST", f"{CLU}/{{c}}/split", {"speaker_uuids": ["{s}"]}, 200, 400),
    ("DELETE", f"{CLU}/{{c}}", None, 204, 404),
]


def _fill(obj: Any, **values: str) -> Any:
    if isinstance(obj, str):
        return obj.format(**values)
    if isinstance(obj, list):
        return [_fill(v, **values) for v in obj]
    if isinstance(obj, dict):
        return {k: _fill(v, **values) for k, v in obj.items()}
    return obj


@pytest.mark.parametrize("org_name,in_scope", TENANTS)
@pytest.mark.parametrize(
    "method,path,body,ok_status,foreign_status",
    CLUSTER_ROUTES,
    ids=[f"{r[0]} {r[1]}" for r in CLUSTER_ROUTES],
)
def test_cluster_routes_are_confined_to_the_clusters_tenant(
    client, cluster_world, org_name, in_scope, method, path, body, ok_status, foreign_status
):
    w, cluster = cluster_world
    values = {"c": str(cluster.uuid), "s": str(w.a_speaker.uuid)}
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.request(method, _fill(path, **values), json=_fill(body, **values))
    assert resp.status_code == (ok_status if in_scope else foreign_status), resp.text
    if not in_scope:
        w.db.refresh(cluster)
        w.db.refresh(w.a_speaker)
        assert cluster.label is None and cluster.promoted_to_profile_id is None
        assert w.a_speaker.cluster_id == cluster.id


@pytest.mark.parametrize("org_name,in_scope", TENANTS)
def test_cluster_listings_show_only_the_active_tenant(client, cluster_world, org_name, in_scope):
    w, cluster = cluster_world
    with _acting_as(w.alice, w.org(org_name)):
        listed = client.get(CLU).json()
        stats = client.get(f"{CLU}/stats").json()
        inbox_speaker = _mk_speaker(w.db, w.a_file, name="SPEAKER_07")
        inbox = client.get(f"{CLU}/unverified/inbox").json()
    assert (str(cluster.uuid) in {c["uuid"] for c in listed["items"]}) is in_scope
    assert stats["total_clusters"] == (1 if in_scope else 0)
    assert (str(inbox_speaker.uuid) in {i["speaker_uuid"] for i in inbox["items"]}) is in_scope


@pytest.mark.parametrize("org_name", ["B", None])
def test_batch_verify_skips_speakers_outside_the_active_tenant(client, world, org_name):
    w = world
    target = _mk_speaker(w.db, w.a_file, name="SPEAKER_05")
    with _acting_as(w.alice, w.org(org_name)):
        resp = client.post(
            f"{CLU}/batch-verify", json={"speaker_uuids": [str(target.uuid)], "action": "skip"}
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["failed_count"] == 1
    w.db.refresh(target)
    assert not target.verified


def test_batch_assign_refuses_a_profile_of_another_tenant(client, world):
    w = world
    target = _mk_speaker(w.db, w.p_file, name="SPEAKER_05")
    with _acting_as(w.alice, None):
        resp = client.post(
            f"{CLU}/batch-verify",
            json={
                "speaker_uuids": [str(target.uuid)],
                "action": "assign",
                "profile_uuid": str(w.a_profile.uuid),
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated_count"] == 0
    w.db.refresh(target)
    assert target.profile_id is None


def test_promote_refuses_a_cluster_whose_members_span_tenants(client, world):
    w = world
    mixed = _mk_cluster(w.db, w.alice, w.org_a.id, [w.a_speaker, w.p_speaker])
    with _acting_as(w.alice, w.org_a.id):
        resp = client.post(f"{CLU}/{mixed.uuid}/promote", json={"name": "Mixed"})
    assert resp.status_code == 400, resp.text
    w.db.refresh(w.p_speaker)
    assert w.p_speaker.profile_id is None


def test_merge_refuses_clusters_of_two_tenants(client, world):
    w = world
    org_cluster = _mk_cluster(w.db, w.alice, w.org_a.id, [w.a_speaker])
    personal_cluster = _mk_cluster(w.db, w.alice, None, [w.p_speaker])
    for org_id in (w.org_a.id, None):
        with _acting_as(w.alice, org_id):
            resp = client.post(f"{CLU}/{personal_cluster.uuid}/merge/{org_cluster.uuid}")
        assert resp.status_code == 400, resp.text
    w.db.refresh(w.p_speaker)
    assert w.p_speaker.cluster_id == personal_cluster.id
