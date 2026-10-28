"""Every writer records WHO made a speaker's identity, WHEN, and FROM WHAT (#1280 follow-up).

``identity_source`` says whether a person or a voiceprint wrote a name. These tests pin the rest
of the record, per family of writer, through the real writers on real rows:

* a person's action stores the person, the time and how it was made (typed, accepted a
  suggestion, assigned a profile, batch-assigned, promoted/merged a cluster), including WHICH
  suggestion was accepted;
* a voiceprint path stores no person but the match (origin, confidence, threshold, matched
  profile or speaker, embedding model, task);
* a language-model recommendation stores which provider and model made it, and is never applied;
* reverting an identity to "unknown" forgets the record with it.

Privacy, purge and viewer gating have their own cases at the bottom of this file.
"""

from __future__ import annotations

import uuid as uuid_pkg
from contextlib import contextmanager
from datetime import UTC
from datetime import datetime
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from app.core import constants
from app.models.media import Speaker
from app.models.prompt import UserSetting
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.speaker_clustering_service import SpeakerClusteringService
from app.services.speaker_matching_service import MATCHING_ORIGIN_TASK
from app.services.speaker_matching_service import SpeakerMatchingService
from app.services.speaker_provenance_view import ProvenanceViewer
from app.services.speaker_provenance_view import attach_speaker_provenance
from app.utils.speaker_identity_detail import llm_detail
from app.utils.speaker_identity_detail import voiceprint_detail
from tests.fixtures.speaker_cleanup_seams import VOICE_A
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

_RENAME_DELAY = "app.tasks.rename_propagation_task.propagate_speaker_rename.delay"
_UPDATE_TASK = "app.tasks.speaker_update_task.process_speaker_update_background.delay"
_MODEL = "pyannote/test-embedding"


@pytest.fixture(autouse=True)
def _fixed_embedding_model():
    """The model id is read from the embedding service; pin it so the record is comparable."""
    with (
        patch("app.services.speaker_matching_service.voiceprint_model_id", return_value=_MODEL),
        patch("app.services.embedding_mode_service.voiceprint_model_id", return_value=_MODEL),
    ):
        yield


def _idd(row: Speaker) -> dict[str, Any]:
    """The identity detail of a row that must have one."""
    assert row.identity_source_detail is not None, "no identity detail was recorded"
    return row.identity_source_detail


def _sgd(row: Speaker) -> dict[str, Any]:
    """The suggestion detail of a row that must have one."""
    assert row.suggestion_detail is not None, "no suggestion detail was recorded"
    return row.suggestion_detail


def _headers(user_token_headers: dict) -> dict:
    return {k: v for k, v in user_token_headers.items() if not k.startswith("_")}


def _fresh(db, speaker_id: int) -> Speaker:
    db.expire_all()
    row: Speaker | None = db.get(Speaker, speaker_id)
    assert row is not None
    return row


def _switch_off(db, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def _voice_suggested(db, user, name="Alice", **kw):
    """A speaker carrying a pending voice suggestion, as the matcher leaves it."""
    return make_speaker(
        db,
        user,
        make_file(db, user),
        suggested_name=name,
        suggestion_source="voice_match",
        confidence=0.9,
        suggestion_detail=voiceprint_detail("voice_match", confidence=0.9, threshold=0.75),
        **kw,
    )


def _llm_suggested(db, user, name="Alice", **kw):
    return make_speaker(
        db,
        user,
        make_file(db, user),
        suggested_name=name,
        suggestion_source="llm_analysis",
        confidence=0.8,
        suggestion_detail=llm_detail(provider="openai", model="gpt-test", confidence=0.8),
        **kw,
    )


def _assert_recorded_by(row: Speaker, user, origin: str, *, before: datetime) -> None:
    assert row.identity_source == "human"
    assert row.identity_source_user_id == user.id
    assert row.identity_source_at is not None and row.identity_source_at >= before
    assert _idd(row)["origin"] == origin


# --------------------------------------------------------------------------- #
# A person typed a name
# --------------------------------------------------------------------------- #


def test_a_typed_rename_records_the_person_the_time_and_the_origin(
    client, user_token_headers, db_session, normal_user
):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()
    before = datetime.now(UTC)

    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        response = client.put(
            f"/api/speakers/{speaker.uuid}",
            json={"display_name": "Zed"},
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    _assert_recorded_by(row, normal_user, "typed", before=before)
    assert "accepted_suggestion" not in _idd(row)


def test_typing_the_suggested_name_records_which_voice_suggestion_was_accepted(
    client, user_token_headers, db_session, normal_user
):
    speaker = _voice_suggested(db_session, normal_user)
    db_session.commit()

    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        response = client.put(
            f"/api/speakers/{speaker.uuid}",
            json={"display_name": "Alice"},
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    assert _idd(row)["accepted_suggestion"] == {
        "source": "voice_match",
        "confidence": 0.9,
    }
    assert (row.suggested_name, row.suggestion_detail) == (None, None), "the suggestion is spent"


def test_accepting_an_llm_suggestion_records_the_provider_and_model(
    client, user_token_headers, db_session, normal_user
):
    speaker = _llm_suggested(db_session, normal_user)
    db_session.commit()

    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        response = client.put(
            f"/api/speakers/{speaker.uuid}",
            json={"display_name": "Alice"},
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    accepted = _idd(_fresh(db_session, speaker.id))["accepted_suggestion"]
    assert accepted == {
        "source": "llm_analysis",
        "confidence": 0.8,
        "llm": {"provider": "openai", "model": "gpt-test"},
    }


def test_typing_a_different_name_does_not_claim_to_have_accepted_the_suggestion(
    client, user_token_headers, db_session, normal_user
):
    speaker = _llm_suggested(db_session, normal_user)
    db_session.commit()

    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        client.put(
            f"/api/speakers/{speaker.uuid}",
            json={"display_name": "Someone Else"},
            headers=_headers(user_token_headers),
        )

    assert "accepted_suggestion" not in _idd(_fresh(db_session, speaker.id))


def test_creating_a_profile_while_renaming_records_the_profile_as_the_origin(
    client, user_token_headers, db_session, normal_user
):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()

    with patch(_UPDATE_TASK), patch(_RENAME_DELAY):
        response = client.put(
            f"/api/speakers/{speaker.uuid}",
            json={"display_name": "Zed", "profile_action": "create_new_profile"},
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    assert row.profile_id is not None, "control: the profile was created and linked"
    assert (row.identity_source_user_id, _idd(row)["origin"]) == (
        normal_user.id,
        "profile_created",
    )


def test_the_background_profile_step_keeps_the_record_the_request_made(db_session, normal_user):
    """``auto_create_or_assign_profile`` runs after the request cleared the suggestion."""
    from app.api.endpoints.speaker_update import auto_create_or_assign_profile
    from app.utils.speaker_identity_source import clear_suggestion
    from app.utils.speaker_identity_source import record_human_identity

    speaker = _voice_suggested(db_session, normal_user)
    speaker.display_name = "Alice"
    record_human_identity(speaker, user_id=normal_user.id, origin="typed")
    clear_suggestion(speaker)
    first = (speaker.identity_source_at, dict(speaker.identity_source_detail))
    db_session.commit()

    with patch("app.api.endpoints.speaker_update._update_profile_embedding"):
        assert auto_create_or_assign_profile(speaker, "Alice", db_session, user_id=normal_user.id)
    db_session.commit()

    row = _fresh(db_session, speaker.id)
    assert row.profile_id is not None, "control: the profile was linked"
    assert (row.identity_source_at, row.identity_source_detail) == first


def test_creating_a_named_speaker_records_the_creator(
    client, user_token_headers, db_session, normal_user
):
    media_file = make_file(db_session, normal_user)
    db_session.commit()

    response = client.post(
        "/api/speakers",
        params={"media_file_uuid": str(media_file.uuid)},
        json={"name": "SPEAKER_07", "display_name": "Zed"},
        headers=_headers(user_token_headers),
    )

    assert response.status_code == 200, response.text
    row = db_session.query(Speaker).filter_by(media_file_id=media_file.id).one()
    assert (row.identity_source_user_id, _idd(row)["origin"]) == (
        normal_user.id,
        "created",
    )


# --------------------------------------------------------------------------- #
# Accepting, assigning, batches, clusters
# --------------------------------------------------------------------------- #


def _batch(client, headers, body):
    with patch(_RENAME_DELAY):
        return client.post("/api/speaker-clusters/batch-verify", json=body, headers=headers)


@pytest.mark.parametrize("make", [_voice_suggested, _llm_suggested])
def test_a_batch_accept_records_the_suggestion_it_accepted(
    make, client, user_token_headers, db_session, normal_user
):
    speaker = make(db_session, normal_user)
    suggestion_source = speaker.suggestion_source
    db_session.commit()
    before = datetime.now(UTC)

    response = _batch(
        client,
        _headers(user_token_headers),
        {"speaker_uuids": [str(speaker.uuid)], "action": "accept"},
    )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    _assert_recorded_by(row, normal_user, "accepted_suggestion", before=before)
    assert _idd(row)["accepted_suggestion"]["source"] == suggestion_source


def test_a_batch_name_is_typed(client, user_token_headers, db_session, normal_user):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()

    _batch(
        client,
        _headers(user_token_headers),
        {"speaker_uuids": [str(speaker.uuid)], "action": "name", "display_name": "Zed"},
    )

    row = _fresh(db_session, speaker.id)
    assert (row.identity_source_user_id, _idd(row)["origin"]) == (
        normal_user.id,
        "typed",
    )


def test_assigning_one_speaker_a_profile_differs_from_a_batch_assign(
    client, user_token_headers, db_session, normal_user
):
    profile = make_profile(db_session, normal_user, "Zed")
    one = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    a = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    b = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()
    headers = _headers(user_token_headers)

    single = _batch(
        client,
        headers,
        {"speaker_uuids": [str(one.uuid)], "action": "assign", "profile_uuid": str(profile.uuid)},
    )
    many = _batch(
        client,
        headers,
        {
            "speaker_uuids": [str(a.uuid), str(b.uuid)],
            "action": "assign",
            "profile_uuid": str(profile.uuid),
        },
    )

    assert single.status_code == many.status_code == 200, (single.text, many.text)
    assert _idd(_fresh(db_session, one.id))["origin"] == "profile_assigned"
    for speaker in (a, b):
        row = _fresh(db_session, speaker.id)
        assert _idd(row)["origin"] == "batch_assign"
        assert row.identity_source_user_id == normal_user.id


def test_an_inbox_accept_of_a_profile_records_the_accepted_suggestion(
    client, user_token_headers, db_session, normal_user
):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = _voice_suggested(db_session, normal_user)
    db_session.commit()

    with patch(_RENAME_DELAY):
        response = client.post(
            f"/api/speakers/{speaker.uuid}/verify",
            params={"action": "accept", "profile_uuid": str(profile.uuid)},
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    assert row.identity_source_user_id == normal_user.id
    assert _idd(row)["origin"] == "accepted_suggestion"
    assert _idd(row)["accepted_suggestion"]["source"] == "voice_match"


def test_promoting_a_cluster_records_the_person_on_every_member(db_session, normal_user):
    members = [
        make_speaker(db_session, normal_user, make_file(db_session, normal_user)) for _ in range(2)
    ]
    cluster = make_cluster(db_session, normal_user, *members)

    with (
        patch(_RENAME_DELAY),
        patch(
            "app.services.profile_embedding_service.ProfileEmbeddingService.update_profile_embedding"
        ),
    ):
        profile = SpeakerClusteringService(db_session).promote_cluster_to_profile(
            str(cluster.uuid), "Zed", int(normal_user.id)
        )

    assert profile is not None
    for member in members:
        row = _fresh(db_session, member.id)
        assert (row.identity_source_user_id, _idd(row)["origin"]) == (
            normal_user.id,
            "cluster_promoted",
        )


def test_a_cluster_merge_is_a_persons_act_and_an_upload_join_is_the_voiceprints(
    db_session, normal_user, speaker_assignment_seams
):
    profile = make_profile(db_session, normal_user, "Alice")
    member = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    joiner = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    merged = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    for speaker in (member, joiner, merged):
        speaker_assignment_seams["voiceprints"][str(speaker.uuid)] = VOICE_A
    promoted = make_cluster(db_session, normal_user, member, promoted_to=profile.id, label="Alice")
    loose = make_cluster(db_session, normal_user, merged, label="loose")
    speaker_assignment_seams["clusters"][str(promoted.uuid)] = VOICE_A
    service = SpeakerClusteringService(db_session)

    with patch(_RENAME_DELAY):
        service._join(joiner, promoted, 0.9, None)
        db_session.commit()
        assert service.merge_clusters(str(loose.uuid), str(promoted.uuid), normal_user.id)

    joined = _fresh(db_session, joiner.id)
    assert joined.identity_source == "voiceprint"
    assert joined.identity_source_user_id is None, "a task, not a person, placed it"
    assert _idd(joined)["origin"] == "cluster_join"
    assert _idd(joined)["confidence"] == 0.9
    assert _idd(joined)["matched_profile_uuid"] == str(profile.uuid)
    moved = _fresh(db_session, merged.id)
    assert (moved.identity_source, moved.identity_source_user_id) == ("human", normal_user.id)
    assert _idd(moved)["origin"] == "cluster_merged"


def test_a_skip_keeps_a_voice_suggestion_the_machines_and_says_so(
    client, user_token_headers, db_session, normal_user
):
    speaker = _voice_suggested(db_session, normal_user)
    db_session.commit()

    _batch(
        client,
        _headers(user_token_headers),
        {"speaker_uuids": [str(speaker.uuid)], "action": "skip"},
    )

    row = _fresh(db_session, speaker.id)
    assert row.identity_source == "voiceprint"
    assert row.identity_source_user_id is None
    assert _idd(row)["origin"] == "voice_suggestion_skipped"


def test_a_skip_leaves_a_real_suggestion_source_and_its_detail_alone(
    client, user_token_headers, db_session, normal_user
):
    speaker = _llm_suggested(db_session, normal_user)
    db_session.commit()

    _batch(
        client,
        _headers(user_token_headers),
        {"speaker_uuids": [str(speaker.uuid)], "action": "skip"},
    )

    row = _fresh(db_session, speaker.id)
    assert (row.suggestion_source, _sgd(row)["origin"]) == ("llm_analysis", "llm")


# --------------------------------------------------------------------------- #
# Merge, split, unlink, reject
# --------------------------------------------------------------------------- #


def test_merging_two_speakers_leaves_the_survivors_record_alone(
    client, user_token_headers, db_session, normal_user
):
    media_file = make_file(db_session, normal_user)
    profile = make_profile(db_session, normal_user, "Alice")
    survivor = make_speaker(
        db_session,
        normal_user,
        media_file,
        "SPEAKER_00",
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="human",
        identity_source_user_id=normal_user.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
    )
    source = make_speaker(db_session, normal_user, media_file, "SPEAKER_01")
    db_session.commit()

    with (
        patch("app.tasks.speaker_merge_task.process_speaker_merge_background.delay"),
        patch("app.services.search.reindex_dispatch.dispatch_transcript_reindex"),
    ):
        response = client.post(
            f"/api/speakers/{source.uuid}/merge/{survivor.uuid}",
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, survivor.id)
    assert row.identity_source_at == datetime(2026, 1, 2, tzinfo=UTC)
    assert row.identity_source_detail == {"origin": "typed"}


def test_unlinking_the_profile_a_voiceprint_named_forgets_the_whole_record(db_session, normal_user):
    from app.services.speaker_cleanup import unlink_from_profile
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="voiceprint",
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail=voiceprint_detail("profile_match", confidence=0.9),
        suggested_name="Alice",
        suggestion_source="profile_match",
        confidence=0.9,
        suggestion_detail=voiceprint_detail("profile_match", confidence=0.9),
    )
    db_session.commit()

    unlink_from_profile(db_session, [speaker], int(profile.id), SpeakerRenameTracker())
    db_session.commit()

    row = _fresh(db_session, speaker.id)
    assert row.identity_source is None
    assert (
        row.identity_source_at,
        row.identity_source_user_id,
        row.identity_source_detail,
        row.suggested_name,
        row.suggestion_detail,
    ) == (None, None, None, None, None)


def test_a_cluster_split_is_an_unassignment_that_clears_the_profiles_record(
    db_session, normal_user
):
    """``split_cluster`` unlinks the moved group from the promoted profile."""
    from app.services.speaker_cleanup import detach_from_cluster
    from app.services.speaker_rename_tracker import SpeakerRenameTracker

    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="human",
        identity_source_user_id=normal_user.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
    )
    cluster = make_cluster(db_session, normal_user, speaker, promoted_to=profile.id)

    with patch("app.services.speaker_cleanup.reconcile_speaker_impact"):
        detach_from_cluster(
            db_session, cluster, [speaker], blacklist=False, tracker=SpeakerRenameTracker()
        )
    db_session.commit()

    row = _fresh(db_session, speaker.id)
    assert (row.display_name, row.profile_id, row.identity_source) == (None, None, None)
    assert (row.identity_source_at, row.identity_source_user_id) == (None, None)


def test_rejecting_a_suggestion_withdraws_its_detail(
    client, user_token_headers, db_session, normal_user
):
    speaker = _llm_suggested(db_session, normal_user)
    db_session.commit()

    with patch(_RENAME_DELAY):
        response = client.post(
            f"/api/speakers/{speaker.uuid}/verify",
            params={"action": "reject"},
            headers=_headers(user_token_headers),
        )

    assert response.status_code == 200, response.text
    row = _fresh(db_session, speaker.id)
    assert (row.suggested_name, row.confidence, row.suggestion_detail) == (None, None, None)


# --------------------------------------------------------------------------- #
# The voiceprint writers
# --------------------------------------------------------------------------- #


def _auto_accept(db, user, speaker, match: dict[str, Any]) -> None:
    service = SpeakerMatchingService(db, embedding_service=None)
    with (
        patch("app.services.speaker_matching_service.add_speaker_embedding"),
        patch("app.services.speaker_matching_service.update_speaker_profile", create=True),
        patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        patch.object(SpeakerMatchingService, "_propagate_profile_assignment"),
        patch.object(SpeakerMatchingService, "match_speaker_to_known_speakers", return_value=match),
        patch(_RENAME_DELAY),
    ):
        service.process_speaker_embeddings_native(
            media_file_id=int(speaker.media_file_id),
            user_id=int(user.id),
            native_embeddings={int(speaker.id): np.ones(256, dtype=np.float32) / 16.0},
        )


def test_a_profile_match_auto_accept_records_the_match_and_no_person(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()

    _auto_accept(
        db_session,
        normal_user,
        speaker,
        {
            "confidence": 0.93,
            "suggested_name": "Alice",
            "auto_accept": True,
            "profile_id": profile.id,
            "profile_uuid": str(profile.uuid),
        },
    )

    row = _fresh(db_session, speaker.id)
    assert row.identity_source == "voiceprint"
    assert row.identity_source_user_id is None
    assert row.identity_source_at is not None
    assert row.identity_source_detail == {
        "origin": "profile_match",
        "confidence": 0.93,
        "threshold": 0.75,
        "matched_profile_uuid": str(profile.uuid),
        "embedding_model": _MODEL,
        "origin_task": MATCHING_ORIGIN_TASK,
    }
    assert row.suggestion_source == "profile_match"
    assert row.suggestion_detail == row.identity_source_detail, "same match, same record"


def test_a_voice_match_auto_accept_names_the_speaker_it_matched(db_session, normal_user):
    known = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        verified=True,
        identity_source="human",
    )
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()

    _auto_accept(
        db_session,
        normal_user,
        speaker,
        {
            "confidence": 0.9,
            "suggested_name": "Alice",
            "auto_accept": True,
            "profile_id": None,
            "speaker_uuid": str(known.uuid),
        },
    )

    row = _fresh(db_session, speaker.id)
    assert _idd(row)["origin"] == "voice_match"
    assert _idd(row)["matched_speaker_uuid"] == str(known.uuid)
    assert "matched_profile_uuid" not in _idd(row)


def test_a_below_threshold_match_stores_no_suggestion_and_no_detail(db_session, normal_user):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()

    _auto_accept(
        db_session,
        normal_user,
        speaker,
        {"confidence": 0.6, "suggested_name": "Alice", "auto_accept": False, "profile_id": None},
    )

    row = _fresh(db_session, speaker.id)
    assert (row.suggested_name, row.suggestion_detail, row.identity_source) == (None, None, None)


def test_retroactive_matching_records_the_speaker_a_person_labeled(
    db_session, normal_user, speaker_assignment_seams
):
    from app.api.endpoints.speaker_update import RETROACTIVE_ORIGIN_TASK
    from app.api.endpoints.speaker_update import trigger_retroactive_matching

    profile = make_profile(db_session, normal_user, "Alice")
    labelled = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        identity_source="human",
    )
    other = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    speaker_assignment_seams["voiceprints"].update(
        {str(labelled.uuid): VOICE_A, str(other.uuid): VOICE_A}
    )
    db_session.commit()

    with (
        patch("app.api.endpoints.speaker_update._send_bulk_update_notification"),
        patch("app.api.endpoints.speaker_update.voiceprint_model_id", return_value=_MODEL),
        patch(_RENAME_DELAY),
    ):
        result = trigger_retroactive_matching(labelled, db_session)

    assert result["auto_applied_count"] == 1
    row = _fresh(db_session, other.id)
    assert row.identity_source == "voiceprint" and row.identity_source_user_id is None
    assert row.identity_source_detail == {
        "origin": "voice_match",
        "confidence": pytest.approx(1.0, abs=1e-4),
        "threshold": 0.75,
        "matched_speaker_uuid": str(labelled.uuid),
        "matched_profile_uuid": str(profile.uuid),
        "embedding_model": _MODEL,
        "origin_task": RETROACTIVE_ORIGIN_TASK,
    }
    assert row.suggestion_detail == row.identity_source_detail


def test_propagation_records_the_link_and_the_profile_it_came_from(
    db_session, normal_user, speaker_assignment_seams
):
    profile = make_profile(db_session, normal_user, "Alice")
    seen = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    named = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        identity_source="human",
    )
    db_session.commit()
    speaker_assignment_seams["voiceprints"].update(
        {str(seen.uuid): VOICE_A, str(named.uuid): VOICE_A}
    )
    service = SpeakerMatchingService(db_session, embedding_service=None)

    with patch.object(
        service, "_get_speaker_embedding_for_propagation", return_value=(VOICE_A, "x")
    ):
        service._propagate_profile_assignment(int(seen.id), int(profile.id), int(normal_user.id))

    row = _fresh(db_session, named.id)
    assert row.identity_source == "voiceprint_link"
    assert row.identity_source_user_id is None
    assert _idd(row)["origin"] == "profile_propagation"
    assert _idd(row)["matched_profile_uuid"] == str(profile.uuid)
    assert _idd(row)["matched_speaker_uuid"] == str(seen.uuid)


# --------------------------------------------------------------------------- #
# A language model's recommendation
# --------------------------------------------------------------------------- #


def test_an_llm_suggestion_records_its_provider_and_model_and_is_never_applied(
    db_session, normal_user, monkeypatch
):
    from app.tasks import speaker_identification_task as sit

    media_file = make_file(db_session, normal_user)
    speaker = make_speaker(db_session, normal_user, media_file, "SPEAKER_00")
    db_session.commit()

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr(sit, "session_scope", same_session)
    monkeypatch.setattr(
        "app.services.speaker_rename_tracker.SpeakerRenameTracker.flush", lambda self, db: None
    )

    sit._store_speaker_predictions(
        int(media_file.id),
        {
            "speaker_predictions": [
                {"speaker_label": "SPEAKER_00", "predicted_name": "Zed", "confidence": 0.82}
            ]
        },
        {"provider": "vllm", "model": "gemma-test", "where": "server"},
    )
    db_session.commit()

    row = _fresh(db_session, speaker.id)
    assert (row.suggested_name, row.suggestion_source, row.confidence) == (
        "Zed",
        "llm_analysis",
        0.82,
    )
    assert row.suggestion_detail == {
        "origin": "llm",
        "provider": "vllm",
        "model": "gemma-test",
        "confidence": 0.82,
        "origin_task": sit.SPEAKER_ID_ORIGIN_TASK,
    }
    assert row.display_name is None and row.identity_source is None, "never auto-applied"
    assert row.verified is not True


def test_a_prediction_without_provenance_still_records_that_an_llm_made_it(
    db_session, normal_user, monkeypatch
):
    from app.tasks import speaker_identification_task as sit

    media_file = make_file(db_session, normal_user)
    speaker = make_speaker(db_session, normal_user, media_file, "SPEAKER_00")
    db_session.commit()

    @contextmanager
    def same_session():
        yield db_session

    monkeypatch.setattr(sit, "session_scope", same_session)
    monkeypatch.setattr(
        "app.services.speaker_rename_tracker.SpeakerRenameTracker.flush", lambda self, db: None
    )

    sit._store_speaker_predictions(
        int(media_file.id),
        {
            "speaker_predictions": [
                {"speaker_label": "SPEAKER_00", "predicted_name": "Zed", "confidence": 0.7}
            ]
        },
    )
    db_session.commit()

    detail = _sgd(_fresh(db_session, speaker.id))
    assert detail["origin"] == "llm" and "provider" not in detail


# --------------------------------------------------------------------------- #
# Privacy: hidden with the identity they describe
# --------------------------------------------------------------------------- #


def _machine_record(db, user, **kw):
    profile = make_profile(db, user, "Alice")
    return make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Alice",
        verified=True,
        profile_id=profile.id,
        identity_source="voiceprint",
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail=voiceprint_detail(
            "profile_match", confidence=0.9, matched_profile_uuid=profile.uuid
        ),
        suggested_name="Alice",
        suggestion_source="profile_match",
        confidence=0.9,
        suggestion_detail=voiceprint_detail("profile_match", confidence=0.9),
        **kw,
    )


def test_while_voiceprints_are_off_a_machine_identitys_record_is_withheld(db_session, normal_user):
    speaker = _machine_record(db_session, normal_user)
    db_session.commit()
    row = _fresh(db_session, speaker.id)
    redact_speaker_rows(db_session, [row])
    assert row.identity_source_detail is not None, "control: shown while on"
    assert row.suggestion_detail is not None

    _switch_off(db_session, normal_user)
    row = _fresh(db_session, speaker.id)
    redact_speaker_rows(db_session, [row])

    assert (
        row.identity_source_at,
        row.identity_source_user_id,
        row.identity_source_detail,
        row.suggestion_detail,
    ) == (None, None, None, None)
    assert _fresh(db_session, speaker.id).identity_source_detail is not None, "kept in the database"


def test_a_persons_own_record_is_never_withheld(db_session, normal_user):
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Zed",
        verified=True,
        identity_source="human",
        identity_source_user_id=normal_user.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
    )
    _switch_off(db_session, normal_user)
    row = _fresh(db_session, speaker.id)

    redact_speaker_rows(db_session, [row])
    attach_speaker_provenance(db_session, [row], ProvenanceViewer(user_id=normal_user.id))

    assert row.identity_source_detail == {"origin": "typed"}
    assert row.identity is not None and row.identity["by"] is not None
    assert row.identity["source"] == "human"
    assert row.identity["by"]["uuid"] == normal_user.uuid


def test_the_api_withholds_the_identity_object_of_a_machine_identity_while_off(
    client, user_token_headers, db_session, normal_user
):
    speaker = _machine_record(db_session, normal_user)
    db_session.commit()
    headers = _headers(user_token_headers)

    on = client.get(f"/api/speakers/{speaker.uuid}", headers=headers).json()
    _switch_off(db_session, normal_user)
    off = client.get(f"/api/speakers/{speaker.uuid}", headers=headers).json()

    assert on["identity"]["source"] == "voiceprint", "control: present while on"
    assert on["identity"]["detail"]["origin"] == "profile_match"
    assert on["suggestion"]["detail"]["origin"] == "profile_match"
    assert on["identity"]["by"] is None, "a task made it"
    assert off["identity"] is None
    assert off["suggestion"] is None
    assert "profile_match" not in str(off), "no part of the match is in the response"


def test_a_voiceprint_link_keeps_its_name_but_loses_its_record_while_off(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
        identity_source="voiceprint_link",
        identity_source_detail=voiceprint_detail("profile_propagation"),
    )
    _switch_off(db_session, normal_user)
    row = _fresh(db_session, speaker.id)

    redact_speaker_rows(db_session, [row])
    attach_speaker_provenance(db_session, [row], ProvenanceViewer(user_id=normal_user.id))

    assert (row.display_name, row.profile_id) == ("Alice", None)
    assert (row.identity_source_detail, row.identity) == (None, None)


# --------------------------------------------------------------------------- #
# Who may learn the person
# --------------------------------------------------------------------------- #


def _human_row(db, owner, actor, **file_kw):
    media_file = make_file(db, owner)
    for key, value in file_kw.items():
        setattr(media_file, key, value)
    db.flush()
    return make_speaker(
        db,
        owner,
        media_file,
        display_name="Zed",
        verified=True,
        identity_source="human",
        identity_source_user_id=actor.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
    )


def _by(db, speaker, viewer):
    row = _fresh(db, speaker.id)
    attach_speaker_provenance(db, [row], viewer)
    assert row.identity is not None, "control: the identity itself is shown to every viewer"
    assert row.identity["source"] == "human" and row.identity["at"] is not None
    return row.identity["by"]


def test_the_file_owner_sees_who_made_the_identity(db_session, normal_user, other_user):
    speaker = _human_row(db_session, normal_user, other_user)

    by = _by(db_session, speaker, ProvenanceViewer(user_id=normal_user.id))

    assert by == {"uuid": other_user.uuid, "name": other_user.full_name or other_user.email}


def test_a_shared_viewer_gets_the_source_and_time_but_not_the_person(
    db_session, normal_user, other_user
):
    speaker = _human_row(db_session, normal_user, normal_user)

    assert _by(db_session, speaker, ProvenanceViewer(user_id=other_user.id)) is None
    assert _by(db_session, speaker, None) is None


def test_an_organization_admin_sees_the_person_only_in_their_own_organization(
    db_session, normal_user, other_user
):
    from app.models.organization import Organization

    org = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    elsewhere = Organization(name=f"org-{uuid_pkg.uuid4().hex[:8]}")
    db_session.add_all([org, elsewhere])
    db_session.flush()
    speaker = _human_row(db_session, normal_user, normal_user, organization_id=org.id)

    admin_here = ProvenanceViewer(user_id=other_user.id, admin_org_id=org.id)
    admin_there = ProvenanceViewer(user_id=other_user.id, admin_org_id=elsewhere.id)

    assert _by(db_session, speaker, admin_here)["uuid"] == normal_user.uuid
    assert _by(db_session, speaker, admin_there) is None


def test_a_platform_administrator_sees_the_person(db_session, normal_user, admin_user):
    from app.services.platform_access import TenancyMode
    from app.services.platform_bypass import PlatformBypass

    speaker = _human_row(db_session, normal_user, normal_user)
    bypass = PlatformBypass(user=admin_user, mode=TenancyMode.SINGLE)

    assert _by(db_session, speaker, ProvenanceViewer(user_id=admin_user.id, bypass=bypass))
    assert _by(db_session, speaker, ProvenanceViewer(user_id=admin_user.id)) is None, (
        "control: an administrator whose bypass allows nothing sees no more than any viewer"
    )


def test_a_deleted_account_leaves_the_record_without_a_person(db_session, normal_user, other_user):
    """``identity_source_user_id`` is ``ON DELETE SET NULL``: erasing the person keeps the row."""
    from sqlalchemy import text

    speaker = _human_row(db_session, normal_user, other_user)
    db_session.commit()
    assert _fresh(db_session, speaker.id).identity_source_user_id == other_user.id

    db_session.connection().execute(text('DELETE FROM "user" WHERE id = :i'), {"i": other_user.id})

    assert _fresh(db_session, speaker.id).identity_source_user_id is None
    assert _by(db_session, speaker, ProvenanceViewer(user_id=normal_user.id)) is None


def test_the_suggestion_object_carries_the_origin_for_every_viewer(db_session, normal_user):
    speaker = _llm_suggested(db_session, normal_user)
    row = _fresh(db_session, speaker.id)

    attach_speaker_provenance(db_session, [row], None, view=PrivacyView(db_session))

    assert row.suggestion == {
        "source": "llm_analysis",
        "confidence": 0.8,
        "detail": llm_detail(provider="openai", model="gpt-test", confidence=0.8),
    }
    assert row.identity is None, "no identity recorded: unknown, not 'human'"
