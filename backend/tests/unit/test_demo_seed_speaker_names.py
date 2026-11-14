"""The demo seed names its speakers as a person's names, with a record of it (#628, #1280).

The bundle's curator typed the names, so they are ``human``: switching voiceprints off or a
voiceprint purge must never hide them. Written through the identity helpers, the history
(#1336) records the change too. Real rows on the savepoint-isolated ``db_session``.
"""

from __future__ import annotations

from fixtures.speaker_cleanup_seams import make_file
from fixtures.speaker_cleanup_seams import make_speaker
from sqlalchemy import select

from app.models.speaker_identity_event import SpeakerIdentityEvent
from app.scripts.demo_seed.__main__ import _name_speakers


def test_seeded_names_are_recorded_as_typed_by_a_person(db_session, normal_user):
    media_file = make_file(db_session, normal_user)
    named = make_speaker(db_session, normal_user, media_file, "SPEAKER_00")
    unnamed = make_speaker(db_session, normal_user, media_file, "SPEAKER_01")
    db_session.commit()

    _name_speakers(db_session, media_file.id, {"SPEAKER_00": "Flight director"})
    db_session.commit()

    assert (named.display_name, named.verified, named.identity_source) == (
        "Flight director",
        True,
        "human",
    )
    assert named.identity_source_detail["origin"] == "typed"
    assert named.identity_source_user_id is None
    assert named.identity_source_at is not None
    assert (unnamed.display_name, unnamed.identity_source) == (None, None)

    events = list(
        db_session.scalars(
            select(SpeakerIdentityEvent).where(SpeakerIdentityEvent.speaker_id == named.id)
        )
    )
    assert [(e.kind, e.origin, e.after_value["name"]) for e in events] == [
        ("rename", "typed", "Flight director")
    ]


def test_a_bundle_transcripts_speakers_map_names_the_imported_speakers(
    db_session, normal_user, tmp_path
):
    import json

    from app.scripts.demo_seed.bundle import load_bundle
    from tests.unit._demo_bundle import item
    from tests.unit._demo_bundle import write_bundle

    write_bundle(tmp_path, [item("bls-a")])
    transcript = tmp_path / "transcripts/bls-a.json"
    data = json.loads(transcript.read_text())
    data["speakers"] = {"SPEAKER_00": "BLS narrator"}
    transcript.write_text(json.dumps(data))
    _, items = load_bundle(tmp_path)
    media_file = make_file(db_session, normal_user)
    speaker = make_speaker(db_session, normal_user, media_file, "SPEAKER_00")
    db_session.commit()

    _name_speakers(db_session, media_file.id, items[0].speaker_names())
    db_session.commit()

    assert (speaker.display_name, speaker.verified) == ("BLS narrator", True)
