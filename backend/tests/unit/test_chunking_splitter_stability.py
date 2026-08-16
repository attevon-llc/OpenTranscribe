"""The sentence splitter must be a property of the process, not of the clock.

``chunk_transcript_by_speaker_turns`` splits over-long speaker turns at sentence
boundaries, and NLTK punkt and the regex fallback put those boundaries in
different places. Re-indexing one unchanged corpus (issue #403, D5) compares a
control index against a treatment index, so any input the chunker reads that is
not the transcript makes the comparison meaningless.

``chunking_service._get_nltk_tokenizer`` used to retry a failed punkt load after a 5-minute
``time.time()`` cooldown. A single transient failure — the ``nltk_data`` mount
not yet readable while the stack chowns the model cache — therefore flipped the
splitter to regex and, 300 seconds later, flipped it back **mid-reindex**, so
files chunked early in a run and files chunked late in the same run were cut
differently.
"""

import logging
import time

import pytest

from app.services.search import chunking_service

# ~480 words in one turn: comfortably over SEARCH_CHUNK_TARGET_WORDS (200), so
# the turn reaches _split_long_turn and the sentence splitter actually decides
# where the cuts land. A turn under the target never reaches it.
_SENTENCE = "We should ship the redesign this quarter. "
_LONG_TURN = (_SENTENCE * 60).strip()
_SEGMENTS = [{"start": 0.0, "end": 300.0, "text": _LONG_TURN, "speaker": "Alice"}]


class _TwoSentenceTokenizer:
    """A punkt stand-in that cuts differently from the regex fallback.

    The regex splits on the whitespace *after* terminal punctuation and drops
    it; this keeps a trailing space. Identical sentence count, different strings,
    so the chunk text diverges without the chunk count having to.
    """

    def tokenize(self, text: str) -> list[str]:
        return [part + " " for part in text.split(". ") if part.strip()]


def _chunk() -> list[str]:
    chunks = chunking_service.chunk_transcript_by_speaker_turns(
        segments=_SEGMENTS,
        file_uuid="11111111-1111-1111-1111-111111111111",
        file_id=1,
        user_id=1,
        title="determinism probe",
        speakers=["Alice"],
        tags=[],
        upload_time="2026-01-01T00:00:00",
        language="en",
    )
    return [c["content"] for c in chunks]


@pytest.fixture
def flaky_punkt(monkeypatch):
    """punkt fails the first load attempt, then succeeds on every later one."""
    # A tokenizer resolved by an earlier test would short-circuit the load path.
    monkeypatch.setattr(chunking_service, "_nltk_tokenizers", {})
    attempts: list[str] = []

    def _load(_nltk_data_module, language: str):
        attempts.append(language)
        if len(attempts) == 1:
            raise OSError("nltk_data mount not readable yet")
        return _TwoSentenceTokenizer()

    monkeypatch.setattr(chunking_service, "_load_punkt_model", _load)
    return attempts


def test_transient_punkt_failure_does_not_change_chunking_later_in_the_run(
    flaky_punkt, monkeypatch
):
    """One failed load must pin the regex splitter for the whole process.

    Fails against the 5-minute-cooldown implementation: the second call lands
    after the cooldown expires, punkt loads successfully, and the same segments
    chunk into different text.
    """
    clock = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    early_in_run = _chunk()
    clock[0] += 301  # past the old 300 s cooldown
    late_in_run = _chunk()

    assert late_in_run == early_in_run, (
        "chunk text changed within one process for identical input: "
        f"{len(flaky_punkt)} punkt load attempt(s) were made"
    )


def test_regex_fallback_is_reported_at_warning_level(flaky_punkt, caplog):
    """The fallback has to be visible.

    It was logged at DEBUG, which is why nobody noticed that the workers doing
    the indexing (``celery-embedding-worker``, ``celery-cpu-worker``) carry no
    ``nltk_data`` mount and have always used the regex splitter, while every
    test process resolved punkt and exercised the other branch.
    """
    monkeypatch_free_language = "english"
    with caplog.at_level(logging.WARNING, logger=chunking_service.__name__):
        chunking_service.reset_sentence_splitter_state()
        assert chunking_service._get_nltk_tokenizer(monkeypatch_free_language) is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "regex fallback engaged without a WARNING"
    # Case-insensitive: the message emphasises REGEX in caps. What matters is
    # that the operator is told which splitter is in force, not its casing.
    assert "regex sentence splitter" in warnings[0].getMessage().lower()


def test_punkt_unavailability_is_not_retried(flaky_punkt):
    """A negative result is cached, so the splitter cannot flip on a later call.

    Without this, the only thing stopping a flip is how much time passed.
    """
    chunking_service.reset_sentence_splitter_state()

    first = chunking_service._get_nltk_tokenizer("english")
    second = chunking_service._get_nltk_tokenizer("english")

    assert first is None and second is None
    assert len(flaky_punkt) == 1, (
        f"punkt was re-attempted {len(flaky_punkt)} times; the second attempt "
        "would have succeeded and changed sentence boundaries"
    )
