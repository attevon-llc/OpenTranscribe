"""``SpeakerProfile.embedding_count`` must describe the profile, not accumulate.

Found while fixing #541. Two writers disagreed about what the column means:

* ``profile_embedding_service._process_profile_with_embeddings`` — the authoritative
  full recalculation, and what ``POST /admin/profile-embeddings/repair`` converges on —
  sets ``embedding_count = len(embeddings)``, i.e. the profile's member population.
* ``speaker_matching_service._handle_speaker_match`` read the column back and did
  ``count += 1`` on **every** auto-accepted match, with no check that the speaker was
  already a member.

So re-matching a speaker that already belonged to the profile — which is exactly what
reprocessing a file produces — inflated the count without adding a member. Measured on
the dev stack before the fix: profile 8705 ("Joe Rogan") read ``embedding_count = 7``
against **3** linked speakers.

It is not a cosmetic counter. The incremental blend is
``(stored * count + new) / (count + 1)``, so an inflated count silently under-weights
every subsequent piece of voice evidence — by more than 2x in the measured case — and
the centroid progressively freezes. Nothing surfaces it: there is no error, no log, and
the profile keeps matching.

The fix derives the count from the profile's linked speakers, so the incremental path
converges on the authoritative definition instead of drifting away from it until an
admin happens to run a full recalculation.
"""

from __future__ import annotations

import datetime as dt
import uuid as uuid_pkg

import numpy as np
import pytest

pytestmark = pytest.mark.unit

EMBEDDING_DIMENSION = 8


def _make_file(db, user, *, title="Recording"):
    from app.models.media import MediaFile

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"{title}.mp4",
        title=title,
        storage_path=f"media/test/{uuid_pkg.uuid4()}.mp4",
        content_type="video/mp4",
        file_size=1000,
        status="completed",
        upload_time=dt.datetime.now(dt.UTC),
    )
    db.add(media)
    db.flush()
    return media


def _make_profile(db, user, *, embedding_count: int):
    from app.models.media import SpeakerProfile

    profile = SpeakerProfile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        name=f"P-{uuid_pkg.uuid4().hex[:6]}",
        embedding_count=embedding_count,
    )
    db.add(profile)
    db.flush()
    return profile


def _make_speaker(db, user, media, profile=None, *, name="SPEAKER_00"):
    from app.models.media import Speaker

    speaker = Speaker(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        media_file_id=media.id,
        name=name,
        profile_id=profile.id if profile else None,
    )
    db.add(speaker)
    db.flush()
    return speaker


@pytest.fixture
def matching_service(db_session, monkeypatch):
    """The real service, with only its OpenSearch/propagation seams stubbed.

    The arithmetic under test is the service's own, so nothing about the count is
    faked — only the calls that would reach a cluster.
    """
    from app.services import speaker_matching_service as sms

    stored: list[dict] = []

    monkeypatch.setattr(sms, "add_speaker_embedding", lambda **kw: None)
    monkeypatch.setattr(
        "app.services.opensearch_service.get_profile_embedding",
        lambda _uuid: [1.0] + [0.0] * (EMBEDDING_DIMENSION - 1),
    )
    monkeypatch.setattr(
        "app.services.opensearch_service.store_profile_embedding",
        lambda **kw: stored.append(kw),
    )
    monkeypatch.setattr("app.services.opensearch_service.update_speaker_profile", lambda **kw: None)

    service = sms.SpeakerMatchingService(db_session, embedding_service=None)
    monkeypatch.setattr(service, "find_and_store_speaker_matches", lambda *a, **k: None)
    monkeypatch.setattr(service, "_propagate_profile_assignment", lambda *a, **k: None)
    service._stored_profile_embeddings = stored  # type: ignore[attr-defined]
    return service


def _match(profile) -> dict:
    return {
        "confidence": 0.95,
        "auto_accept": True,
        "suggested_name": profile.name,
        "profile_id": profile.id,
    }


def _embedding() -> np.ndarray:
    vector = np.zeros(EMBEDDING_DIMENSION, dtype=float)
    vector[1] = 1.0
    return vector


def test_rematching_an_existing_member_does_not_inflate_the_count(
    db_session, normal_user, matching_service
):
    """The reprocess case, and the one that produced 7-against-3 on the dev stack.

    The speaker is ALREADY linked to the profile, so re-matching it adds no member
    and the count must not move.
    """
    media = _make_file(db_session, normal_user)
    profile = _make_profile(db_session, normal_user, embedding_count=1)
    speaker = _make_speaker(db_session, normal_user, media, profile)
    db_session.flush()

    matching_service._handle_speaker_match(
        speaker, _match(profile), _embedding(), normal_user.id, media.id
    )
    db_session.refresh(profile)

    assert profile.embedding_count == 1, "a re-match must not add a phantom member"


def test_repeated_rematches_do_not_accumulate(db_session, normal_user, matching_service):
    """Every reprocess of the same file used to add one. Three reprocesses, +3."""
    media = _make_file(db_session, normal_user)
    profile = _make_profile(db_session, normal_user, embedding_count=1)
    speaker = _make_speaker(db_session, normal_user, media, profile)
    db_session.flush()

    for _ in range(3):
        matching_service._handle_speaker_match(
            speaker, _match(profile), _embedding(), normal_user.id, media.id
        )
    db_session.refresh(profile)

    assert profile.embedding_count == 1


def test_a_genuinely_new_member_raises_the_count_by_one(db_session, normal_user, matching_service):
    """The control. Without it, "always return the same number" would pass above."""
    media_one = _make_file(db_session, normal_user, title="One")
    media_two = _make_file(db_session, normal_user, title="Two")
    profile = _make_profile(db_session, normal_user, embedding_count=1)
    _make_speaker(db_session, normal_user, media_one, profile)
    newcomer = _make_speaker(db_session, normal_user, media_two, None)
    db_session.flush()

    matching_service._handle_speaker_match(
        newcomer, _match(profile), _embedding(), normal_user.id, media_two.id
    )
    db_session.refresh(profile)

    assert profile.embedding_count == 2, "a new member must be counted"


def test_the_count_is_repaired_from_a_drifted_value(db_session, normal_user, matching_service):
    """A profile already carrying a drifted count converges on the truth.

    Deriving rather than incrementing means existing damage heals on the next match
    instead of persisting until an admin runs a full recalculation.
    """
    media = _make_file(db_session, normal_user)
    profile = _make_profile(db_session, normal_user, embedding_count=7)
    speaker = _make_speaker(db_session, normal_user, media, profile)
    _make_speaker(db_session, normal_user, _make_file(db_session, normal_user), profile)
    db_session.flush()

    matching_service._handle_speaker_match(
        speaker, _match(profile), _embedding(), normal_user.id, media.id
    )
    db_session.refresh(profile)

    assert profile.embedding_count == 2, "the stored 7 must not survive a real measurement"


def test_the_stored_centroid_is_weighted_by_the_members_it_represents(
    db_session, normal_user, matching_service
):
    """The count is what the blend weights the stored centroid by.

    With one existing member and one newcomer the result must sit halfway between
    the two vectors, not be dominated by a phantom population.
    """
    media_one = _make_file(db_session, normal_user, title="One")
    media_two = _make_file(db_session, normal_user, title="Two")
    profile = _make_profile(db_session, normal_user, embedding_count=1)
    _make_speaker(db_session, normal_user, media_one, profile)
    newcomer = _make_speaker(db_session, normal_user, media_two, None)
    db_session.flush()

    matching_service._handle_speaker_match(
        newcomer, _match(profile), _embedding(), normal_user.id, media_two.id
    )

    stored = matching_service._stored_profile_embeddings
    assert len(stored) == 1, "the profile centroid must have been rewritten exactly once"
    assert stored[0]["speaker_count"] == 2
    blended = np.array(stored[0]["embedding"])
    # Existing centroid is e0, the newcomer is e1, each weighted 1/2 then L2-normalised.
    assert blended[0] == pytest.approx(blended[1], abs=1e-6)


def test_linked_speaker_count_reports_the_real_membership(
    db_session, normal_user, matching_service
):
    """The helper the derivation rests on, asserted directly."""
    media = _make_file(db_session, normal_user)
    profile = _make_profile(db_session, normal_user, embedding_count=99)

    assert matching_service._linked_speaker_count(profile.id) == 0

    _make_speaker(db_session, normal_user, media, profile, name="SPEAKER_00")
    _make_speaker(db_session, normal_user, media, profile, name="SPEAKER_01")
    db_session.flush()

    assert matching_service._linked_speaker_count(profile.id) == 2
