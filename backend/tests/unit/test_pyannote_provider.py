"""Unit tests for the pyannote.ai STT Orchestration provider response parsing.

Network-free: exercises model resolution and `_parse_response` against the documented
pyannote.ai job `output` schema (turnLevelTranscription / wordLevelTranscription, where
each entry's token key is "text", per the get-job TranscriptionSegment schema).
"""

from __future__ import annotations

from app.services.asr.pyannote_provider import PyAnnoteProvider


def _provider(model_name: str = "parakeet") -> PyAnnoteProvider:
    return PyAnnoteProvider(api_key="test-key", model_name=model_name)


def test_model_resolution_uses_correct_pyannote_ids():
    # Transcription model ids are the live API's transcriptionConfig.model enum values
    # (verified against the API — NO "nvidia-" prefix despite the docs); diarization must
    # be precision-2 (the only model that supports transcription).
    assert _provider("parakeet")._resolve_models() == (
        "precision-2",
        "parakeet-tdt-0.6b-v3",
    )
    assert _provider("whisper-large-v3-turbo")._resolve_models() == (
        "precision-2",
        "faster-whisper-large-v3-turbo",
    )
    # Unknown model falls back to parakeet.
    assert _provider("bogus")._resolve_models()[1] == "parakeet-tdt-0.6b-v3"


def test_parse_response_reads_text_field_and_attaches_words():
    output = {
        "turnLevelTranscription": [
            {"start": 0.5, "end": 2.3, "text": "Hello, how are you?", "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 4.0, "text": "I am fine thanks", "speaker": "SPEAKER_01"},
        ],
        "wordLevelTranscription": [
            {"start": 0.5, "end": 0.8, "text": "Hello,", "speaker": "SPEAKER_00"},
            {"start": 0.9, "end": 1.2, "text": "how", "speaker": "SPEAKER_00"},
            {"start": 1.3, "end": 1.6, "text": "are", "speaker": "SPEAKER_00"},
            {"start": 1.7, "end": 2.3, "text": "you?", "speaker": "SPEAKER_00"},
            {"start": 2.5, "end": 2.8, "text": "I", "speaker": "SPEAKER_01"},
            {"start": 2.9, "end": 3.2, "text": "am", "speaker": "SPEAKER_01"},
            {"start": 3.3, "end": 3.6, "text": "fine", "speaker": "SPEAKER_01"},
            {"start": 3.7, "end": 4.0, "text": "thanks", "speaker": "SPEAKER_01"},
        ],
    }
    segments = _provider()._parse_response(output)

    assert len(segments) == 2
    assert segments[0].text == "Hello, how are you?"
    assert segments[1].text == "I am fine thanks"
    # Two distinct, non-null speakers.
    assert segments[0].speaker and segments[1].speaker
    assert segments[0].speaker != segments[1].speaker
    # Words must be populated from the "text" key — the regression this guards against is
    # reading "word" (absent), which would leave every token an empty string.
    assert [w.word for w in segments[0].words] == ["Hello,", "how", "are", "you?"]
    assert [w.word for w in segments[1].words] == ["I", "am", "fine", "thanks"]
    assert all(w.word for seg in segments for w in seg.words)


def test_parse_response_falls_back_to_diarization_only():
    output = {
        "diarization": [
            {"start": 0.0, "end": 1.5, "speaker": "SPEAKER_00"},
            {"start": 1.5, "end": 3.0, "speaker": "SPEAKER_01"},
        ]
    }
    segments = _provider()._parse_response(output)
    assert len(segments) == 2
    assert all(s.text == "" for s in segments)
    assert segments[0].speaker and segments[1].speaker


def test_parse_response_empty_output_is_safe():
    segments = _provider()._parse_response({})
    assert len(segments) == 1
    assert segments[0].text == ""
