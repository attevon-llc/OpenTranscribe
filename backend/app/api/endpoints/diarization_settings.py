"""The current user's pyannote.ai diarization credential (issue #1204).

Mounted at ``/user-settings/diarization/pyannote`` behind the ``asr.user_providers``
capability, the same gate as the per-user cloud ASR keys: pyannote.ai only ever runs beside
a per-user cloud ASR provider. Every route acts on the CALLER's own row; there is no path
parameter and no admin variant, so one user can never read or change another's key.

Secrecy rules, each pinned by ``tests/api/test_pyannote_credential_endpoints.py``:

- the key is stored with ``encrypt_api_key`` (AES-256-GCM) and returned by no route;
- logs name the user id and the action, never the key;
- the audit events carry ``{"provider", "purpose"}`` only;
- a connection test answers with a fixed sentence, never vendor text.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC
from datetime import datetime

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from fastapi import status
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.auth.audit import AuditEventType
from app.auth.audit import AuditOutcome
from app.auth.audit import audit_logger
from app.auth.audit import request_org_id
from app.auth.rate_limit import get_api_rate_limit
from app.auth.rate_limit import limiter
from app.core.constants import DEFAULT_DIARIZATION_SOURCE
from app.core.constants import PYANNOTE_DEFAULT_DIARIZATION_MODEL
from app.core.locked_settings import locked_transcription_fields
from app.db.base import get_db
from app.middleware.audit import get_request_context
from app.models.user_diarization_settings import UserDiarizationSettings
from app.schemas.diarization_settings import PYANNOTE_KEY_MAX_LEN
from app.schemas.diarization_settings import PYANNOTE_KEY_MIN_LEN
from app.schemas.diarization_settings import PyannoteConnectionTestRequest
from app.schemas.diarization_settings import PyannoteConnectionTestResult
from app.schemas.diarization_settings import PyannoteCredentialDeleted
from app.schemas.diarization_settings import PyannoteCredentialStatus
from app.schemas.diarization_settings import PyannoteCredentialUpdate
from app.schemas.diarization_settings import PyannoteTestCode
from app.schemas.diarization_settings import validate_pyannote_key
from app.services.diarization.factory import PYANNOTE_CREDENTIAL_NAME
from app.services.diarization.factory import PYANNOTE_PROVIDER
from app.utils.encryption import decrypt_api_key
from app.utils.encryption import encrypt_api_key

router = APIRouter()
logger = logging.getLogger(__name__)

DIARIZATION_SOURCE_KEY = "transcription_diarization_source"
LOCKED_DETAIL = (
    "Speaker detection is managed by this deployment, so a pyannote.ai API key cannot be set here."
)
_AUDIT_DETAILS = {"provider": "pyannote.ai", "purpose": "diarization"}


def _own_row(db: Session, user_id: int) -> UserDiarizationSettings | None:
    return (
        db.query(UserDiarizationSettings)
        .filter(
            UserDiarizationSettings.user_id == user_id,
            UserDiarizationSettings.provider == PYANNOTE_PROVIDER,
        )
        .first()
    )


def _source_row(db: Session, user_id: int) -> models.UserSetting | None:
    return (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key == DIARIZATION_SOURCE_KEY,
        )
        .first()
    )


def _is_locked(request: Request) -> bool:
    return "diarization_source" in locked_transcription_fields(request)


def _refuse_if_locked(request: Request) -> None:
    if _is_locked(request):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LOCKED_DETAIL)


INVALID_KEY_DETAIL = (
    f"API key must be {PYANNOTE_KEY_MIN_LEN} to {PYANNOTE_KEY_MAX_LEN} printable characters "
    "with no spaces"
)


def _checked_key(value: str) -> str:
    """The trimmed key, or a 422 with a fixed message that never contains the value."""
    try:
        return validate_pyannote_key(value)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=INVALID_KEY_DETAIL
        ) from None


def _status(row: UserDiarizationSettings | None, *, locked: bool) -> PyannoteCredentialStatus:
    configured = bool(row is not None and row.is_active and row.api_key)
    return PyannoteCredentialStatus(
        configured=configured,
        locked=locked,
        test_status=row.test_status if configured and row else None,
        test_message=row.test_message if configured and row else None,
        last_tested=row.last_tested if configured and row else None,
        updated_at=row.updated_at if configured and row else None,
    )


def _audit(request: Request, user: models.User, event_type: AuditEventType) -> None:
    meta = get_request_context(request)
    audit_logger.log(
        event_type=event_type,
        outcome=AuditOutcome.SUCCESS,
        user_id=user.id,
        username=str(user.email),
        source_ip=meta["source_ip"],
        user_agent=meta["user_agent"],
        organization_id=request_org_id(request),
        details=dict(_AUDIT_DETAILS),
    )


@router.get("/diarization/pyannote", response_model=PyannoteCredentialStatus)
def get_pyannote_credential_status(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> PyannoteCredentialStatus:
    """Whether the current user has a pyannote.ai key stored. The key is never returned."""
    return _status(_own_row(db, current_user.id), locked=_is_locked(request))


@router.put("/diarization/pyannote", response_model=PyannoteCredentialStatus)
def save_pyannote_credential(
    request: Request,
    body: PyannoteCredentialUpdate,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> PyannoteCredentialStatus:
    """Store or replace the current user's pyannote.ai key, encrypted at rest.

    A replaced key loses its previous test result: that result described a different key.
    """
    _refuse_if_locked(request)
    encrypted = encrypt_api_key(_checked_key(body.api_key))
    if not encrypted:
        raise HTTPException(status_code=500, detail="Failed to encrypt API key")

    row = _own_row(db, current_user.id)
    if row is None:
        row = UserDiarizationSettings(
            user_id=current_user.id,
            name=PYANNOTE_CREDENTIAL_NAME,
            provider=PYANNOTE_PROVIDER,
            model_name=PYANNOTE_DEFAULT_DIARIZATION_MODEL,
        )
        db.add(row)
    row.api_key = encrypted
    row.is_active = True
    row.test_status = None
    row.test_message = None
    row.last_tested = None
    db.commit()
    db.refresh(row)

    logger.info("User %d saved a pyannote.ai diarization key", current_user.id)
    _audit(request, current_user, AuditEventType.USER_CREDENTIAL_SET)
    return _status(row, locked=False)


@router.delete("/diarization/pyannote", response_model=PyannoteCredentialDeleted)
def delete_pyannote_credential(
    request: Request,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> PyannoteCredentialDeleted:
    """Delete the current user's key; a ``pyannote`` source reverts to the default.

    Allowed even when the deployment locks the source: removing your own secret is always
    permitted.
    """
    row = _own_row(db, current_user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="No pyannote.ai API key is saved")
    db.delete(row)

    source = _source_row(db, current_user.id)
    reverted = source is not None and source.setting_value == PYANNOTE_PROVIDER
    if reverted and source is not None:
        db.delete(source)
    db.commit()

    logger.info(
        "User %d deleted their pyannote.ai diarization key (source reverted: %s)",
        current_user.id,
        reverted,
    )
    _audit(request, current_user, AuditEventType.USER_CREDENTIAL_DELETE)
    remaining = DEFAULT_DIARIZATION_SOURCE if reverted or source is None else source.setting_value
    return PyannoteCredentialDeleted(
        deleted=True, diarization_source=str(remaining), source_reverted=reverted
    )


@router.post("/diarization/pyannote/test", response_model=PyannoteConnectionTestResult)
@limiter.limit(get_api_rate_limit())
def test_pyannote_credential(
    request: Request,
    body: PyannoteConnectionTestRequest | None = None,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> PyannoteConnectionTestResult:
    """Check a key against pyannote.ai's free ``GET /v1/test``. Runs no diarization job.

    With ``api_key`` in the body that key is tested and nothing is stored. Without it the
    saved key is tested and the outcome is recorded on it.
    """
    from app.services.diarization import pyannote_provider

    _refuse_if_locked(request)
    row = None
    api_key = _checked_key(body.api_key) if body is not None and body.api_key is not None else None
    if api_key is None:
        row = _own_row(db, current_user.id)
        if row is None or not row.api_key:
            raise HTTPException(status_code=404, detail="No pyannote.ai API key is saved")
        api_key = decrypt_api_key(str(row.api_key))
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The saved pyannote.ai API key can no longer be read. Save it again.",
            )

    start = time.time()
    provider = pyannote_provider.PyAnnoteCloudDiarizationProvider(api_key=api_key)
    success, message, _ = provider.validate_connection()
    codes: dict[str, PyannoteTestCode] = {
        pyannote_provider.CONNECTED_MESSAGE: "connected",
        pyannote_provider.KEY_REJECTED_MESSAGE: "rejected",
        pyannote_provider.UNREACHABLE_MESSAGE: "unreachable",
    }
    code = codes.get(message, "error")
    elapsed_ms = int((time.time() - start) * 1000)

    if row is not None:
        row.test_status = "success" if success else "failed"
        row.test_message = message
        row.last_tested = datetime.now(UTC)
        db.commit()

    logger.info(
        "User %d tested a %s pyannote.ai key: %s",
        current_user.id,
        "saved" if row is not None else "unsaved",
        "success" if success else "failed",
    )
    return PyannoteConnectionTestResult(
        success=success, code=code, message=message, response_time_ms=elapsed_ms
    )
