"""Abstract base class for diarization providers."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Callable

from app.services.asr.base import normalize_speaker_label
from app.services.asr.base import sanitize_provider_error

from .types import DiarizeConfig
from .types import DiarizeResult


class DiarizationProvider(ABC):
    """Abstract base for all diarization provider implementations."""

    @abstractmethod
    def diarize(
        self,
        audio_path: str,
        config: DiarizeConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> DiarizeResult:
        """Run diarization on audio file and return speaker segments."""
        ...

    @abstractmethod
    def supports_speaker_count(self) -> bool:
        """Whether this provider accepts min/max/num speaker hints."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Canonical provider identifier."""
        ...

    @abstractmethod
    def validate_connection(self) -> tuple[bool, str, float]:
        """Test the provider connection.

        Returns:
            Tuple of (success, message, response_time_ms).
        """
        ...

    # ── Helpers shared by all providers ──────────────────────────────────────

    def _normalize_speaker_label(self, label: str | int | None) -> str | None:
        """Normalize any speaker label format to 0-indexed SPEAKER_XX.

        Delegates to the shared module-level helper so the ASR and diarization
        hierarchies cannot drift apart.
        """
        return normalize_speaker_label(label)

    def _sanitize_error(self, message: str, api_key: str | None = None) -> str:
        """Strip API keys and credential-like tokens from error messages."""
        return sanitize_provider_error(message, api_key)
