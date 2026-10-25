"""Drive the DOCX / PDF transcript export end to end, in process (issue #1278).

The HTTP route queues a Celery task; in the suite ``Task.apply_async`` is a no-op, so the
harness records what the route dispatched and then runs the task body itself, with the same
database session as the test and every network seam replaced by a recorder:

* ``session_scope`` yields the test's savepointed session (so nothing is committed),
* Redis is a dict (the dedupe guard and the reconnect cache),
* the object-storage upload and presign are captured, so a test reads the BYTES the user would
  download, never an internal flag.

``docx_text`` reads those bytes back the way a reader would: every ``w:t`` run in
``word/document.xml``, in order.
"""

from __future__ import annotations

import contextlib
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from lxml import etree

from tests.unit._fake_liveness_redis import FakeRedis

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

#: Pinned Noto fixture fonts (SIL OFL 1.1, ``OFL.txt`` beside them): Latin regular and bold,
#: Arabic, Hebrew and Thai. Tests never depend on whatever fonts the host happens to have.
FIXTURE_FONT_DIR = Path(__file__).parent / "fonts"


def docx_text(data: bytes) -> str:
    """The visible text of a DOCX: body, header and footer runs joined with spaces."""
    parts: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for name in sorted(archive.namelist()):
            if name.startswith("word/") and name.endswith(".xml") and name.count("/") == 1:
                if any(k in name for k in ("document", "header", "footer")):
                    root = etree.fromstring(archive.read(name))
                    parts.extend(t.text or "" for t in root.iter(f"{_W}t"))
    return " ".join(parts)


def pdf_text(data: bytes) -> str:
    """The text of every page of a PDF, as a reader's text extraction would give it."""
    from pypdf import PdfReader

    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def document_text(fmt: str, data: bytes) -> str:
    """Whitespace-normalised visible text of a ``docx`` or ``pdf`` document."""
    assert fmt in ("docx", "pdf"), fmt
    return " ".join((docx_text(data) if fmt == "docx" else pdf_text(data)).split())


def docx_body_xml(data: bytes) -> bytes:
    """Raw ``word/document.xml``."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read("word/document.xml")


class DocumentExportHarness:
    """Post an export request, run the dispatched task, and expose what it produced."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, db_session: Any) -> None:
        from app.services import minio_service
        from app.tasks import transcript_document as task_module

        self.redis = FakeRedis()
        self.uploaded: dict[str, bytes] = {}
        self.presigned_names: list[str] = []
        self.events: list[dict[str, Any]] = []
        self.dispatched: list[dict[str, Any]] = []
        harness = self

        @contextlib.contextmanager
        def _scope():
            yield db_session

        class _FakeService:
            cache_bucket = "processed"

            def __init__(self, _minio: Any) -> None:
                self.minio_service = self

            def upload_bytes(self, bucket: str, key: str, data: bytes, content_type: str) -> None:
                harness.uploaded[key] = data

        def _presign(key: str, **kwargs: Any) -> str:
            harness.presigned_names.append(str(kwargs.get("download_filename")))
            return f"http://minio.invalid/{key}"

        monkeypatch.setattr(
            "app.services.transcript_document.fonts.DOCUMENT_FONT_DIR", str(FIXTURE_FONT_DIR)
        )
        monkeypatch.setattr(task_module, "session_scope", _scope)
        monkeypatch.setattr(task_module, "VideoProcessingService", _FakeService)
        monkeypatch.setattr(minio_service, "get_presigned_download_url", _presign)
        monkeypatch.setattr(
            task_module,
            "publish_bulk_event",
            lambda job_id, **kw: harness.events.append({"job_id": job_id, **kw}),
        )
        monkeypatch.setattr("app.core.redis.get_redis", lambda: harness.redis)
        monkeypatch.setattr("app.services.download_events.get_redis", lambda: harness.redis)
        monkeypatch.setattr(
            task_module.prepare_transcript_document_task,
            "delay",
            lambda **kwargs: harness.dispatched.append(kwargs),
        )

    def post(self, client: Any, headers: dict, media_file: Any, **body: Any) -> Any:
        """``POST /files/{uuid}/export/document``; ``format`` defaults to ``docx``."""
        payload = {"format": "docx", **body}
        return client.post(
            f"/api/files/{media_file.uuid}/export/document", headers=headers, json=payload
        )

    def run_dispatched(self) -> dict[str, Any]:
        """Run the most recently dispatched task body in this process."""
        from app.tasks.transcript_document import prepare_transcript_document_task

        assert self.dispatched, "the route dispatched nothing"
        result: dict[str, Any] = prepare_transcript_document_task.run(**self.dispatched[-1])
        return result

    @property
    def last_document(self) -> bytes:
        """The bytes of the most recently uploaded document."""
        assert self.uploaded, f"nothing was uploaded; events: {self.events}"
        return list(self.uploaded.values())[-1]

    def export(self, client: Any, headers: dict, media_file: Any, **body: Any) -> bytes:
        """Request, run, and return the document the user would download."""
        response = self.post(client, headers, media_file, **body)
        assert response.status_code == 202, response.text
        result = self.run_dispatched()
        assert result["status"] == "success", f"{result}; events: {self.events}"
        return self.last_document


@pytest.fixture
def document_export(monkeypatch: pytest.MonkeyPatch, db_session: Any) -> DocumentExportHarness:
    """A :class:`DocumentExportHarness` wired to the test's session."""
    return DocumentExportHarness(monkeypatch, db_session)
