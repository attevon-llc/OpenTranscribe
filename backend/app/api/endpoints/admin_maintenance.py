"""Admin maintenance routes: GDPR erasure, integrity, backfill, embedding repair, imohash."""

import logging

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi import status
from sqlalchemy.orm import Session

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_superuser
from app.api.endpoints.auth import get_current_admin_user
from app.api.endpoints.auth.dependencies import _get_client_info
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.roles import ROLE_USER
from app.core.exceptions import DomainError
from app.db.base import get_db
from app.models.user import User
from app.services import system_settings_service
from app.services.request_context import RequestContext

# No basicConfig here — this module is imported via the API router before
# configure_logging() runs; a default root handler would double every log line.
logger = logging.getLogger(__name__)

router = APIRouter()


# ============== GDPR / Right-to-Erasure (super-admin / platform) ==============


@router.post("/gdpr/erase-user/{user_uuid}")
def admin_erase_user(
    user_uuid: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
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
    request: Request,
    force: bool = Query(
        False,
        description=(
            "Override the per-index ratio guard (a sweep may otherwise delete at most "
            "10% of an index). For reconciling a LARGE legitimate orphan set — after a "
            "database restore or out-of-band row removal. Requires confirm=true."
        ),
    ),
    confirm: bool = Query(False, description="Required with force=true."),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Start an OpenSearch orphan cleanup task.

    Scans all OpenSearch indices for documents referencing deleted files
    and removes them.

    **Reconciling a large orphan set.** The sweep refuses (``refused: ratio_guard``)
    to delete more than 10% of an index unasked, because that is what a database
    restored EMPTY looks like from here. When the orphans are real, the operator path
    is: ``GET /admin/data-integrity/counts`` (a dry run that names every orphaned
    identifier per index in ``orphan_keys``), review it, then
    ``POST /admin/data-integrity?force=true&confirm=true``. The forced run is
    audit-logged (``admin.data_integrity.purge``). It can never delete the documents
    of a file or speaker that exists: every candidate is re-checked against Postgres
    after the index is read and before anything is deleted, and an EMPTY Postgres
    side is refused outright, force or not.
    """
    ctx.bypass.require_deployment_scope("The orphan sweep")
    from app.tasks.opensearch_integrity_task import get_integrity_status
    from app.tasks.opensearch_integrity_task import opensearch_orphan_cleanup_task

    if force and not confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "force=true overrides the orphan-sweep ratio guard and deletes index "
                "documents deployment-wide. Review GET /admin/data-integrity/counts, then "
                "retry with confirm=true."
            ),
        )

    integrity = get_integrity_status()
    if integrity.get("running"):
        return {"status": "already_running"}

    if force:
        client_ip, user_agent = _get_client_info(request)
        audit_logger.log(
            event_type=AuditEventType.ADMIN_DATA_INTEGRITY_PURGE,
            outcome=AuditOutcome.SUCCESS,
            user_id=current_user.id,
            username=str(current_user.email),
            source_ip=client_ip,
            user_agent=user_agent,
            details={"action": "forced_orphan_purge_requested"},
        )

    # Pass the requester so progress reaches THEM. The task used to publish to a
    # hardcoded user_id=1, so an admin who was not account 1 triggered a sweep and
    # then waited forever while whoever held id 1 got the toasts (issue #431).
    result = opensearch_orphan_cleanup_task.delay(user_id=int(current_user.id), force=force)
    return {"status": "started", "task_id": str(result.id), "force": force}


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


@router.post("/media-duration-backfill")
def start_media_duration_backfill(
    dry_run: bool = True,
    current_user: User = Depends(get_current_active_superuser),
) -> dict:
    """Start the issue #969 media-duration provenance backfill. Super-admin only.

    Restores the container duration on rows the pre-#969 pipeline overwrote with
    the transcript's speech extent. ``dry_run=true`` (the default) reports what
    would change without writing anything or dispatching a reindex — see
    ``app.tasks.recovery_tasks.media_duration_backfill`` for the full tiering,
    safety guarantees, and why quarantined/legal-hold rows are never touched.
    """
    from app.tasks.recovery_tasks import media_duration_backfill

    result = media_duration_backfill.delay(user_id=None, dry_run=dry_run)
    return {"status": "started", "task_id": str(result.id), "dry_run": dry_run}


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
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Start an embedding consistency repair task."""
    ctx.bypass.require_deployment_scope("Repairing speaker embeddings")
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
    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except Exception as e:
        logger.exception("Failed to read GPU profiles from Redis")
        raise HTTPException(status_code=500, detail="Failed to read GPU profiles.") from e


@router.post("/embedding-consistency/stop")
def stop_embedding_consistency_repair(
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Cancel a running embedding consistency repair."""
    ctx.bypass.require_deployment_scope("Stopping the speaker embedding repair")
    from app.tasks.speaker_embedding_consistency import stop_consistency_repair

    return stop_consistency_repair()


@router.post("/profile-embeddings/repair")
def repair_profile_embeddings(
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Recalculate all profile embeddings by averaging assigned speakers.

    Fixes profiles where the embedding was stored from a single speaker
    instead of the true centroid of all assigned speakers.
    """
    ctx.bypass.require_deployment_scope("Repairing profile embeddings")
    from app.models.speaker import SpeakerProfile
    from app.services.profile_embedding_service import ProfileEmbeddingService

    profiles = db.query(SpeakerProfile).all()
    if not profiles:
        return {"status": "ok", "message": "No profiles found", "total": 0}

    profile_ids = [p.id for p in profiles]

    # Privacy policy (#1280, plan D8): this recomputes voiceprints, so a profile whose owner
    # scope has them off is skipped, never written. When EVERY profile is off there is
    # nothing this request could do, and it says why (409) instead of reporting an empty ok.
    from app.services.privacy.redact_view import voiceprints_disabled_error

    skipped = ProfileEmbeddingService.profiles_off_by_policy(db, profile_ids)
    if skipped and len(skipped) == len(profile_ids):
        raise voiceprints_disabled_error(next(iter(skipped.values())))
    allowed_ids = [pid for pid in profile_ids if pid not in skipped]
    results = ProfileEmbeddingService.batch_update_profile_embeddings(db, allowed_ids)

    success = sum(1 for v in results.values() if v)
    failed = len(results) - success

    logger.info(
        f"Profile embedding repair: {success} updated, {failed} failed, "
        f"{len(skipped)} skipped by privacy policy out of {len(profile_ids)}"
    )
    return {
        "status": "ok",
        "total": len(profile_ids),
        "updated": success,
        "failed": failed,
        "skipped_by_policy": len(skipped),
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
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict:
    """Manually (re)trigger the imohash fingerprint recompute (admin only).

    Clears the completion flag and dispatches the batched recompute task. Safe
    to run any time; it overwrites every ``media_file.imohash`` via fast ranged
    reads. No-op guard if a recompute is already running.
    """
    ctx.bypass.require_deployment_scope("The imohash recompute")
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
