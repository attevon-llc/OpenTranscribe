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


class OpenTranscribeError(Exception):
    """Base exception for all application errors."""

    def __init__(self, message: str, detail: str | None = None):
        self.message = message
        self.detail = detail
        super().__init__(message)


class TranscriptionError(OpenTranscribeError):
    """Errors during the transcription pipeline."""


class StorageError(OpenTranscribeError):
    """MinIO/S3 storage operation failures."""


class SearchIndexError(OpenTranscribeError):
    """OpenSearch indexing or query failures."""


class AuthenticationError(OpenTranscribeError):
    """Authentication or authorization failures."""


class LLMServiceError(OpenTranscribeError):
    """LLM provider communication failures."""


class MigrationError(OpenTranscribeError):
    """Data migration failures."""


class ASRConfigurationError(OpenTranscribeError):
    """ASR provider resolution is impossible under the current deployment config.

    Raised by ``services/asr/factory.py`` when resolution would otherwise silently
    fall back to ``LocalASRProvider`` on a deployment that cannot run it — e.g. a
    ``DEPLOYMENT_MODE=lite`` image, which ships without whisperx/faster-whisper
    (``requirements-lite.txt``). Without this guard the failure surfaces much later,
    deep in a Celery GPU task, as a raw ``ModuleNotFoundError``.
    """


class EmailDeliveryError(OpenTranscribeError):
    """A transactional email could not be handed to a mail transport.

    Raised by ``services/email_service.py``. Callers on an anti-enumeration path
    (password reset, verification resend) must absorb it — see that module.
    The message is scrubbed of email addresses so it is safe to surface.
    """
