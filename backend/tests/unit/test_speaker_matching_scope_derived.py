"""The tenant a matching call is judged in comes from the speaker's FILE, not the caller (#1280).

``SpeakerMatchingService`` used to default ``organization_id`` to ``None``, which the policy
gate reads as PERSONAL scope. A caller that simply omitted the argument (the batch script calls
``find_and_store_speaker_matches`` that way) was therefore judged against the owner's personal
switches, and an organization that had voiceprints off was never consulted.

Now a call that names a speaker derives the organization from that speaker's file (as
``PrivacyView`` does) and treats the argument only as a hint that can narrow but never widen. A
call that names no speaker and gives no organization cannot know its scope and is refused,
because "I don't know" must not read as "personal".

Every refusal has its control (the same call is allowed when the scope allows it), so a gate
that always said no could not pass.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from app.services.speaker_matching_service import SpeakerMatchingService
from tests.fixtures.privacy_switches import file_in
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams")

VOICE = np.array([1.0, 0.0, 0.0, 0.0])


@pytest.fixture
def knn(monkeypatch) -> dict[str, Any]:
    """Record the kNN reads and the tenant scope they were made in."""
    calls: dict[str, Any] = {"speaker": 0, "profile": 0, "orgs": []}

    def find_matching_speaker(*args, **kwargs):
        calls["speaker"] += 1
        calls["orgs"].append(kwargs.get("organization_id"))
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
def test_a_caller_that_omits_the_org_is_judged_in_the_speakers_file_organization(
    db_session, normal_user, knn, allowed
):
    org = make_org(db_session, voiceprints=allowed)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, org))

    SpeakerMatchingService(db_session, None).find_and_store_speaker_matches(
        speaker.id, VOICE, normal_user.id
    )

    assert knn["speaker"] == (1 if allowed else 0)
    if allowed:
        assert knn["orgs"] == [org.id]  # the kNN is scoped to the file's tenant, not personal


def test_a_hint_naming_a_permissive_org_cannot_widen_a_file_whose_org_is_off(
    db_session, normal_user, knn
):
    off, on = make_org(db_session, voiceprints=False), make_org(db_session, voiceprints=True)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, off))

    SpeakerMatchingService(db_session, None).find_and_store_speaker_matches(
        speaker.id, VOICE, normal_user.id, organization_id=on.id
    )

    assert knn["speaker"] == 0


def test_a_hint_naming_a_stricter_org_still_narrows(db_session, normal_user, knn):
    on, off = make_org(db_session, voiceprints=True), make_org(db_session, voiceprints=False)
    speaker = make_speaker(db_session, normal_user, file_in(db_session, normal_user, on))

    SpeakerMatchingService(db_session, None).find_and_store_speaker_matches(
        speaker.id, VOICE, normal_user.id, organization_id=off.id
    )

    assert knn["speaker"] == 0


def test_an_unknown_speaker_is_refused(db_session, normal_user, knn):
    SpeakerMatchingService(db_session, None).find_and_store_speaker_matches(
        2**31 - 1, VOICE, normal_user.id
    )

    assert knn["speaker"] == 0


@pytest.mark.parametrize(
    "call",
    [
        lambda svc, user: svc.match_speaker_to_known_speakers(VOICE, user.id),
        lambda svc, user: svc.match_speaker_to_profiles(VOICE, user.id),
        lambda svc, user: svc.match_speaker_to_profile(VOICE, user.id),
    ],
    ids=["known_speakers", "profiles", "profile"],
)
def test_an_embedding_only_call_that_names_no_scope_is_refused(db_session, normal_user, knn, call):
    assert call(SpeakerMatchingService(db_session, None), normal_user) is None

    assert knn["speaker"] == 0 and knn["profile"] == 0


def test_an_embedding_only_call_that_states_personal_scope_still_runs(db_session, normal_user, knn):
    """The control: an explicit ``None`` is personal scope, which is a statement, not a gap."""
    SpeakerMatchingService(db_session, None).match_speaker_to_profile(
        VOICE, normal_user.id, organization_id=None
    )

    assert knn["speaker"] == 1
