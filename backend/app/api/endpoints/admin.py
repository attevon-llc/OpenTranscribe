import asyncio
import logging
import platform
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.endpoints.admin_system_metrics import get_cpu_usage
from app.api.endpoints.admin_system_metrics import get_disk_usage
from app.api.endpoints.admin_system_metrics import get_gpu_usage
from app.api.endpoints.admin_system_metrics import get_memory_usage
from app.api.endpoints.admin_system_metrics import get_system_uptime
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.roles import ELEVATED_ROLES
from app.auth.roles import ROLE_SUPER_ADMIN
from app.auth.roles import ROLE_USER
from app.core.exceptions import DomainError
from app.core.version import APP_VERSION
from app.db.base import get_db
from app.models.user import User
from app.schemas.user import User as UserSchema
from app.schemas.user import UserCreate
from app.services.account_security_service import DeletedUser
from app.services.account_security_service import audit_user_deleted
from app.services.user_deletion_service import _assert_no_held_files
from app.services.user_deletion_service import _delete_user_media_files
from app.services.user_deletion_service import _delete_user_owned_records
from app.services.user_deletion_service import _delete_user_speakers

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


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
        from app.transcription.diar_sidecar_status import describe_diarizer_status
        from app.transcription.diar_sidecar_status import sidecar_diagnostics

        # Single resolver, shared with /system/stats (app/utils/stats_helpers.py) — issue
        # #672's second half. This used to be a hand-rolled, UNVALIDATED copy of the
        # diarizer_backend resolution (via engine_settings._resolve_setting) with its own
        # description table; it disagreed with the other panel's answer on a bad config
        # value, and both of them reported the CONFIGURED backend rather than the one
        # actually serving. describe_diarizer_status() does a bounded (<=5s, TTL-cached)
        # network probe when the configured backend is "native", so it is offloaded to a
        # thread — this handler is `async def` and a probe run inline would stall every
        # other in-flight request on this worker's event loop for the same 5s.
        diarizer_status = await asyncio.to_thread(describe_diarizer_status)
        # issue #656 Step 9: a live, deployment-wide "can it serve right now" probe — same
        # asyncio.to_thread offload as above and for the same reason (a synchronous 5s probe
        # inline would block this worker's whole event loop). Kept even when the configured
        # backend is "pyannote" so an admin can see whether the sidecar has come back before
        # switching to it.
        sidecar_info = await asyncio.to_thread(sidecar_diagnostics)

        models_info = {
            "whisper": {
                "name": settings.WHISPER_MODEL,
                "description": f"Whisper {settings.WHISPER_MODEL}",
                "purpose": "Speech Recognition & Transcription",
            },
            "diarization": {
                # Weights are shared by both engines (local_provider.py); the engine
                # actually serving is what varies. "description" reports the EFFECTIVE
                # engine — what is really running — since that is the question this panel
                # exists to answer; "configured_backend"/"using_fallback" carry the other
                # half (what was asked for) so an operator can tell "native, and it's
                # working" from "native, and I'm silently on the fallback".
                "name": "pyannote/speaker-diarization-community-1",
                "description": diarizer_status["effective_description"],
                "purpose": "Speaker Identification & Segmentation",
                "configured_backend": diarizer_status["configured"],
                "configured_description": diarizer_status["configured_description"],
                "effective_backend": diarizer_status["effective"],
                "using_fallback": diarizer_status["using_fallback"],
                "require_sidecar": diarizer_status["require_sidecar"],
                "refusing": diarizer_status["refusing"],
                "sidecar": sidecar_info,
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
    except (HTTPException, DomainError):
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
    except (HTTPException, DomainError):
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
    except (HTTPException, DomainError) as he:
        logger.error(f"HTTP error creating user: {he.detail}")
        raise he
    except Exception as e:
        logger.error("Error creating user: %s", e, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An internal error occurred. Please try again.",
        ) from e


@router.delete("/users/{user_uuid}", response_model=dict[str, Any])
def delete_admin_user(
    user_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_admin_user),
):
    """Delete a user and all their data (admin only).

    **Phase boundary (issue #695).** The three cascade helpers below delete
    ``media_file`` (and every table hanging off it) via **bulk** SQL — one statement,
    no instances loaded, no cascade — which never reaches ``file_cleanup_service``'s
    object-storage/OpenSearch destroy. Left alone, every deleted account's recordings
    stayed in MinIO forever. The fix keeps that bulk delete (it is why
    ``_delete_user_media_files`` exists — see its docstring) but reads the storage plan
    with ``load_account_purge_plans`` **before** the savepoint, and destroys those
    external copies with ``purge_account_external_copies`` **after** ``db.commit()`` —
    with no transaction open, matching ``purge_media_file``'s own phase split. Doing
    that I/O on a live session previously wedged the DB for up to 1h26m
    (``idle in transaction``); doing it inside the savepoint would have meant a slow
    MinIO/OpenSearch round trip holds a lock for the whole cascade.

    Args:
        user_uuid: UUID of the user to delete
        db: Database session
        current_user: Current admin user

    Returns:
        ``{"message": str, "storage_objects_failed": int}`` — the count is 0 when
        every external object was confirmed gone.

    Raises:
        HTTPException: If user not found or deletion not allowed
    """
    from app.services.file_cleanup_service import load_account_purge_plans
    from app.services.file_cleanup_service import purge_account_external_copies
    from app.services.uuid_helpers import get_user_by_uuid

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

        # Legally-held evidence outranks the deletion, and the refusal has to land
        # BEFORE the first of the three cascade passes: a partial destroy followed by a
        # 409 is worse than either outcome on its own (issue #689).
        _assert_no_held_files(db, int(user_id))

        # Phase 0 — read the storage/OpenSearch plan while the rows still exist and the
        # transaction is open. Postgres reads only; no object storage touched yet.
        purge_plan = load_account_purge_plans(db, int(user_id))

        # Capture what the audit record needs before the row is gone: after the commit
        # the ORM object is expired, so reading user.email would re-query a deleted row.
        deleted_snapshot = DeletedUser.of(user)
        client_ip, user_agent = _get_client_info(request)

        # Phase 1 — delete all user data atomically using a savepoint
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

        # Phase 2 — object storage + OpenSearch. NO transaction is held here. The rows
        # are already gone, so a failure here is NOT retryable: it must be visible
        # (residual_errors -> PARTIAL audit outcome + ERROR log), never swallowed.
        residual_errors = purge_account_external_copies(purge_plan)

        # This endpoint destroys a user, their files and their transcripts irreversibly
        # and recorded NOTHING — while its twin, DELETE /api/users/{uuid}, performs the
        # identical deletion through these same three helpers and does audit it. Emitted
        # after the commit, matching that twin, so a failed delete leaves no record of a
        # deletion that did not happen. FedRAMP AU-2/AU-12, GDPR Art. 30(2)(d).
        audit_user_deleted(deleted_snapshot, current_user, client_ip, user_agent, residual_errors)

        logger.info(f"User deletion completed successfully: {user_id}")
        return {
            "message": "User deleted successfully",
            "storage_objects_failed": len(residual_errors),
        }

    except (HTTPException, DomainError):
        raise
    except Exception as e:
        logger.exception(f"User deletion failed, all changes rolled back: {e}")
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="User deletion failed",
        ) from e
