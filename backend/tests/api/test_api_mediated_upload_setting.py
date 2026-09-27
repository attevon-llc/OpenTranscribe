"""``API_MEDIATED_UPLOAD_ENABLED`` — turning off the upload path through the API (#1008).

Uploads normally go browser -> object storage over presigned URLs. ``POST /api/files``
is the fallback that streams the whole file through the API process. A deployment can
now switch that fallback off: the route is refused before its body is read, the
setting is advertised to the frontend (which then never falls back), and
``/files/prepare`` stops answering "no presigned plan, use POST /files" — it reports
that direct upload is temporarily unavailable instead. The default is on, so nothing
changes for an existing deployment.
"""

from __future__ import annotations

import io

import pytest
from fastapi import status

from app.core.config import settings
from app.middleware.upload_limit import UploadBodyLimitMiddleware
from app.models.media import MediaFile


@pytest.fixture
def api_upload_off(monkeypatch):
    monkeypatch.setattr(settings, "API_MEDIATED_UPLOAD_ENABLED", False)


class _Source:
    def __init__(self) -> None:
        self.pulled = 0

    async def __call__(self):
        self.pulled += 1
        return {"type": "http.request", "body": b"\0" * 1024, "more_body": False}


class TestPostFilesRefusedWhenDisabled:
    @pytest.mark.asyncio
    async def test_refused_before_a_single_byte_is_read(self, api_upload_off):
        reached: list[bool] = []

        async def downstream(scope, receive, send):
            reached.append(True)

        sent: list[dict] = []

        async def send(message):
            sent.append(message)

        source = _Source()
        scope = {"type": "http", "method": "POST", "path": "/api/files", "headers": []}
        await UploadBodyLimitMiddleware(downstream)(scope, source, send)

        assert sent[0]["status"] == 404
        assert source.pulled == 0
        assert reached == []

    def test_refused_over_http(self, client, user_token_headers, api_upload_off, monkeypatch):
        reached: list[str] = []
        monkeypatch.setattr(
            "app.api.endpoints.files.process_file_upload",
            lambda *a, **k: reached.append("handler"),
        )
        response = client.post(
            "/api/files",
            headers=user_token_headers,
            files={"file": ("a.wav", io.BytesIO(b"RIFF0000WAVE"), "audio/wav")},
        )
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
        assert reached == []

    def test_unchanged_when_enabled(self, client, user_token_headers):
        """Default on: the request reaches the handler's own validation as before."""
        assert settings.API_MEDIATED_UPLOAD_ENABLED is True
        response = client.post(
            "/api/files",
            headers=user_token_headers,
            files={"file": ("notes.txt", io.BytesIO(b"hello"), "text/plain")},
        )
        assert response.status_code == status.HTTP_400_BAD_REQUEST, response.text


class TestAdvertisedToTheFrontend:
    def test_capabilities_default_to_enabled(self, client):
        response = client.get("/api/system/capabilities")
        assert response.status_code == 200
        assert response.json()["api_mediated_upload_enabled"] is True

    def test_capabilities_report_disabled(self, client, api_upload_off):
        response = client.get("/api/system/capabilities")
        assert response.status_code == 200
        assert response.json()["api_mediated_upload_enabled"] is False


class TestPrepareWithoutAPresignedPlan:
    def _prepare(self, client, headers, filename: str):
        return client.post(
            "/api/files/prepare",
            headers=headers,
            json={
                "filename": filename,
                "file_size": 4096,
                "content_type": "audio/wav",
                "use_presigned": True,
            },
        )

    def test_is_503_and_leaves_no_row_when_the_fallback_is_disabled(
        self, client, user_token_headers, api_upload_off, monkeypatch, db_session
    ):
        monkeypatch.setattr("app.services.multipart_upload.build_upload_plan", lambda *a, **k: None)
        response = self._prepare(client, user_token_headers, "no-plan-off.wav")

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE, response.text
        assert (
            db_session.query(MediaFile).filter(MediaFile.filename == "no-plan-off.wav").count() == 0
        )

    def test_still_hands_the_client_to_the_fallback_when_enabled(
        self, client, user_token_headers, monkeypatch
    ):
        monkeypatch.setattr("app.services.multipart_upload.build_upload_plan", lambda *a, **k: None)
        response = self._prepare(client, user_token_headers, "no-plan-on.wav")

        assert response.status_code == status.HTTP_200_OK, response.text
        assert "upload_url" not in response.json()
