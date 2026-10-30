"""Search reindex control endpoints: trigger, stop, status, degraded-embedding re-embed."""

import logging
from typing import Any

from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Query
from redis.exceptions import RedisError

from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_admin_user
from app.core.config import settings
from app.core.exceptions import DomainError
from app.models.user import User
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause
from app.services.platform_access import TenancyMode
from app.services.request_context import RequestContext
from app.services.search.reindex_cancel import LEGACY_CANCEL_VALUE
from app.services.search.reindex_cancel import cancel_requested
from app.services.search.reindex_cancel import clear_fanout
from app.services.search.reindex_cancel import read_fanout
from app.services.search.reindex_cancel import request_cancel
from app.services.search.reindex_lock import ReindexLockUnavailableError
from app.services.search.reindex_lock import running_reindex_run_id as _running_reindex_run_id

logger = logging.getLogger(__name__)

router = APIRouter()


def _no_pending(message: str) -> dict[str, Any]:
    """The "there is nothing to queue" answer, in the shape the panel expects."""
    return {
        "task_id": None,
        "status": "no_pending",
        "message": message,
        "reindex_task_ids": {},
        "reindex_users": 0,
    }


@router.post("/reindex")
def trigger_reindex(
    file_uuids: list[str] | None = Body(
        None, description="Optional list of specific file UUIDs to reindex"
    ),
    pending_only: bool = Query(False, description="Only reindex files without chunks"),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Re-index existing transcripts **deployment-wide**, one coordinator per owner.

    Admin-only, and because the scope is every tenant's corpus it also needs the
    deployment-scope right: any admin in a single-tenant deployment, a ``super_admin``
    in a multi-tenant one (``ctx.bypass.require_deployment_scope``).

    Every mode used to dispatch a single ``reindex_transcripts_task`` for
    ``current_user.id`` (issue #627). Since that coordinator filters
    ``MediaFile.user_id == user_id``, an admin pressing "Reindex all" repaired
    their own account and left every other user's files untouched — silently,
    with a success toast. The fan-out is #437's
    ``dispatch_reindex_for_every_owner``, reused rather than reimplemented; the
    scope of the two narrower modes comes from
    ``services/search/reindex_scope.py``.

    Args:
        file_uuids: Optional list of specific file UUIDs, resolved to whichever
            accounts own them. ``None`` = every owner's whole corpus.
        pending_only: If True, only reindex files that have no chunks in
            OpenSearch — surveyed across every owner, not just the caller's.

    Returns:
        Dict with the caller's ``task_id`` (the run the settings panel's progress
        stream is keyed to), the per-owner task ids, and how many owners were
        dispatched.

    Raises:
        HTTPException: 503 when nothing could be queued at all.
    """
    ctx.bypass.require_deployment_scope("Reindexing")
    from app.services.search.model_switch import ReindexDispatchError
    from app.services.search.model_switch import dispatch_reindex_for_every_owner
    from app.services.search.reindex_scope import owners_of_files
    from app.services.search.reindex_scope import pending_files_by_owner

    scope: dict[int, list[str]] | None = None

    if file_uuids is not None:
        scope = owners_of_files(file_uuids)
        if not scope:
            return _no_pending(
                f"None of the {len(file_uuids)} requested file(s) are completed "
                f"transcripts that can be indexed."
            )
    elif pending_only:
        scope, indexable_total = pending_files_by_owner()
        if not indexable_total:
            return _no_pending("No completed files found to index.")
        if not scope:
            return _no_pending("All files are already indexed.")
        queued = sum(len(uuids) for uuids in scope.values())
        logger.info(
            f"Pending-only reindex: {queued} files across {len(scope)} owner(s) need "
            f"indexing (out of {indexable_total} total)"
        )

    try:
        dispatch = dispatch_reindex_for_every_owner(current_user.id, scope)
    except ReindexDispatchError as e:
        logger.error(f"Re-index could not be queued: {e}")
        raise HTTPException(status_code=503, detail=str(e)) from e

    task_ids = dispatch["reindex_task_ids"]
    logger.info(
        f"Re-index dispatched by user {current_user.id} for {dispatch['reindex_users']} "
        f"owner(s), files: {'named' if scope else 'all'}"
    )

    return {
        # The caller's own coordinator, because that is the run the panel's
        # progress stream is keyed to. It is absent when a named-file scope
        # contains nothing the caller owns. `POST /reindex/stop` is NOT keyed to
        # it — it cancels every owner in the fan-out `dispatch_reindex_for_every_owner`
        # recorded (#691).
        "task_id": task_ids.get(current_user.id),
        "status": "started",
        "message": (
            f"Re-indexing started for {dispatch['reindex_users']} user(s) across the "
            f"deployment. Progress will be sent via WebSocket."
        ),
        **dispatch,
    }


@router.post(
    "/reindex/stop",
    responses={503: {"description": "Reindex state cannot be determined (Redis unreachable)."}},
)
def stop_reindex(
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Cancel the re-index run this admin started — **every owner's coordinator**.

    ``POST /reindex`` fans out one coordinator per owner (#627); this used to set
    a single ``reindex_cancel:{caller id}`` flag, and each coordinator reads the
    flag belonging to the *file's* owner. So an admin could start a
    deployment-wide re-embed and cancel only their own share of it, with every
    other owner's coordinator running to completion (#691). It now enumerates the
    owner set the dispatch recorded under ``reindex_fanout:{caller id}`` and flags
    all of them, naming each coordinator's run id so a coordinator still sitting
    in the broker queue can honour the stop instead of clearing it on entry.
    ``services/search/reindex_cancel.py`` holds the mechanism and its rationale.

    Scoped to **this admin's own run**, not to every reindex on the deployment: a
    concurrent run another admin started, and the per-owner coordinators
    ``search_index_maintenance`` dispatches on its own schedule, are left alone.
    A caller whose own coordinator is running but belongs to no recorded fan-out
    is still flagged, so a maintenance-dispatched run stays stoppable exactly as
    it was.

    Each flagged coordinator stops after its current file completes and restores
    normal index settings (``refresh_interval``).

    Returns:
        Dict with the stop status and how many coordinators were signalled.
    """
    ctx.bypass.require_deployment_scope("Stopping a reindex")
    user_id = current_user.id

    try:
        dispatched = read_fanout(user_id)
        targets = dict(dispatched)
        if user_id not in targets and _check_reindex_task_active(user_id):
            # A run this admin did not fan out: dispatched by
            # `search_index_maintenance`, or started before the record existed.
            # There is no run id to name, so the flag falls back to the legacy
            # value — truthy for the batch workers of a coordinator that is
            # already running, which is the only kind that can exist here.
            targets[user_id] = LEGACY_CANCEL_VALUE

        if not any(_check_reindex_task_active(uid) for uid in targets):
            return {
                "status": "not_running",
                "message": "No reindex task is currently running.",
            }

        stopped = request_cancel(targets)
        if dispatched:
            clear_fanout(user_id)

        logger.info(f"Reindex stop requested by user {user_id} for owner(s) {stopped}")

        return {
            "status": "stop_requested",
            "stopped_users": len(stopped),
            "message": (
                f"Stop signal sent to {len(stopped)} re-index coordinator(s) — the whole "
                f"run you started, not just your own files. Each stops after the file it "
                f"is on completes."
            ),
        }
    except (HTTPException, DomainError):
        # Re-raise deliberate HTTP responses unchanged. The broad handler below turns
        # anything it catches into a 500, which would report a deliberate 401/403/404/422
        # raised inside this block as an internal server error (issue #431).
        raise
    except (ReindexLockUnavailableError, RedisError, ConnectionError) as e:
        # "Cannot tell" is not "not running": answering 200 not_running here would
        # tell an admin a live reindex is idle. The detail stays generic on purpose.
        logger.error("Reindex stop refused, Redis unavailable: %s", type(e).__name__)
        raise HTTPException(
            status_code=503,
            detail="Reindex state is temporarily unavailable. Please try again shortly.",
        ) from e
    except Exception as e:
        logger.error("Failed to request reindex stop: %s", e, exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="An internal error occurred. Please try again.",
        ) from e


def _check_reindex_task_active(user_id: int) -> bool:
    """Whether a reindex coordinator is currently running for this user."""
    return _running_reindex_run_id(user_id) is not None


@router.get("/degraded-embeddings")
def get_degraded_embeddings(
    limit: int = Query(500, le=5000),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Preview the files stranded text-only by a neural-search degraded window (#626).

    Read-only survey — pairs with ``POST /search/reembed-degraded``, which requires this
    preview to be confirmed rather than dispatching on click.

    ``total_files`` is the size of what a re-embed would touch, which is deployment-wide, so
    the deployment-wide figures (``total_files`` and ``outside_scope_files``) go to a caller
    who holds the deployment-scope right (every admin in a single-tenant deployment, a
    ``super_admin`` otherwise). Any other admin of a multi-tenant deployment gets the counts
    of the files listed to them, and ``outside_scope_files`` is 0. In that deployment ``files``
    names only the files of the caller's active tenant (the caller's own rows, unless the
    request's bypass sees the whole scope). A single-tenant deployment lists everything.
    """
    from app.db.session_utils import session_scope
    from app.models.media import MediaFile
    from app.services.permission_service import org_scope_pred
    from app.services.search.embedding_provenance import survey_degraded_files

    files, truncated = survey_degraded_files(limit=limit)

    if not files:
        return {
            "total_files": 0,
            "truncated": False,
            "affected_users": 0,
            "outside_scope_files": 0,
            "files": [],
        }

    single_tenant = ctx.bypass.mode is TenancyMode.SINGLE
    file_uuids = [f.file_uuid for f in files]
    with session_scope() as db:
        query = db.query(MediaFile.uuid, MediaFile.filename).filter(MediaFile.uuid.in_(file_uuids))
        if not single_tenant:
            query = query.filter(org_scope_pred(MediaFile.organization_id, ctx.org_id))
            if not ctx.bypass.sees_all_in_scope:
                query = query.filter(MediaFile.user_id == current_user.id)
        titles = {str(row[0]): row[1] for row in query.all()}

    # Single-tenant: every surveyed file is listed, an unknown one under its uuid.
    listed = [f for f in files if single_tenant or f.file_uuid in titles]
    deployment_wide = ctx.bypass.deployment_scope_writable
    return {
        "total_files": len(files) if deployment_wide else len(listed),
        "truncated": truncated,
        "affected_users": len({f.user_id for f in listed}),
        "outside_scope_files": len(files) - len(listed) if deployment_wide else 0,
        "files": [
            {
                "file_uuid": f.file_uuid,
                "title": titles.get(f.file_uuid) or f.file_uuid,
                "user_id": f.user_id,
            }
            for f in listed
        ],
    }


@router.post("/reembed-degraded")
def trigger_reembed_degraded(
    limit: int = Query(500, le=5000),
    ctx: RequestContext = Depends(get_current_context),
    current_user: User = Depends(get_current_admin_user),
) -> dict[str, Any]:
    """Dispatch the operator-triggered re-embed of #626's degraded (text-only) files.

    Deployment-wide: it re-embeds every tenant's files, so it needs the deployment-scope
    right (every admin in a single-tenant deployment, a ``super_admin`` otherwise).

    Checks the task's own lock before dispatching, rather than dispatching into a lock it
    knows is already held — matches ``start_embedding_consistency_repair``'s shape in
    ``admin.py``.
    """
    ctx.bypass.require_deployment_scope("Re-embedding degraded files")
    from app.tasks.search_reembed_task import REEMBED_LOCK_KEY
    from app.tasks.search_reembed_task import reembed_degraded_files_task
    from app.utils.task_lock import task_lock_manager

    if task_lock_manager.is_locked(REEMBED_LOCK_KEY):
        return {
            "task_id": None,
            "status": "already_running",
            "message": "A re-embed of degraded files is already in progress.",
        }

    from app.services.search.embedding_provenance import survey_degraded_files

    files, _truncated = survey_degraded_files(limit=limit)
    if not files:
        return {
            "task_id": None,
            "status": "no_degraded_files",
            "message": "No degraded (text-only) files found to re-embed.",
        }

    result = reembed_degraded_files_task.apply_async(
        kwargs={"triggered_by": current_user.id, "limit": limit},
    )
    return {
        "task_id": str(result.id),
        "status": "started",
        "message": "Re-embedding started. Progress will be sent via WebSocket.",
    }


@router.get("/reindex/status")
def reindex_status(
    ctx: RequestContext = Depends(get_current_context),
) -> dict[str, Any]:
    """
    Check re-indexing status and index health.

    File counts cover the caller's own files in the ACTIVE tenant.

    Returns:
        Dict with total_files, indexed_files, pending info, current model.
    """
    from sqlalchemy import exists
    from sqlalchemy import select

    from app.db.session_utils import session_scope
    from app.models.media import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment
    from app.services.opensearch_service import opensearch_client
    from app.services.search.settings_service import get_search_embedding_settings
    from app.services.search.tenant_scope import org_filter_clauses
    from app.utils.db_helpers import owned_in_tenant

    current_user = ctx.user
    with session_scope() as db:
        # Only count completed files that have transcript segments (indexable)
        has_segments = exists(
            select(TranscriptSegment.id).where(TranscriptSegment.media_file_id == MediaFile.id)
        )
        total_files = (
            db.query(MediaFile)
            .filter(
                owned_in_tenant(MediaFile, user_id=current_user.id, organization_id=ctx.org_id),
                MediaFile.status == FileStatus.COMPLETED,
                has_segments,
            )
            .count()
        )

    # Count indexed files in OpenSearch chunks index (skip redundant exists check)
    indexed_files = 0
    last_indexed_at = None
    if opensearch_client:
        try:
            count_response = opensearch_client.search(
                index=settings.OPENSEARCH_CHUNKS_INDEX,
                body={
                    "size": 0,
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"user_id": current_user.id}},
                                *org_filter_clauses(ctx.org_id),
                                # G4, again: the number the admin UI shows as
                                # "indexed files" must count files with chunks.
                                chunk_plane_clause(),
                            ]
                        }
                    },
                    "aggs": {
                        "unique_files": {"cardinality": {"field": "file_uuid"}},
                        "last_indexed": {"max": {"field": "indexed_at"}},
                    },
                },
            )
            aggs = count_response.get("aggregations", {})
            indexed_files = aggs.get("unique_files", {}).get("value", 0)
            last_indexed_at = aggs.get("last_indexed", {}).get("value_as_string")
        except Exception as e:
            logger.exception(f"Error checking index status: {e}")

    # Check if a reindex task is actively running for this user, and which run.
    status_unavailable = False
    try:
        running_run_id = _running_reindex_run_id(current_user.id)
    except ReindexLockUnavailableError:
        running_run_id = None
        status_unavailable = True
    in_progress = running_run_id is not None

    # Check if stop has been requested — for THIS run. A flag naming a finished
    # run would otherwise show the panel a stop that cannot apply to what is
    # currently indexing (#691).
    stop_requested = in_progress and cancel_requested(current_user.id, running_run_id)

    current_model, current_dimension = get_search_embedding_settings()

    return {
        "total_files": total_files,
        "indexed_files": indexed_files,
        "pending_files": max(0, total_files - indexed_files),
        "in_progress": in_progress,
        "stop_requested": stop_requested,
        # True when Redis could not be read: in_progress/stop_requested are then
        # placeholders, not "idle". Clients must treat the run state as unknown.
        "status_unavailable": status_unavailable,
        "current_model": current_model,
        "current_dimension": current_dimension,
        "last_indexed_at": last_indexed_at,
    }
