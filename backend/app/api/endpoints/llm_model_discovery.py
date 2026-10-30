"""Provider model-listing endpoints (Ollama, OpenAI-compatible, Anthropic)."""

import logging
import uuid
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response
from sqlalchemy.orm import Session

from app import models
from app.api.endpoints.auth import get_current_active_user
from app.api.endpoints.llm_endpoint_safety import _pin_llm_endpoint
from app.auth.rate_limit import get_llm_outbound_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key
from app.db.base import get_db
from app.utils.encryption import decrypt_api_key
from app.utils.url_validation import PinnedTarget

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/ollama/models")
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
async def get_ollama_models(
    request: Request,
    base_url: str,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from an Ollama instance

    Rate-limited (issue #676): fetches a caller-supplied ``base_url`` server-side —
    see ``get_llm_outbound_rate_limit``.
    """
    import aiohttp

    from app.utils.url_validation import pinned_aiohttp_session

    # Clean up base URL
    clean_url = base_url.strip().rstrip("/")
    if clean_url.endswith("/v1"):
        clean_url = clean_url[:-3]  # Remove /v1 suffix

    models_url = f"{clean_url}/api/tags"

    # Validate and PIN the URL actually fetched, not `base_url` — they differ, and a
    # validate-only check would let aiohttp re-resolve at connect time. This stays OUTSIDE
    # the try: the bare `except Exception` below would turn the 400 into a 200 carrying
    # `success: false`, i.e. a refused SSRF reported as a connection problem.
    target = _pin_llm_endpoint(models_url, "Ollama model discovery")

    try:
        async with (
            pinned_aiohttp_session(target, timeout_seconds=10) as session,
            # `original_url`: the resolver does the pinning, so the request keeps its
            # hostname and aiohttp derives SNI + certificate name from it unaided.
            # `allow_redirects=False`: a pin covers one hop.
            #
            # Bound as `http_response`, not `response`: this handler already has a
            # `response: Response` parameter (slowapi injects the rate-limit headers
            # through it), and rebinding that name to an `aiohttp.ClientResponse`
            # shadowed it — leaving the two objects one identifier apart in a body
            # that reads `.status`/`.json()` off one and would want `.headers` on
            # the other. Its two sibling fetchers already have distinct scopes.
            session.get(target.original_url, allow_redirects=False) as http_response,
        ):
            if http_response.status == 200:
                data = await http_response.json()
                models = []

                if "models" in data:
                    for model in data["models"]:
                        models.append(
                            {
                                "name": model.get("name", ""),
                                "size": model.get("size", 0),
                                "modified_at": model.get("modified_at", ""),
                                "digest": model.get("digest", ""),
                                "details": model.get("details", {}),
                                "display_name": model.get("name", "").split(":")[
                                    0
                                ],  # Remove tag for display
                            }
                        )

                return {
                    "success": True,
                    "models": models,
                    "total": len(models),
                    "message": f"Found {len(models)} models on Ollama server",
                }
            else:
                error_text = await http_response.text()
                return {
                    "success": False,
                    "models": [],
                    "total": 0,
                    "message": (
                        f"Failed to fetch models: HTTP {http_response.status} - {error_text}"
                    ),
                }
    except aiohttp.ClientError as e:
        return {
            "success": False,
            "models": [],
            "total": 0,
            "message": f"Connection error: {str(e)}",
        }
    except Exception:
        # Broad catch around whatever aiohttp/JSON parsing can raise beyond the
        # narrow ClientError above — kept generic rather than echoing the
        # exception text into the response (#859).
        logger.exception(f"Error fetching Ollama models from {base_url}")
        return {
            "success": False,
            "models": [],
            "total": 0,
            "message": "Unexpected error while contacting the provider.",
        }


# --- Model Discovery Helper Functions ---


def _model_discovery_response(
    success: bool, model_list: list[Any] | None = None, message: str = ""
) -> dict[str, Any]:
    """Create a standardized model discovery response."""
    return {
        "success": success,
        "models": model_list or [],
        "total": len(model_list) if model_list else 0,
        "message": message,
    }


def _get_stored_api_key(db: Session, config_id: str, user_id: int) -> str | None:
    """Retrieve and decrypt the stored API key of one of the caller's OWN configs.

    Model discovery dials a caller-supplied ``base_url``, so a shared config's key is
    never lent here — it would go wherever the caller pointed it.
    """
    try:
        config_uuid = uuid.UUID(config_id)
        config = (
            db.query(models.UserLLMSettings)
            .filter(
                models.UserLLMSettings.uuid == config_uuid,
                models.UserLLMSettings.user_id == user_id,
            )
            .first()
        )
        if config and config.api_key and str(config.provider) != "bedrock":
            return decrypt_api_key(str(config.api_key))
    except (ValueError, Exception) as e:
        logger.warning(f"Could not retrieve stored API key for config {config_id}: {e}")
    return None


def _extract_raw_models(data: Any) -> tuple[list | None, str | None]:
    """
    Extract raw models list from various OpenAI-compatible response formats.

    Returns (raw_models, error_message). If error_message is set, raw_models is None.
    """
    # Format 1: OpenAI standard { "data": [...] }
    if isinstance(data, dict) and "data" in data and isinstance(data["data"], list):
        logger.debug("Model discovery: Using OpenAI standard format (data array)")
        return data["data"], None

    # Format 2: Direct array response [...]
    if isinstance(data, list):
        logger.debug("Model discovery: Using direct array format")
        return data, None

    # Format 3: { "models": [...] } (some providers use this)
    if isinstance(data, dict) and "models" in data and isinstance(data["models"], list):
        logger.debug("Model discovery: Using 'models' key format")
        return data["models"], None

    # Format 4: { "object": "list", "data": [...] } (explicit OpenAI format)
    if isinstance(data, dict) and data.get("object") == "list" and "data" in data:
        logger.debug("Model discovery: Using OpenAI list object format")
        return data["data"], None

    # Unexpected format
    keys_info = list(data.keys()) if isinstance(data, dict) else type(data).__name__
    error_msg = (
        f"Unexpected response format. Expected 'data' or 'models' array. Got keys: {keys_info}"
    )
    logger.warning(f"Model discovery: {error_msg}")
    return None, error_msg


def _parse_model_entry(model: Any) -> dict | None:
    """Parse a single model entry into standardized format."""
    if isinstance(model, dict):
        model_id = model.get("id") or model.get("name") or model.get("model") or ""
        return {
            "name": model.get("name", model_id),
            "id": model_id,
            "owned_by": model.get("owned_by", model.get("owner", "")),
            "created": model.get("created", model.get("created_at", 0)),
        }
    if isinstance(model, str):
        return {"name": model, "id": model, "owned_by": "", "created": 0}
    logger.warning(f"Model discovery: Skipping unexpected model format: {type(model)}")
    return None


def _get_http_error_message(status_code: int, models_url: str, error_text: str = "") -> str:
    """Get user-friendly error message for HTTP error codes."""
    error_messages = {
        401: "Authentication failed: Invalid or missing API key",
        403: "Access forbidden: Check API key permissions",
        404: f"Models endpoint not found at {models_url}. Check base URL configuration.",
    }
    if status_code in error_messages:
        return error_messages[status_code]
    return f"Failed to fetch models: HTTP {status_code} - {error_text[:200]}"


@router.get("/openai-compatible/models")
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
async def get_openai_compatible_models(
    request: Request,
    base_url: str,
    api_key: str | None = None,
    config_id: str | None = None,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from an OpenAI-compatible API endpoint.

    Supports: OpenAI, vLLM, OpenRouter, and other OpenAI-compatible providers.
    If config_id is provided and no api_key, will use the stored API key from that config.

    Rate-limited (issue #676): fetches a caller-supplied ``base_url`` server-side —
    see ``get_llm_outbound_rate_limit``.
    """
    import aiohttp

    # Resolve effective API key
    effective_api_key = api_key
    if not effective_api_key and config_id:
        effective_api_key = _get_stored_api_key(db, config_id, current_user.id)

    # Build models URL
    clean_url = base_url.strip().rstrip("/")
    if not clean_url.endswith("/v1"):
        clean_url = f"{clean_url}/v1"
    models_url = f"{clean_url}/models"

    # Prepare headers
    headers = {"Authorization": f"Bearer {effective_api_key}"} if effective_api_key else {}

    # Validate and PIN the URL actually fetched (`models_url`, not `base_url`). Outside the
    # try, so the 400 is not swallowed into a `success: false` 200 by the handlers below.
    target = _pin_llm_endpoint(models_url, "OpenAI-compatible model discovery")

    try:
        return await _fetch_and_parse_models(target, headers, base_url)
    except aiohttp.ClientConnectorError:
        logger.warning(f"Model discovery: Connection failed to {base_url}")
        return _model_discovery_response(
            False,
            message=f"Connection failed: Could not reach {base_url}. Check the URL and ensure the server is running.",
        )
    except aiohttp.ClientError as e:
        logger.warning(f"Model discovery: Client error for {base_url}: {e}")
        return _model_discovery_response(False, message=f"Connection error: {str(e)}")
    except TimeoutError:
        logger.warning(f"Model discovery: Timeout connecting to {base_url}")
        return _model_discovery_response(
            False,
            message=f"Connection timeout: Server at {base_url} did not respond within 10 seconds.",
        )
    except Exception:
        # Broad catch beyond the narrow aiohttp errors above — kept generic
        # rather than echoing the exception text into the response (#859).
        logger.exception(f"Error fetching OpenAI-compatible models from {base_url}")
        return _model_discovery_response(
            False, message="Unexpected error while contacting the provider."
        )


async def _fetch_and_parse_models(
    target: PinnedTarget, headers: dict, base_url: str
) -> dict[str, Any]:
    """Fetch models from a **pinned** target and parse the response.

    Takes a ``PinnedTarget`` rather than a URL string so the address that the SSRF guard
    validated is the address dialled — passing a URL here would let aiohttp resolve the
    hostname a second time, which is the whole DNS-rebinding window.
    """
    from app.utils.url_validation import pinned_aiohttp_session

    models_url = target.original_url
    async with (
        pinned_aiohttp_session(target, timeout_seconds=10) as session,
        # A pin covers one hop; a 302 to an internal address would otherwise be followed
        # with no check at all.
        session.get(models_url, headers=headers, allow_redirects=False) as response,
    ):
        if response.status != 200:
            error_text = await response.text() if response.status not in (401, 403, 404) else ""
            if response.status not in (401, 403, 404):
                logger.warning(
                    f"Model discovery: HTTP {response.status} from {models_url}: {error_text[:200]}"
                )
            return _model_discovery_response(
                False, message=_get_http_error_message(response.status, models_url, error_text)
            )

        # Parse JSON response
        try:
            data = await response.json()
        except Exception:
            # A response body that failed to parse as JSON could quote a token or
            # other sensitive fragment of what the provider sent back — never
            # returned to the caller (#859).
            logger.exception(f"Model discovery: Invalid JSON response from {models_url}")
            return _model_discovery_response(
                False, message="The provider returned an unparseable response."
            )

        # Extract raw models from various formats
        raw_models, error_msg = _extract_raw_models(data)
        if error_msg:
            return _model_discovery_response(False, message=error_msg)

        # Parse each model entry
        model_list = [parsed for m in (raw_models or []) if (parsed := _parse_model_entry(m))]

        if not model_list and raw_models:
            logger.warning(
                f"Model discovery: Found {len(raw_models)} raw models but none could be parsed"
            )
            return _model_discovery_response(
                False,
                message=f"Found {len(raw_models)} models but could not parse them. Check provider compatibility.",
            )

        logger.info(
            f"Model discovery: Successfully found {len(model_list)} models from {models_url}"
        )
        return _model_discovery_response(True, model_list, f"Found {len(model_list)} models")


def _parse_anthropic_model(model: dict) -> dict:
    """Parse a single Anthropic model entry into standardized format."""
    return {
        "id": model.get("id", ""),
        "display_name": model.get("display_name", model.get("id", "")),
        "created_at": model.get("created_at", ""),
        "type": model.get("type", "model"),
    }


async def _fetch_anthropic_models(headers: dict) -> dict[str, Any]:
    """Fetch and parse models from Anthropic API."""
    import aiohttp

    models_url = "https://api.anthropic.com/v1/models"
    timeout = aiohttp.ClientTimeout(total=10)

    async with (
        aiohttp.ClientSession(timeout=timeout) as session,
        session.get(models_url, headers=headers) as response,
    ):
        if response.status != 200:
            error_text = await response.text() if response.status not in (401, 403) else ""
            return _model_discovery_response(
                False, message=_get_http_error_message(response.status, models_url, error_text)
            )

        data = await response.json()
        # Anthropic returns { "data": [...], "has_more": bool, "first_id": str, "last_id": str }
        model_list = [_parse_anthropic_model(m) for m in data.get("data", [])]

        logger.info(f"Anthropic model discovery: Found {len(model_list)} models")
        return _model_discovery_response(True, model_list, f"Found {len(model_list)} models")


@router.get("/anthropic/models")
async def get_anthropic_models(
    api_key: str | None = None,
    config_id: str | None = None,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """
    Get available models from Anthropic API.
    If config_id is provided and no api_key, will use the stored API key from that config.
    """
    import aiohttp

    # Resolve effective API key
    effective_api_key = api_key
    if not effective_api_key and config_id:
        effective_api_key = _get_stored_api_key(db, config_id, current_user.id)

    if not effective_api_key:
        return _model_discovery_response(
            False, message="API key is required to fetch Anthropic models"
        )

    headers = {
        "x-api-key": effective_api_key,
        "anthropic-version": "2023-06-01",
    }

    try:
        return await _fetch_anthropic_models(headers)
    except aiohttp.ClientConnectorError:
        return _model_discovery_response(
            False, message="Connection failed: Could not reach Anthropic API"
        )
    except aiohttp.ClientError as e:
        return _model_discovery_response(False, message=f"Connection error: {str(e)}")
    except TimeoutError:
        return _model_discovery_response(
            False, message="Connection timeout: Anthropic API did not respond within 10 seconds"
        )
    except Exception:
        # Broad catch beyond the narrow aiohttp errors above — kept generic
        # rather than echoing the exception text into the response (#859).
        logger.exception("Error fetching Anthropic models")
        return _model_discovery_response(
            False, message="Unexpected error while contacting the provider."
        )
