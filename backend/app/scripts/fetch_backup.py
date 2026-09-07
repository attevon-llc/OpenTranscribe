"""Fetch a scheduled backup artifact out of its S3 destination (issue #600).

For an S3-destination backup, ``_perform_backup_s3`` always deletes the local artifact
after upload — the file exists **only in the bucket**. The credentials to reach that
bucket are the AES-256-GCM-encrypted ``backup.s3_secret_key`` row in ``SystemSettings``,
decryptable only via ``ENCRYPTION_KEY`` (``backup_service._get_s3_secret_key``). A host
shell script cannot obtain them — so this fetch step has to run **inside the backend
container**, where the app's DB session and encryption key are already available.

☠️ **This must run BEFORE ``./opentr.sh restore`` does anything destructive.** The
credentials needed to fetch the backup live inside the very database a restore is about
to drop. Fetch first, verify the file landed on disk, THEN restore. ``./opentr.sh restore
--from-s3 <name>`` wraps this ordering automatically; the manual two-step below is the
fallback when the backend container itself is not running.

Usage (inside the backend container, ``./opentr.sh shell backend`` or via
``docker compose exec -T backend``)::

    python -m app.scripts.fetch_backup --list
    python -m app.scripts.fetch_backup opentranscribe-20260827-030000.dump [--force]

Reuses ``backup_service.get_settings`` / ``_build_s3_client`` / ``_get_s3_secret_key`` /
``list_backups_s3`` — no new S3 client construction, no new credential handling. Writes
into ``cfg["destination"]`` (``/backups`` by default, mounted from ``BACKUP_HOST_PATH`` on
the host), refuses to overwrite an existing file without ``--force``, and verifies the
downloaded artifact (size against the object's reported length, magic bytes against our
own backup format) before leaving it in place — a truncated download that "restores
successfully" into an empty database is the exact failure family issue #600 is about.
Never prints the S3 secret; never writes it anywhere.

Session lifetime: this reads its plain-data settings/secret in a short session that is
CLOSED before the slow S3 network work (list/head/download) starts — never a session held
open across it. See ``backend/app/tasks/CLAUDE.md``'s session-lifetime rule and
``scripts/audit-session-lifetime.py``, the gate that catches a function which both accepts
a ``Session`` and does slow work.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from app.db.session_utils import session_scope
from app.services import backup_service as bs

logger = logging.getLogger("fetch_backup")


class FetchError(RuntimeError):
    """A fetch that must not proceed — always accompanied by a clear stderr message."""


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch_backup",
        description="Download a scheduled backup artifact from its configured S3 destination.",
    )
    parser.add_argument(
        "filename",
        nargs="?",
        default=None,
        help="Backup filename to fetch (as shown by --list), e.g. opentranscribe-<ts>.dump[.gpg]",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List backups available in the configured S3 destination and exit.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file at the destination.",
    )
    return parser.parse_args(argv)


def _looks_like_our_artifact(data: bytes, filename: str) -> bool:
    """Cheap magic-byte sanity check — catches a truncated/wrong-body download early.

    A plaintext ``.dump`` starts with the literal ``PGDMP`` magic bytes pg_dump's custom
    format always writes. A ``.dump.gpg`` envelope's first byte is a real OpenPGP packet
    header: every OpenPGP packet tag byte has its high bit set (RFC 4880 §4.2), which
    holds across GnuPG versions and cipher choices, unlike hardcoding one exact byte
    value. Neither shape can start with ``<`` — which is what a misconfigured/misrouted
    S3 request commonly returns instead of object bytes (an XML error body).
    """
    if not data:
        return False
    if data.startswith(b"<"):
        return False
    if filename.endswith(bs._GPG_SUFFIX):
        return bool(data[0] & 0x80)
    return data[:5] == b"PGDMP"


def _do_list(cfg: dict[str, Any]) -> int:
    """List available backups. Takes no session — ``list_backups_s3(cfg, db=None)`` opens
    its own short-lived session just for the secret lookup, closed before the slow S3
    listing begins (``backup_service._session``), so nothing is held open here either.
    """
    backups = bs.list_backups_s3(cfg, None)
    print(json.dumps(backups, indent=2))  # noqa: T201 - intentional CLI output
    return 0


def _fetch(cfg: dict[str, Any], secret: str | None, filename: str, *, force: bool) -> Path:
    """Download ``filename`` from the configured S3 destination into ``cfg["destination"]``.

    Takes the already-decrypted ``secret`` as plain data, never a ``Session`` — this
    function does the slow network work (head + download), and a function that accepts a
    ``Session`` AND does slow work is exactly the session-lifetime defect
    ``scripts/audit-session-lifetime.py`` exists to catch (see module docstring).

    Raises ``FetchError`` on any refusal (not S3-backed, unwritable destination, existing
    file without ``--force``, truncated/malformed download). Never partially leaves a
    corrupt file at the final path — writes to a sibling ``.part`` file and renames only
    after both size and magic-byte checks pass.
    """
    if cfg["destination_type"] != bs.DEST_S3:
        raise FetchError(
            f"backup.destination_type is {cfg['destination_type']!r}, not 's3' — there is "
            "nothing to fetch. A local-destination backup is already on disk."
        )

    dest_status = bs.destination_status(cfg["destination"])
    if not dest_status["writable"]:
        raise FetchError(
            f"destination {cfg['destination']!r} is not a writable mount — refusing to fetch. "
            "Start the backend with the --with-backup overlay (mounts BACKUP_HOST_PATH)."
        )

    target = Path(cfg["destination"]) / filename
    if target.exists() and not force:
        raise FetchError(f"{target} already exists — pass --force to overwrite.")

    bucket = (cfg.get("s3_bucket") or "").strip()
    if not bucket:
        raise FetchError("S3 backup destination has no bucket configured.")

    client = bs._build_s3_client(cfg, secret)
    key = f"{bs._s3_prefix(cfg)}{filename}"

    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - surface as a clean FetchError, never a traceback
        raise FetchError(f"could not find s3://{bucket}/{key}: {exc}") from exc
    expected_size = int(head.get("ContentLength", -1))

    part_path = target.with_name(target.name + ".part")
    logger.info("Downloading s3://%s/%s -> %s", bucket, key, target)
    try:
        client.download_file(bucket, key, str(part_path))
    except Exception as exc:  # noqa: BLE001 - surface as a clean FetchError, never a traceback
        part_path.unlink(missing_ok=True)
        raise FetchError(f"download of s3://{bucket}/{key} failed: {exc}") from exc

    actual_size = part_path.stat().st_size
    if expected_size >= 0 and actual_size != expected_size:
        part_path.unlink(missing_ok=True)
        raise FetchError(
            f"downloaded {actual_size} bytes but S3 reported {expected_size} — truncated "
            "or interrupted download. Refusing to leave a partial artifact at the destination."
        )

    with part_path.open("rb") as fh:
        head_bytes = fh.read(5)
    if not _looks_like_our_artifact(head_bytes, filename):
        part_path.unlink(missing_ok=True)
        raise FetchError(
            f"downloaded body for {filename!r} does not look like a pg_dump/gpg artifact "
            f"(first bytes: {head_bytes!r}) — refusing to leave it at the destination."
        )

    part_path.rename(target)
    return target


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args = _parse_args(argv)

    # Checked before opening a DB session: pure usage-error, no reason to touch the
    # database (real or test) just to reject a malformed invocation.
    if not args.list and not args.filename:
        logger.error("filename is required unless --list is given")
        return 2

    # Short read session: plain settings dict, session closed before any S3 network work.
    with session_scope() as db:
        cfg = bs.get_settings(db)

    if args.list:
        return _do_list(cfg)

    # A second short session, only when actually fetching — closed before the slow
    # head/download calls, never held across them.
    with session_scope() as secret_db:
        secret = bs._get_s3_secret_key(secret_db)

    try:
        target = _fetch(cfg, secret, args.filename, force=args.force)
    except FetchError as exc:
        logger.error("%s", exc)
        return 1

    print(json.dumps({"path": str(target)}))  # noqa: T201 - intentional CLI output
    logger.info("Fetched %s", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
