"""ASR connection test endpoints (``/test``, ``/test-config/{config_uuid}``).

Mirrors ``llm_connection_tests``: ad-hoc and saved-config connection checks, with provider
error text scrubbed of credentials before it is stored or returned.
"""

import logging
import re
import time
from datetime import UTC
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints.asr_config_state import _get_config_or_404
from app.api.endpoints.auth import get_current_active_user
from app.auth.rate_limit import get_api_rate_limit
from app.auth.rate_limit import limiter
from app.db.base import get_db
from app.services.request_context import RequestContext
from app.utils.encryption import decrypt_api_key

router = APIRouter()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Sensitive-data sanitization for error messages
# ---------------------------------------------------------------------------

# Matches Bearer tokens and high-entropy strings with common API-key prefixes,
# as well as generic base64-ish blobs of 32+ chars that could be a raw key.
_KEY_PATTERN = re.compile(
    r"(Bearer\s+)\S+|(sk-|dg_|aai_)\S+",
    re.IGNORECASE,
)


def _sanitize_message(message: str, *secrets: str | None) -> str:
    """Remove known secrets and API-key-shaped tokens from an error message.

    Performs two passes:
    1. Exact replacement of each provided *secret* value.
    2. Regex-based scrubbing of Bearer tokens and common API-key prefixes.
    """
    if not message:
        return message
    result = message
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    result = _KEY_PATTERN.sub(
        lambda m: m.group(1) + "***" if m.group(1) else m.group(2) + "***", result
    )
    return result


def _create_asr_provider(
    provider: str,
    api_key: str | None,
    model: str | None,
    base_url: str | None,
    region: str | None,
    access_key_id: str | None = None,
):
    """Create an ASR provider instance via the factory."""
    from app.services.asr.factory import ASRProviderFactory  # type: ignore[import]

    return ASRProviderFactory.create_from_config(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        region=region,
        access_key_id=access_key_id,
    )


@router.post("/test")
@limiter.limit(get_api_rate_limit())
def test_asr_connection(
    request: Request,
    settings_in: schemas.ASRConnectionTestRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Test an ad-hoc ASR connection without saving configuration."""
    provider_name = settings_in.provider.value
    api_key = settings_in.api_key
    base_url = settings_in.base_url
    region = settings_in.region
    model_name = settings_in.model_name
    access_key_id = getattr(settings_in, "access_key_id", None)

    start_time = time.time()
    try:
        provider = _create_asr_provider(
            provider=provider_name,
            api_key=api_key,
            model=model_name,
            base_url=base_url,
            region=region,
            access_key_id=access_key_id,
        )
        success, message, response_time_ms = provider.validate_connection()
    except Exception as exc:
        response_time_ms = (time.time() - start_time) * 1000
        sanitized = _sanitize_message(str(exc), api_key)
        logger.warning(
            "ASR ad-hoc test failed for provider '%s': %s",
            provider_name,
            sanitized,
        )
        return {
            "success": False,
            "message": sanitized,
            "response_time_ms": int(response_time_ms),
        }

    return {
        "success": success,
        "message": message,
        "response_time_ms": int(response_time_ms),
    }


@router.post("/test-config/{config_uuid}")
@limiter.limit(get_api_rate_limit())
def test_saved_asr_config(
    request: Request,
    config_uuid: UUID,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Test a saved ASR configuration and persist the result (test_status, test_message, last_tested)."""
    current_user = ctx.user
    config = _get_config_or_404(db, config_uuid, current_user.id, shared_in=ctx)

    # Decrypt the stored key only for error sanitization below — provider construction
    # (and its own decryption) goes through the shared factory helper so this path and
    # the transcription-job path cannot drift apart (issue #300).
    api_key = None
    if config.api_key:
        api_key = decrypt_api_key(str(config.api_key))
        if api_key is None:
            logger.error("Failed to decrypt API key for ASR config %s", config_uuid)
            raise HTTPException(status_code=500, detail="Failed to decrypt stored API key")

    start_time = time.time()
    try:
        from app.services.asr.factory import ASRProviderFactory

        provider = ASRProviderFactory.create_from_db_config(config)
        success, message, response_time_ms = provider.validate_connection()
    except Exception as exc:
        success = False
        # Sanitize before storing or returning — provider exceptions may echo back the key
        message = _sanitize_message(str(exc), api_key)
        response_time_ms = (time.time() - start_time) * 1000
        logger.warning("ASR saved-config test failed for config %s: %s", config_uuid, message)

    # Persist test result only if the current user owns the config
    if config.user_id == current_user.id:
        config.test_status = "success" if success else "failed"  # type: ignore[assignment]
        config.test_message = _sanitize_message(message, api_key)  # type: ignore[assignment]
        config.last_tested = datetime.now(UTC)  # type: ignore[assignment]
        db.add(config)
        db.commit()

    return {
        "success": success,
        "message": message,
        "response_time_ms": int(response_time_ms),
        "config_uuid": str(config.uuid),
    }
