"""A voice comparison is never written onto a speaker a person verified, and is cleared if there (#1280).

A speaker whose name a person typed or confirmed carries that person's word. A voiceprint
suggestion on it (``voice_match`` / ``profile_match``, its confidence and its detail naming the
speaker or profile the voice matched) is voice data on a person's record. Three layers hold:

* the writers (``record_suggestion`` and the four paths that call it) refuse to record one,
  unless the same step replaces the identity with the voiceprint's;
* while the file's voiceprints are off, every reader withholds the suggestion block whoever
  verified the speaker, and the person's NAME stays;
* the audited purge clears the suggestion columns of such a row (and only those: the person's
  identity record is untouched), counts them, and the residual measure sees them.

Each refusal has a control that is written (an unverified speaker, a machine identity, an LLM
suggestion), so "never write" cannot pass.
"""

from __future__ import annotations

import importlib
from datetime import UTC
from datetime import datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import Any
from typing import cast
from unittest.mock import patch

import numpy as np
import pytest

from app.core import constants
from app.models.media import Speaker
from app.models.prompt import UserSetting
from app.services.privacy import purge
from app.services.privacy.purge_residual import measure_voiceprint_residual
from app.services.privacy.purge_voiceprints import VoiceprintTargets
from app.services.privacy.purge_voiceprints import purge_voiceprints_postgres
from app.services.privacy.redact_view import PrivacyView
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.speaker_provenance_view import ProvenanceViewer
from app.services.speaker_provenance_view import attach_speaker_provenance
from app.services.speaker_rename_tracker import SpeakerRenameTracker
from app.utils import speaker_identity_source as ids
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

_DETAIL = {"origin": "voice_match", "matched_speaker_uuid": "00000000-0000-0000-0000-000000000001"}
_VOICE = np.ones(256, dtype=np.float32) / 16.0


# --- record_suggestion ---------------------------------------------------------------------


def _row(**kw: Any) -> SimpleNamespace:
    base = {
        "verified": True,
        "identity_source": "human",
        "suggested_name": None,
        "suggestion_source": None,
        "confidence": None,
        "suggestion_detail": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


def _record(row: SimpleNamespace, source: str = "voice_match", **kw: Any) -> bool:
    return ids.record_suggestion(
        row, source=source, name="Alice", confidence=0.9, detail=dict(_DETAIL), **kw
    )


@pytest.mark.parametrize("source", ["voice_match", "profile_match"])
@pytest.mark.parametrize("identity_source", ["human", None])
def test_a_voice_suggestion_is_refused_on_a_person_verified_speaker(source, identity_source):
    row = _row(identity_source=identity_source)

    assert _record(row, source) is False

    assert (row.suggested_name, row.suggestion_source) == (None, None)
    assert (row.confidence, row.suggestion_detail) == (None, None)


@pytest.mark.parametrize("source", ["voice_match", "profile_match"])
@pytest.mark.parametrize(
    "row_kw",
    [
        {"verified": False, "identity_source": "human"},
        {"verified": None, "identity_source": None},
        {"verified": True, "identity_source": "voiceprint"},
        {"verified": True, "identity_source": "voiceprint_link"},
    ],
)
def test_a_voice_suggestion_is_recorded_when_no_person_vouches_for_the_row(source, row_kw):
    row = _row(**row_kw)

    written = _record(row, source)

    assert written is not False
    assert (row.suggested_name, row.suggestion_source, row.confidence) == ("Alice", source, 0.9)
    assert row.suggestion_detail == _DETAIL


def test_a_voice_suggestion_is_recorded_when_the_same_step_replaces_the_identity():
    row = _row()

    assert _record(row, identity_follows=True) is True

    assert row.suggested_name == "Alice"


def test_a_suggestion_nobody_derived_from_a_voice_is_recorded_on_a_person_verified_speaker():
    row = _row()

    written = _record(row, "llm_analysis")

    assert written is not False

    assert (row.suggested_name, row.suggestion_source) == ("Alice", "llm_analysis")


# --- the writers -----------------------------------------------------------------------------


def _verified_and_control(db, user, **extra: Any) -> tuple[Speaker, Speaker]:
    """A speaker a person named and verified, and an unverified one beside it."""
    person = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Dana",
        verified=True,
        identity_source="human",
        identity_source_user_id=user.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
        **extra,
    )
    control = make_speaker(db, user, make_file(db, user), **extra)
    db.commit()
    return person, control


def _suggestion(speaker: Speaker) -> tuple[Any, Any, Any, Any]:
    return (
        speaker.suggested_name,
        speaker.suggestion_source,
        speaker.confidence,
        speaker.suggestion_detail,
    )


def test_a_re_run_voice_match_records_no_suggestion_on_a_person_verified_speaker(
    db_session, normal_user, monkeypatch
):
    from app.services.speaker_matching_service import SpeakerMatchingService

    person, control = _verified_and_control(db_session, normal_user)
    svc = SpeakerMatchingService(db_session, None)
    monkeypatch.setattr(
        svc,
        "find_and_store_speaker_matches",
        lambda *args, **kw: [
            {"speaker_id": 0, "speaker_uuid": None, "display_name": "Eve", "confidence": 0.9}
        ],
    )
    monkeypatch.setattr(
        "app.services.speaker_matching_service.add_speaker_embedding", lambda **kw: 1
    )

    for speaker in (person, control):
        svc._handle_no_speaker_match(speaker, _VOICE, normal_user.id, speaker.media_file_id)
    db_session.commit()
    db_session.expire_all()

    assert _suggestion(db_session.get(Speaker, person.id)) == (None, None, None, None)
    assert db_session.get(Speaker, person.id).display_name == "Dana"
    assert db_session.get(Speaker, control.id).suggested_name == "Eve", "control"


@pytest.mark.parametrize("auto_accept", [False, True])
def test_a_matched_speaker_gets_a_suggestion_only_if_nobody_vouched_or_the_match_takes_over(
    db_session, normal_user, auto_accept
):
    from app.services.speaker_matching_service import SpeakerMatchingService

    person, control = _verified_and_control(db_session, normal_user)
    match = {
        "confidence": 0.93,
        "suggested_name": "Eve",
        "auto_accept": auto_accept,
        "profile_id": None,
    }
    svc = SpeakerMatchingService(db_session, None)

    with (
        patch("app.services.speaker_matching_service.add_speaker_embedding"),
        patch.object(SpeakerMatchingService, "find_and_store_speaker_matches"),
        patch("app.services.speaker_rename_tracker.SpeakerRenameTracker.record"),
    ):
        for speaker in (person, control):
            svc._handle_speaker_match(
                speaker, dict(match), _VOICE, normal_user.id, speaker.media_file_id
            )
    db_session.commit()
    db_session.expire_all()

    kept, other = db_session.get(Speaker, person.id), db_session.get(Speaker, control.id)
    assert other.suggested_name == "Eve" and other.confidence == 0.93, "control"
    if auto_accept:
        assert (kept.suggested_name, kept.identity_source) == ("Eve", "voiceprint")
    else:
        assert _suggestion(kept) == (None, None, None, None)
        assert (kept.display_name, kept.identity_source) == ("Dana", "human")


def test_the_batch_script_records_no_suggestion_on_a_person_verified_speaker(
    db_session, normal_user, monkeypatch
):
    from app.services.speaker_matching_service import SpeakerMatchingService

    script = importlib.import_module("app.scripts.batch_speaker_matching")
    person, control = _verified_and_control(db_session, normal_user)
    mine = {person.id, control.id}

    class Session:
        def __getattr__(self, name):
            return getattr(db_session, name)

        def close(self):
            return None

    monkeypatch.setattr(script, "get_db", lambda: iter([Session()]))
    monkeypatch.setattr(script, "SpeakerEmbeddingService", lambda: None)
    monkeypatch.setattr(script, "get_speaker_embedding", lambda speaker_id: [0.1] * 256)

    def only_mine(speaker_id, *args, **kw):
        return [{"confidence": 0.9, "display_name": "Eve"}] if speaker_id in mine else []

    with (
        patch.object(
            SpeakerMatchingService, "find_and_store_speaker_matches", side_effect=only_mine
        ),
        patch("app.services.speaker_rename_tracker.SpeakerRenameTracker.record"),
    ):
        script.batch_process_speaker_matches()
    db_session.expire_all()

    assert _suggestion(db_session.get(Speaker, person.id)) == (None, None, None, None)
    assert db_session.get(Speaker, control.id).suggested_name == "Eve", "control"


@pytest.mark.parametrize("similarity", [0.6, 0.9])
def test_retroactive_matching_suggests_to_a_person_verified_speaker_only_by_taking_it_over(
    db_session, normal_user, similarity
):
    from app.api.endpoints import speaker_update

    person, control = _verified_and_control(db_session, normal_user)
    person.display_name = "Alice"
    control.display_name = None
    db_session.commit()
    trigger = {
        "id": 0,
        "uuid": None,
        "display_name": "Alice",
        "profile_id": None,
        "profile_uuid": None,
        "embedding_model": None,
    }
    scored = [{"id": s.id, "similarity": similarity} for s in (person, control)]
    tracker = SpeakerRenameTracker()

    with patch.object(speaker_update, "store_speaker_match"):
        speaker_update._persist_match_results(db_session, trigger, scored, tracker)
    db_session.commit()
    db_session.expire_all()

    kept, other = db_session.get(Speaker, person.id), db_session.get(Speaker, control.id)
    assert other.suggested_name == "Alice", "control"
    if similarity >= speaker_update.AUTO_APPLY_CONFIDENCE:
        assert kept.identity_source in ids.MACHINE_IDENTITY_SOURCES, "the voiceprint took over"
        assert kept.suggested_name == "Alice"
    else:
        assert _suggestion(kept) == (None, None, None, None)
        assert kept.identity_source == "human"


# --- the readers -----------------------------------------------------------------------------


def _off(db: Session, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def _legacy_suggested(db: Session, user: Any, **kw: Any) -> Speaker:
    """A row from before the writers refused: a person's speaker with a voice suggestion."""
    speaker = make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Bea",
        verified=True,
        identity_source=kw.pop("identity_source", "human"),
        suggested_name="Alice",
        suggestion_source=kw.pop("suggestion_source", "profile_match"),
        confidence=0.9,
        suggestion_detail={"origin": "profile_match", "matched_profile_uuid": "x"},
        **kw,
    )
    db.commit()
    return cast("Speaker", speaker)


def _reload(db: Session, speaker: Speaker) -> Speaker:
    db.expire_all()
    row = db.get(Speaker, speaker.id)
    assert row is not None
    return row


@pytest.mark.parametrize("source", ["voice_match", "profile_match"])
def test_the_suggestion_block_of_a_person_verified_speaker_is_withheld_while_off(
    db_session, normal_user, source
):
    speaker = _legacy_suggested(db_session, normal_user, suggestion_source=source)
    row = _reload(db_session, speaker)
    attach_speaker_provenance(db_session, [row], ProvenanceViewer(user_id=normal_user.id))
    assert row.suggestion is not None, "control: shown while voiceprints are on"
    assert row.suggestion["detail"]["origin"] == "profile_match"

    _off(db_session, normal_user)
    row = _reload(db_session, speaker)
    redact_speaker_rows(db_session, [row])
    attach_speaker_provenance(db_session, [row], ProvenanceViewer(user_id=normal_user.id))

    assert row.suggestion is None
    assert (row.suggested_name, row.suggestion_detail) == (None, None)
    assert (row.display_name, row.verified) == ("Bea", True), "the person's name stays"


def test_the_suggestion_block_is_withheld_even_if_the_redaction_step_was_skipped(
    db_session, normal_user
):
    speaker = _legacy_suggested(db_session, normal_user)
    _off(db_session, normal_user)
    row = _reload(db_session, speaker)

    attach_speaker_provenance(
        db_session, [row], ProvenanceViewer(user_id=normal_user.id), view=PrivacyView(db_session)
    )

    assert row.suggested_name == "Alice", "control: nothing redacted the row"
    assert row.suggestion is None


def test_an_llm_suggestion_block_is_shown_while_off(db_session, normal_user):
    speaker = _legacy_suggested(db_session, normal_user, suggestion_source="llm_analysis")
    _off(db_session, normal_user)
    row = _reload(db_session, speaker)

    redact_speaker_rows(db_session, [row])
    attach_speaker_provenance(db_session, [row], ProvenanceViewer(user_id=normal_user.id))

    assert row.suggestion is not None and row.suggestion["source"] == "llm_analysis"


# --- the purge -------------------------------------------------------------------------------


class _KeepOpen:
    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self) -> None:
        pass


@pytest.fixture()
def opensearch(monkeypatch) -> FakeOpenSearch:
    fake = FakeOpenSearch()
    monkeypatch.setattr("app.services.opensearch_service.opensearch_client", fake)
    return fake


def _measure(db: Session, user):
    return measure_voiceprint_residual(
        purge.PurgeScope(kind="user", user_id=user.id),
        VoiceprintTargets(),
        session_factory=lambda: cast("Session", _KeepOpen(db)),
        errors=[],
    )


@pytest.mark.parametrize("source", ["voice_match", "profile_match"])
def test_a_purge_clears_the_voice_suggestion_of_a_person_verified_speaker_and_nothing_else(
    db_session, normal_user, opensearch, source
):
    profile = make_profile(db_session, normal_user, "Bea")
    speaker = _legacy_suggested(
        db_session,
        normal_user,
        suggestion_source=source,
        profile_id=profile.id,
        identity_source_user_id=normal_user.id,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
        identity_source_detail={"origin": "typed"},
    )
    speaker.suggestion_detail = {"origin": source, "matched_profile_uuid": "x"}
    llm = _legacy_suggested(db_session, normal_user, suggestion_source="llm_analysis")
    llm.suggestion_detail = {"origin": "llm", "provider": "p", "model": "m"}
    db_session.commit()
    scope = purge.PurgeScope(kind="user", user_id=normal_user.id)
    before = _measure(db_session, normal_user).counts

    done = purge_voiceprints_postgres(db_session, scope)
    db_session.expire_all()
    after = _measure(db_session, normal_user).counts

    row = db_session.get(Speaker, speaker.id)
    assert _suggestion(row) == (None, None, None, None)
    assert (row.display_name, row.verified, row.profile_id, row.identity_source) == (
        "Bea",
        True,
        profile.id,
        "human",
    )
    assert row.identity_source_user_id == normal_user.id
    assert row.identity_source_at is not None
    assert row.identity_source_detail == {"origin": "typed"}
    kept = db_session.get(Speaker, llm.id)
    assert (kept.suggested_name, kept.suggestion_source) == ("Alice", "llm_analysis")
    assert kept.suggestion_detail is not None
    assert done.counts["suggestions_cleared"] == 1
    assert done.counts["suggestion_details_cleared"] == 1
    assert speaker.id in done.cleared and llm.id not in done.cleared
    assert (before["voiceprint_suggestions"], before["voiceprint_suggestion_details"]) == (1, 1)
    assert (after["voiceprint_suggestions"], after["voiceprint_suggestion_details"]) == (0, 0)


# --- the residual measure --------------------------------------------------------------------


def test_the_residual_counts_a_voice_suggestion_detail_on_any_speaker(db_session, normal_user):
    for verified in (True, False, None):
        make_speaker(
            db_session,
            normal_user,
            make_file(db_session, normal_user),
            verified=verified,
            suggestion_detail={"origin": "voice_match"},
        )
    make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        suggestion_detail={"origin": "llm", "provider": "p"},
    )
    db_session.commit()

    assert _measure(db_session, normal_user).counts["voiceprint_suggestion_details"] == 3


def test_the_residual_counts_a_record_left_on_a_row_with_no_identity(db_session, normal_user):
    """An identity record whose ``identity_source`` was lost is still a record to measure."""
    orphan = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        identity_source=None,
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    machine = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        identity_source="voiceprint",
        identity_source_detail={"origin": "voice_match"},
    )
    clean_person = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        identity_source="human",
        identity_source_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    db_session.commit()

    counted = _measure(db_session, normal_user).counts["speaker_identity_provenance"]

    assert counted == 2, "the orphaned record and the machine's; never a person's"
    db_session.delete(orphan)
    db_session.delete(machine)
    db_session.commit()
    assert _measure(db_session, normal_user).counts["speaker_identity_provenance"] == 0
    assert clean_person.id is not None


# --- a machine write replaces the person ------------------------------------------------------


def _human_row(**kw: Any) -> SimpleNamespace:
    base = {
        "display_name": "Alice",
        "profile_id": None,
        "verified": True,
        "identity_source": "human",
        "identity_source_user_id": 7,
        "identity_source_at": datetime(2026, 1, 2, tzinfo=UTC),
        "identity_source_detail": {"origin": "typed"},
        "suggested_name": None,
        "suggestion_source": None,
        "confidence": None,
        "suggestion_detail": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.parametrize(
    "write",
    [
        lambda row: ids.record_machine_identity(row, previous_display_name="Bob", detail=_DETAIL),
        lambda row: ids.record_machine_link(row, detail=_DETAIL),
    ],
    ids=["identity", "link"],
)
def test_a_voiceprint_write_never_keeps_naming_the_person_who_made_the_old_identity(write):
    row = _human_row()

    write(row)

    assert row.identity_source in ids.MACHINE_IDENTITY_SOURCES
    assert row.identity_source_user_id is None
    assert row.identity_source_detail == _DETAIL


def test_a_skip_marker_does_not_keep_naming_a_person_either():
    row = _human_row(
        display_name=None,
        verified=True,
        identity_source=None,
        suggested_name="Eve",
        suggestion_source="voice_match",
    )

    ids.after_skip(row)

    assert row.identity_source == "voiceprint"
    assert row.identity_source_user_id is None
