"""Backend for the local watch-folder picker in the UI.

Lists subdirectories under ``WATCH_FOLDER_PATH`` so the UI can browse the mount
(including NAS auto-mounts that appear without a restart) when configuring a
local source. Relative paths only; no symlink follow; ``..`` traversal rejected.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from app.core.config import settings
from app.schemas.watch_source import DirectoryEntry
from app.schemas.watch_source import DirectoryListResponse

logger = logging.getLogger(__name__)


def _root() -> Path:
    if not settings.WATCH_FOLDER_PATH:
        raise ValueError("WATCH_FOLDER_PATH is not configured")
    return Path(settings.WATCH_FOLDER_PATH).resolve()


def validate_path(relative_path: str) -> Path:
    """Resolve a UI-supplied relative path under the watch root, guarded.

    Raises ``ValueError`` on traversal/symlink escape or a non-directory.
    """
    rel = (relative_path or "").strip().lstrip("/")
    if ".." in rel.split("/"):
        raise ValueError("Path must not contain '..' traversal segments")
    root = _root()
    candidate = Path(os.path.realpath(root / rel))
    if candidate != root and root not in candidate.parents:
        raise ValueError("Resolved path escapes the watch root")
    if not candidate.exists() or not candidate.is_dir():
        raise ValueError("Path is not an existing directory")
    return candidate


def list_directories(relative_path: str = "") -> DirectoryListResponse:
    """Return immediate subdirectories of ``relative_path`` under the root."""
    root = _root()
    current = validate_path(relative_path)
    rel_current = os.path.relpath(current, root)
    rel_current = "" if rel_current == "." else rel_current

    parent_path: str | None = None
    if rel_current:
        parent = os.path.dirname(rel_current)
        parent_path = parent  # "" means the root

    entries: list[DirectoryEntry] = []
    try:
        for child in sorted(current.iterdir(), key=lambda p: p.name.lower()):
            if child.is_symlink() or not child.is_dir():
                continue
            rel = os.path.relpath(child, root)
            entries.append(DirectoryEntry(name=child.name, path=rel))
    except OSError as e:
        logger.warning("Failed to list directories under %s: %s", current, e)

    return DirectoryListResponse(
        current_path=rel_current,
        parent_path=parent_path,
        directories=entries,
    )
