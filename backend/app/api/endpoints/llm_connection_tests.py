"""LLM connection, reasoning and context-window probe endpoints."""

import contextlib
import logging
import time
from datetime import UTC
from datetime import datetime
from typing import Any
from typing import TypedDict

from fastapi import APIRouter
from fastapi import Depends
from fastapi import HTTPException
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app import models
from app import schemas
from app.api.deps_context import get_current_context
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.llm_config_helpers import _get_accessible_config
from app.api.endpoints.llm_config_helpers import _visible_to
from app.api.endpoints.llm_endpoint_safety import _assert_safe_llm_endpoint
from app.auth.rate_limit import get_llm_outbound_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.services import llm_context_window
from app.services import llm_reasoning
from app.services.bedrock_settings import BedrockConnection
from app.services.bedrock_settings import connection_from_encrypted
from app.services.bedrock_settings import merge_stored
from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider as ServiceLLMProvider
from app.services.request_context import RequestContext
from app.utils.encryption import decrypt_api_key

router = APIRouter()
logger = logging.getLogger(__name__)


def _bedrock_for_test(
    db: Session, test_request: schemas.ConnectionTestRequest, current_user: models.User
) -> BedrockConnection:
    """The connection a Bedrock test runs with: entered values over the stored row's.

    A blank credential field in edit mode means "keep what is saved", so the stored key pair
    is used; with neither, the server's credential chain applies.
    """
    stored = None
    if test_request.config_id:
        row = (
            db.query(models.UserLLMSettings)
            .filter(
                models.UserLLMSettings.uuid == test_request.config_id,
                models.UserLLMSettings.user_id == current_user.id,
            )
            .first()
        )
        if row is not None and row.api_key and str(row.provider) == "bedrock":
            stored = connection_from_encrypted(str(row.api_key))
    return merge_stored(
        stored or BedrockConnection(),
        region=test_request.aws_region,
        region_given="aws_region" in test_request.model_fields_set,
        access_key_id=test_request.aws_access_key_id,
        secret_access_key=test_request.aws_secret_access_key,
        session_token=test_request.aws_session_token,
        clear_credentials=False,
    )


@router.post("/test", response_model=schemas.ConnectionTestResponse)
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
def test_llm_connection(
    *,
    request: Request,
    test_request: schemas.ConnectionTestRequest,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Test connection to LLM provider without saving settings.
    If config_id is provided and no api_key, will use the stored API key from that config.
    Only the caller's OWN config lends its key here: this request carries a
    caller-chosen ``base_url``, so a shared config's key would go wherever the caller
    pointed it. Users of a shared config test it with ``/test-config/{uuid}``, which
    dials the endpoint stored on the config.

    Rate-limited (issue #676): this handler makes a server-side outbound request to a
    caller-supplied ``base_url`` and sits behind ``get_current_active_user``, not an
    admin gate — see ``get_llm_outbound_rate_limit``.
    """
    _assert_safe_llm_endpoint(test_request.base_url, "LLM test-connection")
    start_time = time.time()

    try:
        # If no api_key provided but config_id is, look up the stored key
        effective_api_key = test_request.api_key
        if not effective_api_key and test_request.config_id:
            try:
                config = (
                    db.query(models.UserLLMSettings)
                    .filter(
                        models.UserLLMSettings.uuid == test_request.config_id,
                        models.UserLLMSettings.user_id == current_user.id,
                    )
                    .first()
                )
                if config and config.api_key and str(config.provider) != "bedrock":
                    # Decrypt the stored API key
                    effective_api_key = decrypt_api_key(str(config.api_key))
            except Exception as e:
                logger.warning(
                    f"Could not retrieve stored API key for config {test_request.config_id}: {e}"
                )

        # Map schema enum to service enum
        service_provider = ServiceLLMProvider(test_request.provider.value)

        bedrock = None
        if service_provider == ServiceLLMProvider.BEDROCK:
            effective_api_key = None
            bedrock = _bedrock_for_test(db, test_request, current_user)

        # Create LLM config for testing
        llm_config = LLMConfig(
            provider=service_provider,
            model=test_request.model_name,
            api_key=effective_api_key,
            base_url=test_request.base_url,
            bedrock=bedrock,
        )

        # Create and test LLM service
        llm_service = LLMService(llm_config)
        try:
            # Test the connection
            actual_url = llm_service.endpoints[service_provider]
            logger.debug(
                f"Testing LLM connection to: {actual_url} (Provider: {service_provider}, Model: {test_request.model_name})"
            )

            success, message = llm_service.validate_connection()
            response_time = int((time.time() - start_time) * 1000)

            return schemas.ConnectionTestResponse(
                success=success,
                status=schemas.ConnectionStatus.SUCCESS
                if success
                else schemas.ConnectionStatus.FAILED,
                message=f"{message} (URL: {actual_url})",
                response_time_ms=response_time,
            )
        finally:
            llm_service.close()

    except Exception:
        # This broad handler wraps config load + key decryption + service
        # construction, not just the connection dial — so the caught exception can
        # be an internal detail (a decryption failure, a malformed stored config)
        # rather than anything about the provider's reachability. The useful
        # diagnostic already comes back via the success path's own `message`
        # (which embeds the actual URL dialled); this arm stays generic (#859).
        response_time = int((time.time() - start_time) * 1000)
        logger.exception("LLM connection test failed")

        return schemas.ConnectionTestResponse(
            success=False,
            status=schemas.ConnectionStatus.FAILED,
            message="Connection test failed.",
            response_time_ms=response_time,
        )


class _BedrockTestFields(TypedDict, total=False):
    """The aws_* keyword arguments of ``ConnectionTestRequest``, typed for the ** splat.

    ``total=False`` because a non-Bedrock row must leave them UNSET, not None:
    ``aws_region in model_fields_set`` decides whether the test honours a given region.
    """

    aws_region: str | None
    aws_access_key_id: str | None
    aws_secret_access_key: str | None
    aws_session_token: str | None


def _test_request_from_row(user_config: models.UserLLMSettings) -> schemas.ConnectionTestRequest:
    """A test request describing a stored configuration, credentials decrypted in-process.

    The request object lives only for the call that tests it; it is never serialized.
    """
    try:
        api_key, bedrock = LLMService.credentials_from_row(user_config)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials") from exc

    aws: _BedrockTestFields = {}
    if bedrock is not None:
        aws = {
            "aws_region": bedrock.region,
            "aws_access_key_id": bedrock.access_key_id,
            "aws_secret_access_key": bedrock.secret_access_key,
            "aws_session_token": bedrock.session_token,
        }
    return schemas.ConnectionTestRequest(
        provider=schemas.LLMProvider(user_config.provider),
        model_name=str(user_config.model_name),
        api_key=api_key,
        base_url=str(user_config.base_url) if user_config.base_url else None,
        **aws,
    )


@router.post("/test-current", response_model=schemas.ConnectionTestResponse)
def test_active_configuration(
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Test connection using current user's active LLM configuration
    """
    current_user = ctx.user
    # Get active configuration
    active_setting = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == current_user.id,
            models.UserSetting.setting_key == "active_llm_config_id",
        )
        .first()
    )

    if not active_setting or not active_setting.setting_value:
        raise HTTPException(
            status_code=404,
            detail="No active LLM configuration found. Please set an active configuration first.",
        )

    try:
        active_config_id = int(active_setting.setting_value)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid active configuration ID") from e

    user_config = (
        db.query(models.UserLLMSettings)
        .filter(models.UserLLMSettings.id == active_config_id, _visible_to(ctx))
        .first()
    )

    if not user_config:
        raise HTTPException(
            status_code=404,
            detail="Active LLM configuration not found.",
        )

    test_request = _test_request_from_row(user_config)

    # `db=db` is not optional: `test_llm_connection` declares `db: Session = Depends(get_db)`,
    # so calling it in-process without it binds `db` to the `fastapi.params.Depends` OBJECT.
    # Harmless only while these two callers never set `config_id` on the request they build —
    # the first one that does gets an AttributeError on a Depends instance.
    # `request=request` is required for the same reason (issue #676 made it keyword-only, for
    # the outbound rate limiter's per-user/per-IP key); omitting it is a TypeError at call time,
    # which is what broke both of these endpoints.
    result = test_llm_connection(
        request=request, test_request=test_request, current_user=current_user, db=db
    )

    # Only write back test status if the current user owns the config
    if user_config.user_id == current_user.id:
        user_config.test_status = result.status.value  # type: ignore[assignment]
        user_config.test_message = result.message  # type: ignore[assignment]
        user_config.last_tested = datetime.now(UTC)  # type: ignore[assignment]

        db.add(user_config)
        db.commit()

    return result


@router.post("/test-config/{config_uuid}", response_model=schemas.ConnectionTestResponse)
def test_specific_configuration(
    config_uuid: str,
    request: Request,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """
    Test connection for a specific LLM configuration
    """
    current_user = ctx.user
    user_config = _get_accessible_config(db, config_uuid, ctx)

    test_request = _test_request_from_row(user_config)

    # `db=db` is not optional: `test_llm_connection` declares `db: Session = Depends(get_db)`,
    # so calling it in-process without it binds `db` to the `fastapi.params.Depends` OBJECT.
    # Harmless only while these two callers never set `config_id` on the request they build —
    # the first one that does gets an AttributeError on a Depends instance.
    # `request=request` is required for the same reason (issue #676 made it keyword-only, for
    # the outbound rate limiter's per-user/per-IP key); omitting it is a TypeError at call time,
    # which is what broke both of these endpoints.
    result = test_llm_connection(
        request=request, test_request=test_request, current_user=current_user, db=db
    )

    # Only write back test status if the current user owns the config
    if user_config.user_id == current_user.id:
        user_config.test_status = result.status.value  # type: ignore[assignment]
        user_config.test_message = result.message  # type: ignore[assignment]
        user_config.last_tested = datetime.now(UTC)  # type: ignore[assignment]

        db.add(user_config)
        db.commit()

    return result


def _config_to_llm_config(db: Session, user_config: models.UserLLMSettings) -> LLMConfig:
    """Build the LLMConfig a stored row describes, decrypting its key."""
    try:
        api_key, bedrock = LLMService.credentials_from_row(user_config)
    except ValueError as exc:
        raise HTTPException(status_code=500, detail="Failed to decrypt stored credentials") from exc
    return LLMConfig(
        provider=ServiceLLMProvider(user_config.provider),
        model=str(user_config.model_name),
        api_key=api_key,
        base_url=str(user_config.base_url) if user_config.base_url else None,
        max_tokens=int(user_config.max_tokens),
        temperature=float(user_config.temperature),
        bedrock=bedrock,
    )


@router.get("/config/{config_uuid}/reasoning", response_model=schemas.ReasoningCapability)
def get_reasoning_capability(
    config_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Read the recorded reasoning off-switch verdict for one configuration.

    Pure read — it never dials the provider. A model that has never been probed
    reports ``unknown``, which is the default everywhere and renders no control.
    """
    user_config = _get_accessible_config(db, config_uuid, ctx)

    provider = str(user_config.provider)
    base_url = str(user_config.base_url) if user_config.base_url else None
    model = str(user_config.model_name)
    return _reasoning_capability_payload(db, provider, base_url, model)


@router.post("/config/{config_uuid}/reasoning-probe", response_model=schemas.ReasoningCapability)
def probe_reasoning_capability(
    config_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Measure, and record, whether this model honours a "do not reason" request.

    **Explicitly invoked, never automatic.** Three real generations against the
    configured endpoint is far too much to put in front of a user's first chat
    turn, and a background sweep would dial every configured third-party
    provider unprompted — an egress event, and a bill, that nobody asked for.
    So it sits beside "Test connection", where the cost is visible and the user
    chose to pay it.

    The verdict is keyed to (provider, endpoint, model), so it survives until
    one of those changes and is then simply not found again — there is no stale
    answer to invalidate.
    """
    user_config = _get_accessible_config(db, config_uuid, ctx)

    llm_config = _config_to_llm_config(db, user_config)
    result = llm_reasoning.probe(llm_config)
    llm_reasoning.record(db, llm_config, result)
    return _reasoning_capability_payload(
        db, str(llm_config.provider), llm_config.base_url, llm_config.model
    )


def _reasoning_capability_payload(
    db: Session, provider: str, base_url: str | None, model: str
) -> schemas.ReasoningCapability:
    """Shape a stored verdict (or its absence) into the wire schema."""
    stored = llm_reasoning.read_record(db, provider, base_url, model)
    chars = stored.get("reasoning_chars") or {}
    probed_at = None
    raw_probed_at = stored.get("probed_at")
    if isinstance(raw_probed_at, str):
        with contextlib.suppress(ValueError):
            probed_at = datetime.fromisoformat(raw_probed_at)
    return schemas.ReasoningCapability(
        off_switch=llm_reasoning.verdict_of(stored),
        probeable=ServiceLLMProvider(provider) in llm_reasoning.PROBEABLE_PROVIDERS,
        probed_at=probed_at,
        reasoning_chars_on=int(chars.get("on") or 0),
        reasoning_chars_off=int(chars.get("off") or 0),
        reasoning_chars_omitted=int(chars.get("omitted") or 0),
        detail=str(stored.get("detail") or ""),
    )


@router.get("/config/{config_uuid}/context-window", response_model=schemas.ContextWindowCapability)
def get_context_window(
    config_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Read the recorded context-window measurement for one configuration.

    Pure read — it never dials the provider. An unprobed model reports
    ``unknown`` and the user's declared ``max_tokens`` stands.
    """
    user_config = _get_accessible_config(db, config_uuid, ctx)
    return _context_window_payload(db, user_config)


@router.post(
    "/config/{config_uuid}/context-window-probe", response_model=schemas.ContextWindowCapability
)
def probe_context_window(
    config_uuid: str,
    db: Session = Depends(get_db),
    ctx: RequestContext = Depends(get_current_context),
) -> Any:
    """Discover, and record, the model's maximum context window (issue #533).

    One metadata call (``/v1/models`` or ``/api/show``) — no generation, no user
    content. **Explicitly invoked, never automatic**, for the same reason as the
    reasoning probe beside it: a background sweep would dial every configured
    endpoint unprompted. The verdict is keyed to (provider, endpoint, model);
    editing any of the three orphans it rather than staling it.
    """
    user_config = _get_accessible_config(db, config_uuid, ctx)
    llm_config = _config_to_llm_config(db, user_config)
    result = llm_context_window.probe(llm_config)
    llm_context_window.record(db, llm_config, result)
    return _context_window_payload(db, user_config)


def _context_window_payload(
    db: Session, user_config: models.UserLLMSettings
) -> schemas.ContextWindowCapability:
    """Shape a stored measurement (or its absence) into the wire schema."""
    provider = str(user_config.provider)
    base_url = str(user_config.base_url) if user_config.base_url else None
    model = str(user_config.model_name)
    stored = llm_context_window.read_record(db, provider, base_url, model)
    window = llm_context_window.measured_window(stored)
    configured = int(user_config.max_tokens)
    relation = None
    if window is not None:
        relation = "below" if configured < window else "above" if configured > window else "match"
    probed_at = None
    raw_probed_at = stored.get("probed_at")
    if isinstance(raw_probed_at, str):
        with contextlib.suppress(ValueError):
            probed_at = datetime.fromisoformat(raw_probed_at)
    raw_status = stored.get("status")
    try:
        status = llm_context_window.ContextWindowStatus(str(raw_status))
    except ValueError:
        # Includes a record written by a newer build: an unrecognised status
        # must read as unprobed, not raise the settings page.
        status = llm_context_window.ContextWindowStatus.UNKNOWN
    return schemas.ContextWindowCapability(
        status=status,
        discoverable=(ServiceLLMProvider(provider) in llm_context_window.DISCOVERABLE_PROVIDERS),
        context_window=window,
        configured_max_tokens=configured,
        relation=relation,
        probed_at=probed_at,
        detail=str(stored.get("detail") or ""),
    )
