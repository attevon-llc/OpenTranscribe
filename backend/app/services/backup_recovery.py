"""Recovery-key companion for scheduled backups (issue #243).

A ``pg_dump`` contains AES-256-GCM *ciphertext* whose master key (``ENCRYPTION_KEY``)
lives only in ``.env`` — restore onto a host with a different key and every encrypted
column (user API keys, watch-source credentials, email passwords, MFA secrets, the S3
backup secret itself) is permanently undecryptable. The same applies to
``JWT_SECRET_KEY`` (session continuity) and ``MINIO_KMS_SECRET_KEY`` (MinIO at-rest
encryption). This module makes each backup destination self-describing about those keys:

- **gpg encryption ON**: write ``opentranscribe-recovery.env.gpg`` beside the dumps —
  the essential env keys, gpg-symmetric-encrypted with the *same* passphrase as the
  dumps. The passphrase (kept in a password manager, never on the backup media) then
  unlocks both the dump and its keys, so the destination alone suffices for a restore.
- **gpg encryption OFF**: plaintext keys must never sit next to a plaintext dump.
  Write ``RECOVERY-README.txt`` naming the keys the operator has to preserve separately
  (values redacted to fingerprints); the caller surfaces a one-time admin warning.

One canonical companion per destination, refreshed on every successful run: the keys
must match the *current* database ciphertext, so a single always-current copy is the
correct semantic — a stale companion could not decrypt the newest dump anyway. Neither
filename matches the ``opentranscribe-<ts>.dump`` pattern, so GFS retention and the
backup listing ignore both.

Key VALUES are never logged — log lines carry only key names and fingerprints.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import subprocess  # noqa: S404 - gpg invoked with a fixed argv list, no shell
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

# Canonical companion filenames (one per destination, overwritten each run).
COMPANION_NAME = "opentranscribe-recovery.env.gpg"
README_NAME = "RECOVERY-README.txt"

# Statuses folded into ``last_result["recovery"]``.
STATUS_KEYS_INCLUDED = "keys_included"
STATUS_README_WRITTEN = "readme_written"
STATUS_ERROR = "error"


def key_fingerprint(value: str) -> str:
    """Return a short SHA-256 fingerprint identifying a key without revealing it."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def collect_key_material() -> dict[str, str]:
    """Return the env keys a restored database cannot live without.

    ``MINIO_KMS_SECRET_KEY`` is optional (only present when MinIO at-rest encryption
    is configured) and is read from the environment — it is a MinIO-service setting,
    not part of the backend's typed config.
    """
    material = {
        "ENCRYPTION_KEY": settings.ENCRYPTION_KEY,
        "JWT_SECRET_KEY": settings.JWT_SECRET_KEY,
    }
    minio_kms = os.environ.get("MINIO_KMS_SECRET_KEY", "").strip()
    if minio_kms:
        material["MINIO_KMS_SECRET_KEY"] = minio_kms
    return material


def build_recovery_text() -> str:
    """Build the plaintext (pre-gpg) companion body: env-format keys + instructions."""
    material = collect_key_material()
    lines = [
        "# OpenTranscribe recovery keys — KEEP SECRET",
        f"# Generated: {datetime.now(UTC).isoformat()}",
        "#",
        "# Restore: put these values into .env on the target host BEFORE starting the",
        "# stack, then restore the matching opentranscribe-*.dump. Without ENCRYPTION_KEY",
        "# every encrypted column in the dump is permanently undecryptable.",
        "#",
    ]
    lines += [f"# {name} fingerprint: {key_fingerprint(value)}" for name, value in material.items()]
    lines += [f"{name}={value}" for name, value in material.items()]
    return "\n".join(lines) + "\n"


def build_readme_text() -> str:
    """Build the no-secrets README written when backups are not gpg-encrypted."""
    fingerprints = "\n".join(
        f"  {name}: fingerprint {key_fingerprint(value)}"
        for name, value in collect_key_material().items()
    )
    return (
        "OpenTranscribe backup recovery notes\n"
        f"Generated: {datetime.now(UTC).isoformat()}\n"
        "\n"
        "THE DUMPS IN THIS FOLDER ARE NOT ENOUGH TO RESTORE OPENTRANSCRIBE.\n"
        "\n"
        "The database encrypts sensitive columns (API keys, watch-source credentials,\n"
        "email passwords, MFA secrets) with a master key that lives ONLY in the .env\n"
        "file of the host that wrote these dumps. Because these backups are not\n"
        "encrypted, that key material is deliberately NOT stored here.\n"
        "\n"
        "You must preserve the following .env values separately (password manager or\n"
        "secrets vault), or a restored dump will be permanently undecryptable:\n"
        "\n"
        f"{fingerprints}\n"
        "\n"
        "The fingerprints above (SHA-256, truncated) let you verify during a restore\n"
        "drill that the keys you saved match the ones that wrote this data.\n"
        "\n"
        "Tip: enable backup encryption (Settings -> Backups -> Encrypt) and these keys\n"
        "will be written here automatically as opentranscribe-recovery.env.gpg,\n"
        "protected by your gpg passphrase.\n"
    )


def write_companion(dest_dir: Path, passphrase: str) -> dict[str, Any]:
    """Write the gpg-encrypted key companion into ``dest_dir`` (never raises).

    The plaintext is staged as a 0600 temp file in the same directory, encrypted with
    ``gpg --symmetric`` using the backup passphrase, then unlinked. Returns a status
    dict for ``last_result["recovery"]``.
    """
    tmp_path = dest_dir / f".{COMPANION_NAME}.tmp"
    out_path = dest_dir / COMPANION_NAME
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(build_recovery_text())
        cmd = [
            "gpg",
            "--batch",
            "--yes",
            "--symmetric",
            "--cipher-algo",
            "AES256",
            "--passphrase-fd",
            "0",
            "--output",
            str(out_path),
            str(tmp_path),
        ]
        subprocess.run(  # noqa: S603  # nosec B603 - fixed argv, no shell
            cmd,
            input=passphrase.encode("utf-8"),
            stderr=subprocess.PIPE,
            check=True,
        )
        logger.info("Recovery key companion written: %s", out_path.name)
        return {"status": STATUS_KEYS_INCLUDED, "filename": COMPANION_NAME}
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or b"").decode("utf-8", "replace")[-500:] if exc.stderr else ""
        msg = f"gpg failed (exit {exc.returncode}): {stderr}"
        logger.warning("Could not write recovery key companion: %s", msg)
        return {"status": STATUS_ERROR, "error": msg}
    except OSError as exc:
        logger.warning("Could not write recovery key companion: %s", exc)
        return {"status": STATUS_ERROR, "error": str(exc)}
    finally:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def write_readme(dest_dir: Path) -> dict[str, Any]:
    """Write/refresh the no-secrets ``RECOVERY-README.txt`` in ``dest_dir`` (never raises)."""
    out_path = dest_dir / README_NAME
    try:
        out_path.write_text(build_readme_text(), encoding="utf-8")
        return {"status": STATUS_README_WRITTEN, "filename": README_NAME}
    except OSError as exc:
        logger.warning("Could not write %s: %s", README_NAME, exc)
        return {"status": STATUS_ERROR, "error": str(exc)}
