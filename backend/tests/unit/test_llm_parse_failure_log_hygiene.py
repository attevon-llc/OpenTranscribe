"""LLM parse-failure paths must not write model output to the log (issue #1022).

Model output is derived from the transcript it was given, so a log line carrying
it copies transcript content into log aggregation. Each test drives a real
parse-failure path end to end — ``chat_completion`` through the provider's
response extractor into the feature's parser — with only the HTTP session
stubbed, and asserts that no record at INFO or above (message *or* formatted
traceback) contains a sentinel planted in the model's reply.

Every test also asserts that the failure was logged with a ``sha256=``
fingerprint, so a path that silently stopped logging cannot pass, and that the
DEBUG excerpt still carries the sentinel, which proves the capture could see it.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace

import pytest

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService
from app.services.topic_extraction_service import TopicExtractionService
from app.utils.llm_log_safety import describe_llm_text

SENTINEL = "Zorblatt-said-the-merger-price-is-4.2M-7731"


class _FakeResponse:
    def __init__(self, body: str, status_code: int = 200) -> None:
        self.text = body
        self.status_code = status_code

    def json(self):
        return json.loads(self.text)


class _FakeSession:
    def __init__(self, body: str) -> None:
        self._body = body

    def post(self, *args, **kwargs):
        return _FakeResponse(self._body)


def _service(provider: LLMProvider, body: str, monkeypatch) -> LLMService:
    service = LLMService(
        LLMConfig(
            provider=provider,
            model="test-model",
            base_url="http://llm.test/v1",
            api_key="not-a-secret",
        )
    )
    target = SimpleNamespace(url="http://llm.test/v1/chat/completions", headers={})
    monkeypatch.setattr(
        service, "_endpoint_session", lambda url: (_FakeSession(body), target), raising=True
    )
    return service


def _openai_body(content: str) -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 42},
        }
    )


def _assert_no_leak(caplog: pytest.LogCaptureFixture, fingerprint_of: str | None = None) -> None:
    formatter = logging.Formatter()
    for record in caplog.records:
        if record.levelno < logging.INFO:
            continue
        text = record.getMessage()
        if record.exc_info:
            text += formatter.formatException(record.exc_info)
        assert SENTINEL not in text, f"{record.levelname} record leaked model output: {text!r}"
    if fingerprint_of is not None:
        expected = describe_llm_text(fingerprint_of)
        assert any(
            r.levelno >= logging.WARNING and expected in r.getMessage() for r in caplog.records
        ), "parse failure was not logged with the response fingerprint"
        assert any(
            r.levelno == logging.DEBUG and SENTINEL in r.getMessage() for r in caplog.records
        )


def test_speaker_identification_unparseable_reply_logs_no_names(monkeypatch, caplog):
    reply = f'{{"speaker_predictions": [{{"predicted_name": "{SENTINEL}" oops'
    service = _service(LLMProvider.OPENAI, _openai_body(reply), monkeypatch)
    caplog.set_level(logging.DEBUG)

    result = service.identify_speakers(
        transcript="SPEAKER_00: hello",
        speaker_segments=[{"speaker_label": "SPEAKER_00", "text": "hello"}],
        known_speakers=[],
    )

    assert result["speaker_predictions"] == []
    assert result["error"].startswith("Invalid JSON response")
    _assert_no_leak(caplog, fingerprint_of=reply)


def test_speaker_prediction_missing_fields_logs_keys_not_values(monkeypatch, caplog):
    reply = json.dumps(
        {"speaker_predictions": [{"predicted_name": SENTINEL, "reasoning": SENTINEL}]}
    )
    service = _service(LLMProvider.OPENAI, _openai_body(reply), monkeypatch)
    caplog.set_level(logging.DEBUG)

    result = service.identify_speakers(
        transcript="SPEAKER_00: hello",
        speaker_segments=[{"speaker_label": "SPEAKER_00", "text": "hello"}],
        known_speakers=[],
    )

    assert result["speaker_predictions"] == []
    _assert_no_leak(caplog)
    assert any(
        "missing fields" in r.getMessage() and "predicted_name" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_summary_unrepairable_reply_logs_no_content(monkeypatch, caplog):
    # No colon anywhere: json_repair turns "{key: value" prose into a dict and
    # the summary would then be "recovered" rather than failing.
    reply = f"I cannot summarize this because {SENTINEL} was unclear"
    service = _service(LLMProvider.OPENAI, _openai_body(reply), monkeypatch)
    caplog.set_level(logging.DEBUG)

    result = service._process_single_chunk(
        transcript="SPEAKER_00: hello",
        speaker_data=None,
        prompt_template="{transcript}\n{speaker_data}",
    )

    assert result["error"] == "JSON parsing failed"
    _assert_no_leak(caplog, fingerprint_of=reply)


def test_non_json_http_body_logs_no_content(monkeypatch, caplog):
    body = f"<html>proxy page quoting the reply: {SENTINEL}</html>"
    service = _service(LLMProvider.OPENAI, body, monkeypatch)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(Exception, match="Invalid JSON response"):
        service.chat_completion([{"role": "user", "content": "hi"}])

    _assert_no_leak(caplog, fingerprint_of=body)


def test_ollama_empty_content_logs_message_keys_not_thinking(monkeypatch, caplog):
    body = json.dumps(
        {"message": {"role": "assistant", "content": "", "thinking": SENTINEL}, "done": True}
    )
    service = _service(LLMProvider.OLLAMA, body, monkeypatch)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(Exception, match="Empty content"):
        service.chat_completion([{"role": "user", "content": "hi"}])

    _assert_no_leak(caplog)
    assert any(
        "content is empty" in r.getMessage() and "thinking" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR
    )


@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(f"No JSON here, just a quote: {SENTINEL}", id="no-json-found"),
        pytest.param(f'<answer>{{"suggested_tags": ["{SENTINEL}",]}}</answer>', id="bad-json"),
        pytest.param(json.dumps({"suggested_tags": SENTINEL}), id="schema-invalid"),
    ],
)
def test_topic_extraction_parse_failures_log_no_content(monkeypatch, caplog, reply):
    service = _service(LLMProvider.OPENAI, _openai_body(reply), monkeypatch)
    caplog.set_level(logging.DEBUG)

    result = TopicExtractionService(db=None)._call_llm_for_extraction(
        llm_service=service,
        transcript="SPEAKER_00: the quarterly budget review",
        file_id=1,
        duration=60.0,
    )

    assert result is None
    _assert_no_leak(caplog, fingerprint_of=reply)


def test_describe_llm_text_is_stable_and_content_free():
    first = describe_llm_text(SENTINEL)
    assert first == describe_llm_text(SENTINEL)
    assert first != describe_llm_text(SENTINEL + "x")
    assert first.startswith(f"len={len(SENTINEL)} sha256=")
    assert SENTINEL not in first
    assert describe_llm_text(None) == "len=0 sha256=none"
