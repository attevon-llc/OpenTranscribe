"""Retryable-error taxonomy shared by every cloud ASR provider.

Deliberately NOT part of ``app/core/exceptions.py``: that hierarchy's base is
``OpenTranscribeError(Exception)`` with a ``(message, detail)`` constructor and a global
FastAPI HTTP-response handler wired in ``main.py`` — unrelated semantics to a Celery-task
retry signal. These types instead subclass ``RuntimeError`` so every existing call site
(``except Exception``, message formatting in ``cloud_asr.py``'s broad handlers, and the
nine providers' own ``except Exception as exc: raise RuntimeError(...) from exc`` funnels)
keeps behaving exactly as it does today for anything that is not positively classified.

Fails closed toward today's behavior: anything not positively classified as a vendor
throttle stays a plain ``RuntimeError`` and permanently fails the job, same as before this
module existed. A misclassification only costs one extra retry (bounded by
``CLOUD_ASR_MAX_RETRIES`` in ``core/constants.py``) — it never loses a permanent-failure
signal, because the unclassified case is unchanged.

Quota-exhausted-for-the-billing-period is deliberately IN SCOPE for
:class:`ASRRateLimitedError`: it is wire-indistinguishable from a transient rate limit (both
surface as HTTP 429 with no reliable body field to tell them apart across nine vendors), and
bounding the retry count already caps the wasted work if the classification turns out to be
wrong.

Explicitly OUT OF SCOPE for this module, and deliberately not stubbed: ``ASRAuthError`` /
``ASRPermanentError``. No consumer needs them yet — the Celery task's retry allowlist is
positive (only :class:`ASRRateLimitedError` triggers a retry), so a 401 is already
non-retryable by omission, with no need for an explicit "permanent" type to say so.
"""

from __future__ import annotations

import re
from typing import Any


class ASRProviderError(RuntimeError):
    """Base for provider-raised failures.

    Subclasses ``RuntimeError`` so every existing call site keeps working unmodified —
    see the module docstring for why that matters.
    """

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ASRRateLimitedError(ASRProviderError):
    """HTTP 429 / vendor throttle / quota-exhausted response. RETRYABLE.

    Carries ``retry_after`` (seconds) parsed from a vendor ``Retry-After`` header when one
    was present, else ``None``. See the module docstring for the quota-vs-rate-limit and
    fail-closed reasoning.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


# HTTP statuses treated as a throttle/rate-limit signal across every vendor.
_THROTTLE_STATUSES = frozenset({429})


def http_status_of(exc: Exception) -> int | None:
    """Best-effort HTTP status extraction across the SDK exception shapes this repo hits.

    Tries, in order: ``exc.status_code`` (openai, some requests-based SDKs),
    ``exc.status`` (google api_core), ``exc.response.status_code`` (httpx/requests
    response-bearing exceptions), ``exc.code`` (rare numeric-code SDKs, only if int-like).

    Args:
        exc: The caught provider exception.

    Returns:
        The integer HTTP status if one could be found, else ``None``.
    """
    for attr in ("status_code", "status"):
        val = getattr(exc, attr, None)
        if isinstance(val, int):
            return val

    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "status_code", None)
        if isinstance(val, int):
            return val

    val = getattr(exc, "code", None)
    if isinstance(val, int):
        return val

    return None


def is_rate_limit_status(status: int | None) -> bool:
    """Whether an HTTP status code represents a throttle/rate-limit response."""
    return status in _THROTTLE_STATUSES


def retry_after_from_headers(headers: Any) -> float | None:
    """Parse a vendor ``Retry-After`` header value out of a headers mapping.

    Only the seconds form (``Retry-After: 30``) is parsed. The HTTP-date form
    (``Retry-After: Wed, 21 Oct 2026 07:28:00 GMT``) is intentionally not — callers already
    have a bounded exponential backoff fallback, and parsing an HTTP date correctly (timezone,
    clock skew) is more complexity than the fallback's cost of using its own backoff instead.

    Args:
        headers: A response ``headers`` mapping (anything with a ``.get``), or ``None``.
            Typed ``Any`` deliberately — callers pass ``httpx.Headers``,
            ``requests.structures.CaseInsensitiveDict``, a plain ``dict``, or ``None``
            depending on the vendor SDK, and the ``try/except AttributeError`` below is
            the real type guard; a narrower static type would just be wrong for one of
            them.

    Returns:
        Seconds to wait before retrying, or ``None`` if absent or not the seconds form.
    """
    if not headers:
        return None

    try:
        value = headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None

    value = value.strip()
    if not re.fullmatch(r"\d+(\.\d+)?", value):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def retry_after_of(exc: Exception) -> float | None:
    """Parse a vendor ``Retry-After`` header off an exception's response, if present.

    See :func:`retry_after_from_headers` for the parsing rules.

    Args:
        exc: The caught provider exception.

    Returns:
        Seconds to wait before retrying, or ``None`` if absent or not the seconds form.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    return retry_after_from_headers(headers)
