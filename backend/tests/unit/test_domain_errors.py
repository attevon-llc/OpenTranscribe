"""The ``DomainError`` taxonomy: status per class, HTTPException-equal ``str``, no OSError family."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.exceptions import AccessDeniedError
from app.core.exceptions import AuthenticationError
from app.core.exceptions import ConflictError
from app.core.exceptions import DomainError
from app.core.exceptions import InvalidInputError
from app.core.exceptions import OpenTranscribeError
from app.core.exceptions import OperationFailedError
from app.core.exceptions import ResourceNotFoundError
from app.core.exceptions import ServiceUnavailableError
from app.core.exceptions import UnprocessableInputError
from app.core.exceptions import UpstreamServiceError
from app.core.exceptions import domain_error_for_status
from app.utils.error_classification import categorize_error

CLASSES = [
    (InvalidInputError, 400),
    (AuthenticationError, 401),
    (AccessDeniedError, 403),
    (ResourceNotFoundError, 404),
    (ConflictError, 409),
    (UnprocessableInputError, 422),
    (OperationFailedError, 500),
    (UpstreamServiceError, 502),
    (ServiceUnavailableError, 503),
]


@pytest.mark.unit
@pytest.mark.parametrize(("cls", "status"), CLASSES)
def test_class_carries_its_status(cls: type[DomainError], status: int) -> None:
    assert cls.status_code == status
    assert cls("x").status_code == status


@pytest.mark.unit
@pytest.mark.parametrize(("cls", "status"), CLASSES)
@pytest.mark.parametrize("detail", ["File not found", {"code": "c", "message": "m"}])
def test_str_matches_httpexception(
    cls: type[DomainError], status: int, detail: str | dict[str, str]
) -> None:
    assert str(cls(detail)) == str(HTTPException(status_code=status, detail=detail))
    assert str(cls(detail)) == f"{status}: {detail}"


@pytest.mark.unit
def test_detail_and_headers_are_carried() -> None:
    exc = AuthenticationError("nope", headers={"WWW-Authenticate": "Bearer"})
    assert exc.detail == "nope"
    assert exc.headers == {"WWW-Authenticate": "Bearer"}
    assert AccessDeniedError("x").headers is None
    body = {"code": "c"}
    assert ConflictError(body).detail is body


@pytest.mark.unit
@pytest.mark.parametrize(("cls", "_status"), CLASSES)
def test_hierarchy_avoids_retry_families(cls: type[DomainError], _status: int) -> None:
    assert issubclass(cls, DomainError)
    assert issubclass(cls, OpenTranscribeError)
    # Celery autoretry_for covers OSError/ConnectionError/TimeoutError: a refusal must never match.
    assert not issubclass(cls, (OSError, ConnectionError, TimeoutError, ValueError, LookupError))


@pytest.mark.unit
@pytest.mark.parametrize(("cls", "status"), CLASSES)
def test_domain_error_for_status(cls: type[DomainError], status: int) -> None:
    assert domain_error_for_status(status) is cls


@pytest.mark.unit
def test_domain_error_for_status_rejects_unmapped() -> None:
    with pytest.raises(ValueError, match="418"):
        domain_error_for_status(418)


@pytest.mark.unit
def test_status_prefix_still_drives_error_classification() -> None:
    http = categorize_error(str(HTTPException(status_code=502, detail="Could not download")))
    assert categorize_error(str(UpstreamServiceError("Could not download"))) == http
