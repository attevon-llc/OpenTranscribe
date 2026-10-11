"""Speaker-detection errors whose text is a fixed sentence (#1226).

``str()`` of each is a constant, never the vendor's message, so a caller that stores or
sends ``str(exc)`` cannot leak raw failure text (the #786 / #959 no-raw-echo contract). The
vendor's own text is logged, sanitised, at the raise site.
"""

from __future__ import annotations

from app.services.asr.errors import ASRRateLimitedError
from app.services.error_categorization_service import DIARIZATION_PROVIDER_FAILED_MESSAGE
from app.services.error_categorization_service import DIARIZER_CONFIG_UNUSABLE_MESSAGE


class DiarizerConfigUnusableError(RuntimeError):
    """The saved configuration chosen for speaker detection cannot be used.

    It no longer exists, is no longer shared with the user, needs a capability the
    deployment turned off, cannot be decrypted, or its model returns no speakers. Registered
    in ``ErrorCategorizationService`` (reason ``diarizer_config_unusable``, permanent retry
    category ``CONFIGURATION_REQUIRED``), like ``DiarizationNotConfiguredError`` for a
    missing pyannote.ai key.
    """

    MESSAGE = DIARIZER_CONFIG_UNUSABLE_MESSAGE

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class DiarizationProviderFailedError(RuntimeError):
    """The speaker-detection vendor failed. Non-fatal on every pipeline path today."""

    MESSAGE = DIARIZATION_PROVIDER_FAILED_MESSAGE

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class DiarizationRateLimitedError(ASRRateLimitedError):
    """The speaker-detection vendor throttled the request: retryable, fixed text."""

    def __init__(self, *, provider: str | None = None, retry_after: float | None = None) -> None:
        super().__init__(
            DIARIZATION_PROVIDER_FAILED_MESSAGE, provider=provider, retry_after=retry_after
        )
