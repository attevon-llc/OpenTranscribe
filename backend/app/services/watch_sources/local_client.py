"""Local mounted-folder watch-source client.

Lists media under ``WATCH_FOLDER_PATH / local_path`` with hard symlink-escape
and ``..`` traversal guards (every yielded path is validated against the
resolved root via ``os.path.realpath``). Downloads are no-ops — the file is read
in place; uploads write stitched output back to the folder.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import UTC
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.config import settings
from app.services.watch_sources.base import BaseWatchSourceClient
from app.services.watch_sources.base import RemoteFileInfo

if TYPE_CHECKING:
    from app.models.watch_source import WatchSource

logger = logging.getLogger(__name__)


class LocalWatchClient(BaseWatchSourceClient):
    """Watch a directory under the configured local mount."""

    def __init__(self, source: WatchSource) -> None:
        self._source = source
        self._recursive = bool(source.recursive)

    @property
    def root(self) -> Path:
        """Resolved, traversal-guarded watch directory for this source."""
        return Path(self._source.resolved_local_path)

    def test_connection(self) -> tuple[bool, str]:
        if not settings.WATCH_FOLDER_PATH:
            return False, "Local watch folder is not configured on the server"
        try:
            root = self.root
        except ValueError as e:
            return False, str(e)
        if not root.exists():
            return False, f"Path does not exist: {root}"
        if not root.is_dir():
            return False, f"Path is not a directory: {root}"
        if not os.access(root, os.R_OK):
            return False, f"Path is not readable: {root}"
        return True, f"OK — {root}"

    def _is_within_root(self, path: Path, root: Path) -> bool:
        real = Path(os.path.realpath(path))
        return real == root or root in real.parents

    def list_files(
        self,
        extensions: list[str] | None = None,
        recursive: bool = True,
        min_modified: datetime | None = None,
    ) -> list[RemoteFileInfo]:
        from app.services import watch_settings_service

        root = self.root
        results: list[RemoteFileInfo] = []
        stability_cutoff = time.time() - watch_settings_service.file_stability_seconds()

        iterator = root.rglob("*") if recursive else root.glob("*")
        for entry in iterator:
            try:
                # No symlink follow — guards against escaping the mount.
                if entry.is_symlink():
                    continue
                if not entry.is_file():
                    continue
                if not self._is_within_root(entry, root):
                    logger.warning("Skipping path escaping watch root: %s", entry)
                    continue
                if extensions and entry.suffix.lower() not in extensions:
                    continue
                stat = entry.stat()
                # Stability: skip files still being written.
                if stat.st_mtime > stability_cutoff:
                    continue
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
                if min_modified and mtime < min_modified:
                    continue
                results.append(
                    RemoteFileInfo(
                        path=str(entry),
                        name=entry.name,
                        size=stat.st_size,
                        modified_time=mtime,
                    )
                )
            except OSError as e:
                logger.debug("Skipping unreadable entry %s: %s", entry, e)
                continue
        return results

    def download_file(self, remote_path: str, local_path: str) -> int:
        """No-op copy for local sources — the file is read in place.

        ``remote_path`` IS a local path; we validate it stays within the root
        and return its size. The importer reads the original path directly.
        """
        src = Path(remote_path)
        if not self._is_within_root(src, self.root):
            raise ValueError(f"Refusing to read path outside watch root: {remote_path}")
        return src.stat().st_size

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        dest = Path(remote_path)
        # _is_within_root resolves via os.path.realpath, which handles a
        # not-yet-existing destination correctly (resolves the existing prefix,
        # keeps the rest literal) — no need to fall back to checking root against
        # itself when dest.parent doesn't exist yet. That fallback was a guard
        # bypass: it made the check trivially pass regardless of where dest pointed.
        if not self._is_within_root(dest, self.root):
            raise ValueError(f"Refusing to write outside watch root: {remote_path}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local_path, dest)
        return True

    def delete_file(self, remote_path: str) -> bool:
        """Delete an imported original (used when delete_after_import is set)."""
        src = Path(remote_path)
        if not self._is_within_root(src, self.root):
            raise ValueError(f"Refusing to delete path outside watch root: {remote_path}")
        try:
            src.unlink(missing_ok=True)
            return True
        except OSError as e:
            logger.warning("Failed to delete imported original %s: %s", remote_path, e)
            return False
