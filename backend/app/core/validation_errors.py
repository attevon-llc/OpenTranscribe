"""A 422 must not echo a credential back to the client (or into a proxy log).

FastAPI's default validation response includes each error's ``input``: the value that failed.
For a model-level check (an access key without its secret) that ``input`` is the WHOLE request
body, and for a malformed secret it is the secret itself. This handler is FastAPI's default
response with ``input`` removed from every error that concerns, or contains, a sensitive field.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

#: Request fields whose values must never be sent back.
SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "aws_access_key_id",
        "aws_secret_access_key",
        "aws_session_token",
    }
)


def _is_sensitive(error: dict[str, Any]) -> bool:
    if any(part in SENSITIVE_FIELDS for part in error.get("loc", ())):
        return True
    value = error.get("input")
    return isinstance(value, dict) and not SENSITIVE_FIELDS.isdisjoint(value)


def scrub_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``errors`` with ``input`` dropped from every sensitive entry."""
    return [
        {key: value for key, value in error.items() if key != "input"}
        if _is_sensitive(error)
        else error
        for error in errors
    ]


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """FastAPI's default 422 body, minus any echoed credential."""
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(scrub_errors(list(exc.errors())))},
    )
