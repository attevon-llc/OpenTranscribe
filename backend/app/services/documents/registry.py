"""Parser selection — **the single branch point** for the document tier.

Everything else in this package, and every caller of it, is backend-agnostic. If you find
yourself writing ``if parser.name == "docling.serve"`` anywhere else, the branch belongs
here. This mirrors ``services/asr/factory.py`` and ``services/diarization/factory.py``,
which exist for the same reason: adding a provider must be adding a module plus one
registry entry, never editing call sites.

``DOCUMENT_PARSER_BACKEND`` selects the policy:

===========  ==========================================================================
``auto``     sidecar when ``DOCUMENT_PARSER_URL`` health-checks, else slim, else tika
``slim``     in-worker only. A document needing OCR fails with a typed, actionable error
``serve``    sidecar only. Unreachable is a *retryable* failure, not a parse failure
``tika``     Tika only — the legacy-format escape hatch, for testing that tier
===========  ==========================================================================

Health results are cached for :data:`_HEALTH_TTL_SECONDS`. Without it, ``auto`` would pay
an HTTP round trip per document and a bulk import of 50 small PDFs would spend more time
health-checking than parsing; with a TTL that short, a sidecar that comes back after a
recycle is picked up within seconds.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from app.core import constants as C  # noqa: N812

from .backends.docling_serve import DoclingServeParser
from .backends.docling_slim import DoclingSlimParser
from .backends.tika import TikaParser
from .protocol import DocumentParser
from .types import DocumentParserUnavailableError
from .types import DocumentUnsupportedError

logger = logging.getLogger(__name__)

#: How long a health answer is trusted. Short enough that a recycled sidecar is picked up
#: within one document, long enough that a bulk import is not one probe per file.
_HEALTH_TTL_SECONDS = 30.0

_VALID_POLICIES = frozenset({"auto", "slim", "serve", "tika"})

_lock = threading.Lock()
_instances: dict[str, DocumentParser] = {}
_health_cache: dict[str, tuple[float, bool, str]] = {}


def _policy() -> str:
    policy = (C.DOCUMENT_PARSER_BACKEND or "auto").lower().strip()
    if policy not in _VALID_POLICIES:
        logger.warning(
            "DOCUMENT_PARSER_BACKEND=%r is not one of %s — falling back to auto",
            policy,
            sorted(_VALID_POLICIES),
        )
        return "auto"
    return policy


def get_backend(name: str) -> DocumentParser | None:
    """Return the singleton for *name*, or ``None`` when it is not configured.

    ⚠️ With ``--pool=threads`` on the documents worker, ``worker_process_init`` never
    fires, so nothing pre-warms these. They are cheap (an HTTP client and a version
    string); the *parsers themselves* hold no unsynchronised state — the docling backends
    are constructed per call inside :meth:`DoclingSlimParser.parse`, which is what makes
    the singleton here thread-safe.
    """
    with _lock:
        if name in _instances:
            return _instances[name]

        instance: Any = None
        if name == "docling.slim":
            instance = DoclingSlimParser()
        elif name == "docling.serve":
            if C.DOCUMENT_PARSER_URL:
                instance = DoclingServeParser(C.DOCUMENT_PARSER_URL)
        elif name == "tika" and C.DOCUMENT_TIKA_URL:
            instance = TikaParser(C.DOCUMENT_TIKA_URL)

        if instance is None:
            return None
        parser: DocumentParser = instance
        _instances[name] = parser
        return parser


def health_of(name: str, *, refresh: bool = False) -> tuple[bool, str]:
    """Cached ``(available, detail)`` for one backend. Never raises."""
    now = time.monotonic()
    if not refresh:
        cached = _health_cache.get(name)
        if cached and now - cached[0] < _HEALTH_TTL_SECONDS:
            return cached[1], cached[2]

    backend = get_backend(name)
    if backend is None:
        result = (False, "not configured")
    else:
        try:
            result = backend.health()
        except Exception as exc:  # noqa: BLE001 - a health probe must never raise
            result = (False, f"health probe raised: {exc}")

    _health_cache[name] = (now, result[0], result[1])
    return result


def mark_unavailable(name: str, detail: str) -> None:
    """Record that *name* just failed mid-parse, so the next document does not retry it.

    Without this, a sidecar that dies is still cached as healthy for the rest of the TTL
    and every document submitted in that window pays the full connect timeout before
    falling back. During a bulk import that is the difference between a few seconds of
    degradation and a minute of it. The parse task calls this whenever a backend raises
    :class:`~app.services.documents.types.DocumentParserUnavailableError`.
    """
    _health_cache[name] = (time.monotonic(), False, detail)


def health_report() -> dict[str, dict[str, object]]:
    """Every tier's status, for the admin document-health card."""
    return {
        name: {"available": ok, "detail": detail, "configured": get_backend(name) is not None}
        for name in ("docling.slim", "docling.serve", "tika")
        for ok, detail in (health_of(name),)
    }


def get_parser_for(mime: str, filename: str, *, needs_ocr: bool = False) -> DocumentParser:
    """Resolve the parser for one document. **The single branch point.**

    Args:
        mime: The detected mime — never the client-declared one.
        filename: Used only by backends whose ``supports`` inspects an extension.
        needs_ocr: True when the source has no usable text layer, which excludes every
            tier without OCR. On the ``slim`` policy that is a typed, actionable failure
            rather than a silent empty extraction.

    Raises:
        DocumentParserUnavailableError: the policy's tier is configured but unreachable.
        DocumentUnsupportedError: no configured tier claims this format.
    """
    policy = _policy()
    order = _resolution_order(policy)

    unavailable: list[str] = []
    for name in order:
        backend = get_backend(name)
        if backend is None:
            continue
        if not backend.supports(mime, filename, needs_ocr=needs_ocr):
            continue
        ok, detail = health_of(name)
        if not ok:
            unavailable.append(f"{name} ({detail})")
            continue
        return backend

    if unavailable:
        raise DocumentParserUnavailableError(
            "no document parser is currently reachable for this file",
            detail="; ".join(unavailable),
        )
    if needs_ocr:
        raise DocumentUnsupportedError(
            "this document has no text layer and OCR is not available in this deployment"
        )
    raise DocumentUnsupportedError(f"no configured parser handles {mime}")


def _resolution_order(policy: str) -> tuple[str, ...]:
    """Tier preference for a policy. ``auto`` prefers the sidecar: it is the only tier
    with layout, table structure and OCR, so when it is up it is strictly better."""
    if policy == "slim":
        return ("docling.slim",)
    if policy == "serve":
        return ("docling.serve",)
    if policy == "tika":
        return ("tika",)
    return ("docling.serve", "docling.slim", "tika")


def reset_for_tests() -> None:
    """Drop the singletons and the health cache.

    Exists because the cache is process-global and a test that changed
    ``DOCUMENT_PARSER_URL`` would otherwise be resolved against the previous run's
    instances — a cross-test dependency that only shows up in a different test order.
    """
    with _lock:
        _instances.clear()
        _health_cache.clear()
