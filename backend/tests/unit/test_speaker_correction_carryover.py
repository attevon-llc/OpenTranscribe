"""A user's correction survives a re-run of the file that deletes its speakers (issue #1233).

Re-diarizing, re-transcribing or retrying a file deletes every speaker row of the file,
and ``ON DELETE CASCADE`` took the user's unassignments with them. The re-run then
creates new speakers for the same voices and the voiceprint stage matches them against
the profiles again — so the section the user said was NOT Bob became Bob again, through
exactly the match that was wrong the first time.

The correction is tied to the AUDIO the user judged, not to a row id: before the delete
it is carried as the corrected speaker's time spans; when the re-run saves its segments,
every new speaker whose speech lies mostly inside those spans inherits it.

Real Postgres rows; OpenSearch answered by the recorders in ``speaker_os_seams`` /
``speaker_assignment_seams``.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import numpy as np
import pytest

from app.models.media import Speaker
from app.models.media import SpeakerCannotLink
from app.models.media import SpeakerProfileBlacklist
from app.models.media import TranscriptSegment
from tests.fixtures.speaker_cleanup_seams import make_cluster
from tests.fixtures.speaker_cleanup_seams import make_file
from tests.fixtures.speaker_cleanup_seams import make_profile
from tests.fixtures.speaker_cleanup_seams import make_speaker

pytestmark = pytest.mark.usefixtures("speaker_os_seams", "speaker_assignment_seams")

BOB = "Bob"
# The corrected voice spoke here; the re-run's second speaker is someone else entirely.
CORRECTED_SPANS = [(0.0, 5.0), (10.0, 15.0)]
OTHER_SPANS = [(20.0, 30.0)]


def _unit(v: list[float]) -> list[float]:
    a = np.array(v, dtype=np.float64)
    unit: list[float] = (a / np.linalg.norm(a)).tolist()
    return unit


VA = _unit([1.0, 0.2, 0.0, 0.0])
VB = _unit([1.0, 0.0, 0.2, 0.0])
VC = _unit([1.0, 0.0, 0.0, 0.3])


def _segments(db, file_id: int, speaker_id: int, spans) -> None:
    for start, end in spans:
        db.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=file_id,
                speaker_id=speaker_id,
                start_time=start,
                end_time=end,
                text="words",
            )
        )
    db.flush()


def _corrected_file(db, user, seams) -> dict[str, Any]:
    """Bob's cluster over A, B, C; C (in its own file, with a second speaker) unassigned."""
    from app.services.speaker_clustering_service import SpeakerClusteringService

    bob = make_profile(db, user, BOB)
    speakers = []
    for voice in (VA, VB):
        sp = make_speaker(db, user, make_file(db, user))
        sp.profile_id, sp.display_name, sp.verified = bob.id, BOB, True
        seams["voiceprints"][str(sp.uuid)] = voice
        speakers.append(sp)
    media = make_file(db, user)
    c = make_speaker(db, user, media, name="SPEAKER_00")
    c.profile_id, c.display_name, c.verified = bob.id, BOB, True
    seams["voiceprints"][str(c.uuid)] = VC
    other = make_speaker(db, user, media, name="SPEAKER_01")
    _segments(db, media.id, c.id, CORRECTED_SPANS)
    _segments(db, media.id, other.id, OTHER_SPANS)
    cluster = make_cluster(db, user, *speakers, c, promoted_to=bob.id, label=BOB)
    SpeakerClusteringService(db).unassign_speakers(
        str(cluster.uuid), [str(c.uuid)], user.id, blacklist=True
    )
    db.expire_all()
    a, b = speakers
    return {"profile": bob, "a": a, "b": b, "media": media}


def _clear(db, media, how: str, monkeypatch) -> None:
    if how == "retry":
        from app.utils import task_utils

        monkeypatch.setattr(task_utils, "transcript_is_regenerable", lambda *a, **k: True)
        monkeypatch.setattr(
            "app.tasks.search_indexing_task.index_file_summary.delay", lambda *a, **k: None
        )
        assert task_utils.reset_file_for_retry(db, int(media.id)) is True
        return
    from app.api.endpoints.files.reprocess import clear_selective_data

    monkeypatch.setattr(
        "app.tasks.search_indexing_task.index_file_summary.delay", lambda *a, **k: None
    )
    clear_selective_data(db, db.get(type(media), media.id), [how])


def _rerun(db, user, media) -> dict[str, Speaker]:
    """What a re-run writes: new speakers for the same voices, then its segments."""
    from app.tasks.transcription.speaker_processor import create_speaker_mapping
    from app.tasks.transcription.speaker_processor import process_segments_with_speakers
    from app.tasks.transcription.storage import save_transcript_segments

    raw = [
        {"start": s, "end": e, "text": "words", "speaker": "SPEAKER_00"} for s, e in CORRECTED_SPANS
    ] + [{"start": s, "end": e, "text": "words", "speaker": "SPEAKER_01"} for s, e in OTHER_SPANS]
    mapping = create_speaker_mapping(db, user.id, media.id, {"SPEAKER_00", "SPEAKER_01"})
    save_transcript_segments(db, media.id, process_segments_with_speakers(raw, mapping))
    db.expire_all()
    return {label: db.get(Speaker, sid) for label, sid in mapping.items()}


def _match_against_bob(db, user, speaker, bob, seams, monkeypatch) -> None:
    """The voiceprint stage: Bob's profile embedding matches the new speaker at 0.9."""
    from app.services.speaker_matching_service import SpeakerMatchingService

    svc = SpeakerMatchingService(db, None)
    monkeypatch.setattr(svc, "find_and_store_speaker_matches", lambda *a, **k: [])
    monkeypatch.setattr(svc, "_get_speaker_embedding_for_propagation", lambda sid: (None, None))
    monkeypatch.setattr(
        "app.services.opensearch_service.get_profile_embedding",
        lambda u: seams["profiles"].get(str(u)),
    )
    monkeypatch.setattr(
        "app.services.speaker_matching_service.add_speaker_embedding", lambda **kw: True
    )
    match = {"profile_id": bob.id, "suggested_name": BOB, "confidence": 0.9, "auto_accept": True}
    svc._handle_speaker_match(speaker, match, np.array(VC), user.id, speaker.media_file_id)
    db.commit()
    db.expire_all()


@pytest.mark.parametrize("how", ["rediarize", "transcription", "retry"])
def test_a_rerun_of_the_file_does_not_bring_back_what_the_user_unassigned(
    db_session, normal_user, speaker_os_seams, monkeypatch, how
):
    seeded = _corrected_file(db_session, normal_user, speaker_os_seams)
    bob, media = seeded["profile"], seeded["media"]

    _clear(db_session, media, how, monkeypatch)
    new = _rerun(db_session, normal_user, media)
    for speaker in new.values():
        _match_against_bob(db_session, normal_user, speaker, bob, speaker_os_seams, monkeypatch)

    corrected = db_session.get(Speaker, new["SPEAKER_00"].id)
    assert (corrected.profile_id, corrected.display_name) == (None, None), (
        "the re-run matched the section the user unassigned straight back to Bob"
    )
    assert (
        db_session.query(SpeakerProfileBlacklist)
        .filter(
            SpeakerProfileBlacklist.speaker_id == corrected.id,
            SpeakerProfileBlacklist.profile_id == bob.id,
        )
        .count()
        == 1
    ), "the new speaker for the corrected audio does not carry the correction"
    partners = {
        int(r.cannot_link_speaker_id)
        for r in db_session.query(SpeakerCannotLink).filter(
            SpeakerCannotLink.speaker_id == corrected.id
        )
    }
    assert partners == {seeded["a"].id, seeded["b"].id}, (
        "the cannot-link with Bob's other speakers was lost"
    )
    # Control: the other voice of the file carries no correction and IS matched.
    other = db_session.get(Speaker, new["SPEAKER_01"].id)
    assert other.profile_id == bob.id
    from app.models.media import SpeakerCorrectionCarryover

    assert db_session.query(SpeakerCorrectionCarryover).count() == 0, "carried rows linger"


def test_a_rerun_voice_match_does_not_suggest_the_name_the_user_ruled_out(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """No profile matched, but Bob's labelled speaker did: a >=0.75 suggestion IS a label."""
    from app.services.speaker_matching_service import SpeakerMatchingService

    seeded = _corrected_file(db_session, normal_user, speaker_os_seams)
    media = seeded["media"]
    _clear(db_session, media, "rediarize", monkeypatch)
    new = _rerun(db_session, normal_user, media)
    svc = SpeakerMatchingService(db_session, None)
    a = db_session.get(Speaker, seeded["a"].id)
    monkeypatch.setattr(
        svc,
        "find_and_store_speaker_matches",
        lambda *args, **kw: [{"speaker_id": a.id, "display_name": BOB, "confidence": 0.9}],
    )
    monkeypatch.setattr(
        "app.services.speaker_matching_service.add_speaker_embedding", lambda **kw: True
    )

    for speaker in new.values():
        svc._handle_no_speaker_match(speaker, np.array(VC), normal_user.id, media.id)
    db_session.commit()
    db_session.expire_all()

    assert db_session.get(Speaker, new["SPEAKER_00"].id).suggested_name is None, (
        "the voice match suggested Bob for the audio the user said is not Bob"
    )
    assert db_session.get(Speaker, new["SPEAKER_01"].id).suggested_name == BOB, "control"


def test_only_a_new_speaker_that_is_mostly_the_corrected_audio_inherits_it(
    db_session, normal_user, speaker_os_seams, monkeypatch
):
    """The re-run cut the audio differently: half the corrected speech went elsewhere.

    SPEAKER_00 now holds [0, 5] — all of it corrected audio, so it IS that voice.
    SPEAKER_01 holds [10, 15] and [20, 30] — a third corrected, two thirds someone the
    user never judged — so blocking it could keep the real Bob from his own profile.
    """
    from app.tasks.transcription.speaker_processor import create_speaker_mapping
    from app.tasks.transcription.speaker_processor import process_segments_with_speakers
    from app.tasks.transcription.storage import save_transcript_segments

    seeded = _corrected_file(db_session, normal_user, speaker_os_seams)
    bob, media = seeded["profile"], seeded["media"]
    _clear(db_session, media, "rediarize", monkeypatch)
    raw = [
        {"start": 0.0, "end": 5.0, "text": "w", "speaker": "SPEAKER_00"},
        {"start": 10.0, "end": 15.0, "text": "w", "speaker": "SPEAKER_01"},
        {"start": 20.0, "end": 30.0, "text": "w", "speaker": "SPEAKER_01"},
    ]
    mapping = create_speaker_mapping(
        db_session, normal_user.id, media.id, {"SPEAKER_00", "SPEAKER_01"}
    )
    save_transcript_segments(db_session, media.id, process_segments_with_speakers(raw, mapping))
    db_session.expire_all()

    def blocked(label: str) -> bool:
        rows: int = (
            db_session.query(SpeakerProfileBlacklist)
            .filter(
                SpeakerProfileBlacklist.speaker_id == mapping[label],
                SpeakerProfileBlacklist.profile_id == bob.id,
            )
            .count()
        )
        return rows == 1

    assert blocked("SPEAKER_00")
    assert not blocked("SPEAKER_01"), "a speaker that merely shares a turn was blocked"
