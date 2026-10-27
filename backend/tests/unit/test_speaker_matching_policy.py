"""``SpeakerMatchingService`` and ``ProfileEmbeddingService`` under the privacy policy (#1280).

Every entry point that matches, stores or reads a voiceprint asks the policy for the scope of
the record it works on. Each case is parametrised over ``allowed`` so the same call is made
with the organization's switch on (the control: the work really happens) and off (nothing
is read, matched or written), which a gate that always said no could not pass.

OpenSearch is replaced by recorders, so "nothing was read" is asserted directly.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import numpy as np
import pytest

from app.models.media import SpeakerMatch
from app.models.media import SpeakerProfile
from app.services.profile_embedding_service import ProfileEmbeddingRecomputeError
from app.services.profile_embedding_service import ProfileEmbeddingService
from app.services.speaker_matching_service import SpeakerMatchingService
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.privacy_switches import set_instance_switch
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

VOICE = np.array([1.0, 0.0, 0.0, 0.0])


def _note(sink: list[Any], value: Any, result: Any) -> Any:
    """Record ``value`` and return ``result`` (``list.append`` returns None, so a lambda cannot)."""
    sink.append(value)
    return result


@pytest.fixture
def knn(monkeypatch) -> dict[str, int]:
    """Record the kNN reads the matching service makes."""
    calls = {"speaker": 0, "profile": 0}

    def find_matching_speaker(*args, **kwargs):
        calls["speaker"] += 1
        return None

    def calculate_profile_similarity(*args, **kwargs):
        calls["profile"] += 1
        return []

    monkeypatch.setattr(
        "app.services.speaker_matching_service.find_matching_speaker", find_matching_speaker
    )
    monkeypatch.setattr(
        "app.services.profile_embedding_service.ProfileEmbeddingService.calculate_profile_similarity",
        staticmethod(calculate_profile_similarity),
    )
    return calls


@pytest.mark.parametrize("allowed", [True, False])
def test_matching_a_voiceprint_to_known_speakers_reads_the_index_only_when_allowed(
    db_session, normal_user, knn, allowed
):
    org = make_org(db_session, voiceprints=allowed)

    result = SpeakerMatchingService(db_session, None).match_speaker_to_known_speakers(
        VOICE, normal_user.id, organization_id=org.id
    )

    assert result is None
    assert knn == {"speaker": 1 if allowed else 0, "profile": 1 if allowed else 0}


@pytest.mark.parametrize("allowed", [True, False])
def test_matching_to_profiles_runs_the_profile_knn_only_when_allowed(
    db_session, normal_user, knn, allowed
):
    org = make_org(db_session, voiceprints=allowed)

    SpeakerMatchingService(db_session, None).match_speaker_to_profiles(
        VOICE, normal_user.id, organization_id=org.id
    )

    assert knn["profile"] == (1 if allowed else 0)


@pytest.mark.parametrize("allowed", [True, False])
def test_matching_to_a_single_profile_reads_the_index_only_when_allowed(
    db_session, normal_user, knn, allowed
):
    org = make_org(db_session, voiceprints=allowed)

    SpeakerMatchingService(db_session, None).match_speaker_to_profile(
        VOICE, normal_user.id, organization_id=org.id
    )

    assert knn["speaker"] == (1 if allowed else 0)


@pytest.mark.parametrize("allowed", [True, False])
def test_storing_speaker_matches_reads_the_index_only_when_allowed(
    db_session, normal_user, knn, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))

    found = SpeakerMatchingService(db_session, None).find_and_store_speaker_matches(
        speaker.id, VOICE, normal_user.id, organization_id=org.id
    )

    assert found == []
    assert knn["speaker"] == (1 if allowed else 0)


@pytest.mark.parametrize("allowed", [True, False])
def test_unlabeled_matches_run_the_knn_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    """The index is reached through ``opensearch_client``, which a recorder stands in for."""
    searches: list[dict[str, Any]] = []

    class Client:
        def search(self, index, body):
            searches.append(body)
            return {"hits": {"hits": []}}

    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", Client())
    org = make_org(db_session, voiceprints=allowed)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))

    SpeakerMatchingService(db_session, None).find_unlabeled_speaker_matches(
        VOICE, normal_user.id, speaker.id
    )

    assert len(searches) == (1 if allowed else 0)


@pytest.mark.parametrize("allowed", [True, False])
def test_a_files_native_voiceprints_are_matched_and_stored_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    media_file = file_in(db_session, normal_user, org)
    speaker = make_speaker(db_session, normal_user, media_file)
    handled: list[int] = []

    def process_single(self, speaker_id, embeddings, user_id, media_file_id, **kwargs):
        handled.append(speaker_id)
        return {"speaker_id": speaker_id}

    monkeypatch.setattr(SpeakerMatchingService, "_process_single_speaker", process_single)

    results = SpeakerMatchingService(db_session, None).process_speaker_embeddings_native(
        media_file.id, normal_user.id, {speaker.id: VOICE}
    )

    assert results == ([{"speaker_id": speaker.id}] if allowed else [])
    assert handled == ([speaker.id] if allowed else [])


@pytest.mark.parametrize("allowed", [True, False])
def test_a_single_speakers_voiceprint_is_never_stored_when_off(
    db_session, normal_user, knn, monkeypatch, allowed
):
    """A no-match result STORES the voiceprint, so the gate sits in the per-speaker step too."""
    org = make_org(db_session, voiceprints=allowed)
    media_file = file_in(db_session, normal_user, org)
    speaker = make_speaker(db_session, normal_user, media_file)
    stored: list[int] = []
    monkeypatch.setattr(
        SpeakerMatchingService,
        "_handle_no_speaker_match",
        lambda self, spk, emb, uid, fid, **kw: _note(stored, spk.id, {"stored": True}),
    )

    result = SpeakerMatchingService(db_session, None)._process_single_speaker(
        speaker.id, [VOICE], normal_user.id, media_file.id, organization_id=org.id
    )

    assert (result is not None) is allowed
    assert stored == ([speaker.id] if allowed else [])


@pytest.mark.parametrize("allowed", [True, False])
def test_stored_speaker_matches_are_listed_only_when_allowed(db_session, normal_user, allowed):
    org = make_org(db_session, voiceprints=allowed)
    a = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org), name="A")
    b = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org), name="B")
    db_session.add(SpeakerMatch(speaker1_id=a.id, speaker2_id=b.id, confidence=0.97))
    db_session.commit()

    matches = SpeakerMatchingService(db_session, None).get_speaker_matches(a.id)

    assert [m["speaker_id"] for m in matches] == ([b.id] if allowed else [])
    # Hidden, never deleted: the pair is still stored.
    assert db_session.query(SpeakerMatch).filter(SpeakerMatch.speaker1_id == a.id).count() == 1


@pytest.mark.parametrize("allowed", [True, False])
def test_profile_assignment_is_propagated_to_look_alike_speakers_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="P")
    profile.organization_id = org.id
    db_session.add(profile)
    db_session.flush()
    searches: list[int] = []
    monkeypatch.setattr(
        SpeakerMatchingService,
        "_get_speaker_embedding_for_propagation",
        lambda self, speaker_id: ([0.1] * 4, "stub"),
    )
    monkeypatch.setattr(
        "app.services.similarity_service.SimilarityService.opensearch_similarity_search",
        staticmethod(lambda **kwargs: _note(searches, 1, [])),
    )

    SpeakerMatchingService(db_session, None)._propagate_profile_assignment(
        matched_speaker_id=1, profile_id=profile.id, user_id=normal_user.id
    )

    assert len(searches) == (1 if allowed else 0)


@pytest.mark.parametrize("allowed", [True, False])
def test_a_profile_reference_embedding_is_stored_only_when_allowed(
    db_session, normal_user, monkeypatch, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="P")
    profile.organization_id = org.id
    db_session.add(profile)
    db_session.flush()
    stored: list[str] = []
    monkeypatch.setattr(
        "app.services.opensearch_service.store_profile_embedding",
        lambda **kwargs: _note(stored, kwargs["profile_uuid"], True),
    )

    class Extractor:
        def extract_reference_embedding(self, paths):
            return VOICE

    ok = SpeakerMatchingService(db_session, Extractor()).update_speaker_embeddings(  # type: ignore[arg-type]
        profile.id, ["a.wav"]
    )

    assert ok is allowed
    assert stored == ([str(profile.uuid)] if allowed else [])


# ---------------------------------------------------------------------------
# ProfileEmbeddingService
# ---------------------------------------------------------------------------


@pytest.fixture
def profile_world(db_session, normal_user, monkeypatch):
    """A profile in an organization with one linked speaker, and recorders for its writes."""

    def build(*, allowed: bool):
        org = make_org(db_session, voiceprints=allowed)
        profile = SpeakerProfile(uuid=uuid_pkg.uuid4(), user_id=normal_user.id, name="P")
        profile.organization_id = org.id
        db_session.add(profile)
        db_session.flush()
        make_speaker(
            db_session,
            normal_user,
            file_in(db_session, normal_user, org),
            profile_id=profile.id,
        )
        db_session.commit()
        return profile

    writes: list[int] = []
    monkeypatch.setattr(
        "app.services.profile_embedding_service.get_speaker_embedding", lambda u: [1.0, 0.0]
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.get_speaker_embeddings_batch",
        lambda uuids: {u: [1.0, 0.0] for u in uuids},
    )
    monkeypatch.setattr(
        "app.services.profile_embedding_service._process_profile_with_embeddings",
        lambda profile, profile_id, embeddings: _note(writes, profile_id, True),
    )
    return build, writes


@pytest.mark.parametrize("allowed", [True, False])
def test_a_profile_average_is_computed_and_stored_only_when_allowed(
    db_session, profile_world, allowed
):
    build, writes = profile_world
    profile = build(allowed=allowed)

    ok = ProfileEmbeddingService.update_profile_embedding(db_session, profile.id)

    assert ok is allowed
    assert writes == ([profile.id] if allowed else [])


def test_a_strict_rebuild_raises_while_off_so_the_caller_erases_the_stale_voice(
    db_session, profile_world
):
    """A removed speaker's voice must not survive in a profile that cannot be rebuilt."""
    build, writes = profile_world
    profile = build(allowed=False)

    with pytest.raises(ProfileEmbeddingRecomputeError):
        ProfileEmbeddingService.update_profile_embedding(db_session, profile.id, strict=True)

    assert writes == []


@pytest.mark.parametrize("allowed", [True, False])
def test_the_batch_rebuild_skips_a_profile_whose_scope_is_off(db_session, profile_world, allowed):
    build, writes = profile_world
    profile = build(allowed=allowed)

    result = ProfileEmbeddingService.batch_update_profile_embeddings(db_session, [profile.id])

    assert result == {profile.id: allowed}
    assert writes == ([profile.id] if allowed else [])


def test_profiles_off_by_policy_names_the_layer_that_turned_each_off(db_session, profile_world):
    build, _ = profile_world
    off = build(allowed=False)
    on = build(allowed=True)

    reported = ProfileEmbeddingService.profiles_off_by_policy(db_session, [off.id, on.id])

    assert {pid: d.locked_by for pid, d in reported.items()} == {off.id: "organization"}


def test_the_instance_switch_skips_every_profile(db_session, profile_world):
    build, writes = profile_world
    profile = build(allowed=True)
    set_instance_switch(db_session, "voiceprints", False)

    assert ProfileEmbeddingService.update_profile_embedding(db_session, profile.id) is False
    assert writes == []
