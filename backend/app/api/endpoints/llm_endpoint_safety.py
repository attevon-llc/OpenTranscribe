"""SSRF guard for user-supplied LLM endpoints.

Security-sensitive: every handler that dials or persists a caller-chosen ``base_url`` goes
through one of the two functions here.
"""

import logging

from fastapi import HTTPException

from app.utils.url_validation import PinnedTarget

logger = logging.getLogger(__name__)


def _assert_safe_llm_endpoint(base_url: str | None, purpose: str) -> None:
    """Refuse a user-supplied LLM endpoint that points at internal infrastructure.

    These endpoints take an arbitrary ``base_url`` from any authenticated user and fetch
    it server-side. With open self-registration that is effectively anonymous reach into
    the deployment's private network and cloud instance metadata (issue #284 A0.1).
    Set ``LLM_ALLOW_PRIVATE_ENDPOINTS=true`` on a single-tenant deployment that genuinely
    runs Ollama/vLLM on a private LAN.

    **This check is validate-only and cannot pin.** It is used where the fetch happens
    somewhere else entirely — ``POST /test-connection`` validates here and connects inside
    ``LLMService.validate_connection``, and a saved ``base_url`` is fetched later by a
    Celery task — so there is no single call frame in which one resolution could serve both
    steps. Where the handler *does* fetch, use :func:`_pin_llm_endpoint` instead; it keeps
    the resolved address and hands it to the client.

    **Called from three places, and all three must agree** (issue #820): ``POST
    /test-connection`` (below), ``POST ""`` (create), and ``PUT /config/{uuid}`` (update,
    only when the request actually sets ``base_url``). Before #820 only the test-connection
    handler called this — a config could be created or updated with an unsafe ``base_url``
    simply by never pressing "Test connection" first, including via the API directly. All
    three read the same ``settings.LLM_ALLOW_PRIVATE_ENDPOINTS`` escape hatch, so a LAN
    self-hoster who has opted in is not treated differently at create/update time than at
    test time.
    """
    if not base_url:
        return
    from app.core.config import settings
    from app.utils.url_validation import assert_safe_outbound_url

    assert_safe_outbound_url(
        base_url,
        purpose=purpose,
        allow_private=settings.LLM_ALLOW_PRIVATE_ENDPOINTS,
    )


def _pin_llm_endpoint(url: str, purpose: str) -> PinnedTarget:
    """Validate *url* and return the address to dial, for a handler that fetches inline.

    The model-discovery handlers below take ``base_url`` as a query parameter from any
    authenticated user and fetch it in the same function. Validating the hostname and then
    passing the hostname to aiohttp lets it resolve a **second** time, so a host whose DNS
    alternates public/``127.0.0.1`` passes the guard and is connected somewhere else.
    Pinning removes that window; see ``app.utils.url_validation`` for why it does not
    weaken TLS.

    Args:
        url: The user-supplied URL about to be fetched.
        purpose: Short label for the server-side log line.

    Returns:
        The pinned target.

    Raises:
        fastapi.HTTPException: 400, with a generic detail — the rejection reason
            distinguishes "private IP" from "cannot resolve" and would turn the endpoint
            into a network scanner.
    """
    from app.core.config import settings
    from app.utils.url_validation import resolve_pinned_target

    target, reason = resolve_pinned_target(url, allow_private=settings.LLM_ALLOW_PRIVATE_ENDPOINTS)
    if target is None:
        logger.warning("Blocked %s to %r: %s", purpose, url, reason)
        raise HTTPException(
            status_code=400,
            detail=(
                "The provided URL could not be used. It must be a publicly reachable "
                "http(s) address."
            ),
        )
    return target
