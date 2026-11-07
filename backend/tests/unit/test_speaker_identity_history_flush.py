"""The identity history is written with the change, by every kind of writer (issue #1336).

Real rows, real flushes, the savepoint-isolated ``db_session``: each test makes a change through
the helpers of ``app/utils/speaker_identity_source`` exactly as the writers do and reads back
``speaker_identity_event``.
"""

from __future__ import annotations

from fixtures.speaker_cleanup_seams import make_file
from fixtures.speaker_cleanup_seams import make_profile
from fixtures.speaker_cleanup_seams import make_speaker
from sqlalchemy import select

from app.models.speaker import Speaker
from app.models.speaker_identity_event import SpeakerIdentityEvent
from app.services.speaker_identity_history import forget_voiceprint_events
from app.services.speaker_identity_history import record_bulk_identity_events
from app.utils import speaker_identity_source as ids
from app.utils.speaker_identity_detail import ORIGIN_TYPED
from app.utils.speaker_identity_detail import ORIGIN_VOICE_MATCH
from app.utils.speaker_identity_detail import voiceprint_detail


def _events(db, speaker) -> list[SpeakerIdentityEvent]:
    return list(
        db.scalars(
            select(SpeakerIdentityEvent)
            .where(SpeakerIdentityEvent.speaker_id == speaker.id)
            .order_by(SpeakerIdentityEvent.id)
        )
    )


def test_a_typed_name_is_recorded_with_the_person_and_the_old_value(db_session, normal_user):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    speaker.display_name = "Bob"
    ids.record_human_identity(speaker, user_id=normal_user.id, origin=ORIGIN_TYPED)
    db_session.commit()
    speaker.display_name = "Robert"
    ids.record_human_identity(speaker, user_id=normal_user.id, origin=ORIGIN_TYPED)
    db_session.commit()

    first, second = _events(db_session, speaker)
    assert (first.kind, first.actor_kind, first.actor_user_id) == ("rename", "user", normal_user.id)
    assert (first.before_value, first.after_value) == (
        {"name": None, "identity_source": None},
        {"name": "Bob", "identity_source": "human"},
    )
    assert (second.before_value, second.after_value) == ({"name": "Bob"}, {"name": "Robert"})
    assert second.origin == "typed" and second.voiceprint is False
    assert second.organization_id == speaker.organization_id
    assert second.created_at is not None


def test_the_old_value_is_recorded_even_when_the_writer_never_read_it(db_session, normal_user):
    """``active_history``: an expired row still yields its previous name."""
    speaker = make_speaker(
        db_session, normal_user, make_file(db_session, normal_user), display_name="Ann"
    )
    db_session.commit()
    db_session.expire(speaker)
    speaker.display_name = "Anna"
    ids.record_human_identity(speaker, user_id=normal_user.id, origin=ORIGIN_TYPED)
    db_session.commit()
    assert _events(db_session, speaker)[-1].before_value == {"name": "Ann", "identity_source": None}


def test_a_voiceprint_match_is_recorded_as_the_systems_with_its_model(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()
    previous = speaker.display_name
    speaker.display_name = "Alice"
    speaker.profile_id = profile.id
    ids.record_machine_identity(
        speaker,
        previous_display_name=previous,
        detail=voiceprint_detail(
            ORIGIN_VOICE_MATCH, confidence=0.93, embedding_model="wespeaker-test"
        ),
    )
    db_session.commit()

    (event,) = _events(db_session, speaker)
    assert (event.actor_kind, event.actor_user_id, event.voiceprint) == ("system", None, True)
    assert (event.model, event.confidence, event.origin) == ("wespeaker-test", 0.93, "voice_match")
    assert event.after_value == {
        "name": "Alice",
        "profile_id": profile.id,
        "identity_source": "voiceprint",
    }


def test_a_persons_unlink_names_the_person(db_session, normal_user):
    profile = make_profile(db_session, normal_user, "Alice")
    speaker = make_speaker(
        db_session,
        normal_user,
        make_file(db_session, normal_user),
        display_name="Alice",
        profile_id=profile.id,
    )
    db_session.commit()
    speaker.profile_id = None
    ids.after_unlink(speaker, user_id=normal_user.id)
    db_session.commit()

    event = _events(db_session, speaker)[-1]
    assert (event.kind, event.actor_user_id) == ("unlink", normal_user.id)
    assert event.before_value == {"profile_id": profile.id}


def test_a_merge_with_no_field_change_still_writes_its_row(db_session, normal_user):
    media = make_file(db_session, normal_user)
    target = make_speaker(db_session, normal_user, media, display_name="Bob")
    source = make_speaker(db_session, normal_user, media, name="SPEAKER_07")
    db_session.commit()

    ids.record_speaker_merge(target, user_id=normal_user.id, merged=source)
    db_session.commit()

    event = _events(db_session, target)[-1]
    assert (event.kind, event.actor_user_id) == ("merge", normal_user.id)
    assert event.before_value == {"merged_name": "SPEAKER_07"}
    assert event.detail == {"origin": "speaker_merged", "merged_speaker_uuid": str(source.uuid)}


def test_a_rolled_back_change_records_nothing_and_lends_nothing(db_session, normal_user):
    speaker = make_speaker(db_session, normal_user, make_file(db_session, normal_user))
    db_session.commit()
    nested = db_session.begin_nested()
    speaker.display_name = "Mallory"
    ids.record_human_identity(speaker, user_id=normal_user.id, origin=ORIGIN_TYPED)
    db_session.flush()
    nested.rollback()
    assert _events(db_session, speaker) == []
    # A note left by a helper whose change was never flushed is forgotten on rollback too.
    nested = db_session.begin_nested()
    ids.after_unlink(speaker, user_id=normal_user.id)
    nested.rollback()

    # A later task write of the same row is not attributed to the rolled-back person.
    speaker = db_session.get(Speaker, speaker.id)
    speaker.suggested_name = "Eve"
    ids.record_suggestion(
        speaker, source="llm_analysis", name="Eve", confidence=0.5, detail={"origin": "llm"}
    )
    db_session.commit()
    (event,) = _events(db_session, speaker)
    assert (event.kind, event.actor_kind) == ("suggested", "system")


def test_a_bulk_write_records_one_row_per_speaker_and_the_purge_forgets_only_voiceprint_rows(
    db_session, normal_user
):
    media = make_file(db_session, normal_user)
    machine = make_speaker(db_session, normal_user, media)
    person = make_speaker(db_session, normal_user, media, name="SPEAKER_01")
    db_session.commit()
    machine.display_name = "Alice"
    ids.record_machine_identity(
        machine, previous_display_name=None, detail=voiceprint_detail(ORIGIN_VOICE_MATCH)
    )
    person.display_name = "Bob"
    ids.record_human_identity(person, user_id=normal_user.id, origin=ORIGIN_TYPED)
    db_session.commit()

    written = record_bulk_identity_events(
        db_session,
        [machine.id, person.id],
        kind="purge",
        user_id=None,
        detail={"origin": "voiceprint_purge"},
    )
    deleted = forget_voiceprint_events(
        db_session, select(Speaker.id).where(Speaker.media_file_id == media.id)
    )
    db_session.commit()

    assert (written, deleted) == (2, 1)
    assert [e.kind for e in _events(db_session, machine)] == ["purge"]
    assert [e.kind for e in _events(db_session, person)] == ["rename", "purge"]
    purge = _events(db_session, person)[-1]
    assert (purge.actor_kind, purge.voiceprint, purge.before_value) == ("system", False, None)
