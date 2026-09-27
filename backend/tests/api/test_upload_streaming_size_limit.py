"""Upload size limits are enforced while the body streams, not after (issue #999).

``POST /api/files`` takes an ``UploadFile``. FastAPI parses the multipart body — spooling
the file part to a temp file — *before* the handler runs, so the size check inside
``process_file_upload`` came after the whole body was already on disk. A single oversized
(or endless chunked) request could fill ``/tmp`` and get the backend evicted. The
``app.router.default_max_upload_size`` assignment that looked like a guard is not a
Starlette/FastAPI setting and did nothing.

The limit for ``POST /api/files`` is the same global ceiling ``validate_file_size_for_tenant``
already enforces (``MAX_UPLOAD_BYTES``) plus a small allowance for multipart framing —
never a lower fixed number, because the browser falls back to this route for files the
presigned flow could not take. The presigned single-PUT and multipart paths never carry
the file through the API and are untouched; the last test pins that a >5 GiB declared
upload is still planned as multipart.
"""

from __future__ import annotations

import io

import pytest
from fastapi import status

from app.core.config import settings
from app.main import app
from app.middleware.upload_limit import MULTIPART_OVERHEAD_BYTES
from app.middleware.upload_limit import UploadBodyLimitMiddleware

CHUNK = 64 * 1024


class _Downstream:
    """A stand-in ASGI app that drains the body like Starlette's form parser."""

    def __init__(self) -> None:
        self.bytes_seen = 0
        self.called = False

    async def __call__(self, scope, receive, send) -> None:
        self.called = True
        while True:
            message = await receive()
            self.bytes_seen += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


class _ChunkSource:
    """An ASGI ``receive`` delivering *total* bytes in fixed chunks, counting pulls."""

    def __init__(self, total: int) -> None:
        self.remaining = total
        self.pulled = 0

    async def __call__(self):
        size = min(CHUNK, self.remaining)
        self.remaining -= size
        self.pulled += size
        return {"type": "http.request", "body": b"\0" * size, "more_body": self.remaining > 0}


async def _run(path: str, source, headers: list[tuple[bytes, bytes]] | None = None):
    downstream = _Downstream()
    sent: list[dict] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": headers or [],
    }
    await UploadBodyLimitMiddleware(downstream)(scope, source, send)
    return downstream, sent[0]["status"]


@pytest.fixture
def one_mib_ceiling(monkeypatch):
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024 * 1024)
    return 1024 * 1024 + MULTIPART_OVERHEAD_BYTES


class TestFilesUploadStreamingLimit:
    @pytest.mark.asyncio
    async def test_an_oversized_chunked_body_is_cut_off_mid_stream(self, one_mib_ceiling):
        source = _ChunkSource(total=64 * 1024 * 1024)
        downstream, status_code = await _run("/api/files", source)

        assert status_code == 413
        assert downstream.bytes_seen <= one_mib_ceiling
        # Stopped within one chunk of the ceiling, not after the whole 64 MiB.
        assert source.pulled <= one_mib_ceiling + CHUNK

    @pytest.mark.asyncio
    async def test_an_oversized_content_length_is_refused_before_reading(self, one_mib_ceiling):
        source = _ChunkSource(total=CHUNK)
        headers = [(b"content-length", str(one_mib_ceiling + 1).encode())]
        downstream, status_code = await _run("/api/files/", source, headers)

        assert status_code == 413
        assert downstream.called is False
        assert source.pulled == 0

    @pytest.mark.asyncio
    async def test_a_body_under_the_ceiling_is_passed_through_untouched(self, one_mib_ceiling):
        source = _ChunkSource(total=one_mib_ceiling)
        headers = [(b"content-length", str(one_mib_ceiling).encode())]
        downstream, status_code = await _run("/api/files", source, headers)

        assert status_code == 200
        assert downstream.bytes_seen == one_mib_ceiling

    @pytest.mark.asyncio
    async def test_the_ceiling_is_the_configured_global_cap_not_a_lower_constant(self):
        """A 10 GiB declared upload is inside the default 15 GiB MAX_UPLOAD_BYTES."""
        assert settings.MAX_UPLOAD_BYTES is not None
        declared = 10 * 1024**3
        assert declared < settings.MAX_UPLOAD_BYTES
        headers = [(b"content-length", str(declared).encode())]
        downstream, status_code = await _run("/api/files", _ChunkSource(total=CHUNK), headers)

        assert status_code == 200
        assert downstream.called is True

    @pytest.mark.asyncio
    async def test_no_global_cap_means_no_streaming_cap(self, monkeypatch):
        monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", None)
        headers = [(b"content-length", str(100 * 1024**4).encode())]
        downstream, status_code = await _run("/api/files", _ChunkSource(total=CHUNK), headers)

        assert status_code == 200
        assert downstream.called is True

    @pytest.mark.asyncio
    async def test_other_routes_are_not_governed(self, one_mib_ceiling):
        source = _ChunkSource(total=4 * 1024 * 1024)
        downstream, status_code = await _run("/api/files/prepare", source)

        assert status_code == 200
        assert downstream.bytes_seen == 4 * 1024 * 1024


class TestOverHttp:
    def test_an_oversized_chunked_upload_is_413_before_the_handler(
        self, client, user_token_headers, monkeypatch, one_mib_ceiling
    ):
        reached: list[str] = []
        monkeypatch.setattr(
            "app.api.endpoints.files.process_file_upload",
            lambda *a, **k: reached.append("handler"),
        )
        boundary = "limit-test-boundary"

        def _body():
            yield (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="big.wav"\r\nContent-Type: audio/wav\r\n\r\n'
            ).encode()
            for _ in range((one_mib_ceiling // CHUNK) + 16):
                yield b"\0" * CHUNK
            yield f"\r\n--{boundary}--\r\n".encode()

        response = client.post(
            "/api/files",
            headers={
                **user_token_headers,
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            content=_body(),
        )
        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, response.text
        assert reached == []

    def test_a_small_upload_still_reaches_the_handlers_own_validation(
        self, client, user_token_headers, one_mib_ceiling
    ):
        """Unchanged behaviour below the ceiling: a wrong MIME type is still the
        handler's 400, not something the limit layer invented."""
        response = client.post(
            "/api/files",
            headers=user_token_headers,
            files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text

    def test_the_no_op_router_attribute_is_gone(self):
        assert not hasattr(app.router, "default_max_upload_size")


class TestAvatarStreamingLimit:
    @pytest.mark.asyncio
    async def test_an_oversized_avatar_is_cut_off_mid_stream(self):
        from app.core.constants import MAX_AVATAR_SIZE

        source = _ChunkSource(total=50 * 1024 * 1024)
        downstream, status_code = await _run(
            "/api/speaker-profiles/profiles/0e8f7a3c-0000-4000-8000-000000000000/avatar", source
        )

        assert status_code == 413
        assert downstream.bytes_seen <= MAX_AVATAR_SIZE + MULTIPART_OVERHEAD_BYTES


class TestPresignedPathsUnaffected:
    def test_a_multi_gb_declared_upload_is_still_planned_as_multipart(
        self, client, user_token_headers, monkeypatch
    ):
        """The >5 GiB path never touches POST /api/files and must keep working."""
        monkeypatch.setattr(settings, "STORAGE_BACKEND", "s3")
        monkeypatch.setattr(
            "app.services.multipart_upload.create_upload", lambda name, ctype: "upload-1"
        )
        monkeypatch.setattr(
            "app.services.multipart_upload.presign_parts",
            lambda name, upload_id, numbers: ({n: f"https://s3/{n}" for n in numbers}, 3600),
        )

        response = client.post(
            "/api/files/prepare",
            headers=user_token_headers,
            json={
                "filename": "huge.mp4",
                "file_size": 6 * 1024**3,
                "content_type": "video/mp4",
                "use_presigned": True,
            },
        )
        assert response.status_code == status.HTTP_200_OK, response.text
        body = response.json()
        assert body["upload_method"] == "MULTIPART"
        assert body["multipart"]["part_count"] == 96
