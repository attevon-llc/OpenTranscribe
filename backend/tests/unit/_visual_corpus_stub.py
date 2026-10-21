"""A stub OpenTranscribe backend for the seed-corpus tests (#1238).

Serves just the endpoints ``scripts/seed-fresh-deployment.sh`` and the capture wrapper's
corpus assertion use, with the two behaviours of the real backend that made a naive
implementation wrong:

* ``GET /api/files`` is **paginated** (default 20 per page, capped at 100) and wrapped in
  ``{items, total, page, page_size, total_pages, has_more}``. A client that reads page 1 sees
  a window, not the library.
* ``POST /api/files`` stores ``filename`` **sanitized** by the real
  ``app.utils.filename.sanitize_filename`` while ``title`` keeps the original stem, and records
  the uploaded byte count as ``file_size``. Importing the real sanitizer (rather than copying
  its rules) is what keeps this stub honest if the backend's rules change.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import parse_qs
from urllib.parse import urlparse

from app.utils.filename import sanitize_filename

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

_FILENAME_RE = re.compile(rb'filename="([^"]*)"')


class StubBackend:
    """A threaded-in-background HTTP server holding an in-memory file library."""

    def __init__(
        self,
        *,
        upload_status: str = "completed",
        fail_listing: bool = False,
        reject_uploads: bool = False,
    ) -> None:
        self.library: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.uploaded_names: list[str] = []
        self.upload_status = upload_status
        self.fail_listing = fail_listing
        self.reject_uploads = reject_uploads
        self._server = HTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def add_file(
        self,
        name: str,
        *,
        size: int = 1,
        status: str = "completed",
    ) -> None:
        """Insert a file as the real upload endpoint would have stored it."""
        path = PurePosixPath(name)
        self.library.append(
            {
                "filename": sanitize_filename(name),
                "title": path.stem,
                "file_size": size,
                "status": status,
            }
        )

    def __enter__(self) -> StubBackend:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, payload: object, code: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
                parsed = urlparse(self.path)
                stub.requests.append(("GET", parsed.path))
                if parsed.path in {"/api/health", "/health"}:
                    self._send({"status": "ok"})
                    return
                if parsed.path != "/api/files" or stub.fail_listing:
                    self._send({"detail": "stub: listing unavailable"}, 500)
                    return
                query = parse_qs(parsed.query)
                page = max(1, int(query.get("page", ["1"])[0]))
                size = min(MAX_PAGE_SIZE, int(query.get("page_size", [str(DEFAULT_PAGE_SIZE)])[0]))
                total = len(stub.library)
                start = (page - 1) * size
                total_pages = (total + size - 1) // size if total else 0
                self._send(
                    {
                        "items": stub.library[start : start + size],
                        "total": total,
                        "page": page,
                        "page_size": size,
                        "total_pages": total_pages,
                        "has_more": page < total_pages,
                    }
                )

            def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
                path = urlparse(self.path).path
                stub.requests.append(("POST", path))
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                if path in {"/api/auth/login", "/api/auth/token"}:
                    self._send({"access_token": "stub-token"})
                    return
                if path == "/api/files" and stub.reject_uploads:
                    self._send({"detail": "stub: upload rejected"}, 500)
                    return
                if path == "/api/files":
                    match = _FILENAME_RE.search(body)
                    original = match.group(1).decode() if match else "unknown"
                    # The payload of the single multipart part: between the blank line that
                    # ends its headers and the CRLF before the closing boundary.
                    payload = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                    stub.uploaded_names.append(original)
                    stub.add_file(original, size=len(payload), status=stub.upload_status)
                    self._send({"filename": sanitize_filename(original)})
                    return
                self._send({"detail": "stub: not found"}, 404)

            def log_message(self, *_args: object) -> None:  # keep test output clean
                return

        return Handler
