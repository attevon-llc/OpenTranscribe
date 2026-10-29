"""``DomainError`` renders on the wire exactly as the equivalent ``HTTPException`` does."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.exceptions import AccessDeniedError
from app.core.exceptions import AuthenticationError
from app.core.exceptions import ConflictError
from app.core.exceptions import DomainError
from app.core.exceptions import InvalidInputError
from app.core.exceptions import OperationFailedError
from app.core.exceptions import ResourceNotFoundError
from app.core.exceptions import ServiceUnavailableError
from app.core.exceptions import UnprocessableInputError
from app.core.exceptions import UpstreamServiceError
from app.main import app as real_app

Detail = str | dict[str, Any] | list[Any]

CASES: list[tuple[type[DomainError], int, Detail, dict[str, str] | None]] = [
    (InvalidInputError, 400, "Invalid UUID format: x", None),
    (
        AuthenticationError,
        401,
        "Invalid authentication credentials",
        {"WWW-Authenticate": "Bearer"},
    ),
    (
        AuthenticationError,
        401,
        "Invalid or missing client certificate",
        {"WWW-Authenticate": "Certificate"},
    ),
    (AccessDeniedError, 403, "You do not have permission to access this file", None),
    (ResourceNotFoundError, 404, "File not found", None),
    (ConflictError, 409, {"code": "lite_mode", "message": "Nicht verfügbar"}, None),
    (UnprocessableInputError, 422, ["a", {"b": 1}], None),
    (OperationFailedError, 500, "The media download failed.", None),
    (UpstreamServiceError, 502, "Could not fetch media information", None),
    (
        ServiceUnavailableError,
        503,
        {"code": "audit", "message": "unavailable"},
        {"Retry-After": "5"},
    ),
]


def _client() -> TestClient:
    app = FastAPI(exception_handlers=dict(real_app.exception_handlers))
    for i, (cls, status, detail, headers) in enumerate(CASES):

        def raise_http(
            s: int = status, d: Detail = detail, h: dict[str, str] | None = headers
        ) -> None:
            raise HTTPException(status_code=s, detail=d, headers=h)

        def raise_domain(
            c: type[DomainError] = cls, d: Detail = detail, h: dict[str, str] | None = headers
        ) -> None:
            raise c(d, headers=h)

        app.add_api_route(f"/http/{i}", raise_http)
        app.add_api_route(f"/domain/{i}", raise_domain)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.unit
@pytest.mark.parametrize("index", range(len(CASES)))
def test_response_is_identical_to_httpexception(index: int) -> None:
    client = _client()
    expected = client.get(f"/http/{index}")
    actual = client.get(f"/domain/{index}")
    assert actual.status_code == expected.status_code == CASES[index][1]
    assert actual.content == expected.content
    assert dict(actual.headers) == dict(expected.headers)


@pytest.mark.unit
def test_real_app_registers_the_handler() -> None:
    from app.main import handle_domain_error

    assert real_app.exception_handlers[DomainError] is handle_domain_error


@pytest.mark.unit
def test_domain_handler_outranks_the_generic_app_error_handler() -> None:
    from app.core.exceptions import OpenTranscribeError

    handlers: dict[type, Callable] = real_app.exception_handlers
    mro = type(AuthenticationError("x")).__mro__
    first = next(c for c in mro if c in handlers)
    assert first is DomainError
    assert OpenTranscribeError in handlers
