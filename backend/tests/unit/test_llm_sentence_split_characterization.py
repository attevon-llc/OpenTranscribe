"""How the summarizer cuts an oversized speaker chunk into sentences (D11).

``LLMService._split_oversized_chunk_by_sentences`` packs sentences into sub-chunks that fit
the token budget. A budget of 1 token fits nothing, so every sentence becomes its own
sub-chunk and the output IS the sentence split.
"""

from __future__ import annotations

import pytest

from app.services.llm_service import LLMService
from app.services.llm_types import LLMConfig
from app.services.llm_types import LLMProvider
from app.services.search import sentence_splitter

pytestmark = pytest.mark.unit

ONE_PER_SUBCHUNK = 1
EVERYTHING_FITS = 10_000


@pytest.fixture(autouse=True)
def _fresh_sentence_splitter():
    # Another test may have latched the punkt-unavailable fallback for this process (#449);
    # the sentence cuts below are only defined for the real splitter.
    sentence_splitter.reset_sentence_splitter_state()
    yield
    sentence_splitter.reset_sentence_splitter_state()


@pytest.fixture
def service() -> LLMService:
    config = LLMConfig(provider=LLMProvider.VLLM, model="test-model", base_url="http://llm.test/v1")
    return LLMService(config)


def test_an_abbreviation_is_not_a_sentence_end(service):
    # Changed by D11: the bare `(?<=[.!?])\s+` regex cut after "Dr." too. The summarizer now
    # uses the splitter the chunk index and the digest use (`split_into_sentences`, punkt).
    chunk = "SPEAKER_00: [00:01] Dr. Smith arrived. he sat down! Was it 3.5 hours? Yes."

    assert service._split_oversized_chunk_by_sentences(chunk, ONE_PER_SUBCHUNK) == [
        "SPEAKER_00: [00:01] Dr. Smith arrived.",
        "he sat down!",
        "Was it 3.5 hours?",
        "Yes.",
    ]


def test_cjk_terminators_end_sentences(service):
    # Changed by D11: the bare regex knew only ASCII terminators, so a CJK speaker chunk was
    # one "sentence" and an oversized one could not be cut down to the budget at all.
    chunk = "我们开始吧。今天讨论预算！好吗？"

    assert service._split_oversized_chunk_by_sentences(chunk, ONE_PER_SUBCHUNK) == [
        "我们开始吧。",
        "今天讨论预算！",
        "好吗？",
    ]


def test_a_line_break_without_punctuation_does_not_end_a_sentence(service):
    chunk = "SPEAKER_00: [00:01] no punctuation here\nSPEAKER_01: [00:02] a reply."

    assert service._split_oversized_chunk_by_sentences(chunk, ONE_PER_SUBCHUNK) == [chunk]


def test_sentences_that_fit_are_packed_with_single_spaces(service):
    chunk = "First one.  Second one.\n\nThird one."

    assert service._split_oversized_chunk_by_sentences(chunk, EVERYTHING_FITS) == [
        "First one. Second one. Third one."
    ]


def test_an_empty_chunk_yields_nothing(service):
    assert service._split_oversized_chunk_by_sentences("", EVERYTHING_FITS) == []
