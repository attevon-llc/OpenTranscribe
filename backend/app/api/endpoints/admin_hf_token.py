"""Hugging Face access token (platform secret) - super_admin only, write-only.

The token downloads gated or private models for local transcription and speaker detection.
It is stored encrypted in ``SystemSettings`` and returned by no route: ``GET`` reports only
whether one is in effect and where it comes from. The audit events name the action, never
the value. Resolution and storage live in ``services/huggingface_token.py``.

- ``GET    /admin/hf-token``       - ``{is_set, source}``
- ``PUT    /admin/hf-token``       - save (replaces any saved token)
- ``DELETE /admin/hf-token``       - remove the saved token (the server environment applies again)
- ``POST   /admin/hf-token/test``  - ask Hugging Face whether a token is accepted
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from pydantic import BaseModel
from pydantic import Field
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth.dependencies import _get_client_info
from app.api.endpoints.auth.dependencies import get_current_active_superuser
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.db.base import get_db
from app.services import huggingface_token as hf_token_service

logger = logging.getLogger(__name__)
router = APIRouter()

INVALID_SHAPE_DETAIL = (
    "The token is not in the expected form. A Hugging Face access token starts with hf_ "
    "and has no spaces."
)


class HfTokenStatus(BaseModel):
    """What the client may know. Never any part of the token."""

    is_set: bool
    source: Literal["app", "environment", "none"]


class HfTokenUpdate(BaseModel):
    token: str = Field(..., description="Hugging Face access token. Stored encrypted.")


class HfTokenTestRequest(BaseModel):
    token: str | None = Field(
        default=None, description="A token to test without saving it. Omit to test the one in use."
    )


class HfTokenTestResult(BaseModel):
    ok: bool
    #: Stable code for translated copy; ``message`` is the fixed English sentence.
    code: Literal["valid", "invalid", "unreachable"]
    message: str


_TEST_CODES: dict[str, Literal["valid", "invalid", "unreachable"]] = {
    hf_token_service.TEST_VALID: "valid",
    hf_token_service.TEST_INVALID: "invalid",
    hf_token_service.TEST_UNREACHABLE: "unreachable",
}


def _checked(value: str) -> str:
    """The validated token, or a 422 with a fixed message (never the value)."""
    try:
        return hf_token_service.validate_token(value)
    except ValueError:
        raise HTTPException(status_code=422, detail=INVALID_SHAPE_DETAIL) from None


def _status(db: Session) -> HfTokenStatus:
    is_set, source = hf_token_service.get_status(db)
    return HfTokenStatus(is_set=is_set, source=source)


def _audit(request: Request, actor: models.User, action: str) -> None:
    client_ip, user_agent = _get_client_info(request)
    audit_logger.log(
        event_type=AuditEventType.ADMIN_SETTINGS_CHANGE,
        outcome=AuditOutcome.SUCCESS,
        user_id=actor.id,
        username=str(actor.email),
        source_ip=client_ip,
        user_agent=user_agent,
        details={"setting": "huggingface_access_token", "action": action},
    )


@router.get("", response_model=HfTokenStatus)
def get_hf_token_status(
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> HfTokenStatus:
    """Whether a token is in effect and where it comes from (super_admin only)."""
    return _status(db)


@router.put("", response_model=HfTokenStatus)
def set_hf_token(
    request: Request,
    body: HfTokenUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> HfTokenStatus:
    """Store or replace the token, encrypted at rest."""
    token = _checked(body.token)
    try:
        hf_token_service.save_token(db, token)
    except RuntimeError:
        raise HTTPException(status_code=500, detail="Failed to encrypt the token") from None
    _audit(request, current_user, "set")
    logger.info("Super admin %s saved the Hugging Face access token", current_user.id)
    return _status(db)


@router.delete("", response_model=HfTokenStatus)
def clear_hf_token(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_superuser),
) -> HfTokenStatus:
    """Remove the saved token. The server-environment token, if any, applies again."""
    if hf_token_service.clear_token(db):
        _audit(request, current_user, "clear")
        logger.info("Super admin %s removed the Hugging Face access token", current_user.id)
    return _status(db)


@router.post("/test", response_model=HfTokenTestResult)
def test_hf_token(
    body: HfTokenTestRequest | None = None,
    current_user: models.User = Depends(get_current_active_superuser),
) -> HfTokenTestResult:
    """Check a token with Hugging Face. Fixed sentences only; nothing is stored."""
    if body is not None and body.token is not None:
        token: str | None = _checked(body.token)
    else:
        token = hf_token_service.get_hf_token()
    if not token:
        raise HTTPException(status_code=404, detail="No Hugging Face token is set")
    message = hf_token_service.check_token(token)
    code = _TEST_CODES.get(message, "unreachable")
    return HfTokenTestResult(ok=code == "valid", code=code, message=message)
