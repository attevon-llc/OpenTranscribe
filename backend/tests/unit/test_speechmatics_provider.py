"""Unit tests for ``app/services/asr/speechmatics_provider.py``.

Network-free: no real Speechmatics call is ever made. ``requests.get`` is monkeypatched for
``validate_connection()``, and ``speechmatics.batch.AsyncClient`` is replaced with a fake for
``transcribe()`` — the same lazy-import pattern the pyannote.ai provider test uses for its
client. Response objects are built with ``SimpleNamespace`` (``_build_segments`` only ever
does ``getattr()`` on them — real duck typing, not an isinstance check), but their *shape*
is verified against the installed ``speechmatics-batch==0.4.8`` SDK
(``speechmatics/batch/_models.py``, pinned in ``requirements.txt``/``requirements-ci.txt``):
``RecognitionResult.type`` is a plain string field taking values including ``"word"`` and
``"punctuation"``, and each result carries ``alternatives[0].speaker`` / ``.content``. Notably
the SDK's OWN ``Transcript.transcript_text`` property does NOT filter results by ``type`` — it
walks every result with alternatives and lets content-string sniffing (`content.strip() in
".,!?;:()[]{}\\"'-"`) decide spacing, which is how punctuation survives in the SDK's own
formatter and, since the fixes below, in this provider's ``_build_segments`` too.

What is verified here, in order:

1. **Fixed: `_build_segments` reattaches punctuation to the preceding word** (see the
   punctuation branch inside its loop). Speechmatics's response contains separate
   ``type: "punctuation"`` entries (confirmed against the installed SDK's
   ``RecognitionResult.type`` field, and against the SDK's OWN ``Transcript.transcript_text``
   property, which does NOT filter by type — it walks every result with alternatives and lets
   content-string sniffing decide spacing). This provider now folds a punctuation result's
   content directly onto the immediately preceding ``ASRWord`` (no separating space) instead
   of dropping it or treating it as its own token, matching AWS's and Gladia's providers (not
   tested here, per scope). Leading punctuation with no preceding word in the current group is
   dropped — there is nothing to attach it to.
2. **Fixed: `validate_connection` now fails on any non-2xx status** — not just 401. A 500,
   403, 429, or any other failure status is treated as a failed connection instead of falling
   through to the unconditional success return.
3. The ``asyncio.run(_run())`` sync wrapper (L123-134) correctly surfaces both a successful and
   a failing async batch call to its synchronous caller.
4. Speechmatics's ``"UU"`` (untagged) speaker label is mapped to ``None`` BEFORE
   ``_normalize_speaker_label`` is ever called — the literal string ``"UU"`` never reaches the
   normalizer, which would otherwise hash it into a stable-but-meaningless ``SPEAKER_XX``.
5. ``client.close()`` runs in a ``finally`` block — a positive regression guard against a
   connection leak — confirmed to fire both when the async call succeeds AND when it raises.

Following the network-free provider pattern of ``tests/unit/test_pyannote_provider.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services.asr.speechmatics_provider import SpeechmaticsProvider
from app.services.asr.types import ASRConfig


def _provider(api_key: str = "test-key", model_name: str = "standard") -> SpeechmaticsProvider:
    return SpeechmaticsProvider(api_key=api_key, model_name=model_name)


def _alt(content: str, speaker: str | None) -> Any:
    return SimpleNamespace(content=content, confidence=0.95, speaker=speaker)


def _word(content: str, start: float, end: float, speaker: str | None = None) -> Any:
    return SimpleNamespace(
        type="word", start_time=start, end_time=end, alternatives=[_alt(content, speaker)]
    )


def _punct(content: str, at: float, speaker: str | None = None) -> Any:
    return SimpleNamespace(
        type="punctuation", start_time=at, end_time=at, alternatives=[_alt(content, speaker)]
    )


def _transcript(results: list[Any]) -> Any:
    return SimpleNamespace(results=results)


# ── 1. `_build_segments` reattaches punctuation to the preceding word (fixed) ──────────


def test_build_segments_reattaches_punctuation_to_the_preceding_word():
    """Fixed: punctuation-type results are folded onto the previous word's text."""
    provider = _provider()
    transcript = _transcript(
        [
            _word("Hello", 0.0, 0.5, speaker="S1"),
            _punct(",", 0.5, speaker="S1"),
            _word("world", 0.6, 1.0, speaker="S1"),
            _punct(".", 1.0, speaker="S1"),
        ]
    )

    segments = provider._build_segments(transcript)

    assert len(segments) == 1
    assert segments[0].text == "Hello, world."
    assert [w.word for w in segments[0].words] == ["Hello,", "world."]
    # No extra space was introduced before the punctuation.
    assert "Hello ," not in segments[0].text
    assert "world ." not in segments[0].text


def test_build_segments_drops_leading_punctuation_with_no_preceding_word():
    """A punctuation result with nothing before it in the current group is dropped —
    there is no preceding word to attach it to."""
    provider = _provider()
    transcript = _transcript(
        [
            _punct("-", 0.0, speaker="S1"),
            _word("Hello", 0.1, 0.5, speaker="S1"),
        ]
    )

    segments = provider._build_segments(transcript)

    assert len(segments) == 1
    assert segments[0].text == "Hello"
    assert [w.word for w in segments[0].words] == ["Hello"]


def test_build_segments_keeps_word_results_across_multiple_speakers():
    """Contrast/sanity: word-type results still group correctly by speaker change."""
    provider = _provider()
    transcript = _transcript(
        [
            _word("Hi", 0.0, 0.3, speaker="S1"),
            _word("there", 0.3, 0.6, speaker="S1"),
            _word("Hello", 1.0, 1.3, speaker="S2"),
        ]
    )

    segments = provider._build_segments(transcript)

    assert len(segments) == 2
    assert segments[0].text == "Hi there"
    assert segments[0].speaker == "SPEAKER_00"
    assert segments[1].text == "Hello"
    assert segments[1].speaker == "SPEAKER_01"


# ── 1b. "UU" speaker mapped to None BEFORE normalization ───────────────────────────────


def test_uu_speaker_mapped_to_none_before_normalization(monkeypatch: pytest.MonkeyPatch):
    provider = _provider()
    calls: list[str | int | None] = []
    original = provider._normalize_speaker_label

    def spy(label: str | int | None) -> str | None:
        calls.append(label)
        return original(label)

    monkeypatch.setattr(provider, "_normalize_speaker_label", spy)

    transcript = _transcript(
        [
            _word("Untagged", 0.0, 0.5, speaker="UU"),
            _word("Hello", 0.6, 1.0, speaker="S1"),
        ]
    )

    segments = provider._build_segments(transcript)

    assert len(segments) == 2
    assert segments[0].speaker is None
    assert segments[1].speaker == "SPEAKER_00"
    # "UU" itself never reaches the normalizer — only "S1" (from the second segment) does.
    assert "UU" not in calls
    assert calls == ["S1"]


# ── 2. `validate_connection` fails on any non-2xx status (fixed) ───────────────────────


def test_validate_connection_fails_on_500(monkeypatch: pytest.MonkeyPatch):
    """Fixed: a 500 response is now reported as a failed connection, not a success."""
    provider = _provider(api_key="secret-key-123")
    monkeypatch.setattr("requests.get", lambda *a, **k: SimpleNamespace(status_code=500))

    ok, message, ms = provider.validate_connection()

    assert ok is False
    assert "500" in message
    assert "secret-key-123" not in message  # sanitized via _sanitize_error
    assert isinstance(ms, float)


def test_validate_connection_fails_on_other_non_2xx_statuses(monkeypatch: pytest.MonkeyPatch):
    """Fixed: 403/429 (neither 200 nor 401) are also treated as failures."""
    provider = _provider(api_key="secret-key-123")

    for status in (403, 429):
        monkeypatch.setattr(
            "requests.get", lambda *a, status=status, **k: SimpleNamespace(status_code=status)
        )

        ok, message, ms = provider.validate_connection()

        assert ok is False
        assert str(status) in message
        assert isinstance(ms, float)


def test_validate_connection_succeeds_on_200(monkeypatch: pytest.MonkeyPatch):
    """Contrast: a real 2xx status is still reported as success."""
    provider = _provider(api_key="secret-key-123")
    monkeypatch.setattr("requests.get", lambda *a, **k: SimpleNamespace(status_code=200))

    ok, message, ms = provider.validate_connection()

    assert ok is True
    assert message == "Speechmatics connection successful"
    assert isinstance(ms, float)


def test_validate_connection_detects_401(monkeypatch: pytest.MonkeyPatch):
    """Contrast: the one status code the function DOES check works correctly."""
    provider = _provider(api_key="secret-key-123")
    monkeypatch.setattr("requests.get", lambda *a, **k: SimpleNamespace(status_code=401))

    ok, message, ms = provider.validate_connection()

    assert ok is False
    assert "401" in message
    assert "secret-key-123" not in message  # sanitized via _sanitize_error


# ── 3/4. asyncio.run(_run()) wrapper: success and failure, close() always runs ─────────


def _make_fake_async_client(
    *, fail_with: Exception | None = None, transcript: Any = None
) -> tuple[type, list[Any]]:
    """Build a fake ``AsyncClient`` class recording lifecycle state, no network I/O."""
    created: list[Any] = []

    class _FakeAsyncClient:
        def __init__(self, api_key: str):
            self.api_key = api_key
            self.closed = False
            created.append(self)

        async def submit_job(self, audio_path: str, transcription_config: Any = None) -> Any:
            return SimpleNamespace(id="job-123")

        async def wait_for_completion(
            self, job_id: str, format_type: Any = None, timeout: float | None = None
        ) -> Any:
            if fail_with is not None:
                raise fail_with
            return transcript

        async def close(self) -> None:
            self.closed = True

    return _FakeAsyncClient, created


def test_transcribe_success_surfaces_result_and_closes_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake-audio-bytes")
    transcript = _transcript([_word("Hello", 0.0, 0.5, speaker="S1")])
    fake_cls, created = _make_fake_async_client(transcript=transcript)
    monkeypatch.setattr("speechmatics.batch.AsyncClient", fake_cls)

    provider = _provider()
    result = provider.transcribe(str(audio_path), ASRConfig(language="en", enable_diarization=True))

    assert result.provider_name == "speechmatics"
    assert len(result.segments) == 1
    assert result.segments[0].text == "Hello"
    assert len(created) == 1
    assert created[0].closed is True  # finally-block close ran on the success path


def test_transcribe_failure_reraises_and_still_closes_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake-audio-bytes")
    fake_cls, created = _make_fake_async_client(fail_with=RuntimeError("job timed out"))
    monkeypatch.setattr("speechmatics.batch.AsyncClient", fake_cls)

    provider = _provider()
    with pytest.raises(RuntimeError, match="Speechmatics transcription failed"):
        provider.transcribe(str(audio_path), ASRConfig(language="en"))

    assert len(created) == 1
    # Positive regression guard: close() still ran even though wait_for_completion raised.
    assert created[0].closed is True


# ── Rate-limit taxonomy (issue Lane 5) ────────────────────────────────────────


class _FakeSDKError(Exception):
    """Stand-in for a real vendor SDK exception carrying a `.status_code`.

    Real SDK exception classes declare this attribute themselves; plain ``Exception``
    does not, so it is declared here purely so the assignment below type-checks.
    """

    status_code: int


def test_429_status_code_is_classified_as_asr_rate_limited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake-audio-bytes")
    rate_limit_exc = _FakeSDKError("too many requests")
    rate_limit_exc.status_code = 429  # matches the SDK's exception shape
    fake_cls, created = _make_fake_async_client(fail_with=rate_limit_exc)
    monkeypatch.setattr("speechmatics.batch.AsyncClient", fake_cls)

    provider = _provider()
    with pytest.raises(ASRRateLimitedError) as excinfo:
        provider.transcribe(str(audio_path), ASRConfig(language="en"))

    assert excinfo.value.provider == "speechmatics"
    # Positive regression guard: close() still ran even though wait_for_completion raised.
    assert created[0].closed is True


def test_non_429_error_stays_a_plain_runtime_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Any):
    """Negative control: an error with no rate-limit status must NOT be classified as
    retryable.
    """
    from app.services.asr.errors import ASRRateLimitedError

    audio_path = tmp_path / "audio.wav"
    audio_path.write_bytes(b"fake-audio-bytes")
    server_error = _FakeSDKError("internal error")
    server_error.status_code = 500
    fake_cls, created = _make_fake_async_client(fail_with=server_error)
    monkeypatch.setattr("speechmatics.batch.AsyncClient", fake_cls)

    provider = _provider()
    with pytest.raises(RuntimeError, match="Speechmatics transcription failed") as excinfo:
        provider.transcribe(str(audio_path), ASRConfig(language="en"))

    assert not isinstance(excinfo.value, ASRRateLimitedError)
