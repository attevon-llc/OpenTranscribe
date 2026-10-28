"""The origin records stored beside a speaker's identity and recommendation (#1280 follow-up).

Pure functions on plain objects: the builders keep the stored JSON small and closed (ids and
identifiers, no names, no transcript text), and the helpers stamp the time, the person and the
detail together with the source they describe.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace

import numpy as np
import pytest

from app.utils import speaker_identity_detail as detail
from app.utils import speaker_identity_source as ids


def _speaker(**kw):
    base = {
        "display_name": None,
        "profile_id": None,
        "verified": False,
        "suggested_name": None,
        "suggestion_source": None,
        "suggestion_detail": None,
        "confidence": None,
        "identity_source": None,
        "identity_source_at": None,
        "identity_source_user_id": None,
        "identity_source_detail": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --- the builders -----------------------------------------------------------------------------


def test_a_voiceprint_detail_drops_what_is_unknown_and_stringifies_ids():
    pid = uuid.uuid4()

    got = detail.voiceprint_detail(
        detail.ORIGIN_PROFILE_MATCH,
        confidence=0.912345678,
        threshold=0.75,
        matched_profile_uuid=pid,
        embedding_model="pyannote/wespeaker-voxceleb-resnet34-LM",
    )

    assert got == {
        "origin": "profile_match",
        "confidence": 0.9123,
        "threshold": 0.75,
        "matched_profile_uuid": str(pid),
        "embedding_model": "pyannote/wespeaker-voxceleb-resnet34-LM",
    }


def test_numpy_scalars_become_json_encodable_floats():
    got = detail.voiceprint_detail(detail.ORIGIN_VOICE_MATCH, confidence=np.float32(0.8))

    assert json.loads(json.dumps(got))["confidence"] == pytest.approx(0.8, abs=1e-4)


def test_an_unknown_origin_is_refused_so_the_vocabulary_stays_closed():
    with pytest.raises(ValueError, match="origin"):
        detail.voiceprint_detail("something_else")
    with pytest.raises(ValueError, match="origin"):
        detail.human_detail("auto")


def test_an_llm_detail_names_the_provider_and_model_not_the_endpoint():
    got = detail.llm_detail(provider="vllm", model="m-1", confidence=0.7, origin_task="t")

    assert got == {
        "origin": "llm",
        "provider": "vllm",
        "model": "m-1",
        "confidence": 0.7,
        "origin_task": "t",
    }


# --- what a person accepted --------------------------------------------------------------------


def test_accepting_a_voice_suggestion_is_summarised_without_matched_ids():
    s = _speaker(
        display_name="Alice",
        suggested_name="alice",
        suggestion_source="voice_match",
        confidence=0.9,
        suggestion_detail=detail.voiceprint_detail(
            detail.ORIGIN_VOICE_MATCH, matched_speaker_uuid=uuid.uuid4()
        ),
    )

    assert detail.accepted_suggestion_snapshot(s) == {"source": "voice_match", "confidence": 0.9}


def test_accepting_an_llm_suggestion_keeps_which_model_guessed():
    s = _speaker(
        display_name="Alice",
        suggested_name="Alice",
        suggestion_source="llm_analysis",
        confidence=0.8,
        suggestion_detail=detail.llm_detail(provider="openai", model="gpt-x"),
    )

    assert detail.accepted_suggestion_snapshot(s) == {
        "source": "llm_analysis",
        "confidence": 0.8,
        "llm": {"provider": "openai", "model": "gpt-x"},
    }


def test_typing_a_different_name_is_not_accepting_the_suggestion():
    s = _speaker(display_name="Zed", suggested_name="Alice", suggestion_source="voice_match")

    assert detail.accepted_suggestion_snapshot(s) is None
    assert detail.accepted_suggestion_snapshot(s, require_name_match=False) is not None


@pytest.mark.parametrize(
    "kw",
    [
        {},
        {"suggested_name": "Alice"},
        {"suggested_name": "Alice", "suggestion_source": "user_skipped"},
    ],
)
def test_no_pending_suggestion_means_nothing_was_accepted(kw):
    assert detail.accepted_suggestion_snapshot(_speaker(**kw)) is None


# --- the identity helpers ---------------------------------------------------------------------


def test_a_person_is_recorded_with_the_time_the_user_and_the_origin():
    s = _speaker(display_name="Alice")
    before = datetime.now(UTC)

    ids.record_human_identity(s, user_id=7, origin=detail.ORIGIN_TYPED)

    assert (s.identity_source, s.identity_source_user_id) == ("human", 7)
    assert s.identity_source_at is not None and s.identity_source_at >= before
    assert s.identity_source_at.tzinfo is not None
    assert s.identity_source_detail == {"origin": "typed"}


def test_accepting_a_pending_suggestion_is_in_the_persons_record():
    s = _speaker(
        display_name="Alice",
        suggested_name="Alice",
        suggestion_source="voice_match",
        confidence=0.9,
    )

    ids.record_human_identity(s, user_id=7, origin=detail.ORIGIN_ACCEPTED_SUGGESTION)

    assert s.identity_source_detail["accepted_suggestion"] == {
        "source": "voice_match",
        "confidence": 0.9,
    }


def test_a_machine_identity_has_no_person_and_carries_the_match():
    s = _speaker(display_name="Alice", profile_id=3)
    match = detail.voiceprint_detail(detail.ORIGIN_PROFILE_MATCH, confidence=0.9)

    ids.record_machine_identity(s, previous_display_name=None, detail=match)

    assert s.identity_source == "voiceprint"
    assert s.identity_source_user_id is None
    assert s.identity_source_at is not None
    assert s.identity_source_detail == match


def test_a_later_step_of_the_same_action_keeps_the_record_the_request_made():
    s = _speaker(display_name="Alice", suggested_name="Alice", suggestion_source="voice_match")
    ids.record_human_identity(s, user_id=7, origin=detail.ORIGIN_TYPED)
    first = (s.identity_source_at, dict(s.identity_source_detail))
    ids.clear_suggestion(s)

    ids.record_human_identity(
        s, user_id=7, origin=detail.ORIGIN_PROFILE_ASSIGNED, keep_recorded=True
    )

    assert (s.identity_source_at, s.identity_source_detail) == first
    assert "accepted_suggestion" in s.identity_source_detail


def test_keep_recorded_does_not_protect_a_machine_record():
    s = _speaker(display_name="Alice")
    ids.record_machine_identity(
        s,
        previous_display_name=None,
        detail=detail.voiceprint_detail(detail.ORIGIN_VOICE_MATCH),
    )

    ids.record_human_identity(s, user_id=7, origin=detail.ORIGIN_TYPED, keep_recorded=True)

    assert (s.identity_source, s.identity_source_user_id) == ("human", 7)


def test_reverting_to_unknown_clears_the_whole_record():
    s = _speaker(display_name="Alice", profile_id=3)
    ids.record_machine_link(s, detail=detail.voiceprint_detail(detail.ORIGIN_VOICE_MATCH))
    s.profile_id = None

    ids.after_unlink(s)

    assert s.identity_source is None
    assert (s.identity_source_at, s.identity_source_user_id, s.identity_source_detail) == (
        None,
        None,
        None,
    )


def test_a_voiceprint_whose_name_was_cleared_keeps_the_record_of_its_link():
    s = _speaker(display_name="Alice", profile_id=3)
    ids.record_machine_identity(
        s, previous_display_name=None, detail=detail.voiceprint_detail(detail.ORIGIN_VOICE_MATCH)
    )
    s.display_name = None

    ids.after_name_cleared(s)

    assert s.identity_source == "voiceprint_link"
    assert s.identity_source_detail is not None


def test_a_skipped_voice_suggestion_is_marked_the_machines_with_a_detail():
    s = _speaker(suggested_name="Alice", suggestion_source="voice_match", confidence=0.8)

    ids.after_skip(s)

    assert s.identity_source == "voiceprint"
    assert s.identity_source_detail == {
        "origin": "voice_suggestion_skipped",
        "confidence": 0.8,
    }


# --- the suggestion helpers -------------------------------------------------------------------


def test_a_suggestion_is_stored_with_its_origin_and_never_applied():
    s = _speaker()
    llm = detail.llm_detail(provider="vllm", model="m")

    ids.record_suggestion(s, source="llm_analysis", name="Alice", confidence=0.7, detail=llm)

    assert (s.suggested_name, s.suggestion_source, s.confidence) == ("Alice", "llm_analysis", 0.7)
    assert s.suggestion_detail == llm
    assert s.display_name is None and s.identity_source is None


def test_withdrawing_a_suggestion_clears_its_detail_but_keeps_the_source_marker():
    s = _speaker()
    ids.record_suggestion(
        s, source="llm_analysis", name="Alice", confidence=0.7, detail={"origin": "llm"}
    )

    ids.clear_suggestion(s)

    assert (s.suggested_name, s.confidence, s.suggestion_detail) == (None, None, None)
    assert s.suggestion_source == "llm_analysis"


def test_a_review_skip_never_overwrites_a_real_source():
    real = _speaker(suggestion_source="llm_analysis", suggestion_detail={"origin": "llm"})
    none = _speaker()

    ids.mark_review_skipped(real)
    ids.mark_review_skipped(none)

    assert (real.suggestion_source, real.suggestion_detail) == ("llm_analysis", {"origin": "llm"})
    assert (none.suggestion_source, none.suggestion_detail) == (
        "user_skipped",
        {"origin": "user_skipped"},
    )


def test_provenance_hides_exactly_where_the_link_does():
    for source in ("voiceprint", "voiceprint_link"):
        assert ids.hides_provenance(source, False) is True
        assert ids.hides_provenance(source, True) is False
    for person in ("human", None):
        assert ids.hides_provenance(person, False) is False
