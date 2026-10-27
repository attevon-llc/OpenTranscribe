"""Exports and captions print the speaker label the policy allows (#1280).

A file's txt / srt / md exports and its caption segments name each speaker. While the file's
voiceprints are off a name a voiceprint wrote (``identity_source = 'voiceprint'``) is replaced
by the diarizer label, even when the row is ``verified``; a typed name, with or without a
machine-added profile link, is printed as typed. The "on" control prints every name. The file's
decision is made once, not once per speaker or per segment.
"""

from __future__ import annotations

import pytest

from app.services.captions.source import load_caption_segments
from app.services.privacy import policy
from app.services.transcript_export_service import build_export_content
from tests.fixtures.privacy_switches import make_org
from tests.fixtures.provenance_world import ALICE
from tests.fixtures.provenance_world import BOB
from tests.fixtures.provenance_world import CAROL
from tests.fixtures.provenance_world import build_world


@pytest.fixture(autouse=True)
def _reset_ceiling():
    policy.reset_privacy_ceiling_resolver()
    yield
    policy.reset_privacy_ceiling_resolver()


def _export(world, db, fmt: str) -> str:
    from app.models.media import Speaker
    from app.models.media import TranscriptSegment

    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == world.file.id)
        .order_by(TranscriptSegment.start_time)
        .all()
    )
    speakers = db.query(Speaker).filter(Speaker.media_file_id == world.file.id).all()
    return build_export_content(
        export_format=fmt,
        segments=segments,
        speakers=speakers,
        comments=None,
        include_comments=False,
        filename="meeting.wav",
        duration=10.0,
    )


@pytest.mark.parametrize("fmt", ["txt", "srt", "md"])
def test_an_off_file_exports_the_diarizer_label_for_a_voiceprint_name(db_session, normal_user, fmt):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    text = _export(world, db_session, fmt)

    assert ALICE not in text
    assert "SPEAKER_00" in text
    assert CAROL in text
    assert BOB in text


@pytest.mark.parametrize("fmt", ["txt", "srt", "md"])
def test_an_on_file_exports_every_name(db_session, normal_user, fmt):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    text = _export(world, db_session, fmt)

    assert ALICE in text
    assert CAROL in text
    assert BOB in text


def test_off_file_captions_use_the_diarizer_label(db_session, normal_user):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=False)
    )

    labels = {
        s.speaker_label
        for s in load_caption_segments(db_session, world.file.id, redaction_cfg=None)
    }

    assert labels == {"SPEAKER_00", CAROL, BOB}


def test_on_file_captions_use_every_name(db_session, normal_user):
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    labels = {
        s.speaker_label
        for s in load_caption_segments(db_session, world.file.id, redaction_cfg=None)
    }

    assert labels == {ALICE, CAROL, BOB}


def test_an_export_asks_the_policy_once_for_the_file(db_session, normal_user, monkeypatch):
    world = build_world(
        db_session,
        normal_user,
        organization=make_org(db_session, voiceprints=False),
        segments_per_speaker=30,
    )
    asked: list[object] = []
    real = policy.voiceprint_decision

    def counting(db, **kwargs):
        asked.append(kwargs.get("media_file_id"))
        return real(db, **kwargs)

    monkeypatch.setattr(policy, "voiceprint_decision", counting)

    _export(world, db_session, "txt")

    assert len(asked) == 1


def test_an_unreadable_policy_prints_the_diarizer_label(db_session, normal_user, monkeypatch):
    """Fail closed: when the decision cannot be made, a voiceprint name is not printed."""
    world = build_world(
        db_session, normal_user, organization=make_org(db_session, voiceprints=True)
    )

    def broken(*args, **kwargs):
        raise RuntimeError("policy store unreachable")

    monkeypatch.setattr(policy, "voiceprint_decision", broken)

    text = _export(world, db_session, "txt")

    assert ALICE not in text
    assert "SPEAKER_00" in text
