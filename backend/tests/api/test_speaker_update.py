"""Characterization tests for ``api/endpoints/speaker_update.py``.

Wave-3 (speakers domain). NOTE: despite living under ``api/endpoints/``, this
module exposes NO FastAPI router — it is a library of speaker auto-profiling /
cross-video matching helpers consumed by the speaker Celery tasks
(``speaker_update_task``, ``speaker_embedding_task``). So these are direct
function-level unit tests against the savepoint-isolated ``db_session``, not HTTP
tests.

Pinned behavior:
- ``calculate_cosine_similarity`` (delegates to SimilarityService)
- ``store_speaker_match`` (ordered insert + max-confidence update, no dupes)
- ``_get_profile_uuid``
- ``auto_create_or_assign_profile`` (create-new vs assign-existing, case-insensitive)
- ``trigger_retroactive_matching`` (no-embedding early return)

OpenSearch is OFF in the test env, so ``get_speaker_embedding`` returns None —
the matching paths take their documented "no embedding" early returns, which is
exactly the branch this characterization locks. All rows are savepoint-created;
the 16 real benchmark speakers + their matches are never mutated.

Run: ``venv/bin/pytest tests/api/test_speaker_update.py -v -n0``
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest

from app.api.endpoints.speaker_update import _get_profile_uuid
from app.api.endpoints.speaker_update import auto_create_or_assign_profile
from app.api.endpoints.speaker_update import calculate_cosine_similarity
from app.api.endpoints.speaker_update import store_speaker_match
from app.api.endpoints.speaker_update import trigger_retroactive_matching
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerMatch
from app.models.media import SpeakerProfile


def _make_file(db_session, owner) -> MediaFile:
    mf = MediaFile(
        user_id=owner.id,
        filename="upd.wav",
        storage_path=f"test/{uuid.uuid4().hex}.wav",
        file_size=1024,
        content_type="audio/wav",
        status="completed",
    )
    db_session.add(mf)
    db_session.commit()
    db_session.refresh(mf)
    return mf


def _make_speaker(db_session, owner, media_file, *, name="SPEAKER_00") -> Speaker:
    spk = Speaker(user_id=owner.id, media_file_id=media_file.id, name=name)
    db_session.add(spk)
    db_session.commit()
    db_session.refresh(spk)
    return spk


# ---------------------------------------------------------------------------
# calculate_cosine_similarity
# ---------------------------------------------------------------------------


def test_cosine_similarity_identical_is_one():
    # SimilarityService computes in float32, so identical vectors land at ~1.0
    # rather than exactly 1.0 — pin the documented near-1.0 contract.
    v = np.array([1.0, 2.0, 3.0])
    assert calculate_cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)


def test_cosine_similarity_orthogonal_is_zero():
    a = np.array([1.0, 0.0])
    b = np.array([0.0, 1.0])
    assert abs(calculate_cosine_similarity(a, b)) < 1e-6


# ---------------------------------------------------------------------------
# store_speaker_match
# ---------------------------------------------------------------------------


def test_store_match_orders_ids_smaller_first(db_session, normal_user):
    mf = _make_file(db_session, normal_user)
    s1 = _make_speaker(db_session, normal_user, mf, name="A")
    s2 = _make_speaker(db_session, normal_user, mf, name="B")
    lo, hi = sorted((s1.id, s2.id))

    # Pass the larger id first to prove the helper normalizes ordering.
    store_speaker_match(hi, lo, 0.6, db_session)
    db_session.commit()

    match = (
        db_session.query(SpeakerMatch)
        .filter(SpeakerMatch.speaker1_id == lo, SpeakerMatch.speaker2_id == hi)
        .first()
    )
    assert match is not None
    assert abs(match.confidence - 0.6) < 1e-6


def test_store_match_updates_only_when_higher(db_session, normal_user):
    mf = _make_file(db_session, normal_user)
    s1 = _make_speaker(db_session, normal_user, mf, name="A")
    s2 = _make_speaker(db_session, normal_user, mf, name="B")

    store_speaker_match(s1.id, s2.id, 0.5, db_session)
    db_session.commit()
    store_speaker_match(s1.id, s2.id, 0.9, db_session)  # higher -> update
    store_speaker_match(s1.id, s2.id, 0.3, db_session)  # lower -> ignored
    db_session.commit()

    lo, hi = sorted((s1.id, s2.id))
    matches = (
        db_session.query(SpeakerMatch)
        .filter(SpeakerMatch.speaker1_id == lo, SpeakerMatch.speaker2_id == hi)
        .all()
    )
    assert len(matches) == 1  # no duplicate row
    assert abs(matches[0].confidence - 0.9) < 1e-6


# ---------------------------------------------------------------------------
# _get_profile_uuid
# ---------------------------------------------------------------------------


def test_get_profile_uuid_none_for_none(db_session):
    assert _get_profile_uuid(db_session, None) is None


def test_get_profile_uuid_none_for_unknown(db_session):
    assert _get_profile_uuid(db_session, 999_999_999) is None


def test_get_profile_uuid_resolves(db_session, normal_user):
    prof = SpeakerProfile(user_id=normal_user.id, name=f"P {uuid.uuid4().hex[:6]}")
    db_session.add(prof)
    db_session.commit()
    db_session.refresh(prof)
    assert _get_profile_uuid(db_session, prof.id) == str(prof.uuid)


# ---------------------------------------------------------------------------
# auto_create_or_assign_profile
# ---------------------------------------------------------------------------


def test_auto_create_new_profile(db_session, normal_user):
    mf = _make_file(db_session, normal_user)
    spk = _make_speaker(db_session, normal_user, mf)

    ok = auto_create_or_assign_profile(spk, "Alice Smith", db_session)
    db_session.commit()
    assert ok is True
    assert spk.profile_id is not None

    prof = db_session.query(SpeakerProfile).filter(SpeakerProfile.id == spk.profile_id).first()
    assert prof is not None
    assert prof.name == "Alice Smith"
    assert prof.user_id == normal_user.id


def test_auto_assign_existing_profile_case_insensitive(db_session, normal_user):
    mf = _make_file(db_session, normal_user)
    existing = SpeakerProfile(user_id=normal_user.id, name="Bob Jones")
    db_session.add(existing)
    db_session.commit()
    db_session.refresh(existing)

    spk = _make_speaker(db_session, normal_user, mf, name="SPEAKER_01")
    ok = auto_create_or_assign_profile(spk, "bob jones", db_session)
    db_session.commit()

    assert ok is True
    # Reuses the existing profile (no new row) via ilike match.
    assert spk.profile_id == existing.id
    assert (
        db_session.query(SpeakerProfile).filter(SpeakerProfile.user_id == normal_user.id).count()
        == 1
    )


def test_auto_assign_does_not_cross_users(db_session, normal_user, other_user):
    """A same-named profile owned by ANOTHER user is not reused — a new profile is
    created for ``normal_user`` (the query filters by ``speaker.user_id``)."""
    mf = _make_file(db_session, normal_user)
    foreign = SpeakerProfile(user_id=other_user.id, name="Shared Name")
    db_session.add(foreign)
    db_session.commit()

    spk = _make_speaker(db_session, normal_user, mf)
    ok = auto_create_or_assign_profile(spk, "Shared Name", db_session)
    db_session.commit()

    assert ok is True
    assert spk.profile_id is not None
    new_prof = db_session.query(SpeakerProfile).filter(SpeakerProfile.id == spk.profile_id).first()
    assert new_prof.user_id == normal_user.id
    assert new_prof.id != foreign.id


# ---------------------------------------------------------------------------
# trigger_retroactive_matching  (no embedding in test env -> graceful early exit)
# ---------------------------------------------------------------------------


def test_retroactive_matching_no_embedding_returns_zero(db_session, normal_user):
    """With OpenSearch off, get_speaker_embedding returns None → the function
    returns zero counts without raising."""
    mf = _make_file(db_session, normal_user)
    spk = _make_speaker(db_session, normal_user, mf)
    spk.display_name = "Carol"
    db_session.commit()

    result = trigger_retroactive_matching(spk, db_session)
    assert result == {"auto_applied_count": 0, "suggested_count": 0}
