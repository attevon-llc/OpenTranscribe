"""A shared profile's voiceprint is matched only if its OWNER's policy and consent allow it (#1280).

User A owns a profile that a shared collection makes visible to user B. B may still SEE that
profile (a name and an avatar are user assertions), but B's audio is compared to A's voiceprint
only while A's scope has voiceprints on and every ``share`` consent hook says yes (plan P15,
restrict-only: sharing never widens what the owner's own policy forbids).

Each case has a control with the owner's switch on, and the visibility assertion proves the
matching filter did not also hide the profile from the person it was shared with.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import SpeakerProfile
from app.models.sharing import CollectionShare
from app.services.permission_service import PermissionService
from app.services.privacy.consent import VoiceprintConsentRequest
from app.services.privacy.consent import clear_voiceprint_consent_hooks
from app.services.privacy.consent import register_voiceprint_consent_hook
from tests.fixtures.privacy_switches import set_user_switch
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

PROFILES = "/api/speaker-profiles"


@pytest.fixture(autouse=True)
def _no_consent_hooks_leak():
    clear_voiceprint_consent_hooks()
    yield
    clear_voiceprint_consent_hooks()


@pytest.fixture
def shared(db_session, normal_user, other_user) -> dict[str, Any]:
    """A's profile, reachable by B through a shared collection; B has a file and speaker too."""
    a_file = make_file(db_session, normal_user)
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="Senator Smith")
    db_session.add(profile)
    db_session.flush()
    make_speaker(
        db_session, normal_user, a_file, display_name="Senator Smith", profile_id=profile.id
    )
    collection = Collection(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="shared")
    db_session.add(collection)
    db_session.flush()
    db_session.add(CollectionMember(collection_id=collection.id, media_file_id=a_file.id))
    db_session.add(
        CollectionShare(
            uuid=uuid_pkg.uuid4(),
            collection_id=collection.id,
            shared_by_id=normal_user.id,
            target_type="user",
            target_user_id=other_user.id,
            permission="viewer",
        )
    )
    b_file = make_file(db_session, other_user)
    b_speaker = make_speaker(db_session, other_user, b_file)
    db_session.commit()
    return {"profile": profile, "b_file": b_file, "b_speaker": b_speaker}


def _matchable(db, user) -> set[int]:
    return PermissionService.get_matchable_profile_ids(db, user.id, organization_id=None)


def _visible(db, user) -> set[int]:
    return PermissionService.get_accessible_profile_ids(db, user.id, organization_id=None)


def test_a_shared_profile_is_matchable_while_its_owner_allows_voiceprints(
    db_session, other_user, shared
):
    assert shared["profile"].id in _matchable(db_session, other_user)


def test_a_shared_profile_is_not_matchable_once_its_owner_switches_voiceprints_off(
    db_session, normal_user, other_user, shared
):
    set_user_switch(db_session, normal_user, "voiceprints", False)

    assert shared["profile"].id not in _matchable(db_session, other_user)
    # Still shared, still named, still visible to B: only the matching is withheld.
    assert shared["profile"].id in _visible(db_session, other_user)


def test_the_matching_variant_for_a_file_drops_the_same_profile(
    db_session, normal_user, other_user, shared
):
    file_id = shared["b_file"].id
    assert shared["profile"].id in PermissionService.get_accessible_profile_ids_for_file(
        db_session, other_user.id, file_id
    )

    set_user_switch(db_session, normal_user, "voiceprints", False)

    assert shared["profile"].id not in PermissionService.get_accessible_profile_ids_for_file(
        db_session, other_user.id, file_id
    )


def test_a_requesters_own_profiles_stay_matchable_whatever_the_owner_of_a_share_chose(
    db_session, normal_user, other_user, shared
):
    own = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=other_user.id, name="Mine")
    db_session.add(own)
    db_session.commit()
    set_user_switch(db_session, normal_user, "voiceprints", False)

    assert own.id in _matchable(db_session, other_user)


def test_a_share_consent_hook_that_says_no_removes_the_profile(db_session, other_user, shared):
    seen: list[VoiceprintConsentRequest] = []

    def hook(db, request: VoiceprintConsentRequest) -> bool:
        seen.append(request)
        return request.purpose != "share"

    register_voiceprint_consent_hook(hook)

    assert shared["profile"].id not in _matchable(db_session, other_user)
    share_requests = [r for r in seen if r.purpose == "share"]
    assert len(share_requests) == 1
    assert share_requests[0].requester_user_id == other_user.id
    assert shared["profile"].id in share_requests[0].profile_ids
    # Visibility is not what consent governs.
    assert shared["profile"].id in _visible(db_session, other_user)


def test_a_share_consent_hook_that_says_yes_keeps_the_profile(db_session, other_user, shared):
    register_voiceprint_consent_hook(lambda db, request: True)

    assert shared["profile"].id in _matchable(db_session, other_user)


def test_a_share_consent_hook_that_raises_removes_the_profile(db_session, other_user, shared):
    def broken(db, request):
        raise RuntimeError("consent store unreachable")

    register_voiceprint_consent_hook(broken)

    assert shared["profile"].id not in _matchable(db_session, other_user)


def test_the_suggestions_endpoint_hands_the_knn_only_matchable_profiles(
    client, other_user_auth_headers, db_session, normal_user, shared, speaker_os_seams, monkeypatch
):
    speaker_os_seams["voiceprints"][str(shared["b_speaker"].uuid)] = [1.0, 0.0, 0.0, 0.0]
    scopes: list[set[int] | None] = []

    def similarity(embedding, user_id, **kwargs):
        scopes.append(kwargs.get("accessible_profile_ids"))
        return []

    monkeypatch.setattr(
        "app.services.profile_embedding_service.ProfileEmbeddingService.calculate_profile_similarity",
        staticmethod(similarity),
    )
    url = f"{PROFILES}/speakers/{shared['b_speaker'].uuid}/suggestions"

    assert client.get(url, headers=other_user_auth_headers).status_code == 200
    assert shared["profile"].id in (scopes[-1] or set())

    set_user_switch(db_session, normal_user, "voiceprints", False)
    assert client.get(url, headers=other_user_auth_headers).status_code == 200
    assert shared["profile"].id not in (scopes[-1] or set())
