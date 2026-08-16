"""The mock LLM provider itself (``scripts/mock-llm-server.py``).

These are tests OF the test tool. They exist because every other suite that
uses the mock trusts its scenarios to behave: if ``mock-error`` quietly started
returning 200, a test asserting the app surfaces a provider error would pass
while proving nothing.

Runs with no stack: the fixture falls back to a subprocess when the compose
container is not up.
"""

from __future__ import annotations

import json

import requests

MODELS = ("mock-gpt", "mock-echo", "mock-empty", "mock-error", "mock-slow", "mock-reasoning")


def test_advertises_every_scenario_model(mock_llm_url):
    """Model discovery must list the scenarios, or nobody can select one."""
    listed = requests.get(f"{mock_llm_url}/models", timeout=10).json()
    ids = {entry["id"] for entry in listed["data"]}
    assert set(MODELS) <= ids


def test_default_model_returns_a_cited_answer(mock_llm_completion):
    """The normal path: markdown with [1]/[2] so citation rendering is exercised."""
    result = mock_llm_completion("What was discussed?")
    assert result["status_code"] == 200
    content = result["body"]["choices"][0]["message"]["content"]
    assert "[1]" in content
    assert "[2]" in content
    assert "```" in content  # a code block, for the copy-button path


def test_reply_echoes_the_question_so_responses_look_answered(mock_llm_completion):
    content = mock_llm_completion("pricing objections")["body"]["choices"][0]["message"]["content"]
    assert "pricing objections" in content


def test_echo_model_returns_the_prompt_it_was_given(mock_llm_completion):
    """The scenario that lets a test assert what the app actually SENT.

    This is how a future test verifies redaction masking reached the provider,
    or that the system-prompt layers arrived in the documented order.
    """
    result = mock_llm_completion("SECRET-MARKER-42", model="mock-echo")
    content = result["body"]["choices"][0]["message"]["content"]
    assert "SECRET-MARKER-42" in content
    assert "[user]" in content


def test_empty_model_completes_with_no_content(mock_llm_completion):
    """A configured provider that returns nothing — distinct from being absent."""
    result = mock_llm_completion("anything", model="mock-empty")
    assert result["status_code"] == 200
    assert result["body"]["choices"][0]["message"]["content"] == ""


def test_error_model_fails_with_a_5xx(mock_llm_completion):
    """A configured provider that breaks. This IS worth a red notification."""
    result = mock_llm_completion("anything", model="mock-error")
    assert result["status_code"] == 500
    assert "error" in result["body"]


def test_usage_is_reported_so_metering_can_be_exercised(mock_llm_completion):
    usage = mock_llm_completion("count my tokens")["body"]["usage"]
    assert usage["prompt_tokens"] > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]


def test_reasoning_model_reports_reasoning_content_non_streamed(mock_llm_completion):
    """The non-streaming shape carries reasoning too, matching a real provider."""
    result = mock_llm_completion("what happened", model="mock-reasoning")
    message = result["body"]["choices"][0]["message"]
    assert message["reasoning_content"]
    assert message["content"]  # the final answer is still there, separately


def test_reasoning_streams_before_the_answer(mock_llm_url):
    """Reasoning deltas arrive first, on their own field, then the answer.

    Pins the ordering the frontend's collapsible block depends on: reasoning
    must be distinguishable from — and precede — the final answer text.

    ``chat_template_kwargs`` is what activates thinking, and the mock honours it
    because a real vLLM does (issue #439): unasked, a Gemma-class model streams
    its chain-of-thought inline on ``delta.content`` instead. That unasked shape
    is covered by ``test_llm_reasoning_not_rendered_as_answer.py``.
    """
    response = requests.post(
        f"{mock_llm_url}/chat/completions",
        json={
            "model": "mock-reasoning",
            "messages": [{"role": "user", "content": "explain your thinking"}],
            "stream": True,
            "chat_template_kwargs": {"enable_thinking": True},
        },
        stream=True,
        timeout=60,
    )
    assert response.status_code == 200

    reasoning_chunks: list[str] = []
    content_chunks: list[str] = []
    saw_content_before_reasoning_ended = False
    reasoning_done = False
    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            break
        frame = json.loads(payload)
        delta = (frame.get("choices") or [{}])[0].get("delta", {})
        if delta.get("reasoning_content"):
            if content_chunks:
                saw_content_before_reasoning_ended = True
            reasoning_chunks.append(delta["reasoning_content"])
        elif delta.get("content"):
            reasoning_done = True
            content_chunks.append(delta["content"])

    assert reasoning_chunks, "expected at least one reasoning_content chunk"
    assert content_chunks, "expected the answer to still stream after reasoning"
    assert reasoning_done
    assert not saw_content_before_reasoning_ended


def test_streaming_ends_with_usage_then_done(mock_llm_url):
    """Frame order the app depends on: deltas, a usage-only chunk, then [DONE].

    Also pins that the stream terminates at all — the server sends
    ``Connection: close`` because it uses neither Content-Length nor chunked
    framing, and without it a client blocks past [DONE] until it times out.
    """
    response = requests.post(
        f"{mock_llm_url}/chat/completions",
        json={
            "model": "mock-gpt",
            "messages": [{"role": "user", "content": "stream please"}],
            "stream": True,
        },
        stream=True,
        timeout=60,
    )
    assert response.status_code == 200
    assert "event-stream" in response.headers.get("content-type", "")

    deltas, usage_frames, done = 0, 0, False
    for raw in response.iter_lines(decode_unicode=True):
        if not raw or not raw.startswith("data:"):
            continue
        payload = raw[5:].strip()
        if payload == "[DONE]":
            done = True
            break
        frame = json.loads(payload)
        if frame.get("usage"):
            usage_frames += 1
        elif (frame.get("choices") or [{}])[0].get("delta", {}).get("content"):
            deltas += 1

    assert deltas > 10, "expected token-by-token streaming, not one dump"
    assert usage_frames == 1, "exactly one usage-only chunk, like a real provider"
    assert done, "stream must terminate with [DONE]"
