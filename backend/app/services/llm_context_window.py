"""Measured context-window discovery (issue #533) — the reasoning probe's sibling.

``LLMConfig.max_tokens`` IS the context window (see ``LLMService.__init__``), it
defaults to 8192, and nothing used to tell a user their 60k/128k model was being
driven at 8k — that default silently capped four complete 81-question evaluation
runs at ~1/20th of the available budget before anyone noticed. The fix is the
same shape as ``llm_reasoning``: **measure the capability against the live
endpoint, store the verdict as a fingerprint-keyed measurement, and let the UI
compare it to what the user configured.**

Discovery is metadata-only — one HTTP call, no generation, no user content:

* **vLLM** (and any OpenAI-compatible server that includes the extension):
  ``GET {base_url}/models`` → the entry for this model → ``max_model_len``.
* **Ollama**: ``POST {root}/api/show`` → ``model_info["<arch>.context_length"]``
  — the MODEL's maximum. The *server* may still run a smaller default context
  for requests that don't ask (``OLLAMA_CONTEXT_LENGTH``), but the app sends
  ``num_ctx`` from ``max_tokens`` on every call, so the model maximum is the
  honest ceiling on what a user may configure.
* **Everything else** (Anthropic, OpenRouter, Bedrock, ``custom`` clones):
  ``UNSUPPORTED`` — the declared value stands. **Fail closed, never guess
  upward**: an over-declared window produces provider 400s or silent
  truncation, the exact failure this feature exists to surface.

Like the reasoning probe, this runs on **explicit user action only** (the button
beside "Test connection") — a background sweep would dial every configured
third-party endpoint unprompted, and the verdict is deployment-wide anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime

import requests
from sqlalchemy.orm import Session

from app.core.constants import LLM_CONTEXT_WINDOW_KEY_PREFIX
from app.core.constants import LLM_CONTEXT_WINDOW_PROBE_TIMEOUT_S
from app.core.enums import ContextWindowStatus
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider

logger = logging.getLogger(__name__)

__all__ = [
    "DISCOVERABLE_PROVIDERS",
    "ContextWindowProbeResult",
    "ContextWindowStatus",
    "discovery_key",
    "measured_window",
    "probe",
    "read_record",
    "record",
]

#: Providers whose discovery endpoint this build knows how to read. An entry is
#: earned by a measurement against a real server, not by reading a vendor's
#: documentation — the same rule as ``llm_reasoning.PROBEABLE_PROVIDERS``.
#: Both measured live 2026-08-21: **Ollama** (qwen3.8 → 262,144 via
#: ``qwen35.context_length``; unknown model → 404 → NOT_FOUND) and **vLLM**
#: (gemma-4-e4b → 60,000 via ``max_model_len``; unknown model → NOT_FOUND).
#: ``custom`` stays out on purpose: an
#: OpenAI-clone may serve ``/v1/models`` without the ``max_model_len`` extension
#: (reported as NOT_FOUND if probed), but it may also be a thin proxy where the
#: path 404s — either way the declared value stands, so there is nothing to win.
DISCOVERABLE_PROVIDERS: frozenset[LLMProvider] = frozenset({LLMProvider.VLLM, LLMProvider.OLLAMA})


@dataclass(frozen=True)
class ContextWindowProbeResult:
    """One discovery call, and what it concluded."""

    status: ContextWindowStatus
    context_window: int | None = None
    detail: str = ""


def discovery_key(provider: str, base_url: str | None, model: str) -> str:
    """`SystemSettings` key holding the measurement for one endpoint+model.

    Identical fingerprint scheme to ``llm_reasoning.capability_key`` (the
    window belongs to the software answering at that URL, two users sharing a
    server share one measurement, editing the model orphans the old record),
    under its own prefix so the two measurements can never shadow each other.
    The URL is hashed rather than stored for the same privacy reason.
    """
    identity = f"{provider}|{(base_url or '').strip().rstrip('/')}|{model}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"{LLM_CONTEXT_WINDOW_KEY_PREFIX}{digest}"


def _probe_vllm(config: LLMConfig) -> ContextWindowProbeResult:
    """Read ``max_model_len`` off the OpenAI-compatible ``/v1/models`` list."""
    base = (config.base_url or "").rstrip("/")
    headers = {}
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"
    response = requests.get(
        f"{base}/models", headers=headers, timeout=LLM_CONTEXT_WINDOW_PROBE_TIMEOUT_S
    )
    response.raise_for_status()
    entries = response.json().get("data") or []
    for entry in entries:
        if entry.get("id") == config.model:
            raw = entry.get("max_model_len")
            if isinstance(raw, int) and raw > 0:
                return ContextWindowProbeResult(
                    status=ContextWindowStatus.MEASURED,
                    context_window=raw,
                    detail="max_model_len from /v1/models",
                )
            return ContextWindowProbeResult(
                status=ContextWindowStatus.NOT_FOUND,
                detail=(
                    "/v1/models names the model but carries no max_model_len — the "
                    "server is OpenAI-compatible without the vLLM extension"
                ),
            )
    return ContextWindowProbeResult(
        status=ContextWindowStatus.NOT_FOUND,
        detail=f"/v1/models does not list {config.model!r} ({len(entries)} model(s) listed)",
    )


def _probe_ollama(config: LLMConfig) -> ContextWindowProbeResult:
    """Read ``<arch>.context_length`` off Ollama's native ``/api/show``.

    Ollama's OpenAI-compatible surface lives under ``/v1`` but ``/api/show`` is
    served at the root, so the ``/v1`` suffix is stripped before dialling.
    """
    base = (config.base_url or "").rstrip("/")
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    response = requests.post(
        f"{root}/api/show",
        json={"model": config.model},
        timeout=LLM_CONTEXT_WINDOW_PROBE_TIMEOUT_S,
    )
    if response.status_code == 404:
        # The server answered; it just has no such model (verified live: Ollama
        # 404s /api/show for an unknown name). "Answered without the model" is
        # NOT_FOUND — UNREACHABLE would send the operator debugging the network.
        return ContextWindowProbeResult(
            status=ContextWindowStatus.NOT_FOUND,
            detail=f"/api/show does not know {config.model!r} (HTTP 404)",
        )
    response.raise_for_status()
    model_info = response.json().get("model_info") or {}
    for key, value in model_info.items():
        if key.endswith(".context_length") and isinstance(value, int) and value > 0:
            return ContextWindowProbeResult(
                status=ContextWindowStatus.MEASURED,
                context_window=value,
                detail=f"{key} from /api/show",
            )
    return ContextWindowProbeResult(
        status=ContextWindowStatus.NOT_FOUND,
        detail="/api/show answered without a *.context_length in model_info",
    )


def probe(config: LLMConfig) -> ContextWindowProbeResult:
    """Discover the model's maximum context window from the live endpoint.

    Args:
        config: The configuration to probe — dialled as configured, exactly as
            a chat request would be.

    Returns:
        A :class:`ContextWindowProbeResult`. Never raises: an unreachable or
        unrecognisable endpoint is a recorded verdict, not an exception, so the
        settings page reports it instead of erroring.
    """
    provider = (
        LLMProvider(config.provider)
        if not isinstance(config.provider, LLMProvider)
        else config.provider
    )
    if provider not in DISCOVERABLE_PROVIDERS:
        return ContextWindowProbeResult(
            status=ContextWindowStatus.UNSUPPORTED,
            detail=(
                f"provider {provider!s} exposes no discovery endpoint this build reads — "
                "the declared value stands (never guessed, in either direction)"
            ),
        )
    try:
        if provider is LLMProvider.VLLM:
            return _probe_vllm(config)
        return _probe_ollama(config)
    except requests.RequestException as exc:
        # The exception text can embed the URL; the class name alone diagnoses
        # without turning the settings table into an endpoint directory.
        return ContextWindowProbeResult(
            status=ContextWindowStatus.UNREACHABLE,
            detail=f"{type(exc).__name__} while dialling the endpoint",
        )
    except (ValueError, KeyError, TypeError) as exc:
        return ContextWindowProbeResult(
            status=ContextWindowStatus.NOT_FOUND,
            detail=f"unparseable discovery response ({type(exc).__name__})",
        )


def record(db: Session, config: LLMConfig, result: ContextWindowProbeResult) -> None:
    """Persist one discovery verdict against its model fingerprint.

    A **measurement, not a setting** — same contract as
    ``llm_reasoning.record``: nothing else writes this key, no coded default is
    editable into it, and the row's description says so.
    """
    from app.services.system_settings_service import set_setting

    key = discovery_key(str(config.provider), config.base_url, config.model)
    payload = {
        "status": str(result.status),
        "context_window": result.context_window,
        "provider": str(config.provider),
        "model": config.model,
        "detail": result.detail,
        "probed_at": datetime.now(UTC).isoformat(),
    }
    set_setting(
        db,
        key,
        json.dumps(payload),
        description=(
            "Measured model context window (issue #533) — probe output, not an editable setting"
        ),
    )


def read_record(db: Session, provider: str, base_url: str | None, model: str) -> dict:
    """The stored discovery record for one endpoint+model, or ``{}``.

    Both a missing row and an unreadable one mean "unprobed" downstream, and a
    read failure must never break the caller — same contract as
    ``llm_reasoning.read_record``.
    """
    from app.services.system_settings_service import get_setting

    key = discovery_key(provider, base_url, model)
    try:
        raw = get_setting(db, key)
    except Exception as exc:  # noqa: BLE001 — a capability read must not break settings
        logger.warning("Context-window record read failed for %s: %s", key, exc)
        return {}
    if not raw:
        return {}
    try:
        stored = json.loads(raw)
    except ValueError:
        logger.warning("Discarding unreadable context-window record at %s", key)
        return {}
    return stored if isinstance(stored, dict) else {}


def measured_window(stored: dict) -> int | None:
    """The window a stored record measured, or ``None``.

    ``None`` for every non-``measured`` status, for a record written by a newer
    build with a status this one has never heard of, and for a mangled number —
    the caller falls back to the declared value in all of them.
    """
    if stored.get("status") != ContextWindowStatus.MEASURED:
        return None
    raw = stored.get("context_window")
    return raw if isinstance(raw, int) and raw > 0 else None
