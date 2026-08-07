"""SCIM error bodies (RFC 7644 §3.12).

A SCIM client does not read FastAPI's ``{"detail": ...}``. Okta and Entra both parse
``scimType``/``detail`` out of the ``urn:ietf:params:scim:api:messages:2.0:Error``
body and surface it to the administrator; without it, every failure shows up in their
console as an opaque status code. So this router raises :class:`SCIMError` and
``main.py`` registers :func:`scim_error_handler` for it.

Content type is ``application/scim+json`` throughout, as RFC 7644 §3.1 requires.
"""

from __future__ import annotations

from fastapi import Request
from fastapi import status
from fastapi.responses import JSONResponse

from app.schemas.scim import SCHEMA_ERROR

#: RFC 7644 §3.12's ``scimType`` vocabulary, restricted to the values this server
#: can actually produce.
SCIM_TYPE_INVALID_FILTER = "invalidFilter"
SCIM_TYPE_INVALID_SYNTAX = "invalidSyntax"
SCIM_TYPE_INVALID_VALUE = "invalidValue"
SCIM_TYPE_INVALID_PATH = "invalidPath"
SCIM_TYPE_MUTABILITY = "mutability"
SCIM_TYPE_UNIQUENESS = "uniqueness"
SCIM_TYPE_TOO_MANY = "tooMany"

#: The media type every SCIM response carries.
SCIM_CONTENT_TYPE = "application/scim+json"


class SCIMError(Exception):
    """An error to render as a SCIM Error resource."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        *,
        scim_type: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Build a SCIM error.

        Args:
            status_code: HTTP status. Echoed into the body as a **string**, which is
                what RFC 7644 specifies and what strict clients validate.
            detail: Human-readable message. Never contains a credential.
            scim_type: Optional machine-readable classifier.
            headers: Optional response headers (``WWW-Authenticate`` on a 401).
        """
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.scim_type = scim_type
        self.headers = headers or {}

    def to_response(self) -> JSONResponse:
        """Render this error as its wire body."""
        body: dict[str, str | list[str]] = {
            "schemas": [SCHEMA_ERROR],
            "status": str(self.status_code),
            "detail": self.detail,
        }
        if self.scim_type:
            body["scimType"] = self.scim_type
        return JSONResponse(
            status_code=self.status_code,
            content=body,
            headers=self.headers,
            media_type=SCIM_CONTENT_TYPE,
        )


async def scim_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    """FastAPI exception handler for :class:`SCIMError`."""
    if isinstance(exc, SCIMError):
        return exc.to_response()
    return SCIMError(  # pragma: no cover - defensive
        status.HTTP_500_INTERNAL_SERVER_ERROR, "Internal error"
    ).to_response()


def not_found(resource: str, resource_id: str) -> SCIMError:
    """404 for an unknown resource id."""
    return SCIMError(
        status.HTTP_404_NOT_FOUND,
        f"{resource} {resource_id} not found",
    )


def conflict(detail: str) -> SCIMError:
    """409 with ``scimType=uniqueness`` — the response an IdP retries correctly."""
    return SCIMError(status.HTTP_409_CONFLICT, detail, scim_type=SCIM_TYPE_UNIQUENESS)


def bad_request(detail: str, scim_type: str = SCIM_TYPE_INVALID_VALUE) -> SCIMError:
    """400 with a classifier."""
    return SCIMError(status.HTTP_400_BAD_REQUEST, detail, scim_type=scim_type)
