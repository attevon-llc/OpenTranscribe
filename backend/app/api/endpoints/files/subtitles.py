"""
API endpoints for subtitle generation.
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

router = APIRouter()
logger = logging.getLogger(__name__)


def _resolve_subtitle_redaction(db, media_file, current_user, redact: bool):
    """Resolve (cfg, reveal_categories) for a subtitle export.

    Honors the admin forced-export lock: when ``export_locked`` is set, the original
    can never be exported regardless of the ``redact`` flag.
    """
    try:
        from app.services.redaction.config import resolve_effective_config

        cfg = resolve_effective_config(db, current_user.id)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to resolve redaction config for subtitles: {e}")
        return None, set()

    if getattr(cfg, "export_locked", False):
        return cfg, set()  # forced — never reveal on export
    can_reveal = (media_file.user_id == current_user.id) or current_user.is_admin
    reveal = cfg.reveal_categories(requested=(redact is False), is_owner=can_reveal)
    return cfg, reveal


@router.get("/{file_uuid}/subtitles", response_class=Response)
async def get_subtitles(
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

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate subtitles: {str(e)}"
        ) from e


@router.get("/{file_uuid}/subtitles/validate", response_model=SubtitleValidationResult)
async def validate_subtitles(
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

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to validate subtitles: {str(e)}"
        ) from e


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
    )
    return {"status": "processing", "job_id": job_id}


@router.get("/bulk-export-stream")
async def bulk_export_stream(
    job: str = Query(..., description="Job id returned by /bulk-export/prepare"),
    current_user: User = Depends(get_current_active_user),  # cookie-auth gates the stream
):
    """Server-Sent Events stream for an async bulk export.

    Events: ``progress`` (status text), ``ready`` (``{url, filename, exported, skipped}``),
    ``error`` (``{message}``). On connect the reconnect-safe result cache is checked first,
    so a dropped EventSource still delivers once the worker has finished — no polling.
    """
    import asyncio
    import contextlib
    import json as _json

    import redis.asyncio as aioredis
    from redis.exceptions import RedisError
    from redis.exceptions import TimeoutError as RedisTimeoutError

    from app.core.config import settings
    from app.core.redis import get_redis
    from app.services.download_events import bulk_export_channel

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {_json.dumps(payload)}\n\n"

    async def event_stream():
        # 1. Already finished? deliver immediately (covers reconnects after completion).
        cached = get_redis().get(f"bulk_export_result:{job}")
        if cached:
            yield sse("ready", _json.loads(cached))
            return

        client = aioredis.from_url(
            settings.REDIS_URL, health_check_interval=30, socket_keepalive=True
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(bulk_export_channel(job))
        yield sse("progress", {"message": "Preparing export…", "progress": 0})
        try:
            while True:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                except (RedisTimeoutError, asyncio.TimeoutError):
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
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(bulk_export_channel(job))
                await pubsub.close()
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
async def get_supported_formats():
    """
    Get list of supported subtitle formats.

    Formats:
    - srt: SubRip Text (most compatible)
    - webvtt: Web Video Text Tracks (web-friendly)
    - txt: Plain text with timestamps (human-readable)
    """
    return {"subtitle_formats": ["srt", "webvtt", "txt"]}
