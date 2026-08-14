"""Celery task: parse an uploaded document into IR, chunk it, and persist the result.

Runs on the **cpu** queue (#362 Stage 6b — the DB half is v393/``app/models/document.py``,
the parser half is ``services/documents/``, this is what calls the parser). Named
``documents.parse``, matching ``services/documents/progress.py``'s docstring: it is the
only task allowed to own a ``Task``-equivalent lifecycle for a document, because a shard
creating its own would each claim to be "the" active parse.

Three phases, and the split is load-bearing — root ``CLAUDE.md``'s single highest-risk
pattern in this lane, called out by name in the handoff: **never hold a DB session across
parsing.** A page-heavy scan is minutes; holding ``ACCESS SHARE`` on ``document`` for that
long queues any ``ALTER TABLE`` behind it (an Alembic upgrade, since dev migrates on
backend startup) and pins the vacuum horizon. So: a short read session returning plain
data, the parse with no session open, then a short write session — the exact shape
``speaker_attribute_task.py`` documents as the worked example for this rule.

**OCR shard fan-out is deliberately NOT wired up here.** ``progress.py``'s
``ShardLedger``/``plan_shards`` model "one coherent percentage across N shards", and the
state-of-the-code doc lists shard fan-out as a genuinely open question. Building the
Celery group/chord orchestration to actually split a large OCR job across workers is out
of scope for this step; this task reports two honest checkpoints at the OCR stage
band's edges (start/end) instead of fabricating shard-level granularity it doesn't have.
Revisit once a real fan-out lands — the stage-band math in ``progress.py`` already
supports it, only the dispatch side does not exist yet.

Indexing (#362 Stage 6c, ``documents.index`` in ``document_indexing_task.py``) is
dispatched fire-and-forget at the end of a successful parse, mirroring how
``transcription/postprocess.enrich_and_dispatch`` dispatches ``search_indexing_task`` as a
separate task rather than indexing inline.
"""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from typing import Any

from app.core.celery import celery_app
from app.core.constants import CPUPriority
from app.db.session_utils import session_scope
from app.models.document import Document
from app.models.document import DocumentChunk
from app.models.media import FileStatus
from app.services.documents import DocumentEmptyError
from app.services.documents import DocumentParseError
from app.services.documents import DocumentParserUnavailableError
from app.services.documents import DocumentUnsupportedError
from app.services.documents import ParseOptions
from app.services.documents import ParseSource
from app.services.documents import detect_document_mime
from app.services.documents import get_parser_for
from app.services.documents.chunking import chunk_document
from app.services.documents.progress import overall_progress
from app.services.documents.registry import mark_unavailable
from app.services.minio_service import download_file
from app.tasks.document_indexing_task import dispatch_document_index
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)


def _load_document(document_id: int) -> dict[str, Any] | None:
    """Phase 1 — read. Plain data only; no ORM instance escapes the session."""
    with session_scope() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            return None
        return {
            "id": int(doc.id),
            "uuid": str(doc.uuid),
            "user_id": int(doc.user_id),
            "filename": str(doc.filename),
            "storage_path": str(doc.storage_path),
            "content_type": str(doc.content_type),
        }


def _notify(
    *,
    user_id: int,
    document_uuid: str,
    filename: str,
    content_type: str,
    status: str,
    message: str,
    progress: float,
) -> None:
    """Mirrors ``transcription/notifications.py``'s ``transcription_status`` shape, keyed
    on a document uuid instead of a media file's, so the future upload/progress UI (build
    step 5) can reuse the same websocket handler pattern.
    """
    send_ws_event(
        user_id,
        "document_status",
        {
            "document_id": document_uuid,
            "status": status,
            "message": message,
            "progress": round(progress, 1),
            "filename": filename,
            "content_type": content_type,
        },
    )


def _mark_error(document_id: int, *, error_category: str, error_message: str) -> None:
    """Phase 3 (error path) — short, DB-only write."""
    with session_scope() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            return
        doc.status = FileStatus.ERROR
        doc.error_category = error_category
        doc.last_error_message = error_message
        db.commit()


def _mark_pending_for_retry(document_id: int, *, error_message: str) -> None:
    """The ``DocumentParserUnavailableError`` outcome: the document is fine, the tier
    wasn't reachable. Left at PENDING rather than ERROR — Celery's own ``autoretry_for``
    re-invokes this task, and a retry that lands successfully should not have spent a
    visible "failed" state in between.
    """
    with session_scope() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            return
        doc.status = FileStatus.PENDING
        doc.error_category = "processing_error"
        doc.last_error_message = error_message
        db.commit()


def _parse_with_escalation(source: ParseSource, filename: str, mime: str):
    """Parse, escalating to an OCR-capable tier when the first pass found no text layer.

    ``get_parser_for`` is the single branch point (``registry.py``'s own rule) — this
    function calls it twice rather than inspecting ``parser.name`` anywhere, so a new
    backend added later is picked up with no edit here.
    """
    parser = get_parser_for(mime, filename, needs_ocr=False)
    try:
        parsed = parser.parse(source, options=ParseOptions())
    except DocumentParserUnavailableError as exc:
        mark_unavailable(parser.name, exc.detail or str(exc))
        raise

    if parsed.has_embedded_text:
        return parsed

    # No usable text layer. Escalate — unless no OCR-capable tier is configured, in
    # which case the slim result already carries the "needs OCR" warning and is the
    # best available answer.
    try:
        ocr_parser = get_parser_for(mime, filename, needs_ocr=True)
    except DocumentUnsupportedError:
        return parsed

    if ocr_parser.name == parser.name:
        return parsed

    try:
        return ocr_parser.parse(source, options=ParseOptions())
    except DocumentParserUnavailableError as exc:
        mark_unavailable(ocr_parser.name, exc.detail or str(exc))
        raise


def _parse_document(document_id: int) -> dict[str, Any]:
    """Inner implementation of :func:`parse_document_task`. See the module docstring for
    the phase split.
    """
    info = _load_document(document_id)
    if info is None:
        logger.error("document %s not found for parsing", document_id)
        return {"status": "error", "reason": "not_found"}

    user_id = info["user_id"]
    filename = info["filename"]
    content_type = info["content_type"]
    document_uuid = info["uuid"]

    def notify(status: str, message: str, progress: float) -> None:
        _notify(
            user_id=user_id,
            document_uuid=document_uuid,
            filename=filename,
            content_type=content_type,
            status=status,
            message=message,
            progress=progress,
        )

    with session_scope() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is not None:
            doc.status = FileStatus.PROCESSING
            db.commit()

    notify("processing", "Downloading document", overall_progress("download", 0.0))

    # Phase 2 — the slow work. NO DB session is held from here until the write phase
    # below. Download, mime detection, parsing (possibly an HTTP round trip to the
    # sidecar) and chunking are all CPU/IO bound and unbounded in the OCR case.
    try:
        data_stream, _size, _minio_content_type = download_file(info["storage_path"])
    except FileNotFoundError:
        message = "the uploaded file is missing from storage"
        _mark_error(document_id, error_category="processing_error", error_message=message)
        notify("error", message, 0.0)
        return {"status": "error", "reason": "missing_object"}
    except Exception as exc:  # noqa: BLE001 - storage errors here are not typed; see below
        # download_file wraps every non-NoSuchKey S3Error in a bare Exception, so this is
        # the only way to keep a transient MinIO hiccup from leaving the row stuck at
        # PROCESSING forever with no visible error and nothing to retry it.
        message = f"could not download the document from storage: {exc}"
        _mark_error(document_id, error_category="processing_error", error_message=message)
        notify("error", message, 0.0)
        return {"status": "error", "reason": "download_failed"}
    data = data_stream.getvalue()

    mime = detect_document_mime(filename, data[:512], data) or content_type
    notify("processing", "Parsing document", overall_progress("parse", 0.0))

    source = ParseSource(filename=filename, mime=mime, data=data)
    try:
        parsed = _parse_with_escalation(source, filename, mime)
    except DocumentParserUnavailableError as exc:
        message = str(exc)
        _mark_pending_for_retry(document_id, error_message=message)
        notify("pending", f"Parser unavailable, will retry: {message}", 0.0)
        raise
    except DocumentEmptyError as exc:
        message = str(exc)
        _mark_error(document_id, error_category=exc.error_category, error_message=message)
        notify("error", message, 0.0)
        return {"status": "error", "reason": "empty"}
    except DocumentParseError as exc:
        message = str(exc)
        _mark_error(document_id, error_category=exc.error_category, error_message=message)
        notify("error", message, 0.0)
        return {"status": "error", "reason": exc.error_category}

    if parsed.ocr_applied:
        notify("processing", "Reading scanned pages", overall_progress("ocr", 1.0))

    notify("processing", "Splitting into chunks", overall_progress("chunk", 0.0))
    chunks = chunk_document(parsed, title=filename)

    # Phase 3 — write. Short, DB-only. Delete-then-insert makes a retried parse
    # idempotent regardless of whether the chunk count changed; UniqueConstraint
    # (document_id, chunk_index) is the backstop if it doesn't.
    with session_scope() as db:
        doc = db.query(Document).filter(Document.id == document_id).first()
        if doc is None:
            return {"status": "error", "reason": "not_found"}

        db.query(DocumentChunk).filter(DocumentChunk.document_id == document_id).delete()
        for chunk in chunks:
            db.add(DocumentChunk(document_id=document_id, **chunk.to_row()))

        doc.status = FileStatus.COMPLETED
        doc.error_category = None
        doc.last_error_message = None
        doc.parser = parsed.parser
        doc.parser_version = parsed.parser_version
        doc.parse_version = parsed.ir_version
        doc.page_count = parsed.page_count
        doc.language = parsed.language
        doc.has_embedded_text = parsed.has_embedded_text
        doc.ocr_applied = parsed.ocr_applied
        doc.ocr_pages = parsed.ocr_pages
        doc.parse_warnings = list(parsed.warnings)
        doc.word_count = parsed.word_count
        doc.chunk_count = len(chunks)
        doc.parsed_at = datetime.now(UTC)
        db.commit()

    notify("completed", "Document ready", 100.0)
    dispatch_document_index(document_id)
    _dispatch_document_redaction(document_id, user_id)

    return {
        "status": "success",
        "document_id": document_id,
        "chunks": len(chunks),
        "words": parsed.word_count,
        "ocr_applied": parsed.ocr_applied,
        "warnings": list(parsed.warnings),
    }


@celery_app.task(
    bind=True,
    name="documents.parse",
    priority=CPUPriority.PIPELINE_CRITICAL,
    max_retries=3,
    autoretry_for=(DocumentParserUnavailableError,),
    retry_backoff=True,
    retry_backoff_max=600,
    ignore_result=True,
)
def parse_document_task(self, document_id: int) -> dict[str, Any]:
    """Parse one ``document`` row end to end.

    Args:
        document_id: ``Document.id``.

    Returns:
        A status dict. ``status: "error"`` with a ``reason`` is a real, non-retryable
        outcome (malformed input, empty extraction) — not a task failure.
    """
    return _parse_document(document_id)


def _dispatch_document_redaction(document_id: int, user_id: int) -> None:
    """Dispatch content-redaction detection for a document — ONLY when the owner has
    redaction enabled (or an admin forces it). Mirrors
    ``transcription/postprocess.py::_dispatch_redaction`` exactly: redaction is
    opt-out by default, so the (potentially expensive) scan is skipped for the common
    case. NOTE: unlike transcripts, there is no lazy-dispatch-on-open path yet for a
    user who enables redaction *after* upload (transcripts get this via
    ``crud.py``'s ``_lazy_dispatch_redaction``) — a document parsed before redaction
    was enabled stays unscanned until an admin backfill or a future lazy-open trigger
    is added. Tracked as a residual gap, not silently missing.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        with session_scope() as db:
            cfg = resolve_effective_config(db, user_id)
        if not cfg.enabled:
            logger.info("Redaction off for owner of document %s; skipping detection", document_id)
            return

        from app.tasks.redaction_task import redaction_detect_document_task

        redaction_detect_document_task.delay(document_id=document_id, user_id=user_id)
        logger.info("Dispatched document redaction detection for document %s", document_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to dispatch document redaction detection: %s", exc)


def dispatch_document_parse(document_id: int) -> None:
    """Fire-and-forget dispatch, contained — a broker hiccup must not raise into the
    upload endpoint's response path.
    """
    try:
        parse_document_task.delay(document_id=document_id)
        logger.info("Dispatched document parse for document %s", document_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to dispatch document parse for document %s: %s", document_id, exc)
