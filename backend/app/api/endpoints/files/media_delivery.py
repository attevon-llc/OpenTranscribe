"""Media delivery routes: download preparation, the download SSE stream and the thumbnail.

The download modes, the ready/passthrough resolution, the caller's redaction variant of a
burned-in-subtitle render, the prepare dispatch guard and the SSE event framing.
"""

import json
import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps_context import get_current_context
from app.api.deps_context import refuse_under_support_grant
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.auth import get_optional_current_user
from app.auth.audit_events import AuditEventType
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.media import MediaFile
from app.models.user import User
from app.services.content_audit import KIND_DOWNLOAD_PREPARE
from app.services.content_audit import KIND_DOWNLOAD_STREAM
from app.services.content_audit import record_file_event
from app.services.request_context import RequestContext
from app.utils.media_types import file_extension
from app.utils.media_types import normalize_media_content_type

from .crud import get_media_file_by_uuid
from .streaming import get_thumbnail_streaming_response
from .streaming import validate_file_exists

router = APIRouter()
logger = logging.getLogger(__name__)


_DOWNLOAD_MODES = {
    "video_subtitles",
    "video_original",
    "audio_mp3",
    "audio_wav",
    "audio_original",
}


def _audio_passthrough(db_file: MediaFile, mode: str) -> bool:
    """True when an audio mode can serve the original bytes without ffmpeg."""
    if not (db_file.content_type and db_file.content_type.startswith("audio/")):
        return False
    src_ext = file_extension(str(db_file.filename))
    return (
        mode == "audio_original"
        or (mode == "audio_mp3" and src_ext == ".mp3")
        or (mode == "audio_wav" and src_ext == ".wav")
    )


def _resolve_ready_download(
    db_file: MediaFile, mode: str, redaction_fingerprint: str = ""
) -> dict | None:
    """Return ``{"url", "filename"}`` if the asset can be served now, else None.

    "Ready" means a direct passthrough (original bytes) or an already-cached derived
    asset — both yield an instant presigned URL with no ffmpeg work.

    Args:
        db_file: The media file.
        mode: Download mode.
        redaction_fingerprint: The CALLER's redaction policy digest (issue #85). It is
            part of the burned-in-subtitle cache key, so a cache hit means "a video
            masked under *your* policy exists" rather than "somebody once rendered this
            file". Empty for audio modes and for policies that mask nothing.
    """
    from app.services.minio_bucket_admin import MinIOService
    from app.services.minio_service import get_presigned_download_url
    from app.services.video_processing_service import VideoProcessingService

    filename = str(db_file.filename)
    base_name = filename.rsplit(".", 1)[0] if "." in filename else filename

    if mode == "video_original" or _audio_passthrough(db_file, mode):
        url = get_presigned_download_url(
            str(db_file.storage_path),
            download_filename=filename,
            content_type=normalize_media_content_type(
                str(db_file.content_type) if db_file.content_type else None
            ),
        )
        return {"url": url, "filename": filename}

    service = VideoProcessingService(MinIOService())

    if mode == "video_subtitles":
        if db_file.status != "completed":
            raise HTTPException(status_code=422, detail="Transcript is not ready yet.")
        cache_key = service.generate_cache_key(
            db_file.id,
            filename,
            include_speakers=True,
            redaction_fingerprint=redaction_fingerprint,
        )
        if service.is_video_cached(cache_key):
            dl_name = f"{base_name}_with_subtitles.mp4"
            return {
                "url": service.presigned_download_url(cache_key, dl_name, "video/mp4"),
                "filename": dl_name,
            }
        return None

    audio_format = mode.split("_", 1)[1]
    cached = service.peek_cached_audio(filename, audio_format)
    if cached:
        cache_key, ext, content_type = cached
        dl_name = f"{base_name}.{ext}"
        return {
            "url": service.presigned_download_url(cache_key, dl_name, content_type),
            "filename": dl_name,
        }
    return None


def _download_redaction_variant(
    db: Session, db_file: MediaFile, user_id: int, mode: str, organization_id: int | None = None
) -> str:
    """The caller's redaction fingerprint for a burned-in-subtitle download.

    Only ``video_subtitles`` carries transcript text; every audio mode is the original
    waveform and has no policy to apply, so they stay on the empty variant and their
    cache keys, guard keys and events are byte-identical to before #85.

    Refuses the download when the reader's policy masks this file and its detection
    scan has not produced spans yet — burned-in text cannot be masked afterwards.

    Args:
        organization_id: The requester's active tenant scope (``ctx.org_id``),
            threaded into ``resolve_effective_config`` so a registered per-org
            redaction floor (issue #982/#987) is actually consulted (#988).

    Raises:
        HTTPException: 503 when the policy cannot be resolved, 409 while the file's
            redaction scan is incomplete. The same two codes, for the same two
            reasons, as ``GET /files/{uuid}/subtitles``.
    """
    if mode != "video_subtitles":
        return ""
    if db_file.status != "completed":
        # There is no transcript yet, so there is nothing to mask and no render can be
        # dispatched: `_resolve_ready_download` answers 422 ("Transcript is not ready
        # yet") a few lines on. Reporting "redaction has not finished" for a file that
        # has not been TRANSCRIBED would name the wrong problem.
        return ""

    from app.services.redaction.config import resolve_effective_config
    from app.services.redaction.export_policy import export_masking_is_pending
    from app.services.redaction.export_policy import export_policy_fingerprint

    try:
        cfg = resolve_effective_config(db, user_id, organization_id=organization_id)
    except Exception as e:
        # FAIL CLOSED, as the subtitle endpoint does: an unresolvable policy must not
        # degrade to "no policy", which is exactly what produced the unmasked renders.
        logger.exception("Failed to resolve redaction config; refusing the subtitle burn-in")
        raise HTTPException(
            status_code=503,
            detail="Redaction policy is temporarily unavailable; download withheld.",
        ) from e

    if export_masking_is_pending(cfg, getattr(db_file, "redaction_status", None)):
        raise HTTPException(
            status_code=409,
            detail=(
                "Content redaction has not finished for this file, so its subtitles "
                "could not be masked. Try again once detection completes."
            ),
        )
    return export_policy_fingerprint(cfg)


def _ensure_prepare_enqueued(
    db_file: MediaFile, user_id: int, mode: str, variant: str = ""
) -> None:
    """Queue the ffmpeg prep task at most once per (file, mode, variant) window.

    A short-lived Redis NX guard collapses duplicate requests (double-click, a POST
    plus the SSE stream, multiple tabs) into a single worker task. Once cached, the
    ready path short-circuits before reaching here.

    ``variant`` is the caller's redaction fingerprint (issue #85). Two readers whose
    policies mask differently must NOT dedupe onto one build: the winner's render is
    published to a channel both are listening on, and the loser would receive a video
    masked under someone else's policy. It is ``""`` for audio and for unmasked
    policies, so those guard keys are unchanged.
    """
    from app.core.redis import get_redis
    from app.services.download_events import download_prep_guard_key
    from app.tasks.media_download import prepare_media_download_task

    # Shared with the worker, which RELEASES this key when it finishes -- the expiry
    # below is only a backstop. See release_download_prep_guard().
    guard_key = download_prep_guard_key(db_file.id, mode, variant)
    try:
        first = get_redis().set(guard_key, "1", nx=True, ex=900)
    except Exception:
        # The Redis key is only a duplicate-dispatch guard; losing it costs one
        # redundant prepare task, whereas failing here would block the download.
        logger.exception("Download-prepare dedup guard unavailable; dispatching anyway")
        first = True
    if first:
        prepare_media_download_task.delay(
            file_id=db_file.id, user_id=user_id, mode=mode, variant=variant
        )


@router.post("/{file_uuid}/prepare-download", dependencies=[Depends(refuse_under_support_grant)])
def prepare_download(
    file_uuid: str,
    request: Request,
    mode: str = Query(
        ..., description="video_subtitles|video_original|audio_mp3|audio_wav|audio_original"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Prepare a media download.

    Returns one of:
    - ``{"status": "ready", "url": <presigned>, "filename": <name>}`` — passthrough or
      cache hit; the browser downloads straight from object storage.
    - ``{"status": "processing", "file_id": <uuid>}`` — ffmpeg work was queued on the
      download worker; the client opens the SSE stream (``download-stream``) to receive
      progress and the final presigned URL.
    """
    if mode not in _DOWNLOAD_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid download mode: {mode}")

    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )

    variant = _download_redaction_variant(
        db, db_file, current_user.id, mode, organization_id=ctx.org_id
    )

    # Each request for a download is a distinct copy leaving the system: never collapsed.
    record_file_event(
        db,
        request,
        event=AuditEventType.CONTENT_MEDIA_ACCESS,
        actor=current_user,
        file=db_file,
        details={"kind": KIND_DOWNLOAD_PREPARE, "mode": mode},
        dedup=False,
        bypass=ctx.bypass,
    )

    ready = _resolve_ready_download(db_file, mode, variant)
    if ready:
        return {"status": "ready", **ready}

    _ensure_prepare_enqueued(db_file, current_user.id, mode, variant)
    return {"status": "processing", "file_id": str(db_file.uuid)}


def _download_event_frame(data: dict, mode: str, variant: str = "") -> tuple[str | None, bool]:
    """Map one download pub/sub event to an SSE frame.

    Returns:
        ``(frame, terminal)`` — *frame* is None when the event is for another mode or
        another redaction variant and should be ignored; *terminal* means the stream
        should close after yielding it.
    """
    if data.get("mode") != mode or data.get("variant", "") != variant:
        return None, False
    status = data.get("status")
    if status == "completed" and data.get("url"):
        payload = {"url": data["url"], "filename": data.get("filename", "")}
        return f"event: ready\ndata: {json.dumps(payload)}\n\n", True
    if status == "error":
        payload = {"message": data.get("message", "Download failed.")}
        return f"event: error\ndata: {json.dumps(payload)}\n\n", True
    payload = {"message": data.get("message", ""), "progress": data.get("progress", 0)}
    return f"event: progress\ndata: {json.dumps(payload)}\n\n", False


@router.get("/{file_uuid}/download-stream", dependencies=[Depends(refuse_under_support_grant)])
def download_stream(
    file_uuid: str,
    request: Request,
    mode: str = Query(
        ..., description="video_subtitles|video_original|audio_mp3|audio_wav|audio_original"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
    ctx: RequestContext = Depends(get_current_context),
):
    """Server-Sent Events stream that pushes download progress + the final URL.

    The browser opens this (EventSource, cookie-authenticated) after a ``processing``
    response. Events: ``progress`` (status text), ``ready`` (``{url, filename}``),
    ``error`` (``{message}``). On (re)connect we re-check readiness first, so a dropped
    connection still delivers once the worker has finished — no client polling.
    """
    import asyncio
    import contextlib
    import json as _json

    import redis.asyncio as aioredis

    from app.core.config import settings
    from app.services.download_events import download_events_channel

    if mode not in _DOWNLOAD_MODES:
        raise HTTPException(status_code=400, detail=f"Invalid download mode: {mode}")

    db_file = get_media_file_by_uuid(
        db, file_uuid, current_user.id, bypass=ctx.bypass, organization_id=ctx.org_id
    )
    user_id = current_user.id
    # Resolved once, on the request thread, while the request's session is open: the
    # generator below runs on the event loop and its DB work is threadpooled.
    variant = _download_redaction_variant(db, db_file, user_id, mode, organization_id=ctx.org_id)
    record_file_event(
        db,
        request,
        event=AuditEventType.CONTENT_MEDIA_ACCESS,
        actor=current_user,
        file=db_file,
        details={"kind": KIND_DOWNLOAD_STREAM, "mode": mode},
        dedup=False,
        bypass=ctx.bypass,
    )

    def sse(event: str, payload: dict) -> str:
        return f"event: {event}\ndata: {json.dumps(payload)}\n\n"

    def _ready_frame() -> str | None:
        """SSE frame if the download is ready or errored, else None to keep waiting.

        Blocking: ``_resolve_ready_download`` stats object storage and presigns. The
        generator below runs on the event loop, so it must never call this directly —
        every call goes through ``run_in_threadpool`` (issue #320).
        """
        try:
            ready = _resolve_ready_download(db_file, mode, variant)
        except (HTTPException, DomainError) as e:
            # `_resolve_ready_download`'s only raise site is the 422 below, but the
            # "code" here stays a fixed literal slug — never `e.detail` itself — so
            # the SSE contract guard (test_chat_sse_contract.py) can pin it structurally.
            return sse("error", {"code": "transcript_not_ready", "message": e.detail})
        return sse("ready", ready) if ready else None

    async def event_stream():
        # 1. Fast path — already available (covers reconnects after completion). Checked
        # BEFORE touching Redis so a ready download still works when Redis is down.
        frame = await run_in_threadpool(_ready_frame)
        if frame:
            yield frame
            return

        from redis.exceptions import RedisError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        client = aioredis.from_url(
            settings.REDIS_URL, health_check_interval=30, socket_keepalive=True
        )
        pubsub = client.pubsub()
        await pubsub.subscribe(download_events_channel(file_uuid))
        try:
            # 2. RE-CHECK after subscribing (issue #284 A1.22).
            #
            # A single check-then-subscribe has a lost-wakeup race: a prepare that
            # finishes in the gap publishes to an empty channel, and this stream then
            # waits forever for an event that already happened — the download appears
            # to hang until the client gives up. Subscribing first and re-checking
            # means the completion is either buffered on the subscription or visible
            # to this second check; it cannot fall between them.
            frame = await run_in_threadpool(_ready_frame)
            if frame:
                yield frame
                return

            # 3. Make sure the work is running, then stream its events.
            #
            # Threadpooled because it is a synchronous Redis SETNX plus a Celery
            # dispatch (another blocking broker round trip) — running it inline
            # stalled the loop for every other request (issue #320).
            await run_in_threadpool(_ensure_prepare_enqueued, db_file, user_id, mode, variant)
            yield sse("progress", {"message": "Preparing your download…", "progress": 0})

            while True:
                try:
                    msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                except (TimeoutError, RedisTimeoutError):
                    yield ": keepalive\n\n"  # benign idle read — keep the SSE open
                    continue
                except RedisError as e:
                    logger.warning(f"Download SSE pubsub error for {file_uuid}: {e}")
                    yield sse(
                        "error",
                        {"code": "connection_interrupted", "message": "Connection interrupted."},
                    )
                    return
                if msg is None:
                    yield ": keepalive\n\n"  # comment frame keeps proxies from closing
                    continue
                try:
                    data = _json.loads(msg["data"])
                except Exception:
                    logger.debug("Skipping malformed download event payload")
                    continue
                frame, terminal = _download_event_frame(data, mode, variant)
                if frame is None:
                    continue
                yield frame
                if terminal:
                    return
        except asyncio.CancelledError:
            raise
        finally:
            with contextlib.suppress(Exception):
                await pubsub.unsubscribe(download_events_channel(file_uuid))
                await pubsub.close()
                await client.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering for SSE
        },
    )


@router.get("/{file_uuid}/thumbnail")
def get_thumbnail(
    file_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_current_user),
):
    """
    Get the thumbnail image for a media file.

    Security: an abuse/DMCA takedown outranks every other rule here; after that,
    requires authentication OR the file must be public. Authenticated org users
    are gated to their active tenant scope (cross-org thumbnails 403).

    Note: For gallery/list views, use the presigned thumbnail_url returned in the
    file listing response. This endpoint is a fallback for direct access.
    """
    from app.services.takedown_service import is_hidden_for
    from app.services.uuid_helpers import get_file_by_uuid

    db_file = get_file_by_uuid(db, file_uuid)
    validate_file_exists(db_file)

    # Abuse/DMCA takedown (issue #817). This route resolves with plain
    # `get_file_by_uuid`, so it does NOT inherit the quarantine 404 that
    # `get_file_by_uuid_with_permission` applies to the detail/stream/download
    # surfaces — and the `is_public` short-circuit below skips the whole
    # authorization block, so a taken-down public file was streaming its
    # thumbnail to unauthenticated callers. The gate goes FIRST, before the
    # public branch, and uses the same `is_admin` bypass as the permission
    # helper so admins keep the review visibility they need to release a file.
    # 404 (not 403) keeps a taken-down file indistinguishable from a missing one.
    if is_hidden_for(db_file, is_admin=bool(current_user and current_user.is_admin)):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )

    # Security check: must be public OR user must be authenticated and own file (or be admin)
    is_public = getattr(db_file, "is_public", False)
    if not is_public:
        if not current_user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required. Use presigned thumbnail_url from file listing.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        from app.api.deps_context import resolve_org_context
        from app.api.deps_context import resolve_request_bypass

        # Resolve the caller's tenant scope (None = personal) WITHOUT editing
        # get_optional_current_user (owned by step 1.5), then the bypass through the SAME
        # resolver get_current_context uses, so a support-access grant header is honoured
        # (or refused) by exactly the same rules here.
        org_id, org_role = resolve_org_context(request, db, current_user)
        org_id, _, bypass = resolve_request_bypass(request, db, current_user, org_id, org_role)
        platform_allowed = bypass.allows(
            org_id=db_file.organization_id,
            owner_id=db_file.user_id,
            need="read",
            resource_type="media_file",
            resource_uuid=str(db_file.uuid),
        )
        if not platform_allowed and db_file.user_id != current_user.id:
            from app.services.permission_service import PermissionService

            # Route the share resolution through the org-aware permission path
            # (default-deny).
            perm = PermissionService.get_file_permission(
                db, db_file.id, current_user.id, organization_id=org_id
            )
            if not perm:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Access denied to this file",
                )
        elif not platform_allowed and getattr(db_file, "organization_id", None) != org_id:
            # Direct owner: still enforce the tenant gate so a personal-scope
            # request can't fetch an org-stamped file's thumbnail (and vice versa).
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this file",
            )

    return get_thumbnail_streaming_response(db_file)
