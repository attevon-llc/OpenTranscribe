"""OpenRouter attribution header: configurable, defaults to the public repo, omittable.

It used to be hard-coded to a domain the project does not control, so every install using
OpenRouter advertised it.
"""

from __future__ import annotations

import pytest

from app.core.config import settings
from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService


def _headers() -> dict[str, str]:
    config = LLMConfig(
        provider=LLMProvider.OPENROUTER,
        model="anthropic/claude-haiku-4.5",
        api_key="sk-test",
        base_url="https://openrouter.ai/api/v1",
    )
    return LLMService(config)._get_headers()


def test_default_referer_is_public_repo() -> None:
    assert settings.OPENROUTER_HTTP_REFERER == "https://github.com/attevon-llc/OpenTranscribe"
    assert _headers()["HTTP-Referer"] == "https://github.com/attevon-llc/OpenTranscribe"


def test_referer_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENROUTER_HTTP_REFERER", "https://transcribe.example.org")
    assert _headers()["HTTP-Referer"] == "https://transcribe.example.org"


def test_empty_referer_omits_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENROUTER_HTTP_REFERER", "")
    headers = _headers()
    assert "HTTP-Referer" not in headers
    assert headers["Authorization"] == "Bearer sk-test"
