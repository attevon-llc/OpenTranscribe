"""Gather, mask and lay out one transcript for a DOCX / PDF export, at task time.

:func:`prepare_document` is the read phase: it runs inside the worker's short database session,
re-applies every gate the dispatching request applied (quarantine, a pending redaction scan, the
reader's policy and the admin ``export_locked`` floor), and returns plain frozen data. Nothing
ORM-shaped escapes, so :func:`render_document` runs afterwards with no session held, which
matters because a long PDF takes about a minute.

The policy is resolved HERE, from a ``user_id``, never carried through the Celery signature
(``redaction/export_policy.py`` argues why). ``is_owner`` is recomputed from the file row, and a
platform admin's non-owner reveal is not offered for documents (``admin_reveal_allowed`` is
False): that bypass is scoped to a request, and a worker has none.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.redaction.export_policy import export_masking_is_pending
from app.services.redaction.export_policy import resolve_export_reveal
from app.services.redaction.reveal_audit import audit_unredacted_reveal
from app.services.takedown_service import exclude_quarantined
from app.services.takedown_service import is_review_admin
from app.services.transcript_document.blocks import build_blocks
from app.services.transcript_document.model import Block
from app.services.transcript_document.model import DocumentLabels
from app.services.transcript_document.model import DocumentMeta
from app.services.transcript_document.model import DocumentOptions
from app.services.transcript_document.model import PageSpec
from app.services.transcript_export_turns import comment_rows_from
from app.services.transcript_export_turns import group_turns
from app.services.transcript_export_turns import masked_segments
from app.services.transcript_export_turns import speaker_namer
from app.services.transcript_export_turns import voiceprint_names_for

if TYPE_CHECKING:
    from app.schemas.transcript_export import TranscriptDocumentExportRequest

logger = logging.getLogger(__name__)

#: Audit ``surface`` of a reveal granted to a document export.
AUDIT_SURFACE = "transcript_document_export"


class DocumentExportRefusedError(Exception):
    """The export must not be produced. ``reason`` is a key of ``DOCUMENT_EXPORT_ERRORS``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class PreparedDocument:
    """Everything a renderer needs, as plain data."""

    blocks: list[Block]
    spec: PageSpec
    options: DocumentOptions
    labels: DocumentLabels
    meta: DocumentMeta
    #: The download name without its extension.
    base_filename: str


def _options_of(request: TranscriptDocumentExportRequest) -> DocumentOptions:
    return DocumentOptions(
        include_timestamps=request.include_timestamps,
        include_speakers=request.include_speakers,
        include_comments=request.include_comments,
        line_numbering=request.line_numbering,
        page_size=request.page_size,
    )


def _labels_of(request: TranscriptDocumentExportRequest) -> DocumentLabels:
    return DocumentLabels(**request.labels.model_dump())


def prepare_document(
    db: Session,
    *,
    file_id: int,
    user_id: int | None,
    organization_id: int | None,
    request: TranscriptDocumentExportRequest,
    now: datetime,
) -> PreparedDocument:
    """Resolve policy, mask and group one file's transcript.

    Raises:
        DocumentExportRefusedError: ``unavailable`` (no subject, or the policy cannot be read),
            ``redaction_pending`` (the reader's policy masks and the scan is unfinished) or
            ``failed`` (the file is gone, quarantined, or has no transcript).
    """
    from app.services.redaction.config import resolve_effective_config

    if user_id is None:
        # FAIL CLOSED: no subject means no policy to resolve, and a document is unrecoverable
        # once it reaches the browser.
        logger.error("Document export has no requesting user; refusing to build it")
        raise DocumentExportRefusedError("unavailable")

    media_file = exclude_quarantined(
        db.query(MediaFile).filter(MediaFile.id == file_id),
        include_quarantined=is_review_admin(db, user_id),
    ).first()
    user = db.query(User).filter(User.id == int(user_id)).first()
    if media_file is None or user is None:
        # A quarantined file is deliberately indistinguishable from a deleted one.
        raise DocumentExportRefusedError("failed")

    try:
        cfg = resolve_effective_config(db, user_id, organization_id=organization_id)
    except Exception as e:
        logger.error("Could not resolve the redaction policy for a document export: %s", e)
        raise DocumentExportRefusedError("unavailable") from e
    if export_masking_is_pending(cfg, media_file.redaction_status):
        raise DocumentExportRefusedError("redaction_pending")

    reveal = resolve_export_reveal(
        cfg,
        reveal_requested=request.reveal_original,
        is_owner=bool(media_file.user_id == user_id),
        admin_reveal_allowed=False,
    )
    audit_unredacted_reveal(media_file, user, reveal, surface=AUDIT_SURFACE)

    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(TranscriptSegment.start_time, TranscriptSegment.end_time, TranscriptSegment.id)
        .all()
    )
    if not segments:
        raise DocumentExportRefusedError("failed")
    speakers = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()
    options = _options_of(request)
    labels = _labels_of(request)
    comments: list[Comment] = []
    if options.include_comments:
        comments = (
            db.query(Comment)
            .filter(Comment.media_file_id == file_id)
            .order_by(Comment.timestamp)
            .all()
        )

    # ``masked_segments`` expunges each segment before masking it, so the masked text can never
    # be flushed over the stored transcript when the worker's session commits on exit.
    masked = masked_segments(segments, cfg, reveal)
    masked.sort(key=lambda s: s["start_time"])
    turns = group_turns(
        masked,
        speaker_namer(
            speakers,
            labels.speaker_default_label,
            voiceprint_names=voiceprint_names_for(speakers),
        ),
    )
    blocks = build_blocks(
        turns, comment_rows_from(comments, options.include_comments), options, labels
    )

    source_name = str(media_file.filename or media_file.uuid)
    base = source_name.rsplit(".", 1)[0] if "." in source_name else source_name
    meta = DocumentMeta(
        title=str(media_file.title or base or source_name),
        exported_at=now,
        redacted=bool(cfg.masks_anything and (cfg.enabled_categories - reveal)),
        language=str(media_file.language) if media_file.language else None,
    )
    return PreparedDocument(
        blocks=blocks,
        spec=PageSpec.named(options.page_size),
        options=options,
        labels=labels,
        meta=meta,
        base_filename=base or source_name,
    )
