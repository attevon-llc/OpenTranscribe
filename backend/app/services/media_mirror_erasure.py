"""Erasure-only targeted delete on the media mirror destination (issue #1353, decision D2).

The mirror engine (``media_mirror_engine``) NEVER deletes on its destination: the
``MirrorDestination`` protocol has no delete, and a source delete must not propagate. That
invariant stays true for every mirror run. This module is the one other path that touches the
destination, and it is narrow on purpose:

* it removes only keys under prefixes the CALLER derives from an erasure's captured ids
  (``user_<uid>/file_<fid>/``), never a pattern, never "everything not in the source";
* it is reachable only from ``gdpr_erasure_service`` (and counts through the same prefixes);
* the engine's classes are not extended, so the structural invariant test is untouched.

Without it, erased media persists in a copy that never rotates, indefinitely.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _destination() -> tuple[str, Any] | None:
    """``("local", root)`` / ``("s3", (client, bucket, prefix))``, or ``None`` when not mirrored."""
    from app.services import media_mirror_service as mm

    cfg = mm.get_settings(None)
    if not cfg.get("enabled"):
        return None
    if cfg["destination_type"] == mm.DEST_S3:
        bucket = (cfg.get("s3_bucket") or "").strip()
        if not bucket:
            return None
        prefix = (cfg.get("s3_prefix") or "").lstrip("/")
        return "s3", (mm.build_s3_client(cfg, None), bucket, prefix)
    destination = cfg.get("destination")
    return ("local", Path(destination)) if destination else None


def _safe(prefix: str) -> str:
    if prefix.startswith("/") or ".." in Path(prefix).parts:
        raise ValueError("unsafe mirror prefix")
    return prefix


def _s3_keys(client: Any, bucket: str, base: str, prefix: str) -> list[str]:
    keys: list[str] = []
    for page in client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{base}{prefix}"
    ):
        keys.extend(o["Key"] for o in page.get("Contents", []))
    return keys


def count_mirrored(prefixes: Iterable[str]) -> int:
    """Objects under ``prefixes`` at the mirror destination (0 when none is configured).

    Raises if the destination cannot be read, so the receipt reports unverified.
    """
    target = _destination()
    if target is None:
        return 0
    kind, handle = target
    total = 0
    for prefix in prefixes:
        _safe(prefix)
        if kind == "local":
            path = handle / prefix
            total += sum(1 for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0
        else:
            client, bucket, base = handle
            total += len(_s3_keys(client, bucket, base, prefix))
    return total


def erase_mirrored(prefixes: Iterable[str]) -> int:
    """Delete everything under the erasure's ``prefixes`` at the mirror destination.

    Returns how many objects were removed. Raises on a destination failure; the caller records it
    as an erasure error.
    """
    target = _destination()
    if target is None:
        return 0
    kind, handle = target
    removed = 0
    for prefix in prefixes:
        _safe(prefix)
        if kind == "local":
            path = handle / prefix
            if path.is_dir():
                removed += sum(1 for p in path.rglob("*") if p.is_file())
                shutil.rmtree(path)
        else:
            client, bucket, base = handle
            keys = _s3_keys(client, bucket, base, prefix)
            for start in range(0, len(keys), 1000):
                batch = [{"Key": k} for k in keys[start : start + 1000]]
                client.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            removed += len(keys)
    return removed
