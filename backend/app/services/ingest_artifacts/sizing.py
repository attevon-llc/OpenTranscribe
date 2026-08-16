"""Digest sizing, derived from a MEASUREMENT of the deployed embedding model.

Addendum **G8** says the digest is bounded by the embedding window and instructs the
implementer to *verify the deployed model's actual truncation before sizing*. It was
worth doing: the addendum's working figure was ~256 wordpieces, and the model as
OpenSearch ML Commons actually deploys it truncates at **128**.

## The measurement

Against `otfresh-rag403`'s OpenSearch (the isolated Stage-2 stack) running the registered
``huggingface/sentence-transformers/all-MiniLM-L6-v2``, via
``POST /_plugins/_ml/_predict/text_embedding/<model_id>``. Method: embed a prefix, embed
the same prefix with four distinctive words appended, and compare. Once the prefix fills
the window the appended words fall off the end and the two vectors become *identical* —
cosine 1.0 to eight decimal places, not merely close.

| Probe text | Appending stops changing the vector at |
|---|---|
| single-wordpiece filler words | **126 words** |
| deliberately rare, multi-wordpiece words (~3.6 pieces each) | **35 words** (≈126 pieces) |
| real QMSum transcript text off the stack's chunk index | **92 words** |

Two different word-length regimes converging on ~126 *pieces* is what shows the limit is
tokenizer-side, not word-side: 126 content wordpieces plus ``[CLS]`` and ``[SEP]`` is a
128-token window. Nothing in the model's ML Commons ``model_config`` states this — its
``all_config`` advertises ``max_position_embeddings: 512`` — so it is only knowable by
measuring, which is precisely why G8 asked.

Reproduce with ``scripts/measure_embedding_window.py``.

## What that implies for the digest

A 150–200 word digest — the addendum's fallback suggestion — would have **more than half
its text absent from its own vector**, silently. So this stage takes G8's other branch:
**sectioned digests**, each section short enough to survive whole, indexed by Stage 3 as
separate documents.

The per-section budget is *derived* from the measurement rather than chosen:

    section_max_words = (content_budget − header_reserve) / wordpieces_per_word

The header reserve is Stage 3's business — it prefixes each digest document with
``"{title} | {date} | participants: {roster}"`` before embedding (the zero-LLM
contextualization in #383 Phase 3) — but the reserve has to be subtracted *here*, because
here is where the text gets cut to length. Stage 3 must keep its header inside
:data:`HEADER_WORDPIECE_RESERVE` or re-derive these numbers.
"""

from __future__ import annotations

import math

#: Total token window of the deployed embedding model, MEASURED (see module docstring).
#: Not read from the model config — the config does not state it.
EMBEDDING_MAX_WORDPIECES = 128

#: ``[CLS]`` and ``[SEP]``, which the tokenizer adds and which count against the window.
EMBEDDING_SPECIAL_TOKENS = 2

#: Wordpieces available to actual content.
EMBEDDING_CONTENT_WORDPIECES = EMBEDDING_MAX_WORDPIECES - EMBEDDING_SPECIAL_TOKENS

#: Measured on real QMSum transcript text: 92 words filled the 126-piece content budget.
#: Conservative for digest prose, which has fewer standalone punctuation tokens and no
#: ``{disfmarker}``-style annotation noise than the raw ASR text it was measured on.
MEASURED_WORDPIECES_PER_WORD = 126 / 92  # ≈ 1.37

#: Budget handed to Stage 3 for the ``embedding_text`` prefix it puts on digest documents.
#: ``"Weekly product sync | 2026-08-12 | participants: Dana, Marcus, Priya"`` is ~20
#: wordpieces; 30 leaves room for a longer title without pushing digest text out of the
#: window. If Stage 3's header grows past this, the digest text it embeds is silently
#: clipped — which is the exact failure G8 exists to prevent.
HEADER_WORDPIECE_RESERVE = 30

#: Hard ceiling on one digest section, derived from the measurement above.
DIGEST_SECTION_MAX_WORDS = int(
    (EMBEDDING_CONTENT_WORDPIECES - HEADER_WORDPIECE_RESERVE) / MEASURED_WORDPIECES_PER_WORD
)

#: Selection target. Sentence boundaries are respected, so a section lands somewhere
#: between this and :data:`DIGEST_SECTION_MAX_WORDS`; the gap is the overshoot allowance
#: for the sentence that crosses the target.
DIGEST_SECTION_TARGET_WORDS = 55

#: One digest section per this many source words, so a long meeting gets a longer digest
#: instead of a fixed-size one that describes only its opening.
SOURCE_WORDS_PER_SECTION = 1500

#: Ceiling on section count. Eight sections ≈ 480 digest words for a 12k-word meeting;
#: beyond that the summary tier stops being a summary and Stage 4's map-reduce is the
#: right tool.
MAX_DIGEST_SECTIONS = 8

#: Floor: every transcribed file gets at least one section. The Stage 2 gate is "100% of
#: transcribed files get facts + digest", so there is no "too short to bother" branch.
MIN_DIGEST_SECTIONS = 1


def estimate_wordpieces(text: str) -> int:
    """Estimate the wordpiece count of *text* using the measured ratio.

    An estimate, deliberately: the real tokenizer lives inside OpenSearch and calling it
    per sentence would couple digest generation to index availability — the same coupling
    the plan rejected for sentence embeddings. The ratio is calibrated on transcript text,
    which is denser in tokens than the digest prose it is applied to, so it over-counts.
    """
    return math.ceil(len(text.split()) * MEASURED_WORDPIECES_PER_WORD)


def fits_embedding_window(text: str, *, header_wordpieces: int = HEADER_WORDPIECE_RESERVE) -> bool:
    """True when *text* plus a header of *header_wordpieces* fits the measured window."""
    return estimate_wordpieces(text) + header_wordpieces <= EMBEDDING_CONTENT_WORDPIECES


def section_count_for(total_words: int) -> int:
    """How many digest sections a transcript of *total_words* source words gets."""
    if total_words <= 0:
        return MIN_DIGEST_SECTIONS
    scaled = math.ceil(total_words / SOURCE_WORDS_PER_SECTION)
    return max(MIN_DIGEST_SECTIONS, min(MAX_DIGEST_SECTIONS, scaled))
