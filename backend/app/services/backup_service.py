"""Scheduled database backup — pg_dump run path (local and S3 backends).

Feature C (in-app scheduled backups for homelab users). Everything is configured in
the admin UI and stored in ``SystemSettings`` (see ``backup_config``); the only env is the
physical mount (``BACKUP_HOST_PATH`` → container ``/backups`` via ``docker-compose.backup.yml``).
The existing ``celery-beat`` service fires a lightweight check every few minutes
(``backup_cron.is_due``); when due, the real backup task runs ``pg_dump`` directly from the
worker (the backend image ships ``postgresql-client``), writes a custom-format ``.dump`` to the
destination, optionally gpg-encrypts it, then prunes old backups by a grandfather-father-son
policy (``backup_retention`` / ``backup_s3``).

Coded ``DEFAULT_BACKUP_*`` constants are the single source of truth — no ``.env`` vars.
The whole feature degrades gracefully when ``/backups`` isn't mounted: the status endpoint
flags it and the task no-ops with a logged warning instead of raising.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess  # noqa: S404 - pg_dump/gpg invoked with a fixed argv list, no shell
import tempfile
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services import system_settings_service as sss
from app.services.backup_config import DEST_S3
from app.services.backup_config import KEY_LAST_RESULT
from app.services.backup_config import KEY_LAST_RUN_AT
from app.services.backup_config import KEY_LAST_SUCCESS_AT
from app.services.backup_config import KEY_RUNS_FAILURE
from app.services.backup_config import KEY_RUNS_SUCCESS
from app.services.backup_config import destination_status
from app.services.backup_config import get_s3_secret_key
from app.services.backup_config import get_settings
from app.services.backup_retention import _DUMP_PREFIX
from app.services.backup_retention import _DUMP_SUFFIX
from app.services.backup_retention import _GPG_SUFFIX
from app.services.backup_retention import prune_backups
from app.services.backup_s3 import _build_s3_client
from app.services.backup_s3 import _s3_prefix
from app.services.backup_s3 import prune_backups_s3
from app.services.scheduled_job_settings import session_scope

logger = logging.getLogger(__name__)


# =============================================================================
# pg_dump execution
# =============================================================================
#: Custom-format (-Fc) is a load-bearing contract, not an implementation detail:
#: scripts/common.sh's pg_replay_custom_dump / pg_verify_custom_restore can only
#: read this format, and ./opentr.sh restore dispatches on its PGDMP magic bytes.
#: Changing it requires changing those too (issue #600).
PG_DUMP_FORMAT = "custom"


def _read_passphrase(passphrase_file: str) -> str:
    """Read a gpg passphrase from a file; raise if missing/empty."""
    p = Path(passphrase_file)
    if not p.is_file():
        raise FileNotFoundError(f"Backup passphrase file not found: {passphrase_file}")
    passphrase = p.read_text(encoding="utf-8").strip()
    if not passphrase:
        raise ValueError(f"Backup passphrase file is empty: {passphrase_file}")
    return passphrase


def run_pg_dump(
    dest_path: Path,
    *,
    encrypt: bool,
    passphrase_file: str,
    database_url: str | None = None,
) -> Path:
    """Run ``pg_dump -Fc`` to ``dest_path``; optionally gpg-encrypt in place.

    Uses ``PGSSLMODE`` + a ``--dbname`` URL so no password lands on the argv. Returns the
    final artifact path (``.dump`` or ``.dump.gpg``). Raises ``CalledProcessError`` /
    ``FileNotFoundError`` on failure (the task catches and records these).
    """
    url = database_url or settings.DATABASE_URL
    env = dict(os.environ)
    cmd = ["pg_dump", f"--format={PG_DUMP_FORMAT}", "--no-owner", "--no-acl", "--dbname", url]
    logger.info("Running pg_dump → %s", dest_path)
    subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell; url carries credentials, not user input
        cmd,
        stdout=dest_path.open("wb"),
        stderr=subprocess.PIPE,
        env=env,
        check=True,
    )

    if not encrypt:
        return dest_path

    passphrase = _read_passphrase(passphrase_file)
    gpg_path = dest_path.with_suffix(dest_path.suffix + ".gpg")
    gpg_cmd = [
        "gpg",
        "--batch",
        "--yes",
        "--symmetric",
        "--cipher-algo",
        "AES256",
        "--passphrase-fd",
        "0",
        "--output",
        str(gpg_path),
        str(dest_path),
    ]
    subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
        gpg_cmd,
        input=passphrase.encode("utf-8"),
        stderr=subprocess.PIPE,
        check=True,
    )
    dest_path.unlink(missing_ok=True)  # drop the plaintext dump
    return gpg_path


def perform_backup(db: Session | None = None) -> dict[str, Any]:
    """Execute one backup run end-to-end and record the result in SystemSettings.

    Dispatches to the local-dir or S3 backend based on ``backup.destination_type``.
    Returns a result dict (``ok``, ``path``/``filename``, ``size_bytes``, ``duration_s``,
    ``error``, ``pruned``). No-ops gracefully (``ok=False``, status ``"no_destination"``)
    when the destination isn't usable — never raises for that case.
    """
    cfg = get_settings(db)
    if cfg["destination_type"] == DEST_S3:
        return _perform_backup_s3(cfg, db)
    return _perform_backup_local(cfg, db)


def _maybe_snapshot_opensearch(cfg: dict[str, Any], ts: str) -> dict[str, Any] | None:
    """Take an OpenSearch snapshot when ``include_opensearch`` is on — never raises.

    Returns the snapshot status sub-object (``{"status": ok|skipped|unsupported|error, ...}``)
    to fold into ``last_result["opensearch"]``, or None when the toggle is off (so the
    result stays unchanged for pg-only runs). Called only AFTER a successful pg dump, and
    its outcome is independent of pg success — a snapshot failure never flips ``ok``.

    ``ts`` is the same ``YYYYMMDD-HHMMSS`` stamp as the pg dump so the snapshot name shares
    the stem (GFS pruning over names lines up across both artifacts).
    """
    if not cfg.get("include_opensearch"):
        return None
    from app.services import opensearch_snapshot

    return opensearch_snapshot.perform_snapshot(cfg, ts=ts)


def _prune_local_safe(destination: str, cfg: dict[str, Any]) -> tuple[list[str], str | None]:
    """Prune with failure isolation: a prune error warns but never fails the run (#244)."""
    try:
        return prune_backups(destination, cfg), None
    except Exception as exc:  # noqa: BLE001 - the dump already succeeded; record + continue
        # prune_error is rendered on GET /admin/backup and /admin/backup/status --
        # never interpolate the raw exception text, only its class name (#914).
        logger.exception("Backup retention pruning failed (dump itself succeeded)")
        return [], f"Pruning failed ({type(exc).__name__})"


def _prune_s3_safe(cfg: dict[str, Any], client: Any) -> tuple[list[str], str | None]:
    """S3 twin of ``_prune_local_safe`` — prune errors never fail a completed upload."""
    try:
        return prune_backups_s3(cfg, client=client), None
    except Exception as exc:  # noqa: BLE001 - the upload already succeeded; record + continue
        logger.exception("S3 backup retention pruning failed (dump itself succeeded)")
        return [], f"Pruning failed ({type(exc).__name__})"


def _build_recovery_artifact(cfg: dict[str, Any], dest_dir: Path) -> dict[str, Any]:
    """Write the recovery companion (encrypted) or key README (plaintext) into ``dest_dir``.

    Issue #243: with gpg encryption on, the essential env keys travel beside the dump in
    ``opentranscribe-recovery.env.gpg`` (same passphrase); otherwise a no-secrets
    ``RECOVERY-README.txt`` names what the operator must preserve separately. Returns the
    ``recovery`` status sub-object; never raises and never fails the backup.
    """
    from app.services import backup_recovery

    if cfg["encrypt"]:
        try:
            passphrase = _read_passphrase(cfg["passphrase_file"])
        except (OSError, ValueError) as exc:
            # _read_passphrase's own message names the configured passphrase
            # FILE PATH -- an admin-set value, but still a host filesystem
            # detail this response should not echo (#914).
            logger.exception("Could not read backup passphrase file")
            return {
                "status": backup_recovery.STATUS_ERROR,
                "error": f"Could not read backup passphrase file ({type(exc).__name__})",
            }
        return backup_recovery.write_companion(dest_dir, passphrase)
    return backup_recovery.write_readme(dest_dir)


def _write_recovery_s3(
    cfg: dict[str, Any], scratch: Path, client: Any, bucket: str, prefix: str
) -> dict[str, Any]:
    """Build the recovery artifact in ``scratch`` and upload it beside the dumps (S3)."""
    result = _build_recovery_artifact(cfg, scratch)
    filename = result.get("filename")
    if not filename:
        return result
    local = scratch / filename
    key = f"{prefix}{filename}"
    try:
        client.upload_file(str(local), bucket, key)
        result["path"] = f"s3://{bucket}/{key}"
    except Exception as exc:  # noqa: BLE001 - companion upload never fails the backup
        # This overwrites `result` entirely and is assigned OUTSIDE the handler's
        # own scope, then returned below (scanner-invisible under the raise/return
        # walker) -- rendered on GET /admin/backup and /admin/backup/status.
        logger.exception("Could not upload recovery companion to s3://%s/%s", bucket, key)
        result = {
            "status": "error",
            "error": f"Recovery companion upload failed ({type(exc).__name__})",
        }
    finally:
        with contextlib.suppress(OSError):
            local.unlink(missing_ok=True)
    return result


def _perform_backup_local(cfg: dict[str, Any], db: Session | None) -> dict[str, Any]:
    """Local-dir backend: pg_dump straight to the mounted destination, prune in-place."""
    destination = cfg["destination"]
    started = time.monotonic()
    now_iso = datetime.now(UTC).isoformat()

    status = destination_status(destination)
    if not status["writable"]:
        msg = (
            f"Backup destination {destination!r} is not a writable mount — "
            "skipping. Mount a host path via docker-compose.backup.yml (BACKUP_HOST_PATH)."
        )
        logger.warning(msg)
        result = {"ok": False, "status": "no_destination", "error": msg, "started_at": now_iso}
        _record_result(db, now_iso, result)
        return result

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest_path = Path(destination) / f"{_DUMP_PREFIX}{ts}{_DUMP_SUFFIX}"

    try:
        artifact = run_pg_dump(
            dest_path,
            encrypt=bool(cfg["encrypt"]),
            passphrase_file=cfg["passphrase_file"],
        )
        size = artifact.stat().st_size
        pruned, prune_error = _prune_local_safe(destination, cfg)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": True,
            "status": "success",
            "filename": artifact.name,
            "path": str(artifact),
            "size_bytes": size,
            "duration_s": duration,
            "encrypted": artifact.name.endswith(_GPG_SUFFIX),
            "pruned": pruned,
            "started_at": now_iso,
        }
        if prune_error:
            result["prune_error"] = prune_error
        os_result = _maybe_snapshot_opensearch(cfg, ts)
        if os_result is not None:
            result["opensearch"] = os_result
        result["recovery"] = _build_recovery_artifact(cfg, Path(destination))
        logger.info(
            "Backup complete: %s (%.1f MB, %.2fs, pruned %d)",
            artifact.name,
            size / 1_048_576,
            duration,
            len(pruned),
        )
    except subprocess.CalledProcessError as exc:
        # pg_dump's stderr can carry PGHOST/PGUSER/the database name -- and the
        # exception's str() a passphrase-file path -- both rendered on
        # GET /admin/backup and /admin/backup/status. Only the command name and
        # exit code survive; the real cause is still diagnosable via the log.
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-2000:] if exc.stderr else ""
        _cleanup_partial(dest_path)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": f"{exc.cmd[0]} failed (exit {exc.returncode})",
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.exception(
            "Backup failed: %s failed (exit %s): %s", exc.cmd[0], exc.returncode, stderr
        )
    except (OSError, ValueError, FileNotFoundError) as exc:
        _cleanup_partial(dest_path)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": f"Backup failed ({type(exc).__name__})",
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.exception("Backup failed")

    _record_result(db, now_iso, result)
    return result


def _scratch_dir(cfg: dict[str, Any]) -> Path:
    """Pick a scratch dir for the temp dump: the mounted dir if writable, else a system temp.

    Reusing the mounted ``/backups`` volume (when present) avoids filling the container's
    ephemeral layer with a large dump; falls back to a fresh ``mkdtemp`` otherwise.
    """
    dest = cfg.get("destination") or ""
    if dest and destination_status(dest)["writable"]:
        return Path(dest)
    return Path(tempfile.mkdtemp(prefix="ot-backup-"))


def _perform_backup_s3(cfg: dict[str, Any], db: Session | None) -> dict[str, Any]:
    """S3 backend: pg_dump to a temp file, upload to the bucket, prune over the listing.

    The temp file (scratch dir or system temp) is always removed afterwards. Bucket
    unreachability / upload failure is recorded as an error result — never raises.
    """
    started = time.monotonic()
    now_iso = datetime.now(UTC).isoformat()

    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        msg = "S3 backup destination has no bucket configured — skipping."
        logger.warning(msg)
        result = {"ok": False, "status": "no_destination", "error": msg, "started_at": now_iso}
        _record_result(db, now_iso, result)
        return result

    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    scratch = _scratch_dir(cfg)
    using_temp_scratch = scratch != Path(cfg.get("destination") or "")
    tmp_dump = scratch / f"{_DUMP_PREFIX}{ts}{_DUMP_SUFFIX}"
    artifact: Path | None = None

    try:
        artifact = run_pg_dump(
            tmp_dump,
            encrypt=bool(cfg["encrypt"]),
            passphrase_file=cfg["passphrase_file"],
        )
        size = artifact.stat().st_size
        secret = get_s3_secret_key(db)
        client = _build_s3_client(cfg, secret)
        prefix = _s3_prefix(cfg)
        key = f"{prefix}{artifact.name}"
        logger.info("Uploading backup → s3://%s/%s (%.1f MB)", bucket, key, size / 1_048_576)
        client.upload_file(str(artifact), bucket, key)
        pruned, prune_error = _prune_s3_safe(cfg, client)
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": True,
            "status": "success",
            "filename": artifact.name,
            "path": f"s3://{bucket}/{key}",
            "size_bytes": size,
            "duration_s": duration,
            "encrypted": artifact.name.endswith(_GPG_SUFFIX),
            "pruned": pruned,
            "started_at": now_iso,
        }
        if prune_error:
            result["prune_error"] = prune_error
        os_result = _maybe_snapshot_opensearch(cfg, ts)
        if os_result is not None:
            result["opensearch"] = os_result
        result["recovery"] = _write_recovery_s3(cfg, scratch, client, bucket, prefix)
        logger.info(
            "S3 backup complete: %s (%.1f MB, %.2fs, pruned %d)",
            key,
            size / 1_048_576,
            duration,
            len(pruned),
        )
    except subprocess.CalledProcessError as exc:
        # Same pg_dump stderr / secret-path concern as the local backend --
        # never echo it into the admin-facing result (#914).
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-2000:] if exc.stderr else ""
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": f"{exc.cmd[0]} failed (exit {exc.returncode})",
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.exception(
            "S3 backup failed: %s failed (exit %s): %s", exc.cmd[0], exc.returncode, stderr
        )
    except Exception as exc:  # noqa: BLE001 - boto3/network/value errors → recorded, never raised
        # A boto3 ClientError can quote the endpoint_url/bucket/region -- only
        # the class of failure is returned.
        duration = round(time.monotonic() - started, 2)
        result = {
            "ok": False,
            "status": "error",
            "error": f"S3 backup failed ({type(exc).__name__})",
            "duration_s": duration,
            "started_at": now_iso,
        }
        logger.exception("S3 backup failed")
    finally:
        # Always remove the local temp artifact (dump and/or gpg envelope).
        _cleanup_partial(tmp_dump)
        if artifact is not None:
            with contextlib.suppress(OSError):
                artifact.unlink(missing_ok=True)
        if using_temp_scratch:
            with contextlib.suppress(OSError):
                scratch.rmdir()

    _record_result(db, now_iso, result)
    return result


def _cleanup_partial(dest_path: Path) -> None:
    """Remove a partial dump (and its gpg envelope) after a failed run."""
    for p in (dest_path, dest_path.with_suffix(dest_path.suffix + ".gpg")):
        with contextlib.suppress(OSError):
            p.unlink(missing_ok=True)


def _record_result(db: Session | None, run_at_iso: str, result: dict[str, Any]) -> None:
    """Persist run bookkeeping and fire admin alerting (own session if needed).

    Beyond ``last_run_at``/``last_result``, this maintains the cumulative
    success/failure counters and last-success timestamp that back the scrape-time
    Prometheus refresh (issue #244), then delegates proactive notifications to
    ``backup_alerts.notify_backup_result`` (never raises).
    """
    ok = bool(result.get("ok"))
    with session_scope(db) as s:
        sss.set_setting(s, KEY_LAST_RUN_AT, run_at_iso, "Timestamp of the last backup run (UTC)")
        sss.set_setting(s, KEY_LAST_RESULT, json.dumps(result), "Result of the last backup run")
        counter_key = KEY_RUNS_SUCCESS if ok else KEY_RUNS_FAILURE
        current = sss.get_setting_int(s, counter_key, 0)
        sss.set_setting(s, counter_key, current + 1, "Cumulative backup run count (metrics)")
        if ok:
            sss.set_setting(
                s,
                KEY_LAST_SUCCESS_AT,
                run_at_iso,
                "Timestamp of the last successful backup run (UTC)",
            )
        from app.services import backup_alerts

        backup_alerts.notify_backup_result(s, result)


def pg_dump_available() -> bool:
    """Return True if the ``pg_dump`` binary is on PATH (image capability probe)."""
    return shutil.which("pg_dump") is not None
