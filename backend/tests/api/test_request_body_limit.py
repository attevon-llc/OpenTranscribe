"""Every route's request body is capped; only the file-carrying routes are raised.

FastAPI reads and parses a JSON or form body before any dependency runs, so an
anonymous route such as ``POST /api/auth/token/refresh`` used to buffer whatever it was
sent before authentication could refuse it. ``MAX_REQUEST_BODY_BYTES`` (Starlette's
``RequestBodyLimitMiddleware``, app-wide) now refuses an oversized body with 413 before
the handler runs; ``POST /api/files`` and the speaker avatar route raise that ceiling to
their own upload limits.
"""

from __future__ import annotations

import io

import pytest
from fastapi import HTTPException
from fastapi import status

from app.auth.token_service import token_service
from app.core.config import settings

CHUNK = 64 * 1024


@pytest.fixture
def oversized() -> int:
    return settings.MAX_REQUEST_BODY_BYTES * 2


@pytest.fixture
def refresh_handler_calls(monkeypatch) -> list[str]:
    calls: list[str] = []

    def _record(db, token):
        calls.append("verify_refresh_token")
        return None, None

    monkeypatch.setattr(token_service, "verify_refresh_token", _record)
    return calls


class TestAnonymousAuthRoutes:
    def test_an_oversized_json_body_is_413_before_the_handler(
        self, client, oversized, refresh_handler_calls
    ):
        body = b'{"refresh_token": "' + b"a" * oversized + b'"}'
        response = client.post(
            "/api/auth/token/refresh",
            content=body,
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, response.text
        assert refresh_handler_calls == []

    def test_an_oversized_chunked_body_is_cut_off_mid_stream(
        self, client, oversized, refresh_handler_calls
    ):
        pulled: list[int] = []

        def _body():
            yield b'{"refresh_token": "'
            for _ in range(oversized // CHUNK + 16):
                pulled.append(CHUNK)
                yield b"a" * CHUNK
            yield b'"}'

        response = client.post(
            "/api/auth/token/refresh",
            content=_body(),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, response.text
        assert refresh_handler_calls == []

    def test_an_oversized_login_form_is_413(self, client, oversized):
        response = client.post(
            "/api/auth/token",
            data={"username": "nobody-body-limit@example.com", "password": "x" * oversized},
        )

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, response.text

    def test_a_body_under_the_ceiling_still_reaches_the_handler(
        self, client, refresh_handler_calls
    ):
        response = client.post("/api/auth/token/refresh", json={"refresh_token": "not-a-token"})

        assert response.status_code == status.HTTP_401_UNAUTHORIZED, response.text
        assert refresh_handler_calls == ["verify_refresh_token"]


class TestFileRoutesAreRaised:
    def test_an_upload_larger_than_the_global_ceiling_reaches_the_handler(
        self, client, user_token_headers, oversized, monkeypatch
    ):
        reached: list[int] = []

        async def _accept(file, *_args, **_kwargs):
            reached.append(len(await file.read()))
            # Stop here: only the body's arrival at the handler is under test.
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="stop")

        monkeypatch.setattr("app.api.endpoints.files.process_file_upload", _accept)

        response = client.post(
            "/api/files",
            headers=user_token_headers,
            files={"file": ("big.wav", io.BytesIO(b"\0" * oversized), "audio/wav")},
        )

        assert response.status_code == status.HTTP_409_CONFLICT, response.text
        assert reached == [oversized]

    def test_an_avatar_larger_than_the_global_ceiling_is_not_refused_for_size(
        self, client, user_token_headers
    ):
        from app.core.constants import MAX_AVATAR_SIZE

        size = settings.MAX_REQUEST_BODY_BYTES + 1024
        assert size < MAX_AVATAR_SIZE
        response = client.post(
            "/api/speaker-profiles/profiles/0e8f7a3c-0000-4000-8000-000000000000/avatar",
            headers=user_token_headers,
            files={"file": ("a.png", io.BytesIO(b"\0" * size), "image/png")},
        )

        # The handler's own answer (no such profile), not the body limit's 413.
        assert response.status_code == status.HTTP_404_NOT_FOUND, response.text
