"""Storage orphan sweep: object-store keys whose ``media_file`` row no longer exists.

The app's own delete paths remove a file's objects with its row. Nothing handled the
reverse case: a row removed OUT OF BAND (raw SQL cleanup, a database restore or reset
against a persisted object store) leaves every object behind forever (issue #1212).
This is the storage-plane twin of ``opensearch_integrity_task`` and keeps its shape —
dry run first, delete only on explicit confirm, the same two refusals, and a re-check
against Postgres immediately before anything is removed.

What counts as an orphan: an object whose key matches ``user_<uid>/file_<fid>/...``
(original, thumbnails, playback rendition, derived/masked caches, waveform — all of them
live under that prefix) and whose ``<fid>`` is not a ``media_file.id``. Everything else
is out of scope and never listed: ``media/...`` and ``user_<uid>/youtube_<id>/...`` keys,
bulk exports, avatars, backups and every other bucket.

Refusals, same semantics as the index sweep:

* **Empty Postgres side** (no ``media_file`` rows at all) — a database restored empty is
  indistinguishable from an install with no files, and every object would be an orphan.
  Never overridable, force or not.
* **Ratio guard** — more than 10% of the file-shaped objects (above a floor of 25) needs
  ``force``.

Safety beyond the refusals: a file group with ANY object younger than the grace period is
skipped whole (an upload in flight has its row from ``/files/prepare`` and is not an
orphan anyway; this also covers the window between a row delete and its own cleanup), and
each file id is re-asked of Postgres right before its objects are removed.
"""

import json
import logging
import re
import time
from datetime import UTC
from datetime import datetime
from typing import Any

from app.core.celery import celery_app
from app.core.config import settings
from app.core.constants import NOTIFICATION_TYPE_DATA_INTEGRITY_COMPLETE
from app.core.constants import CPUPriority
from app.core.redis import get_redis
from app.tasks.opensearch_integrity_task import _exceeds_delete_ratio
from app.tasks.opensearch_integrity_task import _file_ids_present
from app.tasks.opensearch_integrity_task import _get_all_file_ids_from_db
from app.utils.websocket_notify import send_ws_event

logger = logging.getLogger(__name__)

_REDIS_LOCK_KEY = "storage_orphan_sweep_running"
_REDIS_LAST_RUN_KEY = "storage_orphan_sweep_last_run"

#: Every object a file owns lives here (``utils/filename.py``, ``utils/thumbnail.py``,
#: ``tasks/thumbnail.py``, the ``.playback.m4a`` rendition and the derived caches).
_FILE_KEY_RE = re.compile(r"^user_(\d+)/file_(\d+)/")

#: Objects younger than this are never touched. An hour is far above the time between a
#: row delete and its own object cleanup, and above any single upload's write window.
GRACE_SECONDS = 3600

#: How many orphaned files one result lists by name (the rest are counted). Keeps the
#: result, which is stored in Redis and pushed over the WebSocket, bounded.
ORPHAN_LISTING_CAP = 200

#: Objects per ``remove_objects`` call.
_DELETE_BATCH = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _empty_result(dry_run: bool, force: bool) -> dict[str, Any]:
    return {
        "dry_run": dry_run,
        "force": force,
        "refused": None,
        "total_objects": 0,
        "total_bytes": 0,
        "orphan_files": 0,
        "orphan_objects": 0,
        "orphan_bytes": 0,
        "orphans": [],
        "orphans_truncated": False,
        "skipped_recent_files": 0,
        "rechecked_present": 0,
        "deleted_files": 0,
        "deleted_objects": 0,
        "deleted_bytes": 0,
        "failed_objects": 0,
        "error": None,
    }


def _collect_candidates(
    client: Any, bucket: str, valid_ids: set[int], result: dict[str, Any]
) -> dict[int, dict[str, Any]]:
    """List ``user_*`` keys and group the ones whose file id is absent from ``valid_ids``.

    Returns:
        ``{file_id: {"user_id", "names", "bytes", "newest"}}`` for orphan candidates.
        Existing files' objects are only counted into ``total_*``, never retained, so
        memory is bounded by the orphan set.
    """
    candidates: dict[int, dict[str, Any]] = {}
    for obj in client.list_objects(bucket, prefix="user_", recursive=True):
        name = str(obj.object_name)
        match = _FILE_KEY_RE.match(name)
        if match is None:
            continue
        size = int(obj.size or 0)
        result["total_objects"] += 1
        result["total_bytes"] += size
        file_id = int(match.group(2))
        if file_id in valid_ids:
            continue
        group = candidates.setdefault(
            file_id,
            {"user_id": int(match.group(1)), "names": [], "bytes": 0, "newest": None},
        )
        group["names"].append(name)
        group["bytes"] += size
        modified = getattr(obj, "last_modified", None)
        if modified is not None and (group["newest"] is None or modified > group["newest"]):
            group["newest"] = modified
    return candidates


def _is_recent(group: dict[str, Any], now: datetime) -> bool:
    """True when the group has an object inside the grace period, or an unknown age."""
    newest = group["newest"]
    if newest is None:
        return True  # cannot prove it is old: keep it
    if newest.tzinfo is None:
        newest = newest.replace(tzinfo=UTC)
    return bool((now - newest).total_seconds() < GRACE_SECONDS)


def _remove_group(client: Any, bucket: str, names: list[str]) -> int:
    """Remove ``names``; return how many failed. Never raises on a per-object error."""
    from minio.deleteobjects import DeleteObject

    failed = 0
    for i in range(0, len(names), _DELETE_BATCH):
        batch = names[i : i + _DELETE_BATCH]
        for err in client.remove_objects(bucket, (DeleteObject(n) for n in batch)):
            failed += 1
            logger.warning("Storage orphan sweep: could not remove an object: %s", err.code)
    return failed


def run_storage_orphan_sweep(dry_run: bool = True, force: bool = False) -> dict[str, Any]:
    """Find, and unless ``dry_run`` remove, storage objects of files that no longer exist.

    Args:
        dry_run: Report only. The refusals are still evaluated, so a dry run shows what
            a real run would do.
        force: Override the ratio guard. Never overrides the empty-Postgres refusal.

    Returns:
        Counters and a capped listing (see :func:`_empty_result`). ``error`` is a fixed
        code, never exception text; the cause goes to the server log.
    """
    from app.services.minio_service import minio_client

    result = _empty_result(dry_run, force)
    bucket = settings.MEDIA_BUCKET_NAME

    valid_ids = _get_all_file_ids_from_db()
    if not valid_ids:
        result["refused"] = "empty_valid_set"
        logger.error(
            "Refusing the storage orphan sweep: Postgres holds no media_file rows, which "
            "would make every object an orphan. An empty and a lost database look identical."
        )
        return result

    try:
        candidates = _collect_candidates(minio_client, bucket, valid_ids, result)
    except Exception:
        logger.exception("Storage orphan sweep: listing the bucket failed")
        result["error"] = "storage_listing_failed"
        return result

    now = _now()
    fresh = {fid: g for fid, g in candidates.items() if not _is_recent(g, now)}
    result["skipped_recent_files"] = len(candidates) - len(fresh)

    if fresh:
        present = {int(i) for i in _file_ids_present(fresh)}
        result["rechecked_present"] = len(present)
        fresh = {fid: g for fid, g in fresh.items() if fid not in present}

    ordered = sorted(fresh)
    result["orphan_files"] = len(ordered)
    result["orphan_objects"] = sum(len(fresh[f]["names"]) for f in ordered)
    result["orphan_bytes"] = sum(fresh[f]["bytes"] for f in ordered)
    result["orphans"] = [
        {
            "file_id": f,
            "user_id": fresh[f]["user_id"],
            "objects": len(fresh[f]["names"]),
            "bytes": fresh[f]["bytes"],
        }
        for f in ordered[:ORPHAN_LISTING_CAP]
    ]
    result["orphans_truncated"] = len(ordered) > ORPHAN_LISTING_CAP

    if not ordered:
        return result

    if _exceeds_delete_ratio(result["total_objects"], result["orphan_objects"]) and not force:
        result["refused"] = "ratio_guard"
        logger.error(
            "Refusing to delete %d of %d storage object(s): over the single-sweep limit. "
            "Verify PostgreSQL is intact, then re-run with force if the orphans are real.",
            result["orphan_objects"],
            result["total_objects"],
        )
        return result

    if dry_run:
        return result

    for file_id in ordered:
        # Immediately before this file's objects go: ask Postgres again. A row that
        # appeared since the snapshot means the file exists after all.
        if _file_ids_present([file_id]):
            result["rechecked_present"] += 1
            continue
        group = fresh[file_id]
        failed = _remove_group(minio_client, bucket, group["names"])
        removed = len(group["names"]) - failed
        result["failed_objects"] += failed
        result["deleted_objects"] += removed
        if failed == 0:
            result["deleted_files"] += 1
            result["deleted_bytes"] += group["bytes"]
    return result


def _audit_completion(user_id: int | None, result: dict[str, Any]) -> None:
    """Record the outcome with counters only: no keys, file ids, names or error text."""
    from app.auth.audit import AuditEventType
    from app.auth.audit import AuditOutcome
    from app.auth.audit import audit_logger

    failed = bool(result.get("error")) or bool(result.get("refused"))
    audit_logger.log(
        event_type=AuditEventType.ADMIN_DATA_INTEGRITY_STORAGE_PURGE,
        outcome=AuditOutcome.FAILURE if failed else AuditOutcome.SUCCESS,
        user_id=user_id,
        details={
            "action": "storage_purge_completed",
            "force": bool(result.get("force")),
            "refused": result.get("refused"),
            "error": result.get("error"),
            "orphan_files": result.get("orphan_files", 0),
            "orphan_objects": result.get("orphan_objects", 0),
            "orphan_bytes": result.get("orphan_bytes", 0),
            "deleted_files": result.get("deleted_files", 0),
            "deleted_objects": result.get("deleted_objects", 0),
            "deleted_bytes": result.get("deleted_bytes", 0),
            "failed_objects": result.get("failed_objects", 0),
            "skipped_recent_files": result.get("skipped_recent_files", 0),
            "rechecked_present": result.get("rechecked_present", 0),
        },
    )


@celery_app.task(name="storage_orphan_sweep", priority=CPUPriority.MAINTENANCE)
def storage_orphan_sweep_task(
    user_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Celery task: delete the storage objects of files that no longer exist.

    Only ever dispatched by ``POST /admin/data-integrity/storage`` after an explicit
    confirm. Guarded by a Redis lock against concurrent runs.
    """
    r = get_redis()
    if not r.set(_REDIS_LOCK_KEY, "1", nx=True, ex=3600):
        return {"status": "already_running"}

    start = time.time()
    try:
        try:
            result = run_storage_orphan_sweep(dry_run=False, force=force)
        except Exception:
            logger.exception("Storage orphan sweep failed")
            result = _empty_result(False, force)
            result["error"] = "sweep_failed"

        r.set(
            _REDIS_LAST_RUN_KEY,
            json.dumps(
                {
                    "timestamp": time.time(),
                    "results": result,
                    "duration_seconds": round(time.time() - start, 1),
                }
            ),
            ex=86400 * 7,
        )
        _audit_completion(user_id, result)
        if user_id is not None:
            send_ws_event(
                user_id,
                NOTIFICATION_TYPE_DATA_INTEGRITY_COMPLETE,
                {
                    "scope": "storage",
                    "status": "error" if result.get("error") else "completed",
                    "results": result,
                },
            )
        return result
    finally:
        r.delete(_REDIS_LOCK_KEY)


def get_storage_sweep_status() -> dict[str, Any]:
    """Running flag and the last run's result, for the admin panel."""
    r = get_redis()
    raw = r.get(_REDIS_LAST_RUN_KEY)
    return {
        "running": bool(r.exists(_REDIS_LOCK_KEY)),
        "last_run": json.loads(raw) if raw else None,
    }


def get_storage_orphan_counts() -> dict[str, Any]:
    """Dry run: what a sweep would remove, deleting nothing."""
    return run_storage_orphan_sweep(dry_run=True)
