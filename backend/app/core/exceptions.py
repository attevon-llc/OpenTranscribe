"""Application-specific exception hierarchy.

All domain exceptions inherit from ``OpenTranscribeError`` so they can
be caught by the global exception handler in ``main.py``.

Scope note (intentional — do not "finish" this in a dedup pass):
this hierarchy is reserved for **service-layer** error signalling. Endpoint
handlers deliberately keep raising ``fastapi.HTTPException`` directly (and the
shared ``utils.error_handlers.ErrorHandler`` builders for opaque 5xx) because
migrating the ~40 endpoint raise-sites onto these classes would change the
client-facing status/detail rendered by the ``main.py`` handler — a behavior
change, not a refactor. The global ``OpenTranscribeError`` handler in
``main.py`` stays in place for the service-layer paths that already raise these.
"""

from collections.abc import Mapping
from typing import Any
from typing import ClassVar


class OpenTranscribeError(Exception):
    """Base exception for all application errors."""

    def __init__(self, message: str, detail: str | None = None):
        self.message = message
        # Widened (not Any): DomainError stores a dict or list here for coded details.
        self.detail: str | dict[str, Any] | list[Any] | None = detail
        super().__init__(message)


class TranscriptionError(OpenTranscribeError):
    """Errors during the transcription pipeline."""


class StorageError(OpenTranscribeError):
    """MinIO/S3 storage operation failures."""


class SearchIndexError(OpenTranscribeError):
    """OpenSearch indexing or query failures."""


class LLMServiceError(OpenTranscribeError):
    """LLM provider communication failures."""


class ASRConfigurationError(OpenTranscribeError):
    """ASR provider resolution is impossible under the current deployment config.

    Raised by ``services/asr/factory.py`` when resolution would otherwise silently
    fall back to ``LocalASRProvider`` on a deployment that cannot run it — e.g. a
    ``DEPLOYMENT_MODE=lite`` image, which ships without whisperx/faster-whisper
    (``requirements-lite.txt``). Without this guard the failure surfaces much later,
    deep in a Celery GPU task, as a raw ``ModuleNotFoundError``.
    """


class PipelinePlanRefusedError(ASRConfigurationError):
    """A file's pipeline plan has a blocking issue, so it is not dispatched (issue #1225).

    ``code`` is the plan's stable issue code and ``message`` its fixed sentence
    (``services/pipeline_plan.ISSUE_MESSAGES``), never exception text. A subclass of
    :class:`ASRConfigurationError` so every caller that already turns a deliberate dispatch
    refusal into a clean error (503 on the API, file ERROR in a batch) handles it unchanged.
    """

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class EmailDeliveryError(OpenTranscribeError):
    """A transactional email could not be handed to a mail transport.

    Raised by ``services/email_service.py``. Callers on an anti-enumeration path
    (password reset, verification resend) must absorb it — see that module.
    The message is scrubbed of email addresses so it is safe to surface.
    """


class ExternalProviderNotAdmittedError(OpenTranscribeError):
    """A registry-based provider's ``auth_type`` is not admitted by the database.

    Raised by ``auth/auth_type_admission.py`` before ``external_sync`` writes
    ``user.auth_type``, in place of the bare ``CheckViolation`` Postgres would raise
    at commit. The fix is a migration in the edition that registered the provider
    (issue #866), so the message names it.
    """


class ExternalIdentityLinkRefusedError(OpenTranscribeError):
    """A registry-based external identity may not be linked/synced to a user.

    Raised by ``auth/external_sync.py`` for a deliberate refusal (unverified
    email match, a provider-id corroboration mismatch, or a super_admin
    target) — never a caught library exception. Exists as its own type
    because the module used to raise the BUILTIN ``PermissionError``, which
    is also raised by real OS-level permission failures (it is a subclass of
    ``OSError``) anywhere in the same call chain. A genuine ``EACCES``
    surfacing through that overloaded type would have been caught by the
    same ``except PermissionError`` this module wrote its refusal for, and
    rendered as this refusal's generic 401 — putting a host filesystem path
    in an authentication response instead of the actual security event.
    """


class DomainError(OpenTranscribeError):
    """A deliberate refusal raised below the API layer.

    ``main.py`` renders it through FastAPI's own ``HTTPException`` handler, so the status,
    the JSON body bytes and the headers are what ``HTTPException(status_code, detail,
    headers)`` would have produced. The detail goes on the wire verbatim: never build one
    from caught exception text.

    Must stay clear of ``OSError``, ``ConnectionError``, ``TimeoutError``, ``ValueError``
    and ``LookupError``: Celery ``autoretry_for`` matches those, and a refusal must not be
    retried.
    """

    status_code: ClassVar[int] = 500

    def __init__(
        self, detail: str | dict[str, Any] | list[Any], *, headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(str(detail))
        self.detail = detail
        self.headers = headers

    # Parity with starlette's HTTPException. Not cosmetic: str(exc) is persisted as
    # media_file.last_error_message and fed to categorize_error, whose outcome keys on the
    # leading status code.
    def __str__(self) -> str:
        return f"{self.status_code}: {self.detail}"

    def __repr__(self) -> str:
        return f"{type(self).__name__}(status_code={self.status_code!r}, detail={self.detail!r})"


class InvalidInputError(DomainError):
    """The request or argument is malformed (400)."""

    status_code = 400


class AuthenticationError(DomainError):
    """Authentication failed or the credentials were refused (401)."""

    status_code = 401


class AccessDeniedError(DomainError):
    """The caller is authenticated but not permitted (403).

    Not named ``PermissionDeniedError``: the openai and anthropic SDKs export that name.
    """

    status_code = 403


class ResourceNotFoundError(DomainError):
    """The resource does not exist, or is hidden from the caller (404).

    Not named ``NotFoundError``: ``opensearchpy.exceptions.NotFoundError`` is imported by
    bare name in several services.
    """

    status_code = 404


class ConflictError(DomainError):
    """The request conflicts with current state (409)."""

    status_code = 409


class UnprocessableInputError(DomainError):
    """The request is well-formed but cannot be processed (422)."""

    status_code = 422


class OperationFailedError(DomainError):
    """An operation failed with an opaque, already-sanitised message (500)."""

    status_code = 500


class UpstreamServiceError(DomainError):
    """A remote service the request depends on misbehaved (502).

    Deliberately not an ``LLMServiceError``: different meaning, and that family renders
    ``exc.message`` through ``handle_app_error``.
    """

    status_code = 502


class ServiceUnavailableError(DomainError):
    """A dependency needed to honour the request is unavailable (503)."""

    status_code = 503


_BY_STATUS: dict[int, type[DomainError]] = {
    cls.status_code: cls
    for cls in (
        InvalidInputError,
        AuthenticationError,
        AccessDeniedError,
        ResourceNotFoundError,
        ConflictError,
        UnprocessableInputError,
        OperationFailedError,
        UpstreamServiceError,
        ServiceUnavailableError,
    )
}


def domain_error_for_status(status_code: int) -> type[DomainError]:
    """Return the ``DomainError`` class for an HTTP status (for status-parameterised factories)."""
    try:
        return _BY_STATUS[int(status_code)]
    except KeyError:
        raise ValueError(f"no DomainError class for HTTP status {status_code}") from None
