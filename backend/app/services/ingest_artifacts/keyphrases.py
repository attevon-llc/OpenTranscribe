"""Keyphrase extraction — stopword-bounded candidates, degree/frequency scored.

Deterministic, dependency-free (nltk stopwords + the Snowball stemmer already in the
image), and — like everything else in this package — no LLM and no network.

**Deviation from the plan text, recorded deliberately.** #383 Phase 2 specifies
"corpus-relative tf-idf … scored against a document-frequency table from OpenSearch".
That is not built here, for the reason the same plan gives when it rejects reading chunk
vectors back out of the index: Stage 2 is Postgres-only, and coupling artifact generation
to index availability means a file transcribed while OpenSearch is down gets no
keyphrases, which breaks the "100% of transcribed files" gate. A corpus DF table also
makes the artifact a function of *when* it was generated, so two identical files ingested
a month apart get different keyphrases and every before/after measurement inherits that
drift.

What is here instead is RAKE-shaped: split on stopwords and punctuation to get candidate
phrases, score a word by ``degree/frequency`` (its co-occurrence breadth over how often
it appears), and sum over the phrase. It needs no corpus, so it is stable per file. A
corpus-relative *re-ranking* pass remains available to Stage 3, which by then has both a
live index and a reason to want cross-file discrimination — it can reorder these
candidates without regenerating them.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from .textrank import stopwords_for
from .textrank import tokenize

#: Bumped when scoring changes in a way that makes stored keyphrases non-comparable.
KEYPHRASE_SCHEMA_VERSION = 1

KEYPHRASE_ALGORITHM = "rake-degree-ratio"

#: How many phrases to keep. Enough to describe a meeting in a facet list or a no-LLM
#: overview; short enough that the tail (all score-1.0 singletons) is not stored.
MAX_KEYPHRASES = 20

#: Longer candidates are almost always a run of speech the stopword split failed on.
MAX_PHRASE_WORDS = 4

#: A phrase seen once in a two-hour meeting is noise. Single-word phrases must clear a
#: higher bar than multi-word ones, which are self-evidencing by being repeated at all.
MIN_UNIGRAM_COUNT = 3
MIN_PHRASE_COUNT = 2

#: Candidate boundaries: anything that is not a word character or an intra-word apostrophe.
_SPLIT_RE = re.compile(r"[^\w']+", re.UNICODE)


def _candidates(text: str, language: str) -> list[list[str]]:
    """Split *text* into stopword-bounded runs of content words."""
    stops = stopwords_for(language)
    phrases: list[list[str]] = []
    current: list[str] = []
    for raw in _SPLIT_RE.split(text.lower()):
        word = raw.strip("'")
        if not word or word.isdigit() or len(word) < 2 or word in stops:
            if current:
                phrases.append(current)
                current = []
            continue
        current.append(word)
    if current:
        phrases.append(current)
    return [p for p in phrases if len(p) <= MAX_PHRASE_WORDS]


def extract_keyphrases(
    text: str,
    *,
    language: str = "en",
    limit: int = MAX_KEYPHRASES,
) -> dict[str, Any]:
    """Extract keyphrases from *text*.

    Args:
        text: Full transcript text (speaker labels and timestamps not required — the
            stopword split removes them either way).
        language: ISO 639-1 code, selecting the stopword corpus and stemmer.
        limit: Maximum phrases to return.

    Returns:
        The ``file_facts.keyphrases`` JSONB payload: ``{"schema_version", "algorithm",
        "language", "phrases": [{"phrase", "score", "count"}]}``, ordered by score
        descending then phrase ascending — a total order, so two runs over the same
        transcript serialise byte-identically.
    """
    phrases = _candidates(text or "", language)

    frequency: dict[str, int] = defaultdict(int)
    degree: dict[str, int] = defaultdict(int)
    for phrase in phrases:
        span = len(phrase) - 1
        for word in phrase:
            frequency[word] += 1
            degree[word] += span

    # RAKE's word score: degree/frequency. A word appearing only alone scores 1.0; a word
    # that always appears inside longer phrases scores higher, which is what promotes
    # "quarterly revenue forecast" over "revenue".
    word_score = {w: (degree[w] + frequency[w]) / frequency[w] for w in frequency}

    surfaces: dict[str, str] = {}
    counts: dict[str, int] = defaultdict(int)
    scores: dict[str, float] = {}
    for phrase in phrases:
        surface = " ".join(phrase)
        # Stemmed key so "budget review"/"budget reviews" are one phrase; the first
        # surface form encountered wins, which under an ordered input is deterministic.
        stem_key = " ".join(tokenize(surface, language) or phrase)
        counts[stem_key] += 1
        scores[stem_key] = sum(word_score[w] for w in phrase)
        surfaces.setdefault(stem_key, surface)

    ranked: list[dict[str, Any]] = [
        {
            "phrase": surfaces[key],
            "score": round(scores[key] * counts[key], 6),
            "count": counts[key],
        }
        for key in surfaces
        if counts[key] >= (MIN_UNIGRAM_COUNT if " " not in key else MIN_PHRASE_COUNT)
    ]
    # (-score, phrase): score alone leaves ties resolved by dict order, and dict order
    # here descends from set/defaultdict construction — deterministic within a process,
    # but not something to rely on across them.
    ranked.sort(key=lambda item: (-item["score"], item["phrase"]))

    return {
        "schema_version": KEYPHRASE_SCHEMA_VERSION,
        "algorithm": KEYPHRASE_ALGORITHM,
        "language": language,
        "phrases": ranked[:limit],
    }
