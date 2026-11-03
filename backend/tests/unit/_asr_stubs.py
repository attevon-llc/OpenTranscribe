"""A concrete ``ASRProvider`` for tests: every abstract member answered, nothing real called.

Fakes subclass it and override only what their test exercises, so the fake is a real
``ASRProvider`` to the type checker as well as at runtime.
"""

from __future__ import annotations

from app.services.asr.base import ASRProvider
from app.services.asr.types import ASRConfig
from app.services.asr.types import ASRResult


class StubASRProvider(ASRProvider):
    def __init__(self, provider_name: str) -> None:
        self._provider_name = provider_name

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def supports_diarization(self) -> bool:
        return False

    def supports_vocabulary(self) -> bool:
        return False

    def supports_translation(self) -> bool:
        return False

    def validate_connection(self) -> tuple[bool, str, float]:
        return True, "ok", 0.0

    def transcribe(self, audio_path: str, config: ASRConfig, progress_callback=None) -> ASRResult:
        raise NotImplementedError("this stub never transcribes; override it")
