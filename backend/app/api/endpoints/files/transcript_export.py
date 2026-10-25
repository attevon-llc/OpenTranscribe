"""API endpoint for full transcript export (txt / json / csv / srt / vtt / md).

This is a transcript read surface and is gated like one — see ``files/subtitles.py``'s
module docstring for the shape of the guarantee. It replaces the client-side exporter that
used to live at ``frontend/src/lib/export/transcriptExport.ts`` (issue #673): that module
serialized already-downloaded transcript data in the browser, so the admin ``export_locked``
floor (mandated censored exports) was enforced only for subtitle downloads and never for
txt/json/csv/srt/vtt. Moving serialization here means every export format now consults the
same redaction policy, resolved the same fail-closed way.
"""

import logging
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.media import Comment
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import TranscriptSegment
from app.models.user import User
from app.services.platform_bypass import PlatformBypass
from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.export_policy import resolve_export_reveal
from app.services.redaction.reveal_audit import audit_unredacted_reveal
from app.services.transcript_export_markdown import MarkdownMeta
from app.services.transcript_export_service import VALID_FORMATS
from app.services.transcript_export_service import build_export_content
from app.utils.content_disposition import attachment_header
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

from .crud import _redaction_pending

router = APIRouter()
logger = logging.getLogger(__name__)

_CONTENT_TYPES = {
    "txt": "text/plain",
    "json": "application/json",
    "csv": "text/csv",
    "srt": "application/x-subrip",
    "vtt": "text/vtt",
    "md": "text/markdown; charset=utf-8",
}


def _markdown_meta(
    media_file: MediaFile, cfg: EffectiveRedactionConfig, reveal: set[str]
) -> MarkdownMeta:
    """Front-matter facts for the ``md`` export, from the file row and the resolved policy."""
    source_name = media_file.filename or str(media_file.uuid)
    recorded = media_file.recorded_date
    return MarkdownMeta(
        title=media_file.title or source_name.rsplit(".", 1)[0] or source_name,
        recorded_date=recorded.astimezone(UTC).date().isoformat() if recorded else None,
        date_source=media_file.recorded_date_source,
        duration_seconds=media_file.duration,
        language=media_file.language,
        file_uuid=str(media_file.uuid),
        exported_at=datetime.now(UTC),
        # True only when some enabled category is still masked after the reveal.
        redacted=bool(cfg.masks_anything and (cfg.enabled_categories - reveal)),
    )


def _resolve_export_redaction(
    db,
    media_file,
    current_user,
    redact: bool,
    organization_id=None,
    bypass: PlatformBypass = PlatformBypass.none(),
):
    """Resolve (cfg, reveal_categories) for a transcript export.

    Identical fail-closed shape to ``subtitles._resolve_subtitle_redaction``: honors the
    admin ``export_locked`` floor unconditionally, and refuses the export outright (503)
    rather than resolving to "unmasked" when the policy itself cannot be read.

    Args:
        organization_id: The requester's active tenant scope (``ctx.org_id``),
            threaded into ``resolve_effective_config`` so a registered per-org
            redaction floor (issue #982/#987) is actually consulted (#988).
        bypass: The request's platform bypass; decides whether a non-owner admin may reveal.

    Raises:
        HTTPException: 503 when the redaction policy cannot be resolved.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, current_user.id, organization_id=organization_id)
    except Exception as e:
        # FAIL CLOSED — see subtitles.py:_resolve_subtitle_redaction for why returning
        # None here would silently skip the export_locked branch below and export raw.
        logger.exception("Failed to resolve redaction config; refusing the transcript export")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; export withheld.",
        ) from e

    reveal = resolve_export_reveal(
        cfg,
        reveal_requested=(redact is False),
        is_owner=(media_file.user_id == current_user.id),
        admin_reveal_allowed=bypass.admin_reveal_allowed,
    )
    audit_unredacted_reveal(media_file, current_user, reveal, surface="transcript_export")
    return cfg, reveal


@router.get(
    "/{file_uuid}/export",
    response_class=Response,
    dependencies=[Depends(refuse_under_support_grant)],
)
def export_transcript(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    export_format: str = Query(..., alias="format", description="txt|json|csv|srt|vtt|md"),
    include_comments: bool = Query(False),
    include_timestamps: bool = Query(True, description="TXT and Markdown only"),
    include_speakers: bool = Query(True, description="TXT and Markdown only"),
    redact: bool = Query(True, description="Apply content redaction (owner/admin may disable)"),
    speaker_default_label: str = Query("Speaker"),
    user_comment_label: str = Query("USER COMMENT"),
    comment_type_label: str = Query("COMMENT"),
    csv_header_default: str = Query("Start Time,End Time,Speaker,Text"),
    csv_header_with_comments: str = Query("Start Time,End Time,Speaker,Text,Comment Type"),
):
    """Generate and download a full transcript export.

    Supports: txt, json, csv, srt, vtt, md. The label/header query params let the frontend
    keep supplying resolved i18n strings (this module stays translation-free, matching
    the rest of the backend), with English defaults if omitted.
    """
    fmt = export_format.lower()
    if fmt not in VALID_FORMATS:
        raise HTTPException(status_code=400, detail=f"Unsupported export format: {export_format}")

    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    file_id = media_file.id

    if media_file.status != "completed":
        raise HTTPException(status_code=400, detail="Transcription not completed yet")

    cfg, reveal = _resolve_export_redaction(
        db, media_file, current_user, redact, organization_id=ctx.org_id, bypass=ctx.bypass
    )

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
            markdown_meta=_markdown_meta(media_file, cfg, reveal) if fmt == "md" else None,
        )
    except HTTPException:
        raise
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
    content_type = _CONTENT_TYPES[fmt]

    return Response(
        content=content,
        media_type=content_type,
        headers={
            "Content-Disposition": attachment_header(filename),
            "Content-Length": str(len(content.encode("utf-8"))),
        },
    )
