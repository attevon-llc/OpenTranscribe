"""Characterization tests for ``app/services/redaction/detectors/llm.py``.

The optional LLM redaction detector (off by default, issue #445) sends transcript text to
the user's configured LLM with a strict-JSON prompt and resolves the returned phrases back
to character spans. It has to be tolerant of a chatty, occasionally malformed model, and it
must never surface a raw exception into the redaction pipeline.

What is pinned here, in order:

1. **``_detect_one`` locates each phrase with ``text.find(phrase)`` (L88)** — that returns only
   the FIRST occurrence. A phrase repeated later in the same segment leaks: only the first
   instance is ever redacted. ``test_repeated_phrase_only_first_occurrence_is_redacted`` asserts
   today's WRONG behaviour on purpose so the defect cannot drift while it is open. Fixed
   behaviour would return one span per occurrence (or otherwise redact every instance), not
   just the first.
2. **``_parse_json_array``'s bracket-slicing (L121-122)** — ``text.find("[")`` /
   ``text.rfind("]")`` span from the FIRST ``[`` to the LAST ``]`` in the whole string, not just
   the JSON array. A model that wraps the array in prose containing a stray bracket (e.g. a
   citation marker like ``[ref 1]``) produces a corrupted slice that fails to parse.
   ``test_bracket_in_surrounding_prose_corrupts_the_parse`` pins that this fails CLOSED — the
   malformed slice falls through to the ``except`` branch and returns ``[]``, never raises.
   Fixed behaviour would locate the actual JSON array (e.g. via a proper bracket-matching scan
   or ``json.JSONDecoder.raw_decode``) rather than the widest possible substring.
3. **Category validation (``_VALID_CATEGORIES``, L30)** — pins that ``"custom"`` is accepted
   even though the prompt (L21-28) only asks the model for ``pii``/``profanity``/``toxicity``.
   A model that hallucinates ``"category": "custom"`` passes validation and produces a span.
4. **Fail-open contract** — ``detect_with_llm`` never raises. Any internal failure (no LLM
   configured, the LLM call itself raising, a malformed response) results in ``{}``, never a
   propagated exception.

Following the characterization-test convention of ``tests/unit/test_chunking_service.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.llm_service import LLMService
from app.services.redaction.detectors.llm import _parse_json_array
from app.services.redaction.detectors.llm import detect_with_llm


class _FakeLLMResponse:
    """The shape ``LLMService.chat_completion`` returns (only the field the detector reads)."""

    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMService:
    """Stand-in for ``LLMService`` that returns a canned response or raises on demand."""

    def __init__(self, content: str = "[]", *, raise_on_chat: bool = False) -> None:
        self._content = content
        self._raise_on_chat = raise_on_chat
        self.closed = False
        self.calls: list[list[dict[str, str]]] = []

    def chat_completion(self, messages: list[dict[str, str]], **kwargs: Any) -> _FakeLLMResponse:
        self.calls.append(messages)
        if self._raise_on_chat:
            raise RuntimeError("simulated LLM transport failure")
        return _FakeLLMResponse(self._content)

    def close(self) -> None:
        self.closed = True


def _install_fake_service(monkeypatch: pytest.MonkeyPatch, service: _FakeLLMService | None) -> None:
    """Patch the seam ``detect_with_llm`` reaches through its inline import.

    ``llm.py`` does ``from app.services.llm_service import LLMService`` *inside*
    ``detect_with_llm`` rather than at module scope, so the seam to patch is the staticmethod
    on the real class, not a name inside the detector module.
    """
    monkeypatch.setattr(
        LLMService, "create_from_settings", staticmethod(lambda user_id=None: service)
    )


# ---------------------------------------------------------------------------
# 1. Repeated-phrase defect (L88 `text.find(phrase)`)
# ---------------------------------------------------------------------------


def test_repeated_phrase_only_first_occurrence_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Today's WRONG behaviour: a phrase repeated in one segment is redacted only once.

    ``_detect_one`` resolves each returned phrase with ``text.find(phrase)``, which always
    returns the index of the FIRST match. The second "John Smith" below is never covered by
    any span and would leak through masking. A fix would need to return a span per
    occurrence (e.g. iterate with ``str.find(phrase, cursor)``) so every instance is caught.
    """
    text = "Contact John Smith or John Smith again for details."
    first_start = text.find("John Smith")
    second_start = text.find("John Smith", first_start + 1)
    assert first_start != second_start  # sanity: the fixture text really does repeat

    content = '[{"text": "John Smith", "category": "pii", "entity_type": "NAME"}]'
    service = _FakeLLMService(content=content)
    _install_fake_service(monkeypatch, service)

    segments = [{"text": text}]
    results = detect_with_llm(segments, user_id=1)

    assert 0 in results
    spans = results[0]
    assert len(spans) == 1
    span = spans[0]
    assert span.char_start == first_start
    assert span.char_end == first_start + len("John Smith")
    # The defect: nothing addresses the second occurrence.
    assert span.char_start != second_start
    assert service.closed is True


# ---------------------------------------------------------------------------
# 2. Bracket-slicing defect in `_parse_json_array` (L121-122)
# ---------------------------------------------------------------------------


def test_bracket_in_surrounding_prose_corrupts_the_parse() -> None:
    """Today's WRONG behaviour: a stray `[`/`]` outside the JSON array corrupts the slice.

    ``text.find("[")`` grabs the FIRST `[` in the whole response and ``text.rfind("]")`` grabs
    the LAST `]` — here that spans from a citation-style marker ``[ref 1]`` all the way to the
    real array's closing bracket, producing a substring that is not valid JSON. The parser
    fails CLOSED: ``json.loads`` raises, the ``except`` branch returns ``[]``, and nothing
    propagates. A fix would locate the actual JSON array boundaries (e.g.
    ``json.JSONDecoder.raw_decode`` from the first `[`, or a proper bracket-depth scan) so a
    stray bracket elsewhere in the prose can't corrupt an otherwise well-formed array.
    """
    content = 'Note [ref 1]: [{"text": "John Smith", "category": "pii", "entity_type": "NAME"}]'
    # Confirm the corrupted slice really is what L121-122 would produce, so this test fails
    # loudly (not silently) if that slicing logic ever changes shape.
    start = content.find("[")
    end = content.rfind("]")
    assert content[start : end + 1].startswith("[ref 1]")  # not "[{" — the real array's start

    result = _parse_json_array(content)

    assert result == []


def test_bracket_free_prose_parses_the_array_normally() -> None:
    """Control for the above: with no stray bracket, the same array parses fine.

    Demonstrates the defect is specifically about a bracket character elsewhere in the
    response, not a general failure to parse prose-wrapped JSON.
    """
    content = (
        'Sure, here you go: [{"text": "John Smith", "category": "pii", "entity_type": "NAME"}]'
    )

    result = _parse_json_array(content)

    assert len(result) == 1
    assert result[0]["text"] == "John Smith"


# ---------------------------------------------------------------------------
# 3. Category validation accepts the hallucinated "custom" category
# ---------------------------------------------------------------------------


def test_custom_category_passes_validation_though_never_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_VALID_CATEGORIES`` includes "custom" even though the prompt only asks for
    pii/profanity/toxicity (L21-28). A model that hallucinates ``"category": "custom"``
    still produces a redaction span rather than being rejected as an unrecognised category.
    """
    text = "The password is hunter2, keep it safe."
    content = '[{"text": "hunter2", "category": "custom", "entity_type": "SECRET"}]'
    service = _FakeLLMService(content=content)
    _install_fake_service(monkeypatch, service)

    results = detect_with_llm([{"text": text}], user_id=1)

    assert 0 in results
    spans = results[0]
    assert len(spans) == 1
    assert spans[0].category == "custom"
    assert spans[0].entity_type == "SECRET"


def test_unrecognised_category_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control for the above: a category outside ``_VALID_CATEGORIES`` produces no span."""
    text = "The password is hunter2, keep it safe."
    content = '[{"text": "hunter2", "category": "banana", "entity_type": "SECRET"}]'
    service = _FakeLLMService(content=content)
    _install_fake_service(monkeypatch, service)

    results = detect_with_llm([{"text": text}], user_id=1)

    assert results == {}


# ---------------------------------------------------------------------------
# 4. Fail-open contract: detect_with_llm never raises
# ---------------------------------------------------------------------------


def test_llm_call_failure_returns_empty_dict_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising LLM call must not propagate — the pipeline must never block on this detector."""
    service = _FakeLLMService(raise_on_chat=True)
    _install_fake_service(monkeypatch, service)

    results = detect_with_llm([{"text": "some segment text"}], user_id=1)

    assert results == {}
    assert service.closed is True  # `finally: service.close()` still runs


def test_no_llm_configured_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_from_settings` returning None (no LLM configured) short-circuits to `{}`."""
    _install_fake_service(monkeypatch, None)

    results = detect_with_llm([{"text": "some segment text"}], user_id=1)

    assert results == {}


def test_llm_service_creation_raising_returns_empty_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """`create_from_settings` itself raising (e.g. a bad config) is also caught and swallowed."""

    def _raise(user_id: int | None = None) -> None:
        raise RuntimeError("simulated settings-lookup failure")

    monkeypatch.setattr(LLMService, "create_from_settings", staticmethod(_raise))

    results = detect_with_llm([{"text": "some segment text"}], user_id=1)

    assert results == {}


# ---------------------------------------------------------------------------
# Baseline sanity: a clean, well-formed response produces the expected span, so the
# fail-open tests above are proven against a real "it can work" control.
# ---------------------------------------------------------------------------


def test_well_formed_response_produces_a_span(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "My email is jane@example.com, reach out anytime."
    content = '[{"text": "jane@example.com", "category": "pii", "entity_type": "EMAIL"}]'
    service = _FakeLLMService(content=content)
    _install_fake_service(monkeypatch, service)

    results = detect_with_llm([{"text": text}], user_id=42)

    assert 0 in results
    spans = results[0]
    assert len(spans) == 1
    span = spans[0]
    assert span.char_start == text.find("jane@example.com")
    assert span.category == "pii"
    assert span.entity_type == "EMAIL"
    assert span.detector == "llm"
    # The call really carried the segment text to the (fake) LLM.
    assert len(service.calls) == 1
    assert "jane@example.com" in service.calls[0][0]["content"]


def test_blank_segment_is_skipped_without_calling_the_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace-only segments are filtered before the LLM is ever called (L57-58)."""
    service = _FakeLLMService(content="[]")
    _install_fake_service(monkeypatch, service)

    results = detect_with_llm([{"text": "   "}], user_id=1)

    assert results == {}
    assert service.calls == []
