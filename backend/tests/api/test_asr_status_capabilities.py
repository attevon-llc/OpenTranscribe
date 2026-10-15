"""``/asr-settings/status`` reports whether the active model returns speakers (#1219, #1224).

Model-level capability wins over provider-level: OpenAI as a provider returns no
speakers, its ``gpt-4o-transcribe-diarize`` model does. The status response also carries
the timing granularity and the speaker-count hints the settings UI badges are built from.
"""

from __future__ import annotations

import pytest
from fastapi import status

_URL = "/api/asr-settings/status"

pytestmark = pytest.mark.usefixtures("pipeline_env")


@pytest.mark.parametrize(
    ("model", "speakers", "timing"),
    [
        ("whisper-1", False, "segment"),
        ("gpt-4o-transcribe", False, "none"),
        ("gpt-4o-transcribe-diarize", True, "segment"),
    ],
)
def test_openai_models_report_their_own_capabilities(
    model, speakers, timing, client, user_token_headers, add_asr_config
):
    add_asr_config("openai", model, active=True)
    response = client.get(_URL, headers=user_token_headers)
    assert response.status_code == status.HTTP_200_OK, response.text
    caps = response.json()["active_model_capabilities"]
    assert caps["provider"] == "openai"
    assert caps["model_id"] == model
    assert caps["supports_diarization"] is speakers
    assert caps["timestamp_granularity"] == timing
    assert caps["speaker_count_hints"] == []


def test_a_provider_that_honours_hints_reports_them(client, user_token_headers, add_asr_config):
    add_asr_config("google", "chirp-3", active=True)
    caps = client.get(_URL, headers=user_token_headers).json()["active_model_capabilities"]
    assert caps["supports_diarization"] is True
    assert caps["timestamp_granularity"] == "word"
    assert caps["speaker_count_hints"] == ["min", "max"]


def test_local_default_reports_word_timing_and_speakers(client, user_token_headers):
    caps = client.get(_URL, headers=user_token_headers).json()["active_model_capabilities"]
    assert caps["provider"] == "local"
    assert caps["supports_diarization"] is True
    assert caps["timestamp_granularity"] == "word"
