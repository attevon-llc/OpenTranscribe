"""SMB/CIFS watch-source client (smbprotocol).

Pure-Python SMB2/3 — no system ``mount`` / ``cifs-utils`` and runs as the
non-root container user. Walks the configured share path, copies files to a
local temp for import, and can write stitched output back.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC
from datetime import datetime

from app.services.watch_sources.base import BaseWatchSourceClient
from app.services.watch_sources.base import RemoteFileInfo

logger = logging.getLogger(__name__)

_CHUNK = 64 * 1024


class SMBWatchClient(BaseWatchSourceClient):
    """Watch an SMB/CIFS share path."""

    def __init__(
        self,
        *,
        server: str | None,
        share: str | None,
        path: str = "/",
        username: str | None = None,
        password: str | None = None,
        domain: str | None = None,
        port: int = 445,
    ) -> None:
        if not server or not share:
            raise ValueError("SMB source requires server and share")
        self._server = server
        self._share = share.strip("/")
        self._path = (path or "/").strip("/")
        self._username = username
        self._password = password
        self._domain = domain
        self._port = port or 445
        self._registered = False

    @property
    def _base_unc(self) -> str:
        """UNC root for the configured share path."""
        unc = f"\\\\{self._server}\\{self._share}"
        if self._path:
            unc += "\\" + self._path.replace("/", "\\")
        return unc

    def _register(self) -> None:
        if self._registered:
            return
        import smbclient

        smbclient.register_session(
            self._server,
            username=self._username,
            password=self._password,
            port=self._port,
        )
        self._registered = True

    def test_connection(self) -> tuple[bool, str]:
        try:
            import smbclient

            self._register()
            # Listing the root path validates auth + path existence.
            smbclient.listdir(self._base_unc)
            return True, f"OK — \\\\{self._server}\\{self._share} reachable"
        except Exception as e:  # noqa: BLE001
            return False, f"SMB connection failed: {e}"

    def list_files(
        self,
        extensions: list[str] | None = None,
        recursive: bool = True,
        min_modified: datetime | None = None,
    ) -> list[RemoteFileInfo]:
        import smbclient

        self._register()
        results: list[RemoteFileInfo] = []
        base = self._base_unc

        walker = smbclient.walk(base) if recursive else [(base, [], smbclient.listdir(base))]
        for dirpath, _dirnames, filenames in walker:
            for fname in filenames:
                if extensions and os.path.splitext(fname)[1].lower() not in extensions:
                    continue
                full = dirpath.rstrip("\\") + "\\" + fname
                try:
                    st = smbclient.stat(full)
                except Exception as e:  # noqa: BLE001
                    logger.debug("smb stat failed for %s: %s", full, e)
                    continue
                modified = datetime.fromtimestamp(st.st_mtime, tz=UTC)
                if min_modified and modified < min_modified:
                    continue
                results.append(
                    RemoteFileInfo(
                        path=full,
                        name=fname,
                        size=int(st.st_size),
                        modified_time=modified,
                    )
                )
        return results

    def download_file(self, remote_path: str, local_path: str) -> int:
        import smbclient

        self._register()
        written = 0
        with smbclient.open_file(remote_path, mode="rb") as src, open(local_path, "wb") as dst:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
                written += len(chunk)
        # Size verify against the remote stat.
        try:
            expected = int(smbclient.stat(remote_path).st_size)
            if written != expected:
                raise RuntimeError(f"SMB download size mismatch ({written} != {expected})")
        except Exception as e:  # noqa: BLE001
            logger.debug("smb size verify skipped for %s: %s", remote_path, e)
        return written

    def upload_file(self, local_path: str, remote_path: str) -> bool:
        import smbclient

        self._register()
        with open(local_path, "rb") as src, smbclient.open_file(remote_path, mode="wb") as dst:
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                dst.write(chunk)
        return True

    def close(self) -> None:
        if not self._registered:
            return
        try:
            import smbclient

            smbclient.delete_session(self._server, port=self._port)
        except Exception as e:  # noqa: BLE001
            logger.debug("smb delete_session failed: %s", e)
        finally:
            self._registered = False
