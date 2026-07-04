"""Abstract base class for ASR providers."""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from collections.abc import Callable

from .types import ASRConfig
from .types import ASRResult


class ASRProvider(ABC):
    """Abstract base for all ASR provider implementations."""

    @abstractmethod
    def transcribe(
        self,
        audio_path: str,
        config: ASRConfig,
        progress_callback: Callable[[float, str], None] | None = None,
    ) -> ASRResult:
        """Transcribe audio file and return normalized result."""
        ...

    @abstractmethod
    def supports_diarization(self) -> bool:
        """Whether this provider can return speaker labels."""
        ...

    @abstractmethod
    def supports_vocabulary(self) -> bool:
        """Whether this provider accepts custom vocabulary hints."""
        ...

    @abstractmethod
    def supports_translation(self) -> bool:
        """Whether this provider can translate to English."""
        ...

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Canonical provider identifier (e.g. 'deepgram')."""
        ...

    @abstractmethod
    def validate_connection(self) -> tuple[bool, str, float]:
        """Test the provider connection. Returns (success, message, response_time_ms)."""
        ...

    # ── Helpers shared by all providers ──────────────────────────────────────

    def _normalize_speaker_label(self, label: str | int | None) -> str | None:  # noqa: C901
        """Normalize any speaker label format to 0-indexed SPEAKER_XX.

        Handled formats
        ---------------
        * ``SPEAKER_00``, ``SPEAKER_01`` … (already normalized, pass-through)
        * ``"0"``, ``"1"`` … (0-indexed integer strings, e.g. Deepgram)
        * ``"A"``, ``"B"`` … (0-indexed letter labels, e.g. AssemblyAI)
        * ``"S1"``, ``"S2"`` … (1-indexed "S#" labels, e.g. Speechmatics)
        * ``"speaker 1"``, ``"speaker 2"`` … (1-indexed "speaker N" strings)
        * ``"speaker_0"``, ``"speaker_1"`` … (0-indexed "speaker_N" strings, e.g. Gladia)
        * ``"Guest-1"``, ``"Guest-2"`` … (1-indexed guest labels, e.g. Azure)
        * Any other string → stable hash into 00-99 range (fallback)

        Google speaker tags (integers 1+) are 1-indexed and must be converted
        *before* calling this method (subtract 1, then pass the resulting
        0-indexed integer or ``"SPEAKER_XX"`` string).  See ``google_provider.py``.
        """
        import re as _re

        if label is None:
            return None
        label_str = str(label).strip()

        # Already in canonical form.
        if label_str.upper().startswith("SPEAKER_"):
            return label_str.upper()

        # Pure integer string — treat as 0-indexed (e.g. Deepgram "0", "1").
        try:
            return f"SPEAKER_{int(label_str):02d}"
        except ValueError:
            pass

        # Single letter — 0-indexed (e.g. AssemblyAI "A" → SPEAKER_00, "B" → SPEAKER_01).
        if len(label_str) == 1 and label_str.isalpha():
            return f"SPEAKER_{ord(label_str.upper()) - ord('A'):02d}"

        # Speechmatics "S#" format — 1-indexed (S1 → SPEAKER_00, S2 → SPEAKER_01, …).
        m = _re.fullmatch(r"[Ss](\d+)", label_str)
        if m:
            return f"SPEAKER_{max(int(m.group(1)) - 1, 0):02d}"

        # "speaker N" (space-separated) — 1-indexed (e.g. "speaker 1" → SPEAKER_00).
        if label_str.lower().startswith("speaker "):
            try:
                n = int(label_str.split()[-1])
                return f"SPEAKER_{max(n - 1, 0):02d}"
            except (ValueError, IndexError):
                pass

        # "speaker_N" (underscore, lowercase) — 0-indexed (e.g. Gladia "speaker_0").
        if label_str.lower().startswith("speaker_"):
            try:
                return f"SPEAKER_{int(label_str.split('_')[-1]):02d}"
            except (ValueError, IndexError):
                pass

        # "spk_N" — AWS Transcribe 0-indexed format (e.g. "spk_0" → SPEAKER_00).
        m = _re.fullmatch(r"spk_(\d+)", label_str, _re.IGNORECASE)
        if m:
            return f"SPEAKER_{int(m.group(1)):02d}"

        # "Guest-N" (Azure ConversationTranscriber) — 1-indexed.
        m = _re.fullmatch(r"[Gg]uest-(\d+)", label_str)
        if m:
            return f"SPEAKER_{max(int(m.group(1)) - 1, 0):02d}"

        # Fallback: stable hash into 00-99.
        return f"SPEAKER_{abs(hash(label_str)) % 100:02d}"

    def _sanitize_error(self, message: str, api_key: str | None = None) -> str:
        """Strip API keys and credential-like tokens from error messages before logging or returning.

        Performs three passes:
        1. Exact replacement of the provided *api_key* value (handles any key format).
        2. Regex removal of Bearer/token header values.
        3. Regex removal of strings that start with known API-key prefixes.
        """
        if not message:
            return message
        import re

        scrubbed = message
        # Pass 1 – exact match (handles keys with unusual characters / formats)
        if api_key:
            scrubbed = scrubbed.replace(api_key, "***")
        # Pass 2 – Bearer tokens in Authorization headers
        scrubbed = re.sub(r"(Bearer\s+)\S+", r"\1***", scrubbed)
        # Pass 3 – Known key prefixes (OpenAI, Deepgram, AssemblyAI, Speechmatics, Gladia, etc.)
        scrubbed = re.sub(r"(sk-|dg_|aai_|sk_live_|sk_test_)\S+", r"\1***", scrubbed)
        return scrubbed
