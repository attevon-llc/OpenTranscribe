"""Hook refusals raised below the API layer reach the client as the HTTPException they replaced."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.core.exceptions import DomainError
from app.main import handle_domain_error
from app.services.chat.hooks import ChatQuotaExceededError
from app.tasks.transcription.hooks import DispatchBlockedError
from app.tasks.transcription.hooks import LegalAcceptanceRequiredError
from app.tasks.transcription.hooks import QuotaExceededError

CASES = [
    (ChatQuotaExceededError(), 402, "Chat quota exceeded"),
    (QuotaExceededError(hours_over=1.5), 402, "Transcription quota exceeded"),
    (DispatchBlockedError(), 403, "Transcription blocked"),
    (DispatchBlockedError(detail="Legal hold", status_code=451), 451, "Legal hold"),
    (
        LegalAcceptanceRequiredError("legal_gate_unavailable", ("tos",)),
        503,
        {"code": "legal_gate_unavailable", "documents": ["tos"]},
    ),
]


def _client_raising(exc: Exception) -> TestClient:
    app = FastAPI()
    app.exception_handler(DomainError)(handle_domain_error)

    @app.get("/boom")
    def boom() -> None:
        raise exc

    return TestClient(app)


@pytest.mark.unit
@pytest.mark.parametrize(("exc", "status", "detail"), CASES)
def test_hook_refusal_wire_format_equals_http_exception(
    exc: Exception, status: int, detail: str | dict[str, object]
) -> None:
    expected = _client_raising(HTTPException(status_code=status, detail=detail)).get("/boom")
    got = _client_raising(exc).get("/boom")
    assert got.status_code == expected.status_code == status
    assert got.json() == expected.json() == {"detail": detail}


@pytest.mark.unit
def test_hook_refusals_are_not_http_exceptions() -> None:
    for exc, _, _ in CASES:
        assert isinstance(exc, DomainError)
        assert not isinstance(exc, HTTPException)
