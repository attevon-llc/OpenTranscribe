"""Provider SDK compatibility smoke tests.

The cloud ASR/LLM provider modules lazy-import their SDKs inside methods, so
mocked endpoint tests can pass while an SDK major upgrade silently breaks the
real call path. These tests pin the contract: every provider module imports,
and the exact SDK symbols/call paths our code uses still exist.

Run after ANY provider-SDK version change (dependabot or manual):
    pytest backend/tests/unit/test_provider_sdk_compat.py -v
"""

from __future__ import annotations

import importlib

import pytest

ASR_PROVIDER_MODULES = [
    "deepgram_provider",
    "assemblyai_provider",
    "openai_provider",
    "gladia_provider",
    "speechmatics_provider",
    "aws_provider",
    "google_provider",
    "azure_provider",
    "pyannote_provider",
    "local_provider",
]


@pytest.mark.parametrize("module_name", ASR_PROVIDER_MODULES)
def test_asr_provider_module_imports(module_name: str) -> None:
    """Every ASR provider module must import against installed SDK versions."""
    importlib.import_module(f"app.services.asr.{module_name}")


class TestDeepgramSdkContract:
    """deepgram-sdk symbols/call path used by deepgram_provider.py."""

    def test_client_importable(self) -> None:
        from deepgram import DeepgramClient  # noqa: F401

    def test_transcribe_file_call_path(self) -> None:
        """provider calls client.listen.v1.media.transcribe_file(...)."""
        from deepgram import DeepgramClient

        client = DeepgramClient(api_key="test-key-not-real")  # gitleaks:allow
        assert callable(client.listen.v1.media.transcribe_file)


class TestAssemblyAiSdkContract:
    """assemblyai symbols used by assemblyai_provider.py."""

    def test_module_surface(self) -> None:
        import assemblyai as aai

        assert hasattr(aai, "Transcriber")
        assert hasattr(aai, "TranscriptionConfig")
        assert hasattr(aai, "TranscriptStatus")
        assert hasattr(aai, "settings")


class TestOpenAiSdkContract:
    """openai symbols used by openai_provider.py and llm_service.py."""

    def test_client_importable(self) -> None:
        from openai import OpenAI  # noqa: F401


class TestSpeechmaticsSdkContract:
    """speechmatics-batch symbols used by speechmatics_provider.py.

    The legacy speechmatics-python SDK is deprecated AND drops speaker labels
    — the app deliberately uses the async speechmatics-batch client.
    """

    def test_batch_client_importable(self) -> None:
        from speechmatics.batch import AsyncClient  # noqa: F401
        from speechmatics.batch import FormatType  # noqa: F401
