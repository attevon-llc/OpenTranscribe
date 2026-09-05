"""API endpoint for full transcript export (txt / json / csv / srt / vtt).

This is a transcript read surface and is gated like one — see ``files/subtitles.py``'s
module docstring for the shape of the guarantee. It replaces the client-side exporter that
used to live at ``frontend/src/lib/export/transcriptExport.ts`` (issue #673): that module
serialized already-downloaded transcript data in the browser, so the admin ``export_locked``
floor (mandated censored exports) was enforced only for subtitle downloads and never for
txt/json/csv/srt/vtt. Moving serialization here means every export format now consults the
same redaction policy, resolved the same fail-closed way.
"""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import Comment
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.transcript_export_service import VALID_FORMATS
from app.services.transcript_export_service import build_export_content
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

from .crud import _redaction_pending
from .crud import audit_unredacted_reveal

router = APIRouter()
logger = logging.getLogger(__name__)

_CONTENT_TYPES = {
    "txt": "text/plain",
    "json": "application/json",
    "csv": "text/csv",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
}


def _resolve_export_redaction(db, media_file, current_user, redact: bool):
    """Resolve (cfg, reveal_categories) for a transcript export.

    Identical fail-closed shape to ``subtitles._resolve_subtitle_redaction``: honors the
    admin ``export_locked`` floor unconditionally, and refuses the export outright (503)
    rather than resolving to "unmasked" when the policy itself cannot be read.

    Raises:
        HTTPException: 503 when the redaction policy cannot be resolved.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, current_user.id)
    except Exception as e:
        # FAIL CLOSED — see subtitles.py:_resolve_subtitle_redaction for why returning
        # None here would silently skip the export_locked branch below and export raw.
        logger.exception("Failed to resolve redaction config; refusing the transcript export")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; export withheld.",
        ) from e

    if getattr(cfg, "export_locked", False):
        return cfg, set()  # forced — never reveal on export
    can_reveal = (media_file.user_id == current_user.id) or current_user.is_admin
    reveal = cfg.reveal_categories(requested=(redact is False), is_owner=can_reveal)
    audit_unredacted_reveal(media_file, current_user, reveal, surface="transcript_export")
    return cfg, reveal


@router.get("/{file_uuid}/export", response_class=Response)
def export_transcript(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    export_format: str = Query(..., alias="format", description="txt|json|csv|srt|vtt"),
    include_comments: bool = Query(False),
    include_timestamps: bool = Query(True, description="TXT format only"),
    include_speakers: bool = Query(True, description="TXT format only"),
    redact: bool = Query(True, description="Apply content redaction (owner/admin may disable)"),
    speaker_default_label: str = Query("Speaker"),
    user_comment_label: str = Query("USER COMMENT"),
    comment_type_label: str = Query("COMMENT"),
    csv_header_default: str = Query("Start Time,End Time,Speaker,Text"),
    csv_header_with_comments: str = Query("Start Time,End Time,Speaker,Text,Comment Type"),
):
    """Generate and download a full transcript export.

    Supports: txt, json, csv, srt, vtt. The label/header query params let the frontend
    keep supplying resolved i18n strings (this module stays translation-free, matching
    the rest of the backend), with English defaults if omitted.
    """
    fmt = export_format.lower()
    if fmt not in VALID_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {export_format}")

    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id

    if media_file.status != "completed":
        raise HTTPException(status_code=400, detail="Transcription not completed yet")

    cfg, reveal = _resolve_export_redaction(db, media_file, current_user, redact)

    # Withheld until detection has produced spans to apply — same rule and same reasoning
    # as the subtitle export (files/subtitles.py:116-123).
    if _redaction_pending(db, cfg, media_file):
        raise HTTPException(
            status_code=409,
            detail=(
                "Content redaction has not finished for this file, so an export "
                "could not be masked. Try again once detection completes."
            ),
        )

    segments = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.media_file_id == file_id)
        .order_by(
            TranscriptSegment.start_time,
            TranscriptSegment.end_time,
            TranscriptSegment.id,
        )
        .all()
    )
    if not segments:
        raise HTTPException(status_code=404, detail="No transcript available for this file")

    speakers = db.query(Speaker).filter(Speaker.media_file_id == file_id).all()

    comments: list[Comment] = []
    if include_comments:
        comments = (
            db.query(Comment)
            .filter(Comment.media_file_id == file_id)
            .order_by(Comment.timestamp)
            .all()
        )

    try:
        content = build_export_content(
            export_format=fmt,
            segments=segments,
            speakers=speakers,
            comments=comments,
            include_comments=include_comments,
            include_timestamps=include_timestamps,
            include_speakers=include_speakers,
            filename=media_file.filename,
            duration=media_file.duration,
            redaction_cfg=cfg,
            reveal_categories=reveal,
            speaker_default_label=speaker_default_label,
            user_comment_label=user_comment_label,
            comment_type_label=comment_type_label,
            csv_header_default=csv_header_default,
            csv_header_with_comments=csv_header_with_comments,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Failed to generate {fmt} export for file {file_uuid}")
        raise HTTPException(status_code=500, detail="Failed to generate transcript export.") from e

    if not content.strip():
        raise HTTPException(status_code=404, detail="No transcript available for this file")

    source_name = media_file.filename or str(media_file.uuid)
    base_filename = source_name.rsplit(".", 1)[0] if "." in source_name else source_name
    filename = f"{base_filename}.{fmt}"
    content_type = _CONTENT_TYPES.get(fmt, "text/plain")

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content.encode("utf-8"))),
        },
    )
