import asyncio
import logging
import platform
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import Any

import psutil
from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy import and_
from sqlalchemy import false as sa_false
from sqlalchemy import func
from sqlalchemy import or_
from sqlalchemy.orm import Session
from sqlalchemy.orm import selectinload

from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.lockout import unlock_account as lockout_unlock_account
from app.auth.password_history import add_password_to_history
from app.auth.password_history import check_password_against_history
from app.auth.password_policy import password_expiry_cutoff
from app.auth.rate_limit import get_auth_rate_limit
from app.auth.rate_limit import limiter
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.auth.roles import VALID_ROLES
from app.auth.roles import role_implies_superuser
from app.core.config import settings
from app.core.constants import CeleryQueues
from app.core.security import get_password_hash
from app.core.version import APP_VERSION
from app.db.base import get_db
from app.models.media import Analytics
from app.models.media import Collection
from app.models.media import CollectionMember
from app.models.media import Comment
from app.models.media import FileStatus
from app.models.media import FileTag
from app.models.media import MediaFile
from app.models.media import Speaker
from app.models.media import SpeakerCollection
from app.models.media import SpeakerCollectionMember
from app.models.media import SpeakerProfile
from app.models.media import Tag
from app.models.media import Task as TaskModel
from app.models.media import TranscriptSegment
from app.models.prompt import SummaryPrompt
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.user_mfa import UserMFA
from app.schemas.admin import CacheClearResponse
from app.schemas.admin import CacheConfig
from app.schemas.admin import CacheConfigUpdate
from app.schemas.admin import GarbageCleanupConfig
from app.schemas.admin import GarbageCleanupConfigUpdate
from app.schemas.admin import LinkExternalIdentityRequest
from app.schemas.admin import LinkExternalIdentityResponse
from app.schemas.admin import MediaSource
from app.schemas.admin import MediaSourceCreate
from app.schemas.admin import MediaSourcesList
from app.schemas.admin import MediaSourceUpdate
from app.schemas.admin import QuarantineActionResponse
from app.schemas.admin import QuarantinedFile
from app.schemas.admin import QuarantinedFilesList
from app.schemas.admin import QuarantineRequest
from app.schemas.admin import ReleaseRequest
from app.schemas.admin import RetentionConfig
from app.schemas.admin import RetentionConfigUpdate
from app.schemas.admin import RetentionPreviewFile
from app.schemas.admin import RetentionPreviewResponse
from app.schemas.admin import RetentionRunResponse
from app.schemas.admin import RetryConfig
from app.schemas.admin import RetryConfigUpdate
from app.schemas.user import AdminPasswordResetRequest
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate
from app.services import system_settings_service
from app.services.account_security_service import DeletedUser
from app.services.account_security_service import assert_password_auth_possible
from app.services.account_security_service import audit_password_change
from app.services.account_security_service import audit_role_change
from app.services.account_security_service import audit_user_deleted
from app.services.account_security_service import enforce_password_policy
from app.services.account_security_service import revoke_all_sessions
from app.utils.stats_helpers import format_bytes

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


# System statistics utility functions
def get_system_uptime():
    """Get system uptime in a readable format"""
    try:
        # Get boot time and calculate uptime
        boot_time = datetime.fromtimestamp(psutil.boot_time(), tz=UTC)
        uptime = datetime.now(UTC) - boot_time

        # Format as days, hours, minutes, seconds
        days, remainder = divmod(uptime.total_seconds(), 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, seconds = divmod(remainder, 60)

        if days > 0:
            return f"{int(days)}d {int(hours)}h {int(minutes)}m {int(seconds)}s"
        else:
            return f"{int(hours)}h {int(minutes)}m {int(seconds)}s"
    except Exception as e:
        logger.exception(f"Error getting system uptime: {e}")
        return "Unknown"


def get_memory_usage():
    """Get system memory usage"""
    try:
        # Get virtual memory statistics
        memory = psutil.virtual_memory()

        # Return a dictionary with detailed information
        return {
            "total": format_bytes(memory.total),
            "available": format_bytes(memory.available),
            "used": format_bytes(memory.used),
            "percent": f"{memory.percent}%",
        }
    except Exception as e:
        logger.exception(f"Error getting memory usage: {e}")
        return {
            "total": "Unknown",
            "available": "Unknown",
            "used": "Unknown",
            "percent": "Unknown",
        }


def get_cpu_usage():
    """Get CPU usage information.

    Uses interval=None (non-blocking) which returns CPU usage since the last call.
    The first call after import returns 0.0; subsequent calls return meaningful values.
    This avoids blocking the async event loop for 1+ second.
    """
    try:
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        cpu_percent = sum(per_cpu) / len(per_cpu) if per_cpu else 0.0
        cpu_count = psutil.cpu_count(logical=True)
        physical_cores = psutil.cpu_count(logical=False) or 1

        return {
            "total_percent": f"{cpu_percent:.1f}%",
            "per_cpu": [f"{p:.1f}%" for p in per_cpu],
            "logical_cores": cpu_count,
            "physical_cores": physical_cores,
        }
    except Exception as e:
        logger.exception(f"Error getting CPU usage: {e}")
        return {
            "total_percent": "Unknown",
            "per_cpu": [],
            "logical_cores": 0,
            "physical_cores": 0,
        }


# Prime the CPU percent counter on module load so the first API call returns real data.
# This call is instant (interval=None) and runs once at import time.
psutil.cpu_percent(interval=None)


def get_disk_usage():
    """Get disk usage information"""
    try:
        # Get disk usage for the root directory
        disk = psutil.disk_usage("/")

        return {
            "total": format_bytes(disk.total),
            "used": format_bytes(disk.used),
            "free": format_bytes(disk.free),
            "percent": f"{disk.percent}%",
        }
    except Exception as e:
        logger.exception(f"Error getting disk usage: {e}")
        return {
            "total": "Unknown",
            "used": "Unknown",
            "free": "Unknown",
            "percent": "Unknown",
        }


def _query_gpu_via_smi() -> dict | None:
    """Run nvidia-smi directly to get GPU stats. Returns None if unavailable."""
    import os
    import subprocess

    try:
        device_id = int(os.environ.get("GPU_DEVICE_ID", "0"))
        result = subprocess.run(  # noqa: S603  # nosec B603
            [  # noqa: S607  # nosec B607
                "nvidia-smi",
                "--query-gpu=name,memory.used,memory.total,memory.free,utilization.gpu,temperature.gpu",
                "--format=csv,noheader,nounits",
                f"--id={device_id}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        parts = result.stdout.strip().split(", ")
        used_mib, total_mib, free_mib = float(parts[1]), float(parts[2]), float(parts[3])
        used_bytes = used_mib * 1024 * 1024
        total_bytes = total_mib * 1024 * 1024
        free_bytes = free_mib * 1024 * 1024
        pct = (used_bytes / total_bytes * 100) if total_bytes > 0 else 0
        util = int(parts[4]) if len(parts) > 4 else None
        temp = int(parts[5]) if len(parts) > 5 else None
        return {
            "available": True,
            "name": parts[0],
            "memory_total": format_bytes(total_bytes),
            "memory_used": format_bytes(used_bytes),
            "memory_free": format_bytes(free_bytes),
            "memory_percent": f"{pct:.1f}%",
            "utilization_percent": f"{util}%" if util is not None else "N/A",
            "temperature_celsius": temp,
        }
    except (FileNotFoundError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def get_gpu_usage():
    """Get GPU usage array from Redis cache, falling back to direct nvidia-smi query.

    Returns a list of GPU stat dicts — one per active GPU device.  In normal
    mode this is a single-element list; in --gpu-scale dual-worker mode it
    contains one entry per active GPU worker.
    """
    try:
        import json

        from app.core.celery import celery_app

        redis_client = celery_app.backend.client
        gpu_stats_json = redis_client.get("gpu_stats")

        if gpu_stats_json:
            data = json.loads(gpu_stats_json)
            # Migrate legacy single-dict format to list
            return data if isinstance(data, list) else [data]

        # Redis empty — try nvidia-smi directly (works if backend host has NVIDIA drivers)
        direct_stats = _query_gpu_via_smi()
        if direct_stats:
            result = [direct_stats]
            redis_client.setex("gpu_stats", 600, json.dumps(result))
            return result

        # nvidia-smi not available — dispatch to cpu worker (debounced)
        lock_acquired = redis_client.set("gpu_stats_pending", "1", nx=True, ex=30)
        if lock_acquired:
            celery_app.send_task("system.update_gpu_stats", queue=CeleryQueues.CPU)
            logger.info("Dispatched on-demand GPU stats collection")

        return [
            {
                "available": False,
                "loading": True,
                "name": "Loading GPU stats...",
                "memory_total": "N/A",
                "memory_used": "N/A",
                "memory_free": "N/A",
                "memory_percent": "N/A",
            }
        ]
    except Exception as e:
        logger.exception(f"Error getting GPU usage from Redis: {e}")
        return [
            {
                "available": False,
                "name": "Error",
                "memory_total": "Unknown",
                "memory_used": "Unknown",
                "memory_free": "Unknown",
                "memory_percent": "Unknown",
            }
        ]


def _delete_user_speakers(db: Session, user_id: int) -> None:
    """Delete all speakers for a user, including OpenSearch embeddings.

    Collects speaker UUIDs before bulk SQL delete so OpenSearch can be cleaned
    even though the bulk operation bypasses ORM instance-level callbacks.

    **The segment detach is not optional.** ``transcript_segment.speaker_id`` is a
    plain FK with ``ON DELETE NO ACTION``, and this runs *before*
    ``_delete_user_media_files`` removes the segments — so deleting a speaker any
    segment still points at raises ``ForeignKeyViolation`` on
    ``transcript_segment_speaker_id_fkey``. Every diarized segment carries a
    ``speaker_id``, which made this the first thing to fail for any account with a
    transcribed file: the endpoint's blanket ``except Exception`` reported it as
    ``500 "User deletion failed"`` and named nothing. It went unnoticed because
    both deletion tests deleted a fixture account that owned no files at all.

    The column is nullable, so detaching is a legitimate transient state; the rows
    are deleted moments later by ``_delete_user_media_files``.

    Args:
        db: Database session
        user_id: ID of the user whose speakers to delete
    """
    speaker_rows = db.query(Speaker.id, Speaker.uuid).filter(Speaker.user_id == user_id).all()
    if not speaker_rows:
        return

    speaker_ids = [row[0] for row in speaker_rows]
    speaker_uuids = [str(row[1]) for row in speaker_rows]
    logger.info(f"Deleting {len(speaker_uuids)} speakers for user {user_id}")

    detached = (
        db.query(TranscriptSegment)
        .filter(TranscriptSegment.speaker_id.in_(speaker_ids))
        .update({TranscriptSegment.speaker_id: None}, synchronize_session=False)
    )
    if detached:
        logger.info(f"Detached {detached} transcript segments from these speakers")

    db.query(Speaker).filter(Speaker.user_id == user_id).delete(synchronize_session=False)
    logger.info("Speakers deleted from DB")

    # Clean OpenSearch embeddings after bulk DB delete (non-fatal)
    try:
        from app.services.opensearch_service import remove_speaker_embedding

        for uuid in speaker_uuids:
            remove_speaker_embedding(uuid)
        logger.info(f"Removed {len(speaker_uuids)} speaker embeddings from OpenSearch")
    except Exception as e:
        logger.warning(f"OpenSearch speaker cleanup failed during user {user_id} deletion: {e}")


def _delete_user_owned_records(db: Session, user_id: int) -> None:
    """Delete all user-owned records that are not covered by DB-level CASCADE.

    Cleans up: SpeakerProfile, SpeakerCollection (+ members), Collection (+ members),
    Comment, Task, SummaryPrompt, and Tag records. Must be called BEFORE deleting
    MediaFile rows since some of these tables have FK references to media_file.

    **This list is hand-maintained and its twin is
    ``services/gdpr_erasure_service._delete_owner_scoped_rows``.** Nothing in the
    application compares the two, and neither is derived from the schema, so a new
    table with a plain ``user_id`` FK breaks account deletion with no code change
    here. ``tests/unit/test_user_deletion_fk_coverage.py`` derives every NO-ACTION
    foreign key into ``user`` / ``media_file`` from the live schema and fails when
    one is not accounted for by *both* paths — add the branch here, or record the
    FK there with a reason.

    Rows recording an action this user took on **somebody else's** row are
    deliberately absent: ``auth_config.created_by``/``updated_by``,
    ``auth_config_audit.changed_by``, ``media_file.quarantined_by`` and
    ``summary_prompt.shared_by`` are not owner-scoped, so no query keyed on
    ``user_id`` can find them. They are ``ON DELETE SET NULL`` at the database
    level instead (``v387``), which is enforced for every deletion path including
    ones that do not exist yet.
    """
    # Speaker collections and their members
    sc_ids = [
        sc.id
        for sc in db.query(SpeakerCollection.id).filter(SpeakerCollection.user_id == user_id).all()
    ]
    if sc_ids:
        db.query(SpeakerCollectionMember).filter(
            SpeakerCollectionMember.collection_id.in_(sc_ids)
        ).delete(synchronize_session=False)
        db.query(SpeakerCollection).filter(SpeakerCollection.user_id == user_id).delete(
            synchronize_session=False
        )
        logger.info(f"Deleted {len(sc_ids)} speaker collections for user {user_id}")

    # Collections and their members
    col_ids = [c.id for c in db.query(Collection.id).filter(Collection.user_id == user_id).all()]
    if col_ids:
        db.query(CollectionMember).filter(CollectionMember.collection_id.in_(col_ids)).delete(
            synchronize_session=False
        )
        db.query(Collection).filter(Collection.user_id == user_id).delete(synchronize_session=False)
        logger.info(f"Deleted {len(col_ids)} collections for user {user_id}")

    # Speaker profiles — collect UUIDs first so OpenSearch can be cleaned
    profile_rows = db.query(SpeakerProfile.uuid).filter(SpeakerProfile.user_id == user_id).all()
    profile_uuids = [str(row[0]) for row in profile_rows]
    profiles_deleted = (
        db.query(SpeakerProfile)
        .filter(SpeakerProfile.user_id == user_id)
        .delete(synchronize_session=False)
    )
    if profiles_deleted:
        logger.info(f"Deleted {profiles_deleted} speaker profiles for user {user_id}")
        try:
            from app.services.opensearch_service import remove_profile_embedding

            for puuid in profile_uuids:
                remove_profile_embedding(puuid)
            logger.info(f"Removed {len(profile_uuids)} profile embeddings from OpenSearch")
        except Exception as e:
            logger.warning(f"OpenSearch profile cleanup failed during user {user_id} deletion: {e}")

    # Comments
    comments_deleted = (
        db.query(Comment).filter(Comment.user_id == user_id).delete(synchronize_session=False)
    )
    if comments_deleted:
        logger.info(f"Deleted {comments_deleted} comments for user {user_id}")

    # Background tasks
    tasks_deleted = (
        db.query(TaskModel).filter(TaskModel.user_id == user_id).delete(synchronize_session=False)
    )
    if tasks_deleted:
        logger.info(f"Deleted {tasks_deleted} task records for user {user_id}")

    # Summary prompts
    prompts_deleted = (
        db.query(SummaryPrompt)
        .filter(SummaryPrompt.user_id == user_id)
        .delete(synchronize_session=False)
    )
    if prompts_deleted:
        logger.info(f"Deleted {prompts_deleted} summary prompts for user {user_id}")

    # Tags owned by the user (v374). tag.user_id is a plain FK, so the rows must
    # go before the user row or the delete fails. The file_tag pass is belt and
    # braces rather than load-bearing: file_tag.tag_id IS ON DELETE CASCADE
    # (verified against the live schema Aug 2026), so the database would sweep
    # those rows anyway — including the ones hanging off ANOTHER user's file,
    # which is the case worth naming. System tags (user_id IS NULL) are shared
    # vocabulary and are never touched.
    tag_ids = [t.id for t in db.query(Tag.id).filter(Tag.user_id == user_id).all()]
    if tag_ids:
        db.query(FileTag).filter(FileTag.tag_id.in_(tag_ids)).delete(synchronize_session=False)
        db.query(Tag).filter(Tag.user_id == user_id).delete(synchronize_session=False)
        logger.info(f"Deleted {len(tag_ids)} tags for user {user_id}")


def _delete_user_media_files(db: Session, user_id: int) -> None:
    """Delete all media files and related records for a user.

    **Why the children are deleted by hand here.** ``MediaFile`` declares
    ``cascade="all, delete-orphan"`` on eight relationships, but four of the
    underlying foreign keys — ``transcript_segment``, ``comment``, ``task`` and
    ``analytics`` on ``media_file_id`` — are ``ON DELETE NO ACTION`` in the
    database. The ORM cascade only fires for an *instance* delete
    (``db.delete(file)``, which is what ``file_cleanup_service.purge_media_file``
    and therefore the GDPR path use). This function ends in a **bulk**
    ``query(MediaFile).delete()``, which emits one ``DELETE`` statement and never
    loads the instances, so the ORM cascade is bypassed entirely and the database
    is the only thing left enforcing anything — and for those four it enforces
    *refusal*. Every hand-delete below is therefore load-bearing: remove one and
    this function raises ``ForeignKeyViolation``, which the endpoint's blanket
    ``except Exception`` reports as ``500 "User deletion failed"``.

    ``file_tag``, ``collection_member``, ``speaker`` and ``topic_suggestion`` are the
    other four children; those FKs *are* ``ON DELETE CASCADE``, so the database
    sweeps them whichever delete shape is used. ``file_tag`` is still deleted
    explicitly because ``_delete_user_owned_records`` has already detached the rows
    pointing at this user's tags and the cost of the second pass is nil.

    ``comment`` and ``task`` are **not** covered by the owner-scoped sweep in
    ``_delete_user_owned_records``: a non-owner can comment on a file shared with
    them (``endpoints/comments.create_comment_for_file_nested`` requires viewer+,
    "commenting is collaborative"), so another account's comment on this user's
    file would block the bulk delete. Scope both by ``media_file_id``, not by
    ``user_id``.

    Args:
        db: Database session
        user_id: ID of the user whose media files to delete
    """
    media_ids = [
        row[0] for row in db.query(MediaFile.id).filter(MediaFile.user_id == user_id).all()
    ]
    media_count = len(media_ids)

    if media_count > 0:
        logger.info(f"Found {media_count} media files for user {user_id}")

        # Delete transcript segments for these media files
        segments_deleted = (
            db.query(TranscriptSegment)
            .filter(TranscriptSegment.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if segments_deleted:
            logger.info(f"Deleted {segments_deleted} transcript segments")

        # Delete file_tag records
        file_tags_deleted = (
            db.query(FileTag)
            .filter(FileTag.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if file_tags_deleted:
            logger.info(f"Deleted {file_tags_deleted} file tags")

        # Delete analytics records
        analytics_deleted = (
            db.query(Analytics)
            .filter(Analytics.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if analytics_deleted:
            logger.info(f"Deleted {analytics_deleted} analytics records")

        # Comments on these files, whoever wrote them. `comment.media_file_id` is
        # NO ACTION and commenting is collaborative, so a viewer's comment on a
        # shared file is another account's row that still has to go with the file.
        comments_deleted = (
            db.query(Comment)
            .filter(Comment.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if comments_deleted:
            logger.info(f"Deleted {comments_deleted} comments on these media files")

        # Tasks against these files, whoever owns them. `task.media_file_id` is
        # NO ACTION too; scoping only by `task.user_id` (as the owner-scoped sweep
        # does) leaves any task another account queued against this file behind.
        tasks_deleted = (
            db.query(TaskModel)
            .filter(TaskModel.media_file_id.in_(media_ids))
            .delete(synchronize_session=False)
        )
        if tasks_deleted:
            logger.info(f"Deleted {tasks_deleted} tasks against these media files")

        # Now delete the media files
        db.query(MediaFile).filter(MediaFile.user_id == user_id).delete(synchronize_session=False)
        logger.info(f"Deleted {media_count} media files for user {user_id}")


def _validate_user_deletion(user: User, current_user: User) -> None:
    """Validate that a user can be deleted.

    Args:
        user: User to be deleted
        current_user: User performing the deletion

    Raises:
        HTTPException: If user cannot be deleted
    """
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete your own account",
        )

    # role is the source of truth: only a super_admin may delete a super_admin.
    if user.role == ROLE_SUPER_ADMIN and current_user.role != ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete a super_admin account",
        )


@router.get("/stats", response_model=dict[str, Any])
async def get_admin_stats(
    db: Session = Depends(get_db), current_user: User = Depends(get_current_admin_user)
):
    """Report application and host statistics for the admin dashboard.

    Two fields have a fixed contract worth stating, because both were wrong:

    - ``system.version`` is ``app.core.version.APP_VERSION`` — the build identity
      every other surface reports (``/health``, ``/api/system/stats``, the About
      dialog's staleness check). It was a hardcoded ``"1.0.0"`` that never moved
      through any release.
    - ``system.gpu`` is **always a list** of per-device dicts, one per active GPU
      (``--gpu-scale`` runs more than one). The stats-collection fallback below
      used to substitute a bare dict, so the key's type depended on whether
      psutil raised.

    Returns:
        Nested user/file/transcript/speaker/model/system/task statistics.

    Raises:
        HTTPException: 500 if the aggregation fails.
    """
    logger.info(f"Admin stats requested by user {current_user.email}")

    logger.info("Admin stats requested")

    try:
        # System statistics — offload sync psutil calls to a thread to avoid
        # blocking the async event loop.
        def _collect_system_stats():
            return {
                "cpu": get_cpu_usage(),
                "memory": get_memory_usage(),
                "disk": get_disk_usage(),
                "gpu": get_gpu_usage(),
                "uptime": get_system_uptime(),
            }

        try:
            system_stats = await asyncio.to_thread(_collect_system_stats)
        except Exception as e:
            logger.exception(f"Error getting system stats: {e}")
            system_stats = {
                "cpu": {
                    "total_percent": "Unknown",
                    "per_cpu": [],
                    "logical_cores": 0,
                    "physical_cores": 0,
                },
                # A LIST, like every `get_gpu_usage()` return: one entry per GPU
                # (`--gpu-scale` runs several). A bare dict here made the key's
                # type depend on whether psutil happened to raise, so a client
                # indexing `gpu[0]` broke only in the failure path.
                "gpu": [
                    {
                        "available": False,
                        "name": "Error",
                        "memory_total": "Unknown",
                        "memory_used": "Unknown",
                        "memory_free": "Unknown",
                        "memory_percent": "Unknown",
                    }
                ],
                "memory": {
                    "total": "Unknown",
                    "available": "Unknown",
                    "used": "Unknown",
                    "percent": "Unknown",
                },
                "disk": {
                    "total": "Unknown",
                    "used": "Unknown",
                    "free": "Unknown",
                    "percent": "Unknown",
                },
                "uptime": "Unknown",
            }

        # Consolidated database statistics (3 aggregate queries instead of 15+)
        from app.utils.stats_helpers import get_file_stats
        from app.utils.stats_helpers import get_recent_tasks
        from app.utils.stats_helpers import get_task_stats
        from app.utils.stats_helpers import get_user_stats

        user_stats = get_user_stats(db, include_breakdown=True)
        file_stats = get_file_stats(db, include_status_breakdown=True)
        task_stats = get_task_stats(db)
        recent = get_recent_tasks(db, limit=10)

        # Get AI model configuration
        from app.core.config import settings

        models_info = {
            "whisper": {
                "name": settings.WHISPER_MODEL,
                "description": f"Whisper {settings.WHISPER_MODEL}",
                "purpose": "Speech Recognition & Transcription",
            },
            "diarization": {
                "name": settings.PYANNOTE_MODEL,
                "description": "PyAnnote Speaker Diarization 3.1",
                "purpose": "Speaker Identification & Segmentation",
            },
        }

        total_files = file_stats["total"]
        total_speakers = file_stats["speakers"]
        total_segments = file_stats["segments"]

        # Construct the response
        stats = {
            "users": user_stats,
            "files": {
                "total": total_files,
                "new": file_stats["new"],
                "total_duration": file_stats["total_duration"],
                "segments": total_segments,
                "by_status": file_stats.get("by_status", {}),
                "total_size": file_stats["total_size"],
            },
            "transcripts": {"total_segments": total_segments},
            "speakers": {
                "total": total_speakers,
                "avg_per_file": round(total_speakers / total_files, 2) if total_files > 0 else 0,
            },
            "models": models_info,
            "system": {
                "version": APP_VERSION,
                "uptime": system_stats["uptime"],
                "memory": system_stats["memory"],
                "cpu": system_stats["cpu"],
                "disk": system_stats["disk"],
                "gpu": system_stats["gpu"],
                "platform": platform.platform(),
                "python_version": platform.python_version(),
            },
            "tasks": {**task_stats, "recent": recent},
        }

        return stats
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error getting admin stats: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.get("/users", response_model=list[UserSchema])
def get_admin_users(
    limit: int = Query(200, ge=1, le=1000, description="Max users to return"),
    offset: int = Query(0, ge=0, description="Number of users to skip"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Get users for admin panel with pagination.

    Defaults to 200 users which covers most deployments without breaking
    existing frontend code. Use limit/offset for larger user bases.
    """
    try:
        users = (
            db.query(User).order_by(func.lower(User.full_name)).offset(offset).limit(limit).all()
        )
        return users
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error getting admin users: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.post("/users", response_model=UserSchema)
def create_admin_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """
    Create a new user (admin only)
    """
    logger.info(f"Admin creating new user with email: {user_data.email}")

    # Privilege gate: only a super_admin may mint admin/super_admin accounts.
    # Otherwise a regular admin could escalate by creating a super_admin and
    # logging in as it. create_user() derives is_superuser from role.
    requested_role = user_data.role or ROLE_USER
    if requested_role in ELEVATED_ROLES and current_user.role != ROLE_SUPER_ADMIN:
        logger.warning(
            f"Admin {current_user.email} (role={current_user.role}) attempted to "
            f"create a '{requested_role}' user — denied (super_admin required)"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a super_admin can create admin or super_admin accounts",
        )

    try:
        from app.api.endpoints.users import create_user as create_user_func

        # Call the user creation function from the users endpoint
        return create_user_func(user_data=user_data, db=db)
    except HTTPException as he:
        logger.error(f"HTTP error creating user: {he.detail}")
        raise he
    except Exception as e:
        logger.error("Error creating user: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.delete("/users/{user_uuid}", response_model=dict[str, str])
def delete_admin_user(
    user_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Delete a user and all their data (admin only).

    Args:
        user_uuid: UUID of the user to delete
        db: Database session
        current_user: Current admin user

    Returns:
        Success message

    Raises:
        HTTPException: If user not found or deletion not allowed
    """
    from app.utils.uuid_helpers import get_user_by_uuid

    logger.info(f"Admin deleting user with UUID: {user_uuid}")

    try:
        user = get_user_by_uuid(db, user_uuid)
        user_id = user.id  # Get internal ID for cascade operations

        # Validate deletion is allowed
        _validate_user_deletion(user, current_user)

        # Defence in depth, and UNREACHABLE as the guards above stand today — kept
        # deliberately, not as a live fix (issue #431). The composition already makes
        # it impossible to delete the last super_admin: deleting one requires BEING
        # one (the 403 above), and you cannot target yourself (the 400 above), so the
        # caller always remains. This fires only if a future change relaxes either of
        # those, which is exactly when nobody would be looking. It lives here rather
        # than in _validate_user_deletion because that helper takes no Session.
        from app.api.endpoints.users import _assert_not_last_super_admin

        _assert_not_last_super_admin(db, user, ROLE_USER)

        # Capture what the audit record needs before the row is gone: after the commit
        # the ORM object is expired, so reading user.email would re-query a deleted row.
        deleted_snapshot = DeletedUser.of(user)
        client_ip, user_agent = _get_client_info(request)

        # Delete all user data atomically using a savepoint
        savepoint = db.begin_nested()
        try:
            _delete_user_owned_records(db, int(user_id))
            _delete_user_speakers(db, int(user_id))
            _delete_user_media_files(db, int(user_id))
            logger.info(f"Deleting user with ID {user_id} and email {user.email}")
            db.delete(user)
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise
        db.commit()

        # This endpoint destroys a user, their files and their transcripts irreversibly
        # and recorded NOTHING — while its twin, DELETE /api/users/{uuid}, performs the
        # identical deletion through these same three helpers and does audit it. Emitted
        # after the commit, matching that twin, so a failed delete leaves no record of a
        # deletion that did not happen. FedRAMP AU-2/AU-12, GDPR Art. 30(2)(d).
        audit_user_deleted(deleted_snapshot, current_user, client_ip, user_agent)

        logger.info(f"User deletion completed successfully: {user_id}")
        return {"message": "User deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"User deletion failed, all changes rolled back: {e}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User deletion failed",
        ) from e


@router.get("/settings/retry-config", response_model=RetryConfig)
def get_retry_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetryConfig:
    """
    Get retry configuration settings (admin only).

    Returns the current retry configuration including:
    - max_retries: Maximum retry attempts (0 = unlimited)
    - retry_limit_enabled: Whether limits are enforced
    """
    logger.info(f"Retry config requested by admin {current_user.email}")
    config = system_settings_service.get_retry_config(db)
    return RetryConfig(**config)


@router.put("/settings/retry-config", response_model=RetryConfig)
def update_retry_configuration(
    config: RetryConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetryConfig:
    """
    Update retry configuration settings (admin only).

    Args:
        config: New configuration values (only provided values are updated)

    Returns:
        Updated retry configuration
    """
    logger.info(f"Retry config update by admin {current_user.email}: {config}")

    updated = system_settings_service.update_retry_config(
        db,
        max_retries=config.max_retries,
        retry_limit_enabled=config.retry_limit_enabled,
    )

    return RetryConfig(**updated)


@router.get("/settings/garbage-cleanup", response_model=GarbageCleanupConfig)
def get_garbage_cleanup_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> GarbageCleanupConfig:
    """
    Get garbage cleanup configuration settings (admin only).

    Returns the current garbage cleanup configuration including:
    - garbage_cleanup_enabled: Whether garbage cleanup is active
    - max_word_length: Maximum word length threshold (words longer are replaced)
    """
    logger.info(f"Garbage cleanup config requested by admin {current_user.email}")
    config = system_settings_service.get_garbage_cleanup_config(db)
    return GarbageCleanupConfig(**config)


@router.put("/settings/garbage-cleanup", response_model=GarbageCleanupConfig)
def update_garbage_cleanup_configuration(
    config: GarbageCleanupConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> GarbageCleanupConfig:
    """
    Update garbage cleanup configuration settings (admin only).

    Args:
        config: New configuration values (only provided values are updated)

    Returns:
        Updated garbage cleanup configuration
    """
    logger.info(f"Garbage cleanup config update by admin {current_user.email}: {config}")

    updated = system_settings_service.update_garbage_cleanup_config(
        db,
        garbage_cleanup_enabled=config.garbage_cleanup_enabled,
        max_word_length=config.max_word_length,
    )

    return GarbageCleanupConfig(**updated)


# ============== File Retention Settings ==============


def _get_retention_eligible_files(db: Session, retention_days: int, delete_error_files: bool):
    """Query files eligible for deletion under the given retention parameters."""
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    eligible_statuses = [FileStatus.COMPLETED]
    if delete_error_files:
        eligible_statuses.append(FileStatus.ERROR)

    return (
        db.query(MediaFile)
        .options(selectinload(MediaFile.speakers))
        .filter(
            or_(
                and_(MediaFile.completed_at.isnot(None), MediaFile.completed_at < cutoff),
                and_(MediaFile.completed_at.is_(None), MediaFile.upload_time < cutoff),
            ),
            MediaFile.status.in_([s.value for s in eligible_statuses]),
        )
        .all()
    )


@router.get("/settings/retention-config", response_model=RetentionConfig)
def get_retention_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionConfig:
    """
    Get file retention configuration settings (admin only).

    Returns the current retention configuration including:
    - retention_enabled: Whether automatic deletion is active
    - retention_days: Files older than this are deleted
    - delete_error_files: Whether error-status files are also deleted
    - run_time: HH:MM daily schedule time
    - timezone: IANA timezone for the schedule
    - last_run: ISO timestamp of last run
    - last_run_deleted: Files deleted in last run
    """
    logger.info(f"Retention config requested by admin {current_user.email}")
    config = system_settings_service.get_retention_config(db)
    return RetentionConfig(**config)


@router.put("/settings/retention-config", response_model=RetentionConfig)
def update_retention_configuration(
    config: RetentionConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionConfig:
    """
    Update file retention configuration settings (admin only).

    Args:
        config: New configuration values (only provided values are updated)

    Returns:
        Updated retention configuration
    """
    logger.info(f"Retention config update by admin {current_user.email}: {config}")

    updated = system_settings_service.update_retention_config(
        db,
        retention_enabled=config.retention_enabled,
        retention_days=config.retention_days,
        delete_error_files=config.delete_error_files,
        run_time=config.run_time,
        timezone=config.timezone,
    )

    return RetentionConfig(**updated)


@router.get("/settings/cache-config", response_model=CacheConfig)
def get_cache_configuration(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> CacheConfig:
    """Get derived-cache retention + current usage (admin only).

    Derived assets (subtitle-embedded videos + extracted audio) are regenerable
    duplicates of the originals. They auto-expire via a MinIO lifecycle rule whose
    retention is set here; this also reports how much disk the cache currently uses.
    """
    from app.services import cache_management_service

    stats = cache_management_service.get_cache_stats()
    return CacheConfig(
        retention_days=cache_management_service.resolve_retention_days(db),
        **stats,
    )


@router.put("/settings/cache-config", response_model=CacheConfig)
def update_cache_configuration(
    config: CacheConfigUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> CacheConfig:
    """Update the derived-cache retention window (admin only).

    Applied live by re-setting the MinIO lifecycle rule — no redeploy needed.
    """
    from app.services import cache_management_service

    logger.info(
        f"Cache retention update by admin {current_user.email}: {config.retention_days} day(s)"
    )
    days = cache_management_service.set_retention_days(db, config.retention_days)
    stats = cache_management_service.get_cache_stats()
    return CacheConfig(retention_days=days, **stats)


@router.post("/settings/cache-config/clear", response_model=CacheClearResponse)
def clear_cache_now(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> CacheClearResponse:
    """Immediately purge all derived cache assets (admin only).

    Safe: derived assets are regenerated on the next download. Originals are untouched.
    """
    from app.services import cache_management_service

    logger.info(f"Manual derived-cache clear by admin {current_user.email}")
    deleted = cache_management_service.clear_derived_cache()
    return CacheClearResponse(deleted=deleted)


@router.get("/settings/retention-config/preview", response_model=RetentionPreviewResponse)
def preview_retention_deletion(
    retention_days: int = Query(..., ge=1, le=3650, description="Retention window in days"),
    delete_error_files: bool = Query(False, description="Include error-status files"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionPreviewResponse:
    """
    Dry-run preview of files that would be deleted (admin only).

    Returns count, total size, and a sample list of files that would be deleted
    with the given retention parameters. No files are modified.
    """
    logger.info(
        f"Retention preview requested by admin {current_user.email}: "
        f"{retention_days} days, delete_error_files={delete_error_files}"
    )

    files = _get_retention_eligible_files(db, retention_days, delete_error_files)

    # Build user lookup for owner names
    user_ids = list({f.user_id for f in files})
    users = db.query(User).filter(User.id.in_(user_ids)).all()
    user_map = {u.id: u.email for u in users}

    now_utc = datetime.now(UTC)
    total_size = sum(f.file_size or 0 for f in files)

    # Build preview list (cap at 100 rows for response size)
    preview_files = []
    for f in files[:100]:
        if f.completed_at:
            ref_dt = (
                f.completed_at.replace(tzinfo=UTC)
                if f.completed_at.tzinfo is None
                else f.completed_at
            )
        else:
            ref_dt = (
                f.upload_time.replace(tzinfo=UTC) if f.upload_time.tzinfo is None else f.upload_time
            )
        age_days = (now_utc - ref_dt).days
        preview_files.append(
            RetentionPreviewFile(
                uuid=str(f.uuid),
                title=f.filename or str(f.uuid),
                owner_email=user_map.get(f.user_id, "unknown"),
                completed_at=f.completed_at.isoformat() if f.completed_at else None,
                age_days=age_days,
                size_bytes=f.file_size or 0,
                status=f.status if isinstance(f.status, str) else f.status.value,
            )
        )

    return RetentionPreviewResponse(
        file_count=len(files),
        total_size_bytes=total_size,
        files=preview_files,
    )


@router.post("/settings/retention-config/run", response_model=RetentionRunResponse)
def trigger_retention_run(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionRunResponse:
    """
    Manually trigger a retention cleanup run (admin only).

    Dispatches the cleanup task immediately with force=True, bypassing
    the time-window check and the retention_enabled flag. Useful for
    on-demand cleanup without enabling the automatic schedule.
    """
    logger.info(f"Manual retention run triggered by admin {current_user.email}")

    from app.core.celery import celery_app

    task = celery_app.send_task(
        "cleanup_expired_files",
        kwargs={"force": True},
        queue=CeleryQueues.UTILITY,
    )

    return RetentionRunResponse(
        task_id=str(task.id),
        status="queued",
        message="Retention cleanup task queued successfully.",
    )


@router.get("/settings/retention-config/status", response_model=RetentionConfig)
def get_retention_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> RetentionConfig:
    """
    Get retention configuration with last-run status (admin only).

    Same as GET /retention-config but intended for status polling after
    a manual run to refresh last_run and last_run_deleted values.
    """
    config = system_settings_service.get_retention_config(db)
    return RetentionConfig(**config)


# ============== Protected Media Sources ==============


@router.get("/settings/media-sources", response_model=MediaSourcesList)
def get_media_sources(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> MediaSourcesList:
    """Get all configured protected media sources (admin only)."""
    sources = system_settings_service.get_media_sources(db)
    return MediaSourcesList(sources=[MediaSource(**s) for s in sources])


@router.post("/settings/media-sources", response_model=MediaSource)
def add_media_source(
    source: MediaSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> MediaSource:
    """Add a new protected media source (admin only)."""
    import uuid

    sources = system_settings_service.get_media_sources(db)

    # Check for duplicate hostname
    for existing in sources:
        if existing.get("hostname") == source.hostname:
            raise HTTPException(
                status_code=400,
                detail=f"A source with hostname '{source.hostname}' already exists",
            )

    new_source = {
        "id": str(uuid.uuid4())[:8],
        "hostname": source.hostname,
        "provider_type": source.provider_type,
        "username": source.username,
        "password": source.password,
        "verify_ssl": source.verify_ssl,
        "label": source.label,
    }
    sources.append(new_source)
    system_settings_service.set_media_sources(db, sources)

    # Reload providers so the new host is recognized immediately
    _reload_protected_media_providers()

    logger.info(
        "Media source added by admin %s: %s (%s)",
        current_user.email,
        source.hostname,
        source.provider_type,
    )
    return MediaSource(
        id=str(new_source["id"]),
        hostname=source.hostname,
        provider_type=source.provider_type,
        username=source.username,
        password=source.password,
        verify_ssl=source.verify_ssl,
        label=source.label,
    )


@router.put("/settings/media-sources/{source_id}", response_model=MediaSource)
def update_media_source(
    source_id: str,
    update: MediaSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> MediaSource:
    """Update an existing protected media source (admin only)."""
    sources = system_settings_service.get_media_sources(db)

    for i, s in enumerate(sources):
        if s.get("id") == source_id:
            # Check hostname uniqueness if changed
            if update.hostname and update.hostname != s.get("hostname"):
                for other in sources:
                    if other.get("id") != source_id and other.get("hostname") == update.hostname:
                        raise HTTPException(
                            status_code=400,
                            detail=f"A source with hostname '{update.hostname}' already exists",
                        )

            # Apply updates
            update_data = update.model_dump(exclude_unset=True)
            sources[i] = {**s, **update_data}
            system_settings_service.set_media_sources(db, sources)

            _reload_protected_media_providers()

            logger.info(
                "Media source %s updated by admin %s",
                source_id,
                current_user.email,
            )
            return MediaSource(**sources[i])

    raise HTTPException(status_code=404, detail="Media source not found")


@router.delete("/settings/media-sources/{source_id}")
def delete_media_source(
    source_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Delete a protected media source (admin only)."""
    sources = system_settings_service.get_media_sources(db)
    original_len = len(sources)
    sources = [s for s in sources if s.get("id") != source_id]

    if len(sources) == original_len:
        raise HTTPException(status_code=404, detail="Media source not found")

    system_settings_service.set_media_sources(db, sources)

    _reload_protected_media_providers()

    logger.info(
        "Media source %s deleted by admin %s",
        source_id,
        current_user.email,
    )
    return {"success": True}


def _reload_protected_media_providers():
    """Reload the protected media provider registry after config changes."""
    try:
        from app.services import protected_media_providers
        from app.services.protected_media_providers import _load_providers

        protected_media_providers.PROTECTED_MEDIA_PROVIDERS = _load_providers()
    except Exception as e:
        logger.warning("Failed to reload protected media providers: %s", e)


# ============== Super Admin Role Verification ==============
#
# Re-exported for backwards compatibility with the ~20 call sites in this module.
# The definition lives in api/endpoints/auth/dependencies.py alongside
# get_current_user / get_current_admin_user — it was declared here AND in
# auth_config.py, each re-implementing the same check with its own "super_admin"
# string literal instead of roles.ROLE_SUPER_ADMIN. Three copies of an
# authorization rule is three chances for one of them to drift.
get_current_super_admin_user = get_current_active_superuser


# ============== Account Management (FedRAMP AC-2) ==============


@router.post("/users/{user_uuid}/reset-password")
@limiter.limit(get_auth_rate_limit())
def admin_reset_user_password(
    request: Request,
    response: Response,
    user_uuid: str,
    request_body: AdminPasswordResetRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
):
    """Admin-initiated password reset. Requires super_admin role.

    Security: Password is passed in request body (not query parameter) to prevent
    exposure in server logs, browser history, and HTTP referrer headers.

    This path used to skip every control its sibling in ``users.py`` applies: it
    set a hash on ANY account regardless of ``auth_type`` (planting a local
    password on a directory-managed row), bypassed the password policy and the
    reuse history in both directions, and left the target's existing sessions
    alive — so an admin resetting a compromised account did not actually evict
    the attacker.
    """
    client_ip, user_agent = _get_client_info(request)
    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    assert_password_auth_possible(user)
    enforce_password_policy(request_body.new_password, user)

    if not check_password_against_history(db, user.id, request_body.new_password):
        raise HTTPException(
            status_code=400,
            detail="Password has been used recently. Please choose a different password.",
        )

    new_hash = get_password_hash(request_body.new_password)
    user.hashed_password = new_hash  # type: ignore[assignment]
    user.must_change_password = request_body.force_change  # type: ignore[assignment]
    user.password_changed_at = datetime.now(UTC)  # type: ignore[assignment]
    add_password_to_history(db, user.id, new_hash)
    revoke_all_sessions(db, user, reason="admin password reset")
    db.commit()

    audit_password_change(
        user, current_user, client_ip, user_agent, forced=request_body.force_change
    )

    return {"success": True}


@router.post("/users/{user_uuid}/unlock")
def admin_unlock_account(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin unlock of a locked account — the true inverse of ``/lock``.

    Two different things can stop a user signing in, and this endpoint clears
    both:

    * the **failed-login lockout** (progressive, per-identifier, in Redis), and
    * ``is_active = False``, which is what ``/lock`` sets.

    It previously cleared only the first, so an account locked by an admin could
    not be unlocked by the paired endpoint at all — the only way back was
    ``PUT /users/{uuid}`` with ``is_active``. Two endpoints named lock/unlock
    that are not inverses is a trap, not an API.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Use the lockout manager to clear the failed-login lockout
    unlocked = lockout_unlock_account(str(user.email))

    was_disabled = not bool(user.is_active)
    if was_disabled:
        user.is_active = True  # type: ignore[assignment]
        db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_UNLOCK,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS,
        details={
            "target_user": user_uuid,
            "unlocked_by": "admin",
            "was_locked": unlocked,
            "was_disabled": was_disabled,
        },
    )

    return {"success": True, "was_locked": unlocked, "was_disabled": was_disabled}


@router.post("/users/{user_uuid}/lock")
def admin_lock_account(
    request: Request,
    user_uuid: str,
    reason: str = Query("Admin action", description="Reason for locking the account"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Admin lock of user account."""
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = False  # type: ignore[assignment]
    # Locking an account that keeps a live refresh token is not a lock: token
    # rotation would carry the session past the lock for its full lifetime.
    revoke_all_sessions(db, user, reason="account locked by admin")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_ACCOUNT_DISABLED,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS,
        target_user_id=int(user.id),
        target_username=str(user.email),
        details={
            "target_user": user_uuid,
            "reason": reason,
            "locked_by": "admin",
        },
    )

    return {"success": True}


@router.delete("/users/{user_uuid}/sessions")
def admin_terminate_user_sessions(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Force logout user by terminating all sessions.

    Goes through ``revoke_all_sessions`` — i.e. ``token_service`` — like every
    other revocation path. It used to set ``RefreshToken.revoked_at`` inline,
    which made it the one termination that wrote **no Redis blacklist entry and
    no per-user revocation epoch**. That is a correctness bug before it is an
    audit one: the epoch is the only thing that reaches already-issued *access*
    tokens (they are stateless, so there is no row to revoke), so an admin force-
    logging-out a compromised account left it authenticated for the remaining
    access-token lifetime.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    count = revoke_all_sessions(db, user, reason="admin session termination")
    db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_LOGOUT_ALL,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS,
        details={
            "target_user": user_uuid,
            "sessions_terminated": count,
            "terminated_by": "admin",
        },
    )

    return {"success": True, "sessions_terminated": count}


@router.get("/users/{user_uuid}/sessions")
def admin_get_user_sessions(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """View all active sessions for a user."""
    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    sessions = (
        db.query(RefreshToken)
        .filter(
            RefreshToken.user_id == user.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > datetime.now(UTC),
        )
        .all()
    )

    return {
        "sessions": [
            {
                "id": str(s.jti),
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "expires_at": s.expires_at.isoformat() if s.expires_at else None,
                "ip_address": s.ip_address,
                "user_agent": s.user_agent,
            }
            for s in sessions
        ]
    }


@router.put("/users/{user_uuid}/role")
def admin_change_user_role(
    request: Request,
    user_uuid: str,
    new_role: str = Query(..., description="New role for the user (user, admin, super_admin)"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
):
    """Change user role. Only super_admin can promote to super_admin."""
    client_ip, user_agent = _get_client_info(request)
    if new_role not in VALID_ROLES:
        raise HTTPException(status_code=400, detail="Invalid role")

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if str(user.uuid) == str(current_user.uuid):
        raise HTTPException(status_code=400, detail="Cannot change your own role")

    from app.api.endpoints.users import _assert_not_last_super_admin

    _assert_not_last_super_admin(db, user, new_role)

    old_role = user.role
    user.role = new_role  # type: ignore[assignment]
    # Keep is_superuser in sync with role (derived; v369 CHECK enforces it).
    user.is_superuser = role_implies_superuser(new_role)
    # A role change is usually a reaction to something; the target's existing
    # sessions must not outlive it.
    revoke_all_sessions(db, user, reason="role change")
    db.commit()

    audit_role_change(user, current_user, str(old_role), new_role, client_ip, user_agent)

    return {"success": True, "old_role": old_role, "new_role": new_role}


#: Column each linkable provider's identifier lives on. Mirrors
#: `auth/account_linking.py`'s lookup order (provider id first, email second) —
#: setting this column is what makes a *subsequent* login match here instead of
#: ever reaching the email-match branch that provider's `email_verified` posture
#: might refuse.
_LINK_IDENTITY_COLUMN = {
    "oidc": "oidc_subject",
    "ldap": "ldap_uid",
    "pki": "pki_subject_dn",
}


@router.put("/users/{user_uuid}/link-identity", response_model=LinkExternalIdentityResponse)
def admin_link_external_identity(
    request: Request,
    user_uuid: str,
    payload: LinkExternalIdentityRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
) -> LinkExternalIdentityResponse:
    """Deliberately link an account to an external identity (P1.3).

    The operator remedy `auth/account_linking.py` documents but that, until this
    endpoint, did not exist: when an IdP cannot assert `email_verified` —
    Authentik hardcodes it `false` for every account — the automatic email-match
    link is refused, by design, and the login just fails. This is the explicit
    alternative: a super_admin sets the provider's own identifier on the
    account, so the identity resolves by that identifier on the very next login
    and the email-match branch (and its refusal) is never reached at all.

    Never for a `super_admin` target — that account is local-only by
    architectural invariant, the break-glass account for exactly the IdP that
    might be failing, and linking it to an external identity would make it
    reachable through the login path it exists to survive.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if str(user.role) == ROLE_SUPER_ADMIN:
        raise HTTPException(
            status_code=400,
            detail="super_admin accounts are local-only and cannot be linked to an external identity",
        )

    column = _LINK_IDENTITY_COLUMN[payload.provider]
    conflict = (
        db.query(User)
        .filter(getattr(User, column) == payload.identifier)
        .filter(User.id != user.id)
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=409,
            detail=f"That {payload.provider} identifier is already linked to another account",
        )

    setattr(user, column, payload.identifier)
    db.commit()

    logger.info(
        "super_admin %s linked %s identity %s to user %s",
        current_user.email,
        payload.provider,
        payload.identifier,
        user.email,
    )
    audit_logger.log(
        event_type=AuditEventType.ADMIN_USER_UPDATE,
        outcome=AuditOutcome.SUCCESS,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={
            "action": "link_external_identity",
            "target_user": user_uuid,
            "provider": payload.provider,
        },
    )

    return LinkExternalIdentityResponse(
        success=True, provider=payload.provider, identifier=payload.identifier
    )


# ============== MFA Management ==============


@router.post("/users/{user_uuid}/mfa/reset")
def admin_reset_user_mfa(
    request: Request,
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
):
    """Admin reset of user MFA (if user loses device). Requires super_admin role.

    The audit record reports what actually happened. It used to sit OUTSIDE the
    ``if mfa_settings:`` block and always log ``MFA_DISABLE`` / ``SUCCESS``, so a
    reset against an account with no second factor enrolled recorded a disable
    that did nothing — an event a reviewer would read as "this account's MFA was
    removed on this date". The attempt is still recorded either way (a run of
    resets against accounts that have no MFA is itself worth seeing); only the
    outcome changes.
    """
    client_ip, user_agent = _get_client_info(request)

    user = db.query(User).filter(User.uuid == user_uuid).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    mfa_settings = db.query(UserMFA).filter(UserMFA.user_id == user.id).first()
    # A row that exists with `totp_enabled` already false is the same non-event as
    # no row at all: nothing was in force, so nothing was disabled.
    was_enabled = bool(mfa_settings and mfa_settings.totp_enabled)

    if mfa_settings:
        # DELETE the row, like the user-facing `POST /auth/mfa/disable` does.
        # This branch used to null `totp_secret`, which `v200` made NOT NULL — so
        # the reset raised an IntegrityError (a 500) for every account that
        # actually had a second factor, i.e. the only case the endpoint exists
        # for. Nothing caught it because the audit call, and the `{"success":
        # true}` response, both sat outside this block: an account with no MFA
        # took the no-op path and answered 200.
        db.delete(mfa_settings)
        # Dropping the second factor must not leave sessions that were minted
        # while it was still in force.
        revoke_all_sessions(db, user, reason="admin MFA reset")
        db.commit()

    audit_logger.log(
        event_type=AuditEventType.AUTH_MFA_DISABLE,
        user_id=current_user.id,
        username=str(current_user.email),
        source_ip=client_ip,
        user_agent=user_agent,
        outcome=AuditOutcome.SUCCESS if was_enabled else AuditOutcome.FAILURE,
        error_code=None if was_enabled else "MFA_NOT_ENROLLED",
        details={
            "target_user": user_uuid,
            "reset_by": "admin",
            "mfa_was_enabled": was_enabled,
        },
    )

    return {"success": True}


# ============== User Search ==============


@router.get("/users/search")
def admin_search_users(
    query: str | None = Query(None, description="Search query for email or name"),
    role: str | None = Query(None, description="Filter by role"),
    auth_type: str | None = Query(None, description="Filter by auth type"),
    is_active: bool | None = Query(None, description="Filter by active status"),
    limit: int = Query(default=50, le=200, description="Maximum results to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Advanced user search with filtering and pagination."""
    from app.utils.pagination import paginate

    q = db.query(User)

    if query:
        q = q.filter(or_(User.email.ilike(f"%{query}%"), User.full_name.ilike(f"%{query}%")))

    if role:
        q = q.filter(User.role == role)

    if auth_type:
        q = q.filter(User.auth_type == auth_type)

    if is_active is not None:
        q = q.filter(User.is_active == is_active)

    users, total = paginate(q, offset, limit)

    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "users": [
            {
                "uuid": str(u.uuid),
                "email": u.email,
                "full_name": u.full_name,
                "role": u.role,
                "auth_type": u.auth_type,
                "is_active": u.is_active,
                "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            }
            for u in users
        ],
    }


# ============== Reporting ==============


@router.get("/reports/account-status")
def get_account_status_report(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Account status summary for compliance reporting."""
    # Consolidate 3 User COUNT queries into 1 using FILTER clauses.
    # The expiry cutoff comes from the password policy rather than a local
    # timedelta(PASSWORD_MAX_AGE_DAYS): this report reimplemented the rule that
    # password_policy already owns, so with PASSWORD_POLICY_ENABLED=false it kept
    # reporting expired passwords that nothing would ever act on. A None cutoff
    # means expiry is not enforced, so nothing is expired.
    expiry_threshold = password_expiry_cutoff()
    expired_filter = User.password_changed_at < expiry_threshold if expiry_threshold else sa_false()
    user_row = db.query(
        func.count().label("total"),
        func.count().filter(User.is_active.is_(True)).label("active"),
        func.count().filter(expired_filter).label("pwd_expired"),
    ).one()

    # MFA is a separate table, so one more query
    mfa_enabled = db.query(func.count(UserMFA.id)).filter(UserMFA.totp_enabled.is_(True)).scalar()

    total = user_row.total
    active = user_row.active

    return {
        "total_users": total,
        "active_users": active,
        "inactive_users": total - active,
        "mfa_enabled_users": mfa_enabled,
        "password_expired_users": user_row.pwd_expired,
    }


# ============== Audit Logs (FedRAMP AU-2/AU-3) ==============


@router.get("/audit-logs")
def get_audit_logs(
    start_date: datetime | None = Query(None, description="Start date for log query"),
    end_date: datetime | None = Query(None, description="End date for log query"),
    event_type: str | None = Query(None, description="Filter by event type"),
    user_id: int | None = Query(None, description="Filter by user ID"),
    outcome: str | None = Query(None, description="Filter by outcome"),
    limit: int = Query(default=100, le=1000, description="Maximum results"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
):
    """
    Query audit logs with filtering. Super admin only (global, all tenants).

    Org-admins get a tenant-scoped view of the same data at
    ``GET /org-admin/audit-logs`` (see ``endpoints/org_admin.py``).

    Note: This endpoint queries OpenSearch if audit logging to OpenSearch is
    enabled. If OpenSearch is not available, returns an error message.
    """
    from app.auth.audit import query_audit_logs

    return query_audit_logs(
        start_date=start_date,
        end_date=end_date,
        event_type=event_type,
        user_id=user_id,
        outcome=outcome,
        limit=limit,
        offset=offset,
    )


@router.get("/audit-logs/export")
def export_audit_logs(
    export_format: str = Query("csv", description="Export format (csv or json)"),
    start_date: datetime | None = Query(None, description="Start date for export"),
    end_date: datetime | None = Query(None, description="End date for export"),
    current_user: User = Depends(get_current_super_admin_user),
):
    """Export audit logs for compliance reporting. Super admin only."""
    import csv
    import io
    import json

    from fastapi.responses import StreamingResponse

    if export_format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="Format must be csv or json")

    # Check if OpenSearch audit logging is enabled
    if not settings.AUDIT_LOG_TO_OPENSEARCH:
        raise HTTPException(
            status_code=400,
            detail="Audit log export requires AUDIT_LOG_TO_OPENSEARCH=true",
        )

    try:
        from opensearchpy import OpenSearch

        from app.core.opensearch_auth import opensearch_connection_kwargs

        client = OpenSearch(**opensearch_connection_kwargs())

        # Build query
        must_clauses = []
        if start_date:
            must_clauses.append({"range": {"timestamp": {"gte": start_date.isoformat()}}})
        if end_date:
            must_clauses.append({"range": {"timestamp": {"lte": end_date.isoformat()}}})

        query = {
            "query": {"bool": {"must": must_clauses}} if must_clauses else {"match_all": {}},
            "sort": [{"timestamp": {"order": "desc"}}],
            "size": 10000,  # Maximum export size
        }

        index_pattern = "audit-logs-*"
        response = client.search(index=index_pattern, body=query)
        logs = [hit["_source"] for hit in response["hits"]["hits"]]

        if export_format == "json":
            content = json.dumps(logs, indent=2, default=str)
            media_type = "application/json"
        else:
            # CSV format
            output = io.StringIO()
            if logs:
                fieldnames = [
                    "timestamp",
                    "event_type",
                    "outcome",
                    "user_id",
                    "username",
                    "source_ip",
                    "user_agent",
                    "error_code",
                    "details",
                ]
                writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for log in logs:
                    # Convert details dict to string for CSV
                    if "details" in log and isinstance(log["details"], dict):
                        log["details"] = json.dumps(log["details"])
                    writer.writerow(log)
            content = output.getvalue()
            media_type = "text/csv"

        filename = f"audit-logs-{datetime.now(UTC).strftime('%Y%m%d')}.{export_format}"

        return StreamingResponse(
            iter([content]),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.error("Error exporting audit logs: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


# ============== GDPR / Right-to-Erasure (super-admin / platform) ==============


@router.post("/gdpr/erase-user/{user_uuid}")
def admin_erase_user(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_super_admin_user),
):
    """GDPR Art. 17 erasure of a user — platform/super-admin scope.

    Permanently cascades object storage, OpenSearch transcript + voiceprint
    (biometric) docs, and the relational rows, then deletes the user. This is
    the platform-staff / self-host-operator entry point to the same
    ``erase_user`` service the cloud ``user.deleted`` webhook calls. Idempotent;
    SLA 30 days. The erasure is audit-logged by the service.
    """
    target = db.query(User).filter(User.uuid == user_uuid).first()
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")

    # THE self-erasure guard is the real fix here (issue #431). This is the most
    # destructive account operation in the app — it cascades object storage,
    # OpenSearch voiceprints and the relational rows before dropping the account —
    # and it was the only such route that let a super_admin erase THEMSELVES,
    # mid-request. Its sibling delete routes have always refused that.
    if int(target.id) == int(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot erase your own account",
        )

    # Also defence in depth, and also unreachable as written: this route is
    # super_admin-gated, and the self guard above means the target is someone else,
    # so the caller always remains. Kept for the same reason as its twin on
    # DELETE /admin/users/{uuid} — it is the backstop if either premise changes.
    from app.api.endpoints.users import _assert_not_last_super_admin

    _assert_not_last_super_admin(db, target, ROLE_USER)

    from app.services.gdpr_erasure_service import erase_user

    # Name the ACTING super_admin. Without these, every platform erasure recorded
    # actor_email "data-subject-webhook" — the service's default, meaning "the user
    # deleted their own IdP account" — so a staff-initiated erasure was attributed
    # to a self-service deletion that never happened, 100% of the time. The
    # org-admin twin (erase_org_member_data) has always passed them.
    return erase_user(
        db,
        int(target.id),
        actor_user_id=int(current_user.id),
        actor_email=str(current_user.email),
    )


# ---------------------------------------------------------------------------
# Data Integrity (Orphan Cleanup) Endpoints
# ---------------------------------------------------------------------------


@router.post("/data-integrity")
def start_data_integrity_check(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Start an OpenSearch orphan cleanup task.

    Scans all OpenSearch indices for documents referencing deleted files
    and removes them.
    """
    from app.tasks.opensearch_integrity_task import get_integrity_status
    from app.tasks.opensearch_integrity_task import opensearch_orphan_cleanup_task

    status = get_integrity_status()
    if status.get("running"):
        return {"status": "already_running"}

    # Pass the requester so progress reaches THEM. The task used to publish to a
    # hardcoded user_id=1, so an admin who was not account 1 triggered a sweep and
    # then waited forever while whoever held id 1 got the toasts (issue #431).
    result = opensearch_orphan_cleanup_task.delay(user_id=int(current_user.id))
    return {"status": "started", "task_id": str(result.id)}


@router.get("/data-integrity/status")
def get_data_integrity_status(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Get data integrity check status, last run results, and index overview."""
    from app.tasks.opensearch_integrity_task import get_index_overview
    from app.tasks.opensearch_integrity_task import get_integrity_status

    status = get_integrity_status()
    status["index_overview"] = get_index_overview()
    return status


@router.get("/data-integrity/counts")
def get_data_integrity_counts(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Quick dry-run scan to count orphaned documents without deleting."""
    from app.tasks.opensearch_integrity_task import get_integrity_counts

    return get_integrity_counts()


# ---------------------------------------------------------------------------
# Embedding Consistency (Self-Healing) Endpoints
# ---------------------------------------------------------------------------


@router.get("/embedding-consistency/status")
def get_embedding_consistency_status(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Get embedding consistency check status and last run results."""
    from app.tasks.speaker_embedding_consistency import get_embedding_consistency_status

    return get_embedding_consistency_status()


@router.get("/embedding-consistency/counts")
def get_embedding_consistency_counts(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Quick dry-run count of speakers missing from OpenSearch indices."""
    from app.tasks.speaker_embedding_consistency import get_embedding_consistency_counts

    return get_embedding_consistency_counts()


@router.post("/embedding-consistency/repair")
def start_embedding_consistency_repair(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Start an embedding consistency repair task."""
    from app.tasks.speaker_embedding_consistency import get_embedding_consistency_status
    from app.tasks.speaker_embedding_consistency import speaker_embedding_consistency_check_task

    status_info = get_embedding_consistency_status()
    if status_info.get("running"):
        return {"status": "already_running"}

    result = speaker_embedding_consistency_check_task.apply_async(
        kwargs={"manual": True, "user_id": current_user.id},
    )
    return {"status": "started", "task_id": str(result.id)}


# ---------------------------------------------------------------------------
# GPU VRAM Profiling Endpoints
# ---------------------------------------------------------------------------


@router.get("/gpu-profiles")
def get_gpu_profiles(
    current_user: User = Depends(get_current_admin_user),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[dict]:
    """Return recent GPU VRAM profiles from Redis."""
    import json

    from app.core.redis import get_redis

    try:
        r = get_redis()
        task_ids = r.lrange("gpu:profile:history", 0, limit - 1)
        if not task_ids:
            return []

        profiles = []
        for tid in task_ids:
            tid_str = tid if isinstance(tid, str) else tid.decode()
            data = r.get(f"gpu:profile:{tid_str}")
            if data:
                raw = data if isinstance(data, str) else data.decode()
                profiles.append(json.loads(raw))
        return profiles
    except HTTPException:
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception("Failed to read GPU profiles from Redis")
        raise HTTPException(status_code=500, detail="Failed to read GPU profiles.") from e


@router.post("/embedding-consistency/stop")
def stop_embedding_consistency_repair(
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Cancel a running embedding consistency repair."""
    from app.tasks.speaker_embedding_consistency import stop_consistency_repair

    return stop_consistency_repair()


@router.post("/profile-embeddings/repair")
def repair_profile_embeddings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Recalculate all profile embeddings by averaging assigned speakers.

    Fixes profiles where the embedding was stored from a single speaker
    instead of the true centroid of all assigned speakers.
    """
    from app.models.media import SpeakerProfile
    from app.services.profile_embedding_service import ProfileEmbeddingService

    profiles = db.query(SpeakerProfile).all()
    if not profiles:
        return {"status": "ok", "message": "No profiles found", "total": 0}

    profile_ids = [p.id for p in profiles]
    results = ProfileEmbeddingService.batch_update_profile_embeddings(db, profile_ids)

    success = sum(1 for v in results.values() if v)
    failed = len(results) - success

    logger.info(
        f"Profile embedding repair: {success} updated, {failed} failed out of {len(results)}"
    )
    return {
        "status": "ok",
        "total": len(results),
        "updated": success,
        "failed": failed,
    }


# =============================================================================
# AI Summary System Settings
# =============================================================================


@router.get("/system/ai-summary")
def get_system_ai_summary_setting(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Get system-wide AI summary setting (admin only)."""
    from app.utils.summary_settings import is_summary_enabled_system

    enabled = is_summary_enabled_system(db)
    return {"ai_summary_enabled": enabled, "scope": "system"}


@router.put("/system/ai-summary")
def update_system_ai_summary_setting(
    *,
    enabled: bool,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Enable or disable AI summary generation system-wide (admin only)."""
    system_settings_service.set_setting(
        db,
        "ai.summary_enabled",
        enabled,
        "Global toggle for AI summary auto-generation",
    )
    state = "enabled" if enabled else "disabled"
    logger.info(f"Admin {current_user.email} {state} system-wide AI summaries")
    return {"ai_summary_enabled": enabled, "scope": "system"}


@router.get("/imohash-recompute/status")
def get_imohash_recompute_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Status of the one-time imohash fingerprint recompute (admin only).

    Cross-pipeline dedup (watch sources, re-upload detection) is unreliable
    until this completes, so surface it in the admin Data Integrity panel.
    """
    from app.tasks.imohash_recompute import RECOMPUTE_FLAG_KEY
    from app.tasks.imohash_recompute import recompute_progress

    complete = system_settings_service.get_setting_bool(db, RECOMPUTE_FLAG_KEY, False)
    return {"complete": complete, "progress": recompute_progress.get_status()}


@router.post("/imohash-recompute/start")
def start_imohash_recompute(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Manually (re)trigger the imohash fingerprint recompute (admin only).

    Clears the completion flag and dispatches the batched recompute task. Safe
    to run any time; it overwrites every ``media_file.imohash`` via fast ranged
    reads. No-op guard if a recompute is already running.
    """
    from app.tasks.imohash_recompute import RECOMPUTE_FLAG_KEY
    from app.tasks.imohash_recompute import recompute_all
    from app.tasks.imohash_recompute import recompute_progress

    if recompute_progress.is_running():
        return {
            "status": "already_running",
            "message": "imohash recompute is already in progress",
            "progress": recompute_progress.get_status(),
        }

    # Reset the completion flag so a fresh run is tracked from zero.
    system_settings_service.set_setting(
        db,
        RECOMPUTE_FLAG_KEY,
        "false",
        "One-time imohash package recompute of all media_file.imohash completed",
    )
    task = recompute_all.delay()
    logger.info("Admin %s triggered imohash recompute: %s", current_user.email, task.id)
    return {
        "status": "started",
        "task_id": task.id,
        "message": "imohash recompute dispatched.",
    }


# ===========================================================================
# Abuse / DMCA / safe-harbor takedown (admin quarantine)
# ===========================================================================


def _request_meta(request: Request) -> tuple[str, str]:
    """Extract (source_ip, user_agent) for the takedown audit trail."""
    ip = request.client.host if request.client else ""
    return ip, request.headers.get("user-agent", "")


@router.get("/files/quarantined", response_model=QuarantinedFilesList)
def list_quarantined_files(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """List taken-down files for admin review (abuse/DMCA queue).

    Quarantined files are hidden from every normal read surface, so this is the
    only place an admin can see and act on them. Newest takedown first.
    """
    base = db.query(MediaFile).filter(MediaFile.is_quarantined.is_(True))
    total = base.with_entities(func.count(MediaFile.id)).scalar() or 0
    rows = (
        base.order_by(MediaFile.quarantined_at.desc().nullslast()).offset(offset).limit(limit).all()
    )
    files = [
        QuarantinedFile(
            uuid=str(f.uuid),
            filename=f.filename,
            user_id=int(f.user_id),
            organization_id=f.organization_id,
            quarantine_reason=f.quarantine_reason,
            quarantined_at=f.quarantined_at.isoformat() if f.quarantined_at else None,
            quarantined_by=f.quarantined_by,
            legal_hold=bool(f.legal_hold),
        )
        for f in rows
    ]
    return QuarantinedFilesList(files=files, total=int(total))


@router.post("/files/{file_uuid}/quarantine", response_model=QuarantineActionResponse)
def quarantine_media_file(
    file_uuid: str,
    request_body: QuarantineRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Take a file down (abuse/DMCA). Hides it from all read surfaces + audits.

    The original media/transcript are NOT deleted — the takedown is reversible
    via ``/release`` and a legal-hold (default on) protects the object from
    deletion while a dispute/notice is open.
    """
    from app.services.takedown_service import quarantine_file
    from app.utils.uuid_helpers import get_file_by_uuid

    file = get_file_by_uuid(db, file_uuid)
    source_ip, user_agent = _request_meta(request)
    file = quarantine_file(
        db,
        file,
        admin=current_user,
        reason=request_body.reason,
        legal_hold=request_body.legal_hold,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return QuarantineActionResponse(
        uuid=str(file.uuid),
        is_quarantined=bool(file.is_quarantined),
        legal_hold=bool(file.legal_hold),
        status=str(file.status.value if hasattr(file.status, "value") else file.status),
    )


@router.post("/files/{file_uuid}/release", response_model=QuarantineActionResponse)
def release_media_file(
    file_uuid: str,
    request: Request,
    request_body: ReleaseRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Release a quarantined file: restore access, lift the legal-hold, audit."""
    from app.services.takedown_service import release_file
    from app.utils.uuid_helpers import get_file_by_uuid

    file = get_file_by_uuid(db, file_uuid)
    if not file.is_quarantined:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="File is not quarantined",
        )
    clear_hold = request_body.clear_legal_hold if request_body is not None else True
    source_ip, user_agent = _request_meta(request)
    file = release_file(
        db,
        file,
        admin=current_user,
        clear_legal_hold=clear_hold,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    return QuarantineActionResponse(
        uuid=str(file.uuid),
        is_quarantined=bool(file.is_quarantined),
        legal_hold=bool(file.legal_hold),
        status=str(file.status.value if hasattr(file.status, "value") else file.status),
    )
