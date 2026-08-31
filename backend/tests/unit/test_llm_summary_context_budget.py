"""Unit tests for LLMService's summarization chunking budget (issue #645-adjacent).

`_chunk_transcript_intelligently` decides whether a transcript needs to be split
before it reaches the provider. It used to reserve a flat 2000 tokens for
"prompt + response" regardless of `self.response_tokens` (up to 16384) -- so a
transcript sized to fit that miscalculated budget could still overflow the real
model context once the actual response reservation was added back in on the API
call. Measured live: a 3-hour transcript (204,655 chars, ~51,000 estimated
tokens) was accepted as a single chunk against a 60000-token model with
response_tokens=15000, then vLLM 400'd at 45001 input + 15000 output tokens.

No live stack needed -- LLMService's token-budget math is pure.
"""

from __future__ import annotations

from app.services.llm_service import LLMConfig
from app.services.llm_service import LLMProvider
from app.services.llm_service import LLMService


def _service(max_tokens: int) -> LLMService:
    return LLMService(
        LLMConfig(
            provider=LLMProvider.VLLM,
            model="test-model",
            base_url="http://llm.test/v1",
            max_tokens=max_tokens,
        )
    )


def test_available_tokens_reserves_the_actual_response_budget_not_a_flat_guess():
    """The real bug: `available_tokens` used to ignore `self.response_tokens`.

    context_window=60000 -> response_tokens = max(4000, min(16384, 60000//4)) = 15000, so the
    real single-chunk ceiling is ~43000 estimated tokens (45000 minus this test's small
    overhead). The old formula (`context_window - 2000` = 58000) would accept a transcript
    in the 43000-58000 range as a single chunk -- exactly reproducing the live overflow,
    where a 3-hour transcript was sent whole and vLLM 400'd at 45001 input + 15000 output
    tokens against the 60000-token model. Sized directly off the real `_estimate_tokens`
    heuristic (not a guessed chars-per-token ratio) so the boundary is exact, not approximate.
    """
    service = _service(max_tokens=60000)
    assert service.response_tokens == 15000

    # Realistic transcript shape: sentences, not one giant run-on -- the splitter falls back to
    # sentence boundaries when a chunk is oversized, so a fixture with none of those (e.g. a
    # bare "word " * N run-on) can never be split down, masking the fix either way.
    sentence = "This is a spoken sentence from the transcript. "
    unit_tokens = service._estimate_tokens(sentence)
    repeats = int(50_000 / unit_tokens) + 1  # lands comfortably between 43000 and 58000
    transcript = sentence * repeats
    estimated = service._estimate_tokens(transcript)
    assert 43_000 < estimated < 58_000, (
        f"test fixture must sit strictly between the old (58000) and new (43000) budgets, "
        f"got {estimated} estimated tokens -- adjust `repeats`"
    )

    chunks = service._chunk_transcript_intelligently(transcript)

    # Old code (available_tokens=58000) accepts this whole, as one chunk, reproducing the
    # live overflow once the real 15000-token response reservation is added back on the API
    # call. The fix must refuse to treat it as a single chunk.
    assert len(chunks) > 1, (
        "a transcript this size must not be treated as a single chunk against a "
        "60000-token model with a 15000-token response reservation"
    )


def test_a_transcript_that_genuinely_fits_stays_a_single_chunk():
    """Control: a small transcript on a large window must not be needlessly split."""
    service = _service(max_tokens=60000)
    transcript = "A short transcript that easily fits in one chunk."

    chunks = service._chunk_transcript_intelligently(transcript)

    assert len(chunks) == 1
    assert chunks[0] == transcript


def test_available_tokens_never_goes_negative_on_a_small_context_window():
    """A small-context deployment must not crash or produce a nonsensical negative budget."""
    service = _service(max_tokens=4096)
    assert service.response_tokens == 4000  # floor, per max(4000, min(16384, 4096 // 4))

    transcript = "word " * 2000

    chunks = service._chunk_transcript_intelligently(transcript)

    assert chunks  # must still produce something, not raise or return an empty list
