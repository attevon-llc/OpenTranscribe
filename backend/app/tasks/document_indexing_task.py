"""Celery task: index a parsed document's chunks into the v6 ``transcript_chunks`` index.

Dispatched from ``documents.parse`` once ``document``/``document_chunk`` rows are written
to Postgres (#362 Stage 6b/6c split) — parsing and indexing are deliberately separate
tasks and separate commits, mirroring how ``transcription/postprocess.py`` dispatches
``index_transcript_search`` as its own task rather than indexing inline at the end of
transcription.

Runs on the **embedding** queue, the same one ``index_transcript_search`` uses: same
infrastructure (the neural ingest pipeline embeds server-side — there is no client-side
encoder here either), same reason to keep it off the ``cpu`` queue ``documents.parse`` and
the transcription pipeline both need.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.celery import celery_app
from app.core.constants import EmbeddingPriority
from app.db.session_utils import session_scope
from app.models.document import Document
from app.models.document import DocumentChunk

logger = logging.getLogger(__name__)


def _load_document_for_indexing(document_id: int) -> dict[str, Any] | None:
    """Phase 1 — read. Plain data only; no ORM instance escapes the session.

    Bulk-indexing a few hundred chunks through the OpenSearch client is not the
    subprocess/HTTP/model-load class ``audit-session-lifetime.py`` flags, but the
    read/write split is kept anyway for the same reason ``documents.parse`` keeps it:
    one shape for every task that touches this table is one fewer thing to get wrong
    the next time a chunk count grows past "fast enough not to matter".
    """
    with session_scope() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            return None

        chunk_rows = (
            db.query(DocumentChunk)
            .filter(DocumentChunk.document_id == document_id)
            .order_by(DocumentChunk.chunk_index)
            .all()
        )
        chunks = [
            {
                "chunk_index": int(c.chunk_index),
                "text": c.text,
                "char_start": int(c.char_start),
                "char_end": int(c.char_end),
                "page": c.page,
                # #463 write-side gap: this used to stop at `page`, so
                # `index_document_chunks` had no `section_path` to write even after it
                # learned to write one — the row exists in Postgres and was simply
                # never read.
                "section_path": list(c.section_path or []),
            }
            for c in chunk_rows
        ]

        return {
            "id": int(doc.id),
            "uuid": str(doc.uuid),
            "user_id": int(doc.user_id),
            "organization_id": doc.organization_id,
            "filename": str(doc.filename),
            "upload_time": doc.created_at.isoformat() if doc.created_at else None,
            "language": doc.language,
            "content_type": str(doc.content_type),
            "file_size": doc.file_size,
            "chunks": chunks,
        }


def _index_document(document_id: int) -> dict[str, Any]:
    info = _load_document_for_indexing(document_id)
    if info is None:
        logger.error("document %s not found for indexing", document_id)
        return {"status": "error", "reason": "not_found"}

    if not info["chunks"]:
        logger.warning("document %s has no chunks to index", document_id)
        return {"status": "skipped", "reason": "no_chunks"}

    from app.services.search.indexing_service import TranscriptIndexingService

    service = TranscriptIndexingService()
    result = service.index_document_chunks(
        document_id=info["id"],
        document_uuid=info["uuid"],
        user_id=info["user_id"],
        chunks=info["chunks"],
        title=info["filename"],
        upload_time=info["upload_time"],
        language=info["language"],
        content_type=info["content_type"],
        file_size=info["file_size"],
        organization_id=info["organization_id"],
    )
    if isinstance(result, int):
        # 0 means OpenSearch was unavailable or the bulk load raised — see
        # index_document_chunks's own logging for which.
        return {"status": "error", "reason": "indexing_unavailable", "document_id": document_id}

    return {"status": "success", "document_id": document_id, **result}


@celery_app.task(
    bind=True,
    name="documents.index",
    priority=EmbeddingPriority.PIPELINE_CRITICAL,
    max_retries=3,
    default_retry_delay=30,
    ignore_result=True,
)
def index_document_task(self, document_id: int) -> dict[str, Any]:
    """Index one document's chunks into the v6 ``transcript_chunks`` document plane.

    Args:
        document_id: ``Document.id``.

    Returns:
        A status dict. ``status: "skipped"`` (no chunks) is a real outcome, not a task
        failure — a document that parsed to nothing indexable is not an error.
    """
    return _index_document(document_id)


def dispatch_document_index(document_id: int) -> None:
    """Fire-and-forget dispatch, contained — a broker hiccup must not raise into the
    parse task's completion path.
    """
    try:
        index_document_task.delay(document_id=document_id)
        logger.info("Dispatched document index for document %s", document_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to dispatch document index for document %s: %s", document_id, exc)
