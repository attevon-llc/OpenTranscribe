"""OpenSearch snapshot helper for the scheduled-backup feature (issue #242).

When ``backup.include_opensearch`` is enabled, the scheduled backup ALSO takes an
OpenSearch snapshot alongside the ``pg_dump``. OpenSearch snapshots are a *convenience*
— every index is rebuildable from PostgreSQL — so this whole path degrades gracefully:
any failure (repository not registered, ``path.repo`` not allow-listed, OpenSearch
unreachable) is recorded as a clear status sub-object on ``backup.last_result`` and NEVER
affects the success of the PostgreSQL dump.

Repository model (homelab default): a filesystem (``fs``) snapshot repository whose
``location`` lives under the OpenSearch container's ``path.repo`` allow-list. The
``docker-compose.backup.yml`` overlay bind-mounts that path under ``BACKUP_HOST_PATH`` and
sets ``path.repo`` on the OpenSearch service, so snapshots land beside the ``.dump`` files
when the stack is started with ``--with-backup``. If ``path.repo`` is not configured the
``PUT _snapshot/<repo>`` call fails with a clear ``repository_exception`` and we record an
``unsupported`` status with setup instructions.

S3 repositories (``repository-s3`` plugin) are NOT installed in the shipped OpenSearch
image, so the S3 destination still snapshots to the fs repo (or records ``unsupported`` if
``path.repo`` isn't set). See ``docs-site/docs/operations/backup-restore.md``.

All snapshot names share the ``opentranscribe-YYYYMMDD-HHMMSS`` stem used by the pg dumps,
so the SAME GFS retention selector (``backup_service.select_backups_to_delete``) prunes them.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import UTC
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Repository name we own + manage idempotently.
REPO_NAME = "opentranscribe_backup"
# In-container snapshot location (relative to the OpenSearch path.repo allow-list root).
# docker-compose.backup.yml mounts BACKUP_HOST_PATH/opensearch-snapshots here and sets
# path.repo to the parent, so this location is inside the allow-list.
REPO_LOCATION = "/usr/share/opensearch/backups/opensearch-snapshots"

# Snapshot names: opentranscribe-YYYYMMDD-HHMMSS (no .dump suffix → distinct artifact,
# but the same timestamp stem so GFS pruning over names matches the pg dumps).
_SNAP_PREFIX = "opentranscribe-"
# OpenSearch snapshot names must be lowercase; reuse backup_service's regex shape.
_SNAP_RE = re.compile(r"^opentranscribe-(\d{8})-(\d{6})$")

# Bounded waits so a stuck snapshot can never hang the backup task.
_CREATE_TIMEOUT_S = 600  # wait_for_completion ceiling for the create call
_REQUEST_TIMEOUT_S = 60


def _client() -> Any | None:
    """Return the shared OpenSearch client, or None when disabled/unreachable."""
    from app.services.opensearch_service import get_opensearch_client

    return get_opensearch_client()


def is_reachable(client: Any | None = None) -> bool:
    """Cheap liveness probe — never raises."""
    client = client or _client()
    if client is None:
        return False
    try:
        return bool(client.ping())
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def ensure_repository(client: Any) -> None:
    """Register the fs snapshot repository if it doesn't already exist (idempotent).

    A no-op when the repository already exists with our location. Raises on failure
    (caller wraps and records the status) — the most common failure is ``path.repo``
    not being allow-listed on the OpenSearch container, which surfaces as a
    ``repository_exception``.
    """
    try:
        existing = client.snapshot.get_repository(repository=REPO_NAME)
    except Exception:  # noqa: BLE001 - "not found" raises; treat as needs-create
        existing = None

    if existing and REPO_NAME in existing:
        loc = existing[REPO_NAME].get("settings", {}).get("location")
        if loc == REPO_LOCATION:
            logger.debug("OpenSearch snapshot repository %s already registered", REPO_NAME)
            return

    logger.info("Registering OpenSearch snapshot repository %s → %s", REPO_NAME, REPO_LOCATION)
    client.snapshot.create_repository(
        repository=REPO_NAME,
        body={"type": "fs", "settings": {"location": REPO_LOCATION, "compress": True}},
        request_timeout=_REQUEST_TIMEOUT_S,
    )


def _snapshot_name(ts: str | None = None) -> str:
    """Build a timestamp-stemmed snapshot name (shared stem with the pg dump)."""
    stamp = ts or datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    return f"{_SNAP_PREFIX}{stamp}"


def list_snapshot_names(client: Any) -> list[str]:
    """Return our snapshot names in the repository (empty on any error)."""
    try:
        resp = client.snapshot.get(
            repository=REPO_NAME, snapshot="_all", request_timeout=_REQUEST_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - missing repo / unreachable → no names
        logger.debug("Could not list OpenSearch snapshots: %s", exc)
        return []
    names: list[str] = []
    for snap in resp.get("snapshots", []):
        name = snap.get("snapshot", "")
        if _SNAP_RE.match(name):
            names.append(name)
    return names


def prune_snapshots(client: Any, cfg: dict[str, Any]) -> list[str]:
    """Delete GFS-excess snapshots, reusing the pg-dump retention selector.

    The selector parses the ``opentranscribe-YYYYMMDD-HHMMSS`` stem off a ``.dump``
    filename, so we map each snapshot name to ``<name>.dump`` for the selection and back
    to the bare snapshot name for the DELETE — the *same* daily/weekly/monthly policy then
    applies identically to snapshots and pg dumps. Returns deleted snapshot names.
    """
    from app.services.backup_service import select_backups_to_delete

    names = list_snapshot_names(client)
    if not names:
        return []
    # The selector keys on the .dump-suffixed filename pattern; add/strip it here.
    dump_to_name = {f"{name}.dump": name for name in names}
    selected = select_backups_to_delete(
        list(dump_to_name),
        retention_daily=int(cfg["retention_daily"]),
        retention_weekly=int(cfg["retention_weekly"]),
        retention_monthly=int(cfg["retention_monthly"]),
    )
    to_delete = [dump_to_name[d] for d in selected]
    deleted: list[str] = []
    for name in to_delete:
        try:
            client.snapshot.delete(
                repository=REPO_NAME, snapshot=name, request_timeout=_REQUEST_TIMEOUT_S
            )
            deleted.append(name)
        except Exception as exc:  # noqa: BLE001 - one bad delete shouldn't abort the run
            logger.warning("Could not delete old OpenSearch snapshot %s: %s", name, exc)
    return deleted


def perform_snapshot(cfg: dict[str, Any], ts: str | None = None) -> dict[str, Any]:
    """Take one OpenSearch snapshot end-to-end; return a status sub-object.

    Status values:
      - ``ok``           snapshot created (``snapshot`` name + ``pruned`` list)
      - ``skipped``      OpenSearch disabled / unreachable
      - ``unsupported``  repository can't be registered (``path.repo`` not allow-listed)
      - ``error``        snapshot create/poll failed

    NEVER raises — the caller folds this into ``last_result`` without touching pg success.
    """
    started = time.monotonic()
    client = _client()
    if client is None or not is_reachable(client):
        return {"status": "skipped", "error": "OpenSearch is disabled or unreachable"}

    try:
        ensure_repository(client)
    except Exception as exc:  # noqa: BLE001 - most often path.repo not allow-listed
        msg = (
            "OpenSearch snapshot repository could not be registered "
            f"({exc}). Start the stack with --with-backup so the OpenSearch container "
            "gets path.repo + the snapshot bind-mount, or disable 'Include OpenSearch'."
        )
        logger.warning(msg)
        return {"status": "unsupported", "error": msg}

    name = _snapshot_name(ts)
    try:
        logger.info("Creating OpenSearch snapshot %s/%s", REPO_NAME, name)
        client.snapshot.create(
            repository=REPO_NAME,
            snapshot=name,
            body={"ignore_unavailable": True, "include_global_state": False},
            wait_for_completion=True,
            request_timeout=_CREATE_TIMEOUT_S,
        )
        pruned = prune_snapshots(client, cfg)
        duration = round(time.monotonic() - started, 2)
        logger.info(
            "OpenSearch snapshot complete: %s (%.2fs, pruned %d)", name, duration, len(pruned)
        )
        return {
            "status": "ok",
            "snapshot": name,
            "repository": REPO_NAME,
            "duration_s": duration,
            "pruned": pruned,
        }
    except Exception as exc:  # noqa: BLE001 - snapshot failure is recorded, never raised
        duration = round(time.monotonic() - started, 2)
        logger.error("OpenSearch snapshot failed: %s", exc)
        return {"status": "error", "error": str(exc), "duration_s": duration}


def snapshot_status() -> dict[str, Any]:
    """Reachability + most-recent-snapshot summary for the admin status endpoint.

    Cheap + graceful: reports whether OpenSearch is reachable, whether our repository is
    registered, and the newest snapshot name (if any). Never raises.
    """
    client = _client()
    reachable = is_reachable(client)
    result: dict[str, Any] = {
        "reachable": reachable,
        "repository_registered": False,
        "last_snapshot": None,
    }
    if not reachable or client is None:
        return result
    try:
        repos = client.snapshot.get_repository(repository=REPO_NAME)
        result["repository_registered"] = bool(repos and REPO_NAME in repos)
    except Exception:  # noqa: BLE001 - not registered yet
        result["repository_registered"] = False
    names = sorted(list_snapshot_names(client), reverse=True)
    if names:
        result["last_snapshot"] = names[0]
    return result
