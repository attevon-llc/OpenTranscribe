"""Counted answers: the aggregation tier (#403 Stage 4, Phase 5).

"How many meetings discussed the migration" is not a retrieval question. Ranking
the top 12 chunks and asking a model to count them produces a confident number
that is wrong whenever the answer exceeds the excerpt budget — which, on a
corpus, is most of the time. So this tier does not rank at all: it counts, with
**OpenSearch aggregations and Postgres**, and hands the model a table.

Four bounded shapes, and a worst case written down rather than discovered:
**one** ``size: 0`` search plus at most **one** Postgres query, whatever the
scope. Never N searches, never a per-file LLM call.

| shape | mechanism |
|---|---|
| ``count_files`` | ``size: 0`` phrase filter + ``terms(file_uuid)`` |
| ``list_files`` | the same body; the bucket keys are the answer |
| ``speaker_facet`` | the same body + ``terms(speakers)`` × ``terms(file_uuid)`` |
| ``count_events`` | **Postgres** ``regexp_count`` over ``transcript_segment.text`` |

## Three constraints that are not negotiable

1. **No ``search_pipeline`` and no ``hybrid`` clause on an aggregation body.**
   OpenSearch 3.4 throws ``ArrayIndexOutOfBoundsException`` inside
   ``score-ranker-processor`` when a cardinality agg meets hybrid + collapse +
   RRF. Plain BM25 is also the correct semantics for "mentions X" and is
   deterministic, so this costs nothing.
2. **A truncated bucket list is refused, never reported.** An aggregation that
   silently dropped a shard's tail is a wrong answer that looks like a right one.
3. **Occurrences are counted in Postgres, not OpenSearch.** The chunker overlaps
   a long turn's tail into the next chunk, so counting *occurrences* over chunk
   documents double-counts. Segments are the unsplit turns the chunker was built
   from. File *coverage* is fine over chunks: a file is counted once either way.

## Why the phrase is extracted this way

The subject is recovered by stripping the interrogative frame — everything up to
and including the verb that links the question to its object ("how many meetings
**discussed** …") — and taking the contiguous tail of the user's own words. That
is general English question shape, not a catalogue of the shapes any particular
corpus generates.

That distinction is load-bearing for the evaluation. The harness ships its own
``ReferenceAnswerer`` whose regexes are matched to the eval generator's exact
question frames; it is the *instrument's* control, and if this module imported
those regexes the measurement would be scoring the harness against itself. It
does not, and it is expected to score below the reference for that reason. The
gap is the finding.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.services.chat.router import Route
from app.services.chat.router import TemporalHint
from app.services.ingest_artifacts.index_mapping import chunk_plane_clause

logger = logging.getLogger(__name__)

SHAPE_COUNT_FILES = "count_files"
SHAPE_LIST_FILES = "list_files"
SHAPE_SPEAKER_FACET = "speaker_facet"
SHAPE_COUNT_EVENTS = "count_events"
SHAPES = (SHAPE_COUNT_FILES, SHAPE_LIST_FILES, SHAPE_SPEAKER_FACET, SHAPE_COUNT_EVENTS)

#: Bucket ceiling. Deliberately above :data:`app.core.constants.CHAT_MAX_SCOPE_FILES`
#: (500) so a full-scope aggregation can never be truncated by this number — and
#: :func:`_buckets` refuses rather than reporting a head-of-list if it ever is.
MAX_BUCKETS = 10_000

#: Verbs that link an interrogative frame to its object. The subject phrase is
#: whatever follows the LAST one. Ordered longest-first inside the alternation so
#: "talked about" wins over "talked".
_LINK_VERBS = (
    r"talked about|talk about|talking about|spoke about|speak about|"
    r"said about|say about|says about|"
    r"discussed|discuss|discusses|discussing|"
    r"mentioned|mention|mentions|mentioning|"
    r"covered|cover|covers|covering|"
    r"referenced|reference|references|"
    r"raised|raise|raises|"
    r"deferred|defer|defers|deferring|"
    r"involved|involve|involves|"
    r"about|regarding|concerning"
)
_LINK_RE = re.compile(rf"\b(?:{_LINK_VERBS})\b", re.IGNORECASE)

#: Politeness and list-imperative tails. "List them." is an instruction to the
#: assistant, never part of what is being counted.
_TRAILING_RE = re.compile(
    r"(?:[\s,.]*(?:please|thanks?|list them(?:\s+all)?|list all of them))+[\s.?!]*$",
    re.IGNORECASE,
)

#: Frame words dropped when no link verb is found and the remainder has to be
#: used as loose content words (the ``speaker_facet`` path).
_FRAME_WORDS = frozenset(
    [
        "who",
        "whom",
        "whose",
        "which",
        "what",
        "when",
        "where",
        "why",
        "how",
        "many",
        "much",
        "often",
        "frequently",
        "the",
        "a",
        "an",
        "of",
        "for",
        "in",
        "on",
        "at",
        "to",
        "and",
        "or",
        "is",
        "was",
        "were",
        "are",
        "did",
        "do",
        "does",
        "had",
        "has",
        "have",
        "we",
        "i",
        "you",
        "they",
        "he",
        "she",
        "it",
        "us",
        "them",
        "our",
        "their",
        "most",
        "attended",
        "attend",
        "attends",
        "speaker",
        "speakers",
        "people",
        "participants",
        "attendees",
        "session",
        "sessions",
        "meeting",
        "meetings",
        "recording",
        "recordings",
        "call",
        "calls",
        "conversation",
        "conversations",
        "transcript",
        "transcripts",
        "total",
        "times",
        "time",
        "list",
        "all",
        "every",
        "each",
    ]
)

_WORD_RE = re.compile(r"[\w'-]+")


@dataclass(frozen=True)
class AggregationResult:
    """A counted answer, with the evidence and the honest coverage beside it."""

    shape: str
    mechanism: str
    subject: str
    count: int | None = None
    file_uuids: tuple[str, ...] = ()
    #: Titles parallel to :attr:`file_uuids`. A uuid is not an answer to "which
    #: meetings"; an empty string is an unnamed recording, never a guess.
    file_titles: tuple[str, ...] = ()
    speaker: str | None = None
    speaker_sessions: int | None = None
    #: Facet buckets as ``(key, n)``, already sorted. Rendered into the table.
    rows: tuple[tuple[str, int], ...] = ()
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """Diagnostics for ``msg_metadata`` — counts and ids, never content."""
        return {
            "shape": self.shape,
            "mechanism": self.mechanism,
            "count": self.count,
            "files": len(self.file_uuids),
            "buckets": len(self.rows),
            "coverage": dict(self.coverage),
        }


def _content_words(text: str) -> list[str]:
    """Words that carry the question's subject, frame vocabulary removed."""
    return [
        word
        for word in _WORD_RE.findall(text.lower())
        if word not in _FRAME_WORDS and len(word) > 1
    ]


def extract_subject(question: str, temporal: TemporalHint | None = None) -> str:
    """Recover what the question is *about*, as a contiguous span of its own words.

    Strips the trailing imperative, then everything up to and including the last
    linking verb, then a leading date phrase the router already captured (the
    date is a filter, not part of the phrase being counted).

    Args:
        question: The user's question.
        temporal: The hint the router recovered, whose matched text is removed
            from the phrase so "in March 2025" is not searched for literally.

    Returns:
        The subject phrase, or ``""`` when no linking verb was found — in which
        case the caller falls back to loose content words.
    """
    text = _TRAILING_RE.sub("", " ".join(str(question or "").split())).rstrip("?!. ")
    matches = list(_LINK_RE.finditer(text))
    if not matches:
        return ""
    tail = text[matches[-1].end() :].strip(" ,.;:")
    if temporal is not None and temporal.matched:
        tail = re.sub(
            rf"\b(?:in|during|from|since)?\s*{re.escape(temporal.matched)}\b",
            " ",
            tail,
            flags=re.IGNORECASE,
        ).strip()
    return " ".join(tail.split())


def choose_shape(route: Route) -> str | None:
    """Pick the aggregation shape from the signals the router already recorded.

    Reading the router's signals rather than re-matching the question is what
    keeps one lexicon in the codebase. ``None`` means "no shape applies" and the
    turn answers from the chunk leg alone — declining is always better than
    guessing a mechanism, because every mechanism here returns a *number* and a
    number from the wrong one is indistinguishable from a right one.
    """
    signals = set(route.signals)
    if signals & {"which-speakers", "who-most"}:
        return SHAPE_SPEAKER_FACET
    if signals & {"which-plural", "list-all", "every-single"}:
        return SHAPE_LIST_FILES
    if "in-total" in signals or "how-often" in signals:
        return SHAPE_COUNT_EVENTS
    if signals & {"how-many", "count-of"}:
        return SHAPE_COUNT_FILES
    return None


def buckets(container: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """Bucket list for ``name``, refusing a truncated aggregation."""
    holder = container.get("aggregations")
    if not isinstance(holder, dict):
        holder = container
    agg = holder.get(name) or {}
    if int(agg.get("sum_other_doc_count") or 0) > 0:
        raise RuntimeError(
            f"Aggregation {name!r} truncated at {MAX_BUCKETS} buckets — the answer would "
            "be wrong in a way that looks right."
        )
    return list(agg.get("buckets") or [])


def base_filters(
    *,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
) -> list[dict[str, Any]]:
    """The same access gate every other read applies, and nothing else.

    ``file_uuids is None`` means every accessible file; an empty **list** means
    match nothing. Inverting those leaks the whole library, so the empty list is
    passed through as a ``terms`` on no values rather than skipped.
    """
    from app.services.search.tenant_scope import org_filter_clauses

    filters: list[dict[str, Any]] = [{"terms": {"accessible_user_ids": [user_id]}}]
    filters.extend(org_filter_clauses(organization_id))
    filters.append(chunk_plane_clause())
    if file_uuids is not None:
        filters.append({"terms": {"file_uuid": list(file_uuids)}})
    return filters


def subject_clause(subject: str, question: str, fields: tuple[str, ...]) -> dict[str, Any] | None:
    """The "mentions X" predicate: a phrase when we have one, else content words.

    A recovered subject is a contiguous span of the user's words, so
    ``match_phrase`` is exactly right and is deterministic. Without one, an
    all-terms ``match`` is the honest fallback — looser, and the coverage block
    records which was used so a number is never read without knowing.
    """
    if subject:
        return {
            "bool": {
                "should": [{"match_phrase": {name: subject}} for name in fields],
                "minimum_should_match": 1,
            }
        }
    words = _content_words(question)
    if not words:
        return None
    return {
        "bool": {
            "should": [
                {"match": {name: {"query": " ".join(words), "minimum_should_match": "100%"}}}
                for name in fields
            ],
            "minimum_should_match": 1,
        }
    }
