"""Corpus injection end to end — real Postgres, real OpenSearch (issue #403).

The claim this suite exists to check is not "the code runs". It is: **rows
created without ASR go through the production indexing path and land as
retrievable chunks in ``transcript_chunks``.** Every link in that chain is
something only the real services can confirm — that ``uq_transcript_segment_content``
accepts the rows, that ``index_transcript_search_task`` reads them back the way
it reads ASR output, that ``chunk_transcript_by_speaker_turns`` produces
documents, that the neural ingest pipeline accepts them, and that a re-run
overwrites rather than duplicates.

A stand-in index proves none of that. So this file talks to the real cluster and
**skips loudly** when none is reachable, rather than quietly substituting a fake
under the same test id (the #400 near-miss recorded in
``.rag-403/notes-for-testing-agent.md``).

Point it at an isolated stack — never the shared dev one::

    OPENSEARCH_PORT=5280 POSTGRES_PORT=5276 MINIO_PORT=5278 \\
        pytest backend/tests/integration/test_corpus_injection_e2e.py -m integration

Indexing runs via ``dispatch_indexing(mode="eager")``: the same task function the
Celery worker executes, run in this process so the assertion can follow it. The
broker-backed path is exercised by the CLI, not here — what differs is the
executor, not the code under test.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import func
from sqlalchemy import select

from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection.injector import dispatch_indexing
from app.scripts.corpus_injection.injector import inject_meeting
from app.scripts.corpus_injection.model import TIMING_REAL
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.model import TimingInfo
from app.scripts.corpus_injection.model import Turn
from app.scripts.corpus_injection.model import Word

_OPENSEARCH_ABSENT = os.environ.get("SKIP_OPENSEARCH", "True").lower() == "true"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        _OPENSEARCH_ABSENT,
        reason=(
            "No OpenSearch reachable (SKIP_OPENSEARCH). Start an isolated stack and export "
            "OPENSEARCH_PORT/POSTGRES_PORT — a stand-in index cannot show that injected rows "
            "reach transcript_chunks through the production task."
        ),
    ),
]

# Every run gets its own UUID namespace, so nothing collides with a real
# injection on the same stack and cleanup can be exact.
SEED = f"pytest-{uuid.uuid4().hex[:12]}"
CORPUS = "pytest-corpus"


def _doc(meeting_id: str, turns: list[Turn], timing: TimingInfo | None = None) -> MeetingDoc:
    return MeetingDoc(
        corpus=CORPUS,
        meeting_id=meeting_id,
        title=f"Injection E2E {meeting_id}",
        turns=turns,
        timing=timing or TimingInfo(),
    )


def _timed_meeting(meeting_id: str) -> MeetingDoc:
    """A two-speaker meeting with real word timings and one overlapping turn."""
    turns = [
        Turn(
            0,
            "Project Manager",
            "Welcome everybody to the quarterly planning session .",
            0.0,
            4.0,
            [Word("Welcome", 0.0, 0.6), Word("everybody", 0.6, 1.4)],
        ),
        Turn(
            1,
            "Marketing",
            "Thanks . The remote control budget is the main open question .",
            3.5,
            9.0,
            [Word("Thanks", 3.5, 3.9)],
        ),
        Turn(
            2,
            "Project Manager",
            "Agreed , let us settle the budget before the prototype review .",
            9.2,
            14.0,
            [Word("Agreed", 9.2, 9.8)],
        ),
        Turn(3, "Marketing", "Right .", 14.1, 14.6, [Word("Right", 14.1, 14.6)]),
    ]
    return _doc(meeting_id, turns, TimingInfo(source=TIMING_REAL, reference="fixture:timed"))


def _untimed_meeting(meeting_id: str) -> MeetingDoc:
    turns = [
        Turn(0, "The Chair", "Order . We turn now to the supplementary estimates ."),
        Turn(1, "Hon. Member", "Thank you Chair . My question concerns the transit allocation ."),
        Turn(2, "The Chair", "Yeah ."),
        # Deliberately identical to turn 2: collides under
        # uq_transcript_segment_content unless the writer separates them.
        Turn(3, "Hon. Member", "Yeah ."),
    ]
    return _doc(meeting_id, turns)


@pytest.fixture(scope="module")
def engine():
    from app.db.base import engine as app_engine

    return app_engine


@pytest.fixture(scope="module")
def owner_id(engine) -> int:
    """An existing account on the target stack to own the injected files."""
    from sqlalchemy.orm import Session

    from app.models.user import User

    with Session(engine) as db:
        user_id = db.scalar(select(User.id).order_by(User.id).limit(1))
    if user_id is None:
        # raise, not a bare call: pytest.skip() is not typed NoReturn here, so
        # mypy cannot see that the None branch never reaches the return.
        raise pytest.skip("No user rows on the target stack; nothing can own an injected file.")
    return int(user_id)


@pytest.fixture
def session(engine):
    """A real, committing session. Savepoint isolation cannot be used here.

    ``index_transcript_search_task`` opens its own ``session_scope()``, so it
    would not see uncommitted rows. Cleanup is explicit instead.
    """
    from sqlalchemy.orm import Session

    with Session(engine) as db:
        yield db


@pytest.fixture
def opensearch():
    from app.services.opensearch_service import get_opensearch_client

    client = get_opensearch_client()
    if client is None:
        pytest.skip("OpenSearch client unavailable despite SKIP_OPENSEARCH=False.")
    return client


@pytest.fixture
def cleanup(session, opensearch):
    """Delete every artefact this test created — rows first, then chunks.

    Registered before anything is written and run from a teardown, so a failing
    assertion mid-test still cleans up. The eval stack is shared with other
    injections; leaking rows would corrupt someone else's counts.
    """
    meeting_ids: list[str] = []
    yield meeting_ids.append

    from app.core.config import settings
    from app.services.search.indexing_service import TranscriptIndexingService

    # Refresh BEFORE deleting. The indexer bulk-loads with refresh=false and
    # ``delete_by_query`` refreshes *after* it runs, so freshly indexed chunks
    # are invisible to the delete and survive — which is exactly how this
    # fixture leaked an orphaned chunk group on its first run.
    opensearch.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    indexer = TranscriptIndexingService()
    for meeting_id in meeting_ids:
        file_uuid = ids.file_uuid(CORPUS, meeting_id, SEED)
        media_file = session.execute(
            select(MediaFile).where(MediaFile.uuid == file_uuid)
        ).scalar_one_or_none()
        if media_file is not None:
            session.query(TranscriptSegment).filter(
                TranscriptSegment.media_file_id == media_file.id
            ).delete()
            session.query(Speaker).filter(Speaker.media_file_id == media_file.id).delete()
            session.delete(media_file)
            session.commit()
        indexer.delete_transcript_chunks(str(file_uuid))


def _inject(session, owner_id: int, doc: MeetingDoc, force: bool = False):
    record, turn_rows = inject_meeting(
        session, doc, owner_id, seed=SEED, tool_version="pytest", force=force
    )
    session.commit()
    return record, turn_rows


def _count_chunks(client, file_uuid: str) -> int:
    from app.core.config import settings

    client.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
    return int(
        client.count(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={"query": {"term": {"file_uuid": file_uuid}}},
        )["count"]
    )


class TestChunksActuallyLand:
    def test_injected_transcript_reaches_transcript_chunks(
        self, session, owner_id, opensearch, cleanup
    ):
        meeting_id = "timed-001"
        cleanup(meeting_id)

        # Negative control: the index must be empty for this uuid beforehand,
        # or a passing count would prove nothing.
        file_uuid = str(ids.file_uuid(CORPUS, meeting_id, SEED))
        assert _count_chunks(opensearch, file_uuid) == 0

        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        assert record.action == "created"
        result = dispatch_indexing(record, owner_id, mode="eager")
        assert result is not None

        assert _count_chunks(opensearch, file_uuid) > 0

    def test_the_chunk_body_carries_the_injected_text_and_speakers(
        self, session, owner_id, opensearch, cleanup
    ):
        from app.core.config import settings

        meeting_id = "timed-002"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        dispatch_indexing(record, owner_id, mode="eager")

        opensearch.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
        hits = opensearch.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={"query": {"term": {"file_uuid": record.file_uuid}}, "size": 50},
        )["hits"]["hits"]
        text = " ".join(h["_source"]["content"] for h in hits)
        speakers = {h["_source"].get("speaker") for h in hits}

        assert "quarterly planning session" in text
        assert "Project Manager" in speakers
        assert all(h["_source"]["user_id"] == owner_id for h in hits)

    def test_a_real_query_retrieves_the_injected_chunk(
        self, session, owner_id, opensearch, cleanup
    ):
        """Indexed is not the same as retrievable — ask the engine for it."""
        from app.core.config import settings

        meeting_id = "timed-003"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        dispatch_indexing(record, owner_id, mode="eager")

        opensearch.indices.refresh(index=settings.OPENSEARCH_CHUNKS_INDEX)
        hits = opensearch.search(
            index=settings.OPENSEARCH_CHUNKS_INDEX,
            body={
                "query": {
                    "bool": {
                        "must": [{"match": {"content": "remote control budget"}}],
                        "filter": [{"term": {"file_uuid": record.file_uuid}}],
                    }
                }
            },
        )["hits"]["hits"]
        assert hits, "BM25 found nothing for text that was definitely indexed"

    def test_untimed_corpus_survives_the_duplicate_span_constraint(
        self, session, owner_id, opensearch, cleanup
    ):
        """Two identical "Yeah ." turns at the same synthetic instant.

        This is a real insert against the real unique index — the constraint the
        ORM does not declare and which aborted the first run of the tool.
        """
        meeting_id = "untimed-001"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _untimed_meeting(meeting_id))
        assert record.segment_count == 4
        assert record.timing_source == "synthetic"
        dispatch_indexing(record, owner_id, mode="eager")
        assert _count_chunks(opensearch, record.file_uuid) > 0


class TestIdempotency:
    def test_re_running_creates_no_duplicate_rows(self, session, owner_id, opensearch, cleanup):
        meeting_id = "idem-001"
        cleanup(meeting_id)

        first, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        dispatch_indexing(first, owner_id, mode="eager")
        second, _ = _inject(session, owner_id, _timed_meeting(meeting_id))

        assert first.action == "created"
        assert second.action == "skipped"
        assert first.file_uuid == second.file_uuid
        assert first.media_file_id == second.media_file_id

        file_count = session.scalar(
            select(func.count(MediaFile.id)).where(MediaFile.uuid == uuid.UUID(first.file_uuid))
        )
        segment_count = session.scalar(
            select(func.count(TranscriptSegment.id)).where(
                TranscriptSegment.media_file_id == first.media_file_id
            )
        )
        speaker_count = session.scalar(
            select(func.count(Speaker.id)).where(Speaker.media_file_id == first.media_file_id)
        )
        assert (file_count, segment_count, speaker_count) == (1, 4, 2)

    def test_a_skipped_re_run_still_returns_the_manifest_turn_table(
        self, session, owner_id, opensearch, cleanup
    ):
        """A re-run must produce a complete manifest, not an empty turns.jsonl."""
        meeting_id = "idem-002"
        cleanup(meeting_id)
        _, first_turns = _inject(session, owner_id, _timed_meeting(meeting_id))
        second, second_turns = _inject(session, owner_id, _timed_meeting(meeting_id))

        assert second.action == "skipped"
        assert len(second_turns) == len(first_turns) == 4
        assert second_turns == first_turns

    def test_re_indexing_does_not_duplicate_chunks(self, session, owner_id, opensearch, cleanup):
        meeting_id = "idem-003"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        dispatch_indexing(record, owner_id, mode="eager")
        before = _count_chunks(opensearch, record.file_uuid)

        dispatch_indexing(record, owner_id, mode="eager")
        assert _count_chunks(opensearch, record.file_uuid) == before

    def test_changed_content_is_rewritten_in_place(self, session, owner_id, opensearch, cleanup):
        meeting_id = "idem-004"
        cleanup(meeting_id)
        first, _ = _inject(session, owner_id, _timed_meeting(meeting_id))

        edited = _timed_meeting(meeting_id)
        edited.turns.append(Turn(4, "Marketing", "One more point about packaging .", 15.0, 18.0))
        second, _ = _inject(session, owner_id, edited)

        assert second.action == "updated"
        assert second.media_file_id == first.media_file_id
        assert second.content_sha256 != first.content_sha256
        assert (
            session.scalar(
                select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.media_file_id == first.media_file_id
                )
            )
            == 5
        )


class TestRowShape:
    def test_the_file_never_enters_an_asr_reachable_state(self, session, owner_id, cleanup):
        """``COMPLETED`` + NULL ``completed_at`` is what keeps beat off the row.

        ``PROCESSING`` would have startup recovery delete the segments and queue
        ASR; ``PENDING`` would have the orphan sweeper hard-delete the row; a
        non-NULL ``completed_at`` would have the health check queue
        summarization and topic extraction 30 minutes later.
        """
        meeting_id = "shape-001"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        media_file = session.get(MediaFile, record.media_file_id)

        assert media_file.status.value == "completed"
        assert media_file.completed_at is None
        assert media_file.storage_path
        assert media_file.file_size == 0
        assert media_file.thumbnail_path is None

    def test_provenance_is_queryable_from_the_row_itself(self, session, owner_id, cleanup):
        meeting_id = "shape-002"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _untimed_meeting(meeting_id))
        media_file = session.get(MediaFile, record.media_file_id)

        block = media_file.metadata_important["rag_eval"]
        assert block["timings_are_measurements"] is False
        assert block["timing_source"] == "synthetic"
        assert block["corpus"] == CORPUS
        assert block["meeting_id"] == meeting_id

    def test_synthetic_segments_have_no_word_timings_in_the_database(
        self, session, owner_id, cleanup
    ):
        meeting_id = "shape-003"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _untimed_meeting(meeting_id))
        rows = session.execute(
            select(TranscriptSegment.words).where(
                TranscriptSegment.media_file_id == record.media_file_id
            )
        ).all()
        assert rows and all(row[0] is None for row in rows)

    def test_real_segments_do_have_word_timings_in_the_database(self, session, owner_id, cleanup):
        """The control for the test above — otherwise "all NULL" proves nothing."""
        meeting_id = "shape-004"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        rows = session.execute(
            select(TranscriptSegment.words).where(
                TranscriptSegment.media_file_id == record.media_file_id
            )
        ).all()
        assert any(row[0] for row in rows)

    def test_one_speaker_row_per_distinct_label(self, session, owner_id, cleanup):
        meeting_id = "shape-005"
        cleanup(meeting_id)
        record, _ = _inject(session, owner_id, _timed_meeting(meeting_id))
        names = (
            session.execute(
                select(Speaker.name).where(Speaker.media_file_id == record.media_file_id)
            )
            .scalars()
            .all()
        )
        assert sorted(names) == ["Marketing", "Project Manager"]
