"""Abstract base class for diarization providers."""

from __future__ import annotations

import re
from abc import ABC
from abc import abstractmethod
from collections.abc import Callable

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

        Delegates to the shared ASR normalization logic to avoid duplicating
        the 60+ lines of format handling across provider hierarchies.
        """
        from app.services.asr.base import ASRProvider

        # The method is stateless — instantiation-free call via a throwaway proxy.
        _proxy: ASRProvider = object.__new__(ASRProvider)  # type: ignore[type-abstract]
        return _proxy._normalize_speaker_label(label)

    def _sanitize_error(self, message: str, api_key: str | None = None) -> str:
        """Strip API keys and credential-like tokens from error messages."""
        if not message:
            return message

        scrubbed = message
        # Pass 1 — exact match of the provided key
        if api_key:
            scrubbed = scrubbed.replace(api_key, "***")
        # Pass 2 — Bearer tokens in Authorization headers
        scrubbed = re.sub(r"(Bearer\s+)\S+", r"\1***", scrubbed)
        # Pass 3 — Known key prefixes
        scrubbed = re.sub(r"(sk-|dg_|aai_|sk_live_|sk_test_)\S+", r"\1***", scrubbed)
        return scrubbed
