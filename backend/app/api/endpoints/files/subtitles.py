"""
API endpoints for subtitle generation.

**An export is a transcript read surface, and it is gated like one.**
``SubtitleService`` masks with the segment's *cached* spans, so on a file whose
detection scan never ran, is queued, or is mid-flight there is nothing to apply
and the export writes the raw transcript to disk — with nothing in the resulting
file to say the scan was incomplete. :func:`_redaction_pending` (the same helper
``GET /files/{uuid}`` and ``GET /files/{uuid}/segments`` use) decides that here
too, so the download cannot be a way around the gate the page already applies.
"""

import logging

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi.responses import Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps_context import RequestContext
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.db.base import get_db
from app.models.user import User
from app.schemas.media import SubtitleValidationResult
from app.services.subtitle_service import SubtitleService
from app.utils.uuid_helpers import get_file_by_uuid_with_permission

from .crud import _redaction_pending
from .crud import audit_unredacted_reveal

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_subtitle_redaction(db, media_file, current_user, redact: bool):
    """Resolve (cfg, reveal_categories) for a subtitle export.

    Honors the admin forced-export lock: when ``export_locked`` is set, the original
    can never be exported regardless of the ``redact`` flag.

    Raises:
        HTTPException: 503 when the redaction policy cannot be resolved.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, current_user.id)
    except Exception as e:
        # FAIL CLOSED. Returning None skipped the `export_locked` branch three
        # lines below and handed SubtitleService a None config, whose masking
        # step early-returns — so the user downloaded a fully unredacted
        # SRT/VTT/TXT even under the admin `force_export_redacted` floor.
        # An export is unrecoverable once written to disk; refuse it instead.
        logger.exception("Failed to resolve redaction config; refusing the subtitle export")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; export withheld.",
        ) from e

    if getattr(cfg, "export_locked", False):
        return cfg, set()  # forced — never reveal on export
    can_reveal = (media_file.user_id == current_user.id) or current_user.is_admin
    reveal = cfg.reveal_categories(requested=(redact is False), is_owner=can_reveal)
    # Audit the reveal, exactly as the transcript read does (issue #85). This path
    # wrote NO audit event: an owner could download the unredacted original to disk —
    # the more consequential of the two, because the file leaves the application —
    # with nothing in the compliance trail. `api/CLAUDE.md` claimed both were audited
    # until it was checked; the doc was corrected first and this is the code half.
    audit_unredacted_reveal(media_file, current_user, reveal, surface="subtitle_export")
    return cfg, reveal


@router.get("/{file_uuid}/subtitles", response_class=Response)
def get_subtitles(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
    include_speakers: bool = Query(True, description="Include speaker labels in subtitles"),
    subtitle_format: str = Query("srt", description="Subtitle format (srt, webvtt, txt)"),
    redact: bool = Query(True, description="Apply content redaction (owner/admin may disable)"),
):
    """
    Generate and download subtitles for a media file.

    Returns subtitles in the requested format (SRT by default).
    Supports: srt, webvtt, txt (plain text with timestamps).
    """
    # Get media file and check permissions (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id  # Get internal ID for subtitle generation

    if media_file.status != "completed":
        raise HTTPException(status_code=400, detail="Transcription not completed yet")

    # Resolve read-time redaction (export honors the censor toggle + admin floor).
    cfg, reveal = _resolve_subtitle_redaction(db, media_file, current_user, redact)

    # Withhold the export until detection has produced spans to apply. Note this
    # is checked AFTER the reveal is resolved and ignores it: `?redact=false`
    # reveals categories that were masked, and on an unscanned file there is
    # nothing to reveal from — honoring it here would make the gate opt-out via a
    # query parameter. Deliberately NOT an inline scan: a cold PII model load is
    # ~10 s on a download request, and the transcript view already answers
    # "not yet" for this file, so an export that scanned on demand would hand
    # back content the page refused. 409 rather than the 503 above because the
    # server is fine; the file is not ready, and that is a retryable state.
    if _redaction_pending(db, cfg, media_file):
        raise HTTPException(
            status_code=409,
            detail=(
                "Content redaction has not finished for this file, so an export "
                "could not be masked. Try again once detection completes."
            ),
        )

    try:
        # Generate subtitle content based on format
        format_lower = subtitle_format.lower()
        if format_lower == "webvtt":
            subtitle_content = SubtitleService.generate_webvtt_content(
                db, file_id, include_speakers, cfg, reveal
            )
        elif format_lower == "txt":
            subtitle_content = SubtitleService.generate_txt_content(
                db, file_id, include_speakers, cfg, reveal
            )
        else:
            subtitle_content = SubtitleService.generate_srt_content(
                db, file_id, include_speakers, cfg, reveal
            )

        if not subtitle_content.strip():
            raise HTTPException(status_code=404, detail="No transcript available for this file")

        # Determine content type based on format
        content_type_map = {
            "srt": "application/x-subrip",
            "webvtt": "text/vtt",
            "txt": "text/plain",
        }
        content_type = content_type_map.get(format_lower, "text/plain")

        # Generate filename (filename column is nullable; fall back to the file UUID)
        source_name = media_file.filename or str(media_file.uuid)
        base_filename = source_name.rsplit(".", 1)[0] if "." in source_name else source_name
        filename = f"{base_filename}.{format_lower}"

        return Response(
            content=subtitle_content,
            media_type=content_type,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Length": str(len(subtitle_content.encode("utf-8"))),
            },
        )

    except HTTPException:
        # The 404 raised above for an empty transcript is a deliberate status;
        # the broad handler below turned it into a 500 whose detail embedded the
        # 404's message.
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Failed to generate {subtitle_format} subtitles for file {file_uuid}")
        raise HTTPException(status_code=500, detail="Failed to generate subtitles.") from e


@router.get("/{file_uuid}/subtitles/validate", response_model=SubtitleValidationResult)
def validate_subtitles(
    file_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """
    Validate subtitle timing and content for a media file.

    Returns validation results including any timing issues or problems found.
    """
    # Get media file and check permissions (tenant-gated via ctx.org_id)
    media_file = get_file_by_uuid_with_permission(
        db, file_uuid, current_user.id, is_admin=current_user.is_admin, organization_id=ctx.org_id
    )
    file_id = media_file.id  # Get internal ID for validation

    if media_file.status != "completed":
        raise HTTPException(status_code=400, detail="Transcription not completed yet")

    try:
        # Validate subtitle timing
        issues = SubtitleService.validate_subtitle_timing(db, file_id)

        # Get segment count and total duration via SQL aggregation
        from sqlalchemy import func

        from app.models.media import TranscriptSegment

        stats = (
            db.query(
                func.count(TranscriptSegment.id),
                func.max(TranscriptSegment.end_time),
            )
            .filter(TranscriptSegment.media_file_id == file_id)
            .first()
        )

        total_segments = stats[0] if stats else 0
        total_duration = float(stats[1]) if stats and stats[1] is not None else 0.0

        return SubtitleValidationResult(
            is_valid=len(issues) == 0,
            issues=issues,
            total_segments=total_segments,
            total_duration=float(total_duration),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Failed to validate subtitles for file {file_uuid}")
        raise HTTPException(status_code=500, detail="Failed to validate subtitles.") from e


class BulkExportPrepareRequest(BaseModel):
    """Request for async bulk subtitle export."""

    file_uuids: list[str]
    subtitle_format: str = "srt"
    include_speakers: bool = True


@router.post("/bulk-export/prepare")
def prepare_bulk_export(
    request: BulkExportPrepareRequest = Body(...),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Queue an async bulk subtitle export and return a job id.

    The ZIP is built on the ``download`` worker, uploaded to object storage, and
    delivered to the browser as a presigned URL over the ``bulk-export-stream`` SSE
    channel — the API never proxies the archive bytes. UUIDs are permission-filtered
    here so the worker can trust the resolved file ids without re-authorizing.

    Redaction is applied on the worker with **this** user's effective policy; files
    whose detection scan has not finished are skipped there rather than exported raw
    (the single-file endpoint answers 409 for the same condition).
    """
    if not request.file_uuids:
        raise HTTPException(status_code=400, detail="No file UUIDs provided")
    if len(request.file_uuids) > 100:
        raise HTTPException(status_code=400, detail="Maximum 100 files per export")
    if request.subtitle_format.lower() not in ("srt", "webvtt", "txt"):
        raise HTTPException(
            status_code=400, detail=f"Unsupported format: {request.subtitle_format}"
        )

    file_specs: list[tuple[int, str]] = []
    for file_uuid in request.file_uuids:
        try:
            media_file = get_file_by_uuid_with_permission(
                db,
                file_uuid,
                current_user.id,
                is_admin=current_user.is_admin,
                organization_id=ctx.org_id,
            )
        except HTTPException:
            continue  # inaccessible file — skip silently, mirrors prior per-file skip
        if media_file.status != "completed":
            continue
        filename = str(media_file.filename)
        base = filename.rsplit(".", 1)[0] if "." in filename else filename
        file_specs.append((media_file.id, base))

    if not file_specs:
        raise HTTPException(
            status_code=404,
            detail="No accessible completed files to export.",
        )

    from uuid import uuid4

    from app.tasks.media_download import prepare_bulk_subtitles_task

    job_id = uuid4().hex
    prepare_bulk_subtitles_task.delay(
        file_specs=file_specs,
        subtitle_format=request.subtitle_format,
        include_speakers=request.include_speakers,
        job_id=job_id,
        # The subject whose redaction policy masks the archive — the REQUESTING user,
        # matching the single-file export beside it (issue #85). The worker re-resolves
        # the policy from this id at run time; nothing about the policy is sent here.
        user_id=current_user.id,
    )
    return {"status": "processing", "job_id": job_id}


@router.get("/bulk-export-stream")
def bulk_export_stream(
    job: str = Query(..., description="Job id returned by /bulk-export/prepare"),
    current_user: User = Depends(get_current_active_user),  # cookie-auth gates the stream
):
    """Server-Sent Events stream for an async bulk export.

    Events: ``progress`` (status text), ``ready`` (``{url, filename, exported, skipped}``),
    ``error`` (``{message}``). The reconnect-safe result cache is read both before and
    after subscribing, so a dropped EventSource — or a job that finishes inside the
    subscribe window (issue #334) — still delivers without polling.
    """
    import asyncio
    import contextlib
    import json as _json

    import redis.asyncio as aioredis
    from redis.exceptions import RedisError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from app.core.config import settings
    from app.services.download_events import bulk_export_channel

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(payload)}\n\n"

    async def event_stream():
        # The whole generator is iterated ON the event loop, so every Redis call in it
        # must be async. The result-cache read used to go through the synchronous
        # ``get_redis()`` singleton, stalling every other request for the round trip
        # (issue #320); it now shares the async client already used for the pub/sub.
        client = aioredis.from_url(
            settings.REDIS_URL, health_check_interval=30, socket_keepalive=True
        )

        async def ready_frame() -> str | None:
            """SSE ``ready`` frame if the worker already cached a result, else None."""
            cached = await client.get(f"bulk_export_result:{job}")
            return sse("ready", _json.loads(cached)) if cached else None

        try:
            # 1. Already finished? deliver immediately (covers reconnects after completion).
            frame = await ready_frame()
            if frame:
                yield frame
                return

            pubsub = client.pubsub()
            await pubsub.subscribe(bulk_export_channel(job))
            try:
                # 2. RE-CHECK after subscribing (issue #334, mirroring #284 A1.22).
                #
                # A single check-then-subscribe has a lost-wakeup race: a worker that
                # finishes in the gap writes the result key and publishes to an empty
                # channel, and this stream then waits on `get_message` forever for an
                # event that already happened — the export appears to hang while the
                # ZIP sits ready in object storage. Re-reading after subscribing means
                # the completion is either buffered on the subscription or visible to
                # this second read; it cannot fall between them.
                frame = await ready_frame()
                if frame:
                    yield frame
                    return

                yield sse("progress", {"message": "Preparing export…", "progress": 0})
                while True:
                    try:
                        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                    except (TimeoutError, RedisTimeoutError):
                        yield ": keepalive\n\n"
                        continue
                    except RedisError as e:
                        logger.warning(f"Bulk export SSE pubsub error for job {job}: {e}")
                        yield sse("error", {"message": "Connection interrupted."})
                        return
                    if msg is None:
                        yield ": keepalive\n\n"
                        continue
                    try:
                        data = _json.loads(msg["data"])
                    except Exception:
                        logger.debug("Skipping malformed bulk export event payload")
                        continue
                    status = data.get("status")
                    if status == "completed" and data.get("url"):
                        yield sse("ready", data)
                        return
                    if status == "error":
                        yield sse("error", data)
                        return
                    yield sse("progress", data)
            finally:
                # Only reached once subscribed — unsubscribing an unsubscribed pubsub
                # would open a connection just to tear it down.
                with contextlib.suppress(Exception):
                    await pubsub.unsubscribe(bulk_export_channel(job))
                    await pubsub.close()
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/supported-formats")
def get_supported_formats():
    """
    Get list of supported subtitle formats.

    Formats:
    - srt: SubRip Text (most compatible)
    - webvtt: Web Video Text Tracks (web-friendly)
    - txt: Plain text with timestamps (human-readable)
    """
    return {"subtitle_formats": ["srt", "webvtt", "txt"]}
