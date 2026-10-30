"""Local LLM endpoint discovery (issue #644)."""

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Request
from fastapi import Response

from app import models
from app import schemas
from app.api.endpoints.auth import get_current_active_user
from app.auth.rate_limit import get_llm_outbound_rate_limit
from app.auth.rate_limit import limiter
from app.auth.rate_limit import user_or_ip_key

router = APIRouter()


# --- Local endpoint discovery (issue #644) ---
#
# A self-hoster configuring a local provider has no way to discover the internal
# Docker Compose *service name* a local server answers on (`http://llm-test-vllm:8000/v1`,
# `http://mock-llm:5199/v1`) short of reading a compose file comment. This section is a
# FIXED-ALLOWLIST reachability probe over this repo's own dev/test overlay services —
# never a URL/host/port taken from a request, an env var, or a compose file read at
# runtime. See `docs-site` root CLAUDE.md's #644 write-up for the full design rationale
# and the rejected alternatives (probing a caller-supplied URL, reading compose files in
# the container, enumerating the Docker socket).


@dataclass(frozen=True)
class _LocalEndpointSpec:
    """One row of `_LOCAL_ENDPOINT_TABLE` — see that constant's docstring."""

    id: str
    label: str
    #: Container-network base URL — see `schemas.LocalEndpointStatus`'s docstring on
    #: why this is never a host-side `127.0.0.1:<port>` value.
    base_url: str
    provider: schemas.LLMProvider
    #: Path appended to `base_url` for the liveness probe — a KNOWN metadata path, never
    #: a generic fetch (`/v1/models` for the OpenAI-compatible rows, `/api/tags` for
    #: Ollama's native API).
    probe_path: str
    start_command: str


#: The full set of endpoints this feature can ever dial. Fixed at import time — nothing
#: here is derived from a request, an env var, a DB row, or a compose file read at
#: runtime (issue #644 §C.5, non-negotiable #1). `provider` is `custom` for the two
#: OpenAI-compatible rows, matching the compose overlays' own operator instructions
#: (`docker-compose.llm-test.yml`'s header, `docker-compose.mock-llm.yml`'s) now that
#: issue #839 made `custom` a selectable provider in the admin UI.
#:
#: ⚠️ This table WILL rot as overlays are added or renamed — the drift test
#: (`tests/unit/test_llm_local_endpoints.py`) parses the two compose files and asserts
#: these service names/ports still match, so a rename here fails loudly instead of
#: silently pointing at a service that no longer exists.
_LOCAL_ENDPOINT_TABLE: tuple[_LocalEndpointSpec, ...] = (
    _LocalEndpointSpec(
        id="llm-test-vllm",
        label="vLLM (--with-llm-test)",
        base_url="http://llm-test-vllm:8000/v1",
        provider=schemas.LLMProvider.CUSTOM,
        probe_path="/models",
        start_command="./opentr.sh start dev --with-llm-test",
    ),
    _LocalEndpointSpec(
        id="llm-test-ollama",
        label="Ollama (--with-llm-test, --profile ollama)",
        base_url="http://llm-test-ollama:11434",
        provider=schemas.LLMProvider.OLLAMA,
        probe_path="/api/tags",
        # Not started by --with-llm-test alone (docker-compose.llm-test.yml:116) — the
        # start command must say so, or this reproduces #644's original complaint in a
        # new place (issue #644 §C.8 T2).
        start_command=(
            "docker compose -f docker-compose.yml -f docker-compose.llm-test.yml "
            "--profile ollama up -d llm-test-ollama"
        ),
    ),
    _LocalEndpointSpec(
        id="mock-llm",
        label="Mock LLM (--with-mock-llm)",
        base_url="http://mock-llm:5199/v1",
        provider=schemas.LLMProvider.CUSTOM,
        probe_path="/models",
        start_command="./opentr.sh start dev --with-mock-llm",
    ),
)

#: Per-target timeout AND the effective overall budget, since every target is probed
#: concurrently (issue #644 §C.5 non-negotiable #3) — a fully-down set must not make this
#: endpoint slow.
_LOCAL_ENDPOINT_PROBE_TIMEOUT_SECONDS = 3.0


def _unreachable_local_endpoint(spec: _LocalEndpointSpec) -> dict[str, Any]:
    return {
        "id": spec.id,
        "label": spec.label,
        "base_url": spec.base_url,
        "provider": spec.provider,
        "reachable": False,
        "models": [],
        "start_command": spec.start_command,
    }


def _extract_local_endpoint_models(spec: _LocalEndpointSpec, data: Any) -> list[str]:
    """Best-effort model-name extraction — never raises, never echoes raw response text."""
    try:
        if spec.probe_path == "/api/tags":
            return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]
    except (AttributeError, TypeError):
        return []


async def _probe_local_endpoint(spec: _LocalEndpointSpec) -> dict[str, Any]:
    """Probe one fixed-table row. Reports `reachable: true|false` only — NEVER a reason.

    Distinguishing "refused" from "unresolvable" from "connection reset" from "non-200"
    would turn this endpoint into a network scanner behind an auth cookie (issue #644 §C.5
    non-negotiable #5, mirroring this module's existing model-discovery handlers). Routed
    through `app.utils.url_validation` like every sibling outbound fetch in this file —
    `allow_private=True` unconditionally, because every URL here is one of OUR OWN fixed,
    trusted, hardcoded targets, never user input; it is not gated on this deployment's
    `LLM_ALLOW_PRIVATE_ENDPOINTS`, which governs whether a *saved configuration* may later
    be dialled, a separate question from "is the dev overlay up right now".
    """
    from app.utils.url_validation import pinned_aiohttp_session
    from app.utils.url_validation import resolve_pinned_target

    probe_url = spec.base_url.rstrip("/") + spec.probe_path
    target, _reason = resolve_pinned_target(probe_url, allow_private=True)
    if target is None:
        return _unreachable_local_endpoint(spec)

    try:
        async with (
            pinned_aiohttp_session(
                target, timeout_seconds=_LOCAL_ENDPOINT_PROBE_TIMEOUT_SECONDS
            ) as session,
            session.get(target.original_url, allow_redirects=False) as http_response,
        ):
            if http_response.status != 200:
                return _unreachable_local_endpoint(spec)
            data = await http_response.json()
    except Exception:
        # Broad on purpose — a down/unreachable dev overlay is the expected common case,
        # not a bug to log loudly about, and no failure detail may reach the response.
        return _unreachable_local_endpoint(spec)

    return {
        "id": spec.id,
        "label": spec.label,
        "base_url": spec.base_url,
        "provider": spec.provider,
        "reachable": True,
        "models": _extract_local_endpoint_models(spec, data),
        "start_command": spec.start_command,
    }


@router.get("/local-endpoints", response_model=schemas.LocalEndpointsResponse)
@limiter.limit(get_llm_outbound_rate_limit(), key_func=user_or_ip_key)
async def get_local_endpoints(
    request: Request,
    response: Response = None,  # type: ignore[assignment]  # required by slowapi
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Report which of this repo's own dev/test LLM overlay services are up right now.

    Takes **no** url/host/port parameter and no free parameter of any kind — the set of
    things it can ever dial is the module-level `_LOCAL_ENDPOINT_TABLE` above, fixed at
    import time (issue #644). Rate-limited like this file's other outbound-fetch handlers;
    see `get_llm_outbound_rate_limit`.
    """
    from app.core.config import settings

    results = await asyncio.gather(*(_probe_local_endpoint(spec) for spec in _LOCAL_ENDPOINT_TABLE))
    return {
        "endpoints": results,
        "private_endpoints_allowed": settings.LLM_ALLOW_PRIVATE_ENDPOINTS,
    }
