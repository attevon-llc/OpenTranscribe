"""Inspection of the Hugging Face hub cache for the local Whisper catalog.

One resolver for the cache directory, so discovery and the downloader can never look at
different places. A model counts as downloaded only when it is *complete*: a half-finished
download leaves ``blobs/<etag>.incomplete`` files and, until the final rename, no snapshot.
"""

from __future__ import annotations

import logging
import os
import shutil
from collections.abc import Iterable
from pathlib import Path

from huggingface_hub import constants as hf_constants

logger = logging.getLogger(__name__)

INCOMPLETE_SUFFIX = ".incomplete"
# The files faster-whisper cannot load a model without.
REQUIRED_SNAPSHOT_FILES: tuple[str, ...] = ("config.json", "model.bin")


def hub_cache_dir() -> Path:
    """Return the Hugging Face hub cache directory (``HF_HUB_CACHE`` or ``HF_HOME/hub``)."""
    return Path(hf_constants.HF_HUB_CACHE)


def repo_dir(repo_id: str) -> Path:
    """Return the cache directory of a repository: ``models--<org>--<name>``."""
    return hub_cache_dir() / f"models--{repo_id.replace('/', '--')}"


def _incomplete_blobs(repo_id: str) -> list[Path]:
    blobs = repo_dir(repo_id) / "blobs"
    if not blobs.is_dir():
        return []
    return [p for p in blobs.iterdir() if p.name.endswith(INCOMPLETE_SUFFIX)]


def has_partial(repo_id: str) -> bool:
    """Return True when the repository has any in-flight ``*.incomplete`` blob."""
    return bool(_incomplete_blobs(repo_id))


def is_complete(repo_id: str) -> bool:
    """Return True when the repository's snapshot is whole and loadable.

    ``refs/main`` must name a snapshot holding every file in
    :data:`REQUIRED_SNAPSHOT_FILES` (symlinks that resolve to a blob), and no ``*.incomplete``
    blob may remain.
    """
    root = repo_dir(repo_id)
    try:
        revision = (root / "refs" / "main").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not revision or "/" in revision or revision.startswith("."):
        return False
    snapshot = root / "snapshots" / revision
    if not all((snapshot / name).exists() for name in REQUIRED_SNAPSHOT_FILES):
        return False
    return not has_partial(repo_id)


def present_bytes(repo_id: str) -> int:
    """Return the total size of the repository's complete blobs."""
    blobs = repo_dir(repo_id) / "blobs"
    if not blobs.is_dir():
        return 0
    total = 0
    for p in blobs.iterdir():
        if p.name.endswith(INCOMPLETE_SUFFIX) or not p.is_file():
            continue
        total += p.stat().st_size
    return total


def cache_writable() -> bool:
    """Return True when the hub cache directory exists (or can be created) and is writable."""
    path = hub_cache_dir()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def free_bytes() -> int:
    """Return free bytes on the filesystem holding the hub cache, or 0 when unreadable."""
    probe = hub_cache_dir()
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def remove_incomplete(repo_id: str, blob_names: Iterable[str] | None = None) -> int:
    """Delete in-flight ``*.incomplete`` blobs of one repository.

    Never touches a complete blob or another repository.

    Args:
        repo_id: Repository whose partial downloads to remove.
        blob_names: Restrict to these blob names (without the ``.incomplete`` suffix). ``None``
            removes every ``*.incomplete`` in the repository.

    Returns:
        How many files were removed.
    """
    wanted = None if blob_names is None else {f"{n}{INCOMPLETE_SUFFIX}" for n in blob_names}
    removed = 0
    for path in _incomplete_blobs(repo_id):
        if wanted is not None and path.name not in wanted:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("Could not remove %s: %s", path.name, type(exc).__name__)
    return removed
