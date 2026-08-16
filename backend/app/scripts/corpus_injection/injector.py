"""Turn a parsed meeting into real rows, then hand off to the real indexer.

The rule this module exists to enforce: **an eval harness that indexes documents
its own way measures a fiction.** So nothing here writes to OpenSearch. It
creates the same ``MediaFile`` / ``Speaker`` / ``TranscriptSegment`` rows the
transcription pipeline creates and then dispatches
``index_transcript_search_task`` — the identical task
``tasks/transcription/finalize`` dispatches — so chunking, speaker-turn grouping,
neural embedding and the chunk document body are all production code paths.

Why the rows are safe to create directly (verified against the code, not assumed):

* **Nothing fires on insert.** The only SQLAlchemy event listeners in the backend
  are engine-level query timers (``core/db_metrics.py``), and there is no
  ``CREATE TRIGGER`` / ``NOTIFY`` anywhere in ``alembic/versions/``.
* **Every ASR dispatch goes through ``tasks/transcription/dispatch.py``**, and
  every *automated* caller of it selects on ``PROCESSING``, ``PENDING``,
  ``DOWNLOADING`` or ``ERROR``. Rows are written ``COMPLETED``, which no beat job
  turns back into ASR. (``PROCESSING`` is the dangerous one: startup recovery
  **deletes** a file's segments and re-queues ASR. ``PENDING`` is hard-deleted by
  ``orphan_upload_sweeper`` 30 minutes after ``upload_time``.)
* ``completed_at`` is left **NULL** deliberately. The 10-minute health check's
  "incomplete post-transcription" sweep selects
  ``COMPLETED AND completed_at < now-30min`` and would otherwise queue
  summarization, topic extraction and LLM speaker-ID against the eval corpus half
  an hour after injection. ``NULL < cutoff`` is never true, so the row stays
  inert. Retention cleanup falls back to ``upload_time`` (which is *now*), so
  nothing is at risk of early deletion either.
* **No object is written to storage and none is needed.** ``storage_path`` is
  ``NOT NULL`` but nothing validates that the key resolves, no reconciliation job
  deletes rows whose object is missing, and neither the search-indexing path nor
  the chat/RAG retrieval path reads object storage at all — both are pure
  Postgres + OpenSearch.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime

from sqlalchemy import delete
from sqlalchemy import func
from sqlalchemy import insert
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.enums import FileStatus
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.scripts.corpus_injection import ids
from app.scripts.corpus_injection import rows as rowbuild
from app.scripts.corpus_injection.model import InjectionRecord
from app.scripts.corpus_injection.model import MeetingDoc
from app.scripts.corpus_injection.timings import resolve_timings
from app.services.ingest_artifacts.date_sources import from_filename
from app.services.ingest_artifacts.recorded_date import resolve as resolve_recorded_date
from app.services.ingest_artifacts.recorded_date_service import apply_resolution

logger = logging.getLogger(__name__)

RAG_EVAL_KEY = rowbuild.RAG_EVAL_KEY

TurnRows = list[dict[str, object]]


def _refresh_recorded_date(media_file: MediaFile, doc: MeetingDoc) -> None:
    """Resolve this row's recording date through the SAME resolver a real upload uses.

    The corpus record is this row's container — it has no media, so nothing else can play
    that part — and the filename is consulted too. Both go through
    ``services/ingest_artifacts.resolve``, so the injector picks no winner of its own and
    the precedence rule stays in one place.

    ⚠️ **This is the injector writing what ingest would write, not the product reading eval
    metadata.** The date is also in ``metadata_important['rag_eval']``, and no product code
    may read it there: a retrieval or aggregation path consulting that block would be
    scoring the corpus against its own answer key, and the number would measure nothing.
    The product only ever reads ``media_file.recorded_date``.

    Without this, every injected meeting is dated to the injection run — ``upload_time``
    had exactly ONE distinct value across all 432 files while the meetings span a year.
    """
    apply_resolution(
        media_file,
        resolve_recorded_date(
            [
                rowbuild.recorded_date_candidate(doc),
                from_filename(media_file.filename),
            ]
        ),
    )


def _upsert_media_file(
    db: Session, doc: MeetingDoc, user_id: int, seed: str, tool_version: str, digest: str
) -> tuple[MediaFile, str]:
    file_uuid = ids.file_uuid(doc.corpus, doc.meeting_id, seed)
    existing = db.execute(select(MediaFile).where(MediaFile.uuid == file_uuid)).scalar_one_or_none()
    action = "updated" if existing else "created"

    media_file = existing or MediaFile(uuid=file_uuid, user_id=user_id)
    media_file.filename = f"{doc.corpus}__{doc.meeting_id}.transcript"
    media_file.title = doc.title
    media_file.language = doc.language
    media_file.content_type = rowbuild.INJECTED_CONTENT_TYPE
    media_file.file_size = 0
    media_file.duration = round(doc.duration, 3)
    media_file.status = FileStatus.COMPLETED
    media_file.completed_at = None  # see module docstring
    media_file.upload_time = datetime.now(UTC)
    media_file.is_public = False
    media_file.summary_status = "not_configured"
    media_file.metadata_important = rowbuild.eval_metadata(doc, seed, tool_version, digest)
    media_file.storage_path = media_file.storage_path or "pending"

    _refresh_recorded_date(media_file, doc)

    if existing is None:
        db.add(media_file)
    db.flush()  # the integer PK is needed to build the storage key, as upload.py does
    media_file.storage_path = rowbuild.storage_path(user_id, media_file.id, doc.meeting_id)
    db.flush()
    return media_file, action


def _get_or_create_speakers(
    db: Session, doc: MeetingDoc, media_file: MediaFile, user_id: int, seed: str
) -> dict[str, int]:
    """One ``Speaker`` per distinct label, keyed the way production keys them.

    The live DDL carries ``UNIQUE(user_id, media_file_id, name)`` (which the ORM
    class does not declare), so this must be get-or-create rather than
    delete-and-insert; deleting would also drop any profile links a previous run
    established.
    """
    mapping: dict[str, int] = {}
    for label in doc.speakers:
        speaker = db.execute(
            select(Speaker).where(
                Speaker.user_id == user_id,
                Speaker.media_file_id == media_file.id,
                Speaker.name == label,
            )
        ).scalar_one_or_none()
        if speaker is None:
            speaker = Speaker(
                uuid=ids.speaker_uuid(doc.corpus, doc.meeting_id, seed, label),
                user_id=user_id,
                media_file_id=media_file.id,
                name=label,
                verified=False,
            )
            db.add(speaker)
            db.flush()
        mapping[label] = speaker.id
    return mapping


def _is_unchanged(db: Session, existing: MediaFile, digest: str) -> bool:
    """True when a previous run already wrote exactly this content."""
    prior = (existing.metadata_important or {}).get(RAG_EVAL_KEY, {})
    if prior.get("content_sha256") != digest:
        return False
    has_segments = db.scalar(
        select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == existing.id).limit(1)
    )
    return has_segments is not None


def inject_meeting(
    db: Session,
    doc: MeetingDoc,
    user_id: int,
    seed: str = "",
    tool_version: str = "unknown",
    min_alignment_rate: float = 0.8,
    force: bool = False,
) -> tuple[InjectionRecord, TurnRows]:
    """Create or refresh the rows for one meeting. Does not dispatch indexing.

    Idempotent: ``file_uuid`` is a pure function of ``(corpus, seed,
    meeting_id)``, so a second run finds the same row. When the content hash is
    unchanged and segments exist, the rows are left alone and the record is
    marked ``skipped`` — but the manifest's turn table is still returned, rebuilt
    deterministically, so a re-run produces a complete manifest.
    """
    resolve_timings(doc, min_alignment_rate=min_alignment_rate)
    digest = ids.content_sha256(doc.turns)
    file_uuid = ids.file_uuid(doc.corpus, doc.meeting_id, seed)

    existing = db.execute(select(MediaFile).where(MediaFile.uuid == file_uuid)).scalar_one_or_none()
    if existing is not None and not force and _is_unchanged(db, existing, digest):
        # The skip path still refreshes the recorded date, and deliberately nothing else.
        #
        # "Unchanged" is a statement about the meeting's CONTENT — the segment hash. It is
        # not a statement about the row's derived metadata, which can gain a field the row
        # predates: `recorded_date` is exactly that, added in v390. Without this, a corpus
        # injected before v390 could never acquire its dates except through `--force`,
        # which deletes and reinserts every segment and therefore re-chunks and re-indexes
        # the file. For an eval corpus that is the expensive answer AND the dangerous one:
        # it moves segment ids and chunk boundaries, so every retrieval baseline measured
        # against the previous injection stops being comparable.
        #
        # Writing it here touches no segment, dispatches no indexing, and leaves the
        # OpenSearch index untouched — the date filter reads Postgres.
        _refresh_recorded_date(existing, doc)
        db.flush()
        _, turn_rows, nudged = rowbuild.build_segment_rows(doc, seed, media_file_id=existing.id)
        stored = int(
            db.scalar(
                select(func.count(TranscriptSegment.id)).where(
                    TranscriptSegment.media_file_id == existing.id
                )
            )
            or 0
        )
        record = _record(doc, existing, digest, "skipped", stored)
        record.extra["duplicate_spans_nudged"] = nudged
        return record, turn_rows

    media_file, action = _upsert_media_file(db, doc, user_id, seed, tool_version, digest)
    speaker_ids = _get_or_create_speakers(db, doc, media_file, user_id, seed)
    segment_rows, turn_rows, nudged = rowbuild.build_segment_rows(
        doc, seed, media_file_id=media_file.id, speaker_ids=speaker_ids
    )

    # Delete-then-bulk-insert, the same shape as tasks/transcription/storage.py.
    db.execute(delete(TranscriptSegment).where(TranscriptSegment.media_file_id == media_file.id))
    if segment_rows:
        db.execute(insert(TranscriptSegment), segment_rows)
    db.flush()

    record = _record(doc, media_file, digest, action, len(segment_rows))
    record.extra["duplicate_spans_nudged"] = nudged
    return record, turn_rows


def _record(
    doc: MeetingDoc, media_file: MediaFile, digest: str, action: str, segment_count: int
) -> InjectionRecord:
    return InjectionRecord(
        corpus=doc.corpus,
        meeting_id=doc.meeting_id,
        file_uuid=str(media_file.uuid),
        media_file_id=media_file.id,
        title=doc.title,
        turn_count=len(doc.turns),
        segment_count=segment_count,
        word_count=doc.word_count,
        speaker_count=len(doc.speakers),
        duration_seconds=round(doc.duration, 3),
        timing_source=doc.timing.source,
        timing_reference=doc.timing.reference,
        timing_aligned_turns=doc.timing.aligned_turns,
        timing_alignment_rate=round(doc.timing.alignment_rate, 4),
        synthetic_timing_params=doc.timing.params,
        content_sha256=digest,
        language=doc.language,
        action=action,
        extra=dict(doc.extra),
    )


def dispatch_indexing(record: InjectionRecord, user_id: int, mode: str = "celery") -> str | None:
    """Hand the file to the production search-indexing task.

    ``celery`` publishes to the broker so the real embedding worker does the work
    — identical to what ``finalize`` does after a transcription. ``eager`` runs
    the same task function in this process for environments with no reachable
    broker; the code path is the same, only the executor differs. ``none`` skips
    it, for a rows-only run.
    """
    if mode == "none":
        return None
    from app.tasks.search_indexing_task import index_transcript_search_task

    args = [record.media_file_id, record.file_uuid, user_id]
    if mode == "eager":
        return str(index_transcript_search_task.apply(args=args).id)
    return str(index_transcript_search_task.delay(*args).id)
