"""A voiceprint's suggestion stays the voiceprint's, however the row was reviewed or linked.

Two rows used to read as a person's word although a voiceprint produced the evidence on them:
a voice suggestion an Inbox user skipped (which sets ``verified`` without choosing a name) and a
speaker a voiceprint linked to a profile under a name a person had typed. While voiceprints were
off their suggestion and match confidence were still shown, a purge kept them, and the residual
measured zero. Each case has an "on" control and a person-made control.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING
from typing import cast

import pytest

from app.core import constants
from app.models.media import Speaker
from app.models.media import SpeakerProfile
from app.models.prompt import UserSetting
from app.services.privacy import purge
from app.services.privacy.purge_residual import measure_voiceprint_residual
from app.services.privacy.purge_voiceprints import VoiceprintTargets
from app.services.privacy.purge_voiceprints import purge_voiceprints_postgres
from app.services.privacy.redact_view import redact_speaker_rows
from app.services.speaker_clustering_service import SpeakerClusteringService
from app.utils.speaker_labels import canonical_speaker_label_for_row
from tests.fixtures.fake_speaker_index import FakeOpenSearch
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


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


def _off(db, user) -> None:
    db.add(
        UserSetting(
            user_id=user.id,
            setting_key=constants.PRIVACY_USER_VOICEPRINT_MATCHING_KEY,
            setting_value="false",
        )
    )
    db.commit()


def _skip(db, user, speaker) -> None:
    SpeakerClusteringService(db).batch_verify_speakers(
        speaker_uuids=[str(speaker.uuid)], user_id=user.id, action="skip", organization_id=None
    )
    db.commit()
    db.refresh(speaker)


def _measure(db, user):
    return measure_voiceprint_residual(
        purge.PurgeScope(kind="user", user_id=user.id),
        VoiceprintTargets(),
        session_factory=lambda: cast("Session", _KeepOpen(db)),
        errors=[],
    )


def _purge_and_measure(db, user):
    purge_voiceprints_postgres(db, purge.PurgeScope(kind="user", user_id=user.id))
    db.expire_all()
    return _measure(db, user)


# --- an Inbox skip is a review marker, not a confirmation --------------------------------


def _suggested(db, user, **kw):
    return make_speaker(
        db,
        user,
        make_file(db, user),
        suggested_name="Bob",
        suggestion_source="voice_match",
        confidence=0.9,
        **kw,
    )


def test_a_skipped_voice_suggestion_still_names_the_speaker_while_voiceprints_are_on(
    db_session, normal_user
):
    speaker = _suggested(db_session, normal_user)
    db_session.commit()

    _skip(db_session, normal_user, speaker)

    assert speaker.verified is True, "the Inbox still stops listing it"
    assert canonical_speaker_label_for_row(speaker, db_session) == "Bob"


def test_a_skipped_voice_suggestion_is_hidden_while_off_and_a_purge_clears_it(
    db_session, normal_user, opensearch
):
    speaker = _suggested(db_session, normal_user)
    db_session.commit()
    _skip(db_session, normal_user, speaker)
    _off(db_session, normal_user)

    label = canonical_speaker_label_for_row(speaker, db_session)
    redact_speaker_rows(db_session, [speaker])
    shown = (speaker.suggested_name, speaker.confidence)
    db_session.expire_all()
    residual_before = _measure(db_session, normal_user).counts
    after = _purge_and_measure(db_session, normal_user)

    row = db_session.get(Speaker, speaker.id)
    assert label == "SPEAKER_00", "the voiceprint's name is not the label while off"
    assert shown == (None, None)
    assert residual_before["voiceprint_suggestions"] == 1, "control: the residual does see it"
    assert (row.suggested_name, row.suggestion_source, row.confidence) == (None, None, None)
    assert row.verified is False, "back in the Inbox: nobody ever chose a name"
    assert after.counts["voiceprint_suggestions"] == 0


def test_a_skipped_llm_guess_keeps_no_voiceprint_provenance(db_session, normal_user):
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        suggested_name="Bob",
        suggestion_source="llm_analysis",
        confidence=0.6,
    )
    db_session.commit()

    _skip(db_session, normal_user, speaker)

    assert speaker.identity_source is None


def test_a_skip_never_overwrites_the_provenance_of_a_name_or_link_that_exists(
    db_session, normal_user
):
    profile = make_profile(db_session, normal_user, "Carol")
    named = _suggested(db_session, normal_user, display_name="Alice", identity_source="human")
    linked = _suggested(db_session, normal_user, profile_id=profile.id, identity_source="human")
    db_session.commit()

    _skip(db_session, normal_user, named)
    _skip(db_session, normal_user, linked)

    assert (named.identity_source, linked.identity_source) == ("human", "human")


def test_accepting_the_suggestion_is_a_persons_confirmation_and_stays_when_off(
    db_session, normal_user
):
    speaker = _suggested(db_session, normal_user)
    db_session.commit()
    SpeakerClusteringService(db_session).batch_verify_speakers(
        speaker_uuids=[str(speaker.uuid)],
        user_id=normal_user.id,
        action="accept",
        organization_id=None,
    )
    db_session.commit()
    db_session.refresh(speaker)
    _off(db_session, normal_user)

    assert speaker.identity_source == "human"
    assert canonical_speaker_label_for_row(speaker, db_session) == "Bob"


# --- a voiceprint's link onto a person's name carries the match's evidence --------------------


def _linked(db, user, source):
    profile = make_profile(db, user, f"Alice {source} {uuid.uuid4().hex[:6]}")
    return make_speaker(
        db,
        user,
        make_file(db, user),
        display_name="Alice",
        profile_id=profile.id,
        verified=True,
        suggested_name="Alice",
        suggestion_source="profile_match",
        confidence=0.82,
        identity_source=source,
    )


def test_a_voiceprint_link_onto_a_typed_name_hides_its_match_evidence_while_off(
    db_session, normal_user
):
    machine = _linked(db_session, normal_user, "voiceprint_link")
    person = _linked(db_session, normal_user, "human")
    db_session.commit()
    _off(db_session, normal_user)

    redact_speaker_rows(db_session, [machine, person])

    assert (machine.display_name, machine.verified) == ("Alice", True), "the typed name stays"
    assert (machine.profile_id, machine.suggested_name, machine.confidence) == (None, None, None)
    assert (person.suggested_name, person.confidence) == (None, None), (
        "the voice comparison is hidden on a person's speaker too"
    )
    assert (person.display_name, person.verified, person.profile_id) == (
        "Alice",
        True,
        person.profile_id,
    ), "the person's name and link stay"


def test_a_voiceprint_link_onto_a_typed_name_shows_its_evidence_while_on(db_session, normal_user):
    machine = _linked(db_session, normal_user, "voiceprint_link")
    db_session.commit()

    redact_speaker_rows(db_session, [machine])

    assert (machine.suggested_name, machine.confidence) == ("Alice", 0.82)


def test_a_purge_removes_the_link_and_evidence_but_keeps_the_typed_name_and_a_persons_identity(
    db_session, normal_user, opensearch
):
    machine = _linked(db_session, normal_user, "voiceprint_link")
    person = _linked(db_session, normal_user, "human")
    db_session.commit()
    _off(db_session, normal_user)
    before = _measure(db_session, normal_user).counts["voiceprint_suggestions"]

    residual = _purge_and_measure(db_session, normal_user)

    row, kept = db_session.get(Speaker, machine.id), db_session.get(Speaker, person.id)
    assert (row.display_name, row.verified, row.profile_id) == ("Alice", True, None)
    assert (row.suggested_name, row.suggestion_source, row.confidence) == (None, None, None)
    assert row.identity_source is None
    assert (kept.display_name, kept.verified, kept.profile_id, kept.identity_source) == (
        "Alice",
        True,
        person.profile_id,
        "human",
    ), "the person's own identity is untouched"
    assert (kept.suggested_name, kept.suggestion_source, kept.confidence) == (None, None, None)
    assert before == 2, "control: the residual sees both rows' evidence before the purge"
    assert residual.counts["voiceprint_suggestions"] == 0


def test_the_purge_counts_only_links_that_still_pointed_at_a_profile(db_session, normal_user):
    """A deleted profile already nulled the link (ON DELETE SET NULL): nothing left to remove."""
    live = _linked(db_session, normal_user, "voiceprint_link")
    orphaned = _linked(db_session, normal_user, "voiceprint_link")
    db_session.commit()
    db_session.delete(db_session.get(SpeakerProfile, orphaned.profile_id))
    db_session.commit()
    db_session.refresh(orphaned)
    assert orphaned.profile_id is None, "control: the link is already gone"

    done = purge_voiceprints_postgres(
        db_session, purge.PurgeScope(kind="user", user_id=normal_user.id)
    )

    assert done.counts["speaker_links_removed"] == 1
    db_session.expire_all()
    assert db_session.get(Speaker, live.id).identity_source is None
    assert db_session.get(Speaker, orphaned.id).identity_source is None, "the marker still resets"
