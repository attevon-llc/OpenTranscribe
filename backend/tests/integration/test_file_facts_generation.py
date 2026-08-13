"""End-to-end artifact generation against a real Postgres — the Stage 2 gate, executed.

The gate is *"100% of transcribed files get facts + digest with ``LLM_PROVIDER`` empty,
under 5 s p95 added"*. Three of those four words need a database: the ORM relationship,
the upsert, the cascade, and the fingerprint short-circuit are all things a pure-Python
test would assert about code rather than about behaviour.

Point at an isolated stack — never the shared dev one::

    POSTGRES_PORT=5276 pytest backend/tests/integration/test_file_facts_generation.py \\
        -m integration

Rows created here are rolled back by the ``db_session`` savepoint harness.
"""

from __future__ import annotations

import uuid as uuid_pkg

import pytest

from app.models.file_facts import FileFacts
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.ingest_artifacts.service import GENERATOR_VERSION
from app.services.ingest_artifacts.service import generate_file_artifacts
from app.services.ingest_artifacts.service import load_ordered_segments
from app.services.ingest_artifacts.service import source_fingerprint

pytestmark = pytest.mark.integration

_SCRIPT = [
    ("SPEAKER_00", "Let us start with the quarterly budget review for the new product line."),
    ("SPEAKER_00", "I think we should cut the marketing spend by fifteen percent this quarter."),
    ("SPEAKER_01", "Yeah."),
    (
        "SPEAKER_01",
        "I disagree with cutting marketing now because the launch is six weeks away and we "
        "still need the awareness campaign running through the whole period.",
    ),
    (
        "SPEAKER_02",
        "The engineering timeline slipped again so the launch date is probably moving to "
        "November regardless of what we decide about the marketing budget today.",
    ),
    ("SPEAKER_00", "If the launch moves to November the whole spending question changes."),
    ("SPEAKER_01", "We should revisit the budget once engineering confirms the November date."),
    ("SPEAKER_02", "I will confirm the November launch date with the engineering leads."),
]


@pytest.fixture
def transcribed_file(db_session):
    """A real ``media_file`` with real ``transcript_segment`` rows and real speakers."""
    user = User(
        email=f"facts_{uuid_pkg.uuid4().hex[:10]}@example.com",
        hashed_password="x",
        is_active=True,
        is_superuser=False,
        role="user",
        auth_type="local",
    )
    db_session.add(user)
    db_session.flush()

    media_file = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename="facts.wav",
        storage_path="x/facts.wav",
        file_size=1,
        content_type="audio/wav",
        duration=192.0,
        language="en",
    )
    db_session.add(media_file)
    db_session.flush()

    speakers: dict[str, Speaker] = {}
    for label in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02"):
        speaker = Speaker(
            uuid=uuid_pkg.uuid4(),
            name=label,
            user_id=user.id,
            media_file_id=media_file.id,
        )
        db_session.add(speaker)
        speakers[label] = speaker
    db_session.flush()

    clock = 0.0
    for _ in range(3):
        for label, text in _SCRIPT:
            db_session.add(
                TranscriptSegment(
                    uuid=uuid_pkg.uuid4(),
                    media_file_id=media_file.id,
                    speaker_id=speakers[label].id,
                    start_time=clock,
                    end_time=clock + 8.0,
                    text=text,
                )
            )
            clock += 8.0
    db_session.flush()
    return media_file, speakers


def test_a_transcribed_file_gets_facts_and_a_digest(db_session, transcribed_file):
    """The gate itself, on one file."""
    media_file, _ = transcribed_file
    row = generate_file_artifacts(db_session, media_file.id)

    assert row is not None
    assert row.media_file_id == media_file.id
    assert row.generator_version == GENERATOR_VERSION
    assert row.section_count >= 1
    assert row.digest_word_count > 0
    assert row.digest["sections"], "a transcribed file must not get an empty digest"
    assert row.facts["speaker_count"] == 3
    assert row.facts["duration_seconds"] == 192.0
    assert row.keyphrases["phrases"]
    assert row.generation_ms is not None and row.generation_ms >= 0


def test_no_llm_provider_is_configured_while_this_runs(db_session, transcribed_file):
    """#403 D6, asserted rather than assumed.

    Every other enrichment task on the nlp queue returns early with no provider. If this
    one ever grows that gate, the Stage 2 gate silently stops being met on exactly the
    deployments it exists for — so the test states the precondition it ran under.
    """
    from app.services.llm_service import LLMService

    media_file, _ = transcribed_file
    assert LLMService.create_from_system_settings() is None, (
        "this stack has an LLM configured, so it cannot prove the no-LLM path; "
        "run it against a stack with LLM_PROVIDER empty"
    )
    assert generate_file_artifacts(db_session, media_file.id) is not None


def test_the_roster_uses_resolved_display_names(db_session, transcribed_file):
    """The user's labels, not the diarization labels — and the reason a rename regenerates."""
    media_file, speakers = transcribed_file
    speakers["SPEAKER_00"].display_name = "Dana"
    speakers["SPEAKER_00"].verified = True
    db_session.flush()

    row = generate_file_artifacts(db_session, media_file.id)
    assert row is not None
    assert "Dana" in row.facts["roster"]
    assert "SPEAKER_00" not in row.facts["roster"]


def test_a_speaker_rename_changes_the_fingerprint_and_regenerates(db_session, transcribed_file):
    """Issue #405 joins the digest-regeneration triggers for free.

    The fingerprint covers the *resolved* speaker name, so a rename invalidates the row
    without a separate trigger list to keep in sync.
    """
    media_file, speakers = transcribed_file
    before = generate_file_artifacts(db_session, media_file.id)
    assert before is not None
    fingerprint_before = before.source_fingerprint

    speakers["SPEAKER_01"].display_name = "Marcus"
    speakers["SPEAKER_01"].verified = True
    db_session.flush()

    after = generate_file_artifacts(db_session, media_file.id)
    assert after is not None
    assert after.source_fingerprint != fingerprint_before
    assert "Marcus" in after.facts["roster"]


def test_regeneration_is_skipped_when_nothing_changed(db_session, transcribed_file):
    """The short-circuit that lets Stage 3 call this on every reindex.

    Proven by identity of the row plus an unchanged ``generation_ms``: a second full run
    would produce a different timing, so an equal one means no work was done.
    """
    media_file, _ = transcribed_file
    first = generate_file_artifacts(db_session, media_file.id)
    assert first is not None
    # A marker the generator can never write: `language` is always the resolved ISO code.
    # (`generation_ms` would be the obvious choice but CHECK ck_file_facts_ms forbids a
    # sentinel there — the constraint doing its job.)
    first.language = "ZZ-sentinel"
    db_session.flush()

    second = generate_file_artifacts(db_session, media_file.id)
    assert second is not None
    assert second.language == "ZZ-sentinel", (
        "the short-circuit did not fire; the artifacts were rebuilt for an unchanged transcript"
    )


def test_force_rebuilds_even_when_the_fingerprint_matches(db_session, transcribed_file):
    media_file, _ = transcribed_file
    first = generate_file_artifacts(db_session, media_file.id)
    assert first is not None
    first.language = "ZZ-sentinel"
    db_session.flush()

    rebuilt = generate_file_artifacts(db_session, media_file.id, force=True)
    assert rebuilt is not None
    assert rebuilt.language == "en", "force did not rebuild — the sentinel survived"


def test_regeneration_upserts_rather_than_inserting_a_second_row(db_session, transcribed_file):
    media_file, _ = transcribed_file
    generate_file_artifacts(db_session, media_file.id)
    generate_file_artifacts(db_session, media_file.id, force=True)

    assert db_session.query(FileFacts).filter(FileFacts.media_file_id == media_file.id).count() == 1


def test_the_orm_relationship_reaches_the_row_from_the_file(db_session, transcribed_file):
    media_file, _ = transcribed_file
    generate_file_artifacts(db_session, media_file.id)
    db_session.expire(media_file)
    assert media_file.facts_row is not None
    assert media_file.facts_row.media_file_id == media_file.id


def test_deleting_the_file_removes_its_artifacts(db_session, transcribed_file):
    """``passive_deletes`` + ``ON DELETE CASCADE``, exercised through the ORM."""
    media_file, _ = transcribed_file
    generate_file_artifacts(db_session, media_file.id)
    file_id = media_file.id

    db_session.query(TranscriptSegment).filter(TranscriptSegment.media_file_id == file_id).delete()
    db_session.query(Speaker).filter(Speaker.media_file_id == file_id).delete()
    db_session.delete(media_file)
    db_session.flush()

    assert db_session.query(FileFacts).filter(FileFacts.media_file_id == file_id).count() == 0


def test_a_file_with_no_segments_returns_none_rather_than_failing(db_session, transcribed_file):
    """A real outcome — a file still processing, or one whose transcript was cleared."""
    media_file, _ = transcribed_file
    db_session.query(TranscriptSegment).filter(
        TranscriptSegment.media_file_id == media_file.id
    ).delete()
    db_session.flush()

    assert generate_file_artifacts(db_session, media_file.id) is None


def test_segments_are_read_in_a_total_order(db_session, transcribed_file):
    """(start_time, end_time, id) — not ``start_time`` alone (#433).

    Two segments sharing an onset is the normal case in overlapping speech; Postgres then
    returns them in physical order, which a rewrite reshuffles. Written as a tie the query
    must break the same way twice.
    """
    media_file, speakers = transcribed_file
    for label in ("SPEAKER_00", "SPEAKER_01"):
        db_session.add(
            TranscriptSegment(
                uuid=uuid_pkg.uuid4(),
                media_file_id=media_file.id,
                speaker_id=speakers[label].id,
                start_time=500.0,
                end_time=505.0,
                text=f"overlapping speech from {label}",
            )
        )
    db_session.flush()

    first = load_ordered_segments(db_session, media_file.id)
    second = load_ordered_segments(db_session, media_file.id)
    assert [s["id"] for s in first] == [s["id"] for s in second]
    assert source_fingerprint(first) == source_fingerprint(second)

    tied = [s for s in first if s["start_time"] == 500.0]
    assert len(tied) == 2
    assert tied[0]["id"] < tied[1]["id"], "the id tiebreak did not apply"


def test_digest_provenance_points_at_this_files_real_segments(db_session, transcribed_file):
    """A citation that names a segment id from another file is worse than no citation."""
    media_file, _ = transcribed_file
    row = generate_file_artifacts(db_session, media_file.id)
    assert row is not None

    owned = {
        s.id
        for s in db_session.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == media_file.id)
        .all()
    }
    cited: set[int] = set()
    for section in row.digest["sections"]:
        for sentence in section["sentences"]:
            cited.update(sentence["provenance"]["segment_ids"])
    assert cited
    assert cited <= owned
