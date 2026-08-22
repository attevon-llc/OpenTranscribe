"""Query routing: which retrieval tier(s) should answer this question (#403 Stage 4).

**ROUTE, DON'T FUSE.** The tiers stay separate queries whose results are combined
by the caller. They are never merged into one RRF ranking, because fusing
documents of wildly different length — a 200-word speaker turn against a 55-word
digest section — corrupts the candidate distribution that fusion assumes. This is
a standing decision of the epic, not a local preference.

Four labels, one default:

``lookup``
    Someone's words, in the chunk plane. The default, and the only label that is
    reached without any lexicon matching.
``summarize``
    "What did we cover" — the digest plane leads, the chunk plane still runs.
``aggregate``
    "How many", "which meetings", "who the most" — counted by OpenSearch
    aggregations or Postgres, never by a language model.
``temporal``
    "When did", "since March", "most recent" — the chunk plane with a date hint.

## Why this is rules and not a model

#403 mandates it, and the mandate has teeth: routing by embedding similarity
(``semantic-router`` and friends) needs a client-side encoder on the critical
path of every turn, and its dependency set drags an LLM provider stack into a
product whose no-LLM deployment is first class (**D6**). This module loads
nothing, calls nothing, and runs in microseconds.

The one LLM signal is free by construction: when a rewrite is *already* being
paid for, :mod:`app.services.chat.query_rewriter` asks for an ``INTENT:`` line
alongside the rewrite. Turn 1 has no history, makes no rewrite call, and
therefore still makes **zero** LLM calls before retrieval — which is exactly
where "summarize my meetings this week" lands. There is never a routing-only
call.

## The two invariants that keep a misroute cheap

1. **The chunk tier is never removed.** Every route includes ``chunk``, so a
   query the lexicon misreads still retrieves the same evidence it would have,
   and ``[n]`` markers still resolve to a clickable timestamp. What a misroute
   costs is a *reduced* chunk budget on the summarize and aggregate branches —
   bounded, and measured (see ``docs-site/docs/developer-guide/rag-evaluation.md``).
2. **Structure only ever REMOVES a non-chunk tier; it never changes the label.**
   The lexicon decides intent; the shape of the request decides which tiers may
   serve it. A structural signal that could *promote* a query would let corpus
   size alone reroute an ordinary lookup, which is the one regression **D5**
   forbids outright.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: Intent labels, least to most specific. The ordering is load-bearing: it is
#: what :func:`route` maximises over when the original and the rewritten query
#: disagree.
INTENT_LOOKUP = "lookup"
INTENT_TEMPORAL = "temporal"
INTENT_SUMMARIZE = "summarize"
INTENT_AGGREGATE = "aggregate"
#: W2.5. "What keeps coming up across our meetings" — most specific of the
#: five, and flag-gated end to end (``chat.recurrence_enabled``): with the
#: flag off, :func:`classify` never tests the recurrence lexicon at all, so
#: this label is never produced and every existing route is byte-identical.
INTENT_RECURRENCE = "recurrence"
INTENTS: tuple[str, ...] = (
    INTENT_LOOKUP,
    INTENT_TEMPORAL,
    INTENT_SUMMARIZE,
    INTENT_AGGREGATE,
    INTENT_RECURRENCE,
)

_SPECIFICITY: dict[str, int] = {name: rank for rank, name in enumerate(INTENTS)}

#: Retrieval tiers a route may ask for. ``chunk`` is the transcript-chunk plane,
#: ``digest`` the ``doc_type: digest`` plane, ``aggregate`` the counted path
#: (OpenSearch ``size: 0`` aggregations and Postgres — no ranking at all).
TIER_CHUNK = "chunk"
TIER_DIGEST = "digest"
TIER_AGGREGATE = "aggregate"
#: W2.5. The scope-wide, no-ranking recurrence detector
#: (``aggregation_service.answer_recurrence``) — reads ``summary_data`` and
#: ``file_facts.keyphrases`` across a bounded scope, the same "bounded scope,
#: Postgres, no ranking" shape ``TIER_AGGREGATE`` already uses.
TIER_RECURRENCE = "recurrence"

TIERS_BY_INTENT: dict[str, tuple[str, ...]] = {
    INTENT_LOOKUP: (TIER_CHUNK,),
    INTENT_TEMPORAL: (TIER_CHUNK,),
    INTENT_SUMMARIZE: (TIER_DIGEST, TIER_CHUNK),
    INTENT_AGGREGATE: (TIER_AGGREGATE, TIER_CHUNK),
    INTENT_RECURRENCE: (TIER_RECURRENCE, TIER_CHUNK),
}

#: Nouns that mean "a thing in this corpus". An aggregate head alone is not
#: enough — "how many projects were waiting for approval" is a question about
#: what was *said*, answered from one meeting, and routing it to a count would
#: replace a good answer with a wrong number. Measured: exactly one of QMSum's
#: 1,172 human lookup queries carries an aggregate head, and it is that one.
_CORPUS_NOUN = (
    r"(?:meetings?|recordings?|files?|calls?|sessions?|conversations?|"
    r"transcripts?|documents?|interviews?)"
)

#: Cross-corpus markers that stand in for a corpus noun. "in total" and
#: "across each" assert a span wider than one recording on their own.
_CROSS_CORPUS: tuple[tuple[str, str], ...] = (
    ("in-total", r"\bin total\b"),
    ("across-each", r"\bacross (?:each|all|every)\b"),
    ("corpus-noun", rf"\b{_CORPUS_NOUN}\b"),
)

#: Plural only, and the distinction is semantic rather than grammatical
#: pedantry. "Which **meetings** mention X? List them." enumerates the corpus and
#: is an aggregation. "Which **meeting** recorded 10,000 requests per second?"
#: asks to *identify one recording by something said in it*, which is a lookup
#: that the chunk plane answers with the passage itself. Measured: allowing the
#: singular routed **100** of the synthetic tier's verbatim-control lookups to
#: the aggregate tier — 94% of all lookup leakage in the first run.
_CORPUS_NOUN_PLURAL = (
    r"(?:meetings|recordings|files|calls|sessions|conversations|"
    r"transcripts|documents|interviews)"
)

#: Counting / listing heads that are **ambiguous alone**. "How many X" is a
#: question about the corpus only if something says so; otherwise it is a
#: question about what was said, answered from one meeting. One of these must
#: fire together with a :data:`_CROSS_CORPUS` marker.
_AGGREGATE_HEADS_SCOPED: tuple[tuple[str, str], ...] = (
    ("how-many", r"\bhow many\b"),
    ("how-often", r"\bhow (?:often|frequently)\b"),
    ("list-all", r"\blist (?:all|them|every|each|the)\b"),
    ("count-of", r"\b(?:count|number|total|tally) of\b"),
    ("every-single", r"\b(?:every|all) (?:action items?|decisions?|follow[- ]ups?)\b"),
)

#: Heads that name a corpus-wide facet **by themselves**. "Which speakers
#: discussed the migration?" is a ``terms(speakers)`` aggregation whether or not
#: the sentence also contains the word "meetings", and requiring a second marker
#: would send it to the ranking tier, which cannot enumerate. ``which-plural``
#: is here because the plural corpus noun is inside its own pattern.
#: W2.4. Distinct from ``who-most`` below: "who attended the most" is about
#: SESSIONS (a title-scoped facet tally), "who talked the most" is about TALK
#: TIME (``file_facts.facts['speakers']``) — different mechanisms, different
#: answers, and this repo shipped the bug of answering the second question with
#: the first mechanism. The two patterns are allowed to both fire on the same
#: text ("who talked the most" also satisfies ``who-most``'s generic "the
#: most" shape) — ``aggregation.choose_shape`` gives this one priority, which
#: is also what keeps the flag-off fallback byte-identical to the pre-existing
#: behaviour (see ``aggregation_service.answer_aggregation``).
_AGGREGATE_HEADS_STANDALONE: tuple[tuple[str, str], ...] = (
    ("which-plural", rf"\bwhich {_CORPUS_NOUN_PLURAL}\b"),
    ("which-speakers", r"\bwhich (?:speakers?|people|participants?|attendees?)\b"),
    ("who-most", r"\bwho\b[^?]{0,60}\bthe most\b"),
    (
        "who-talked-most",
        r"\bwho\b[^?]{0,60}\b(?:talked|spoke|speaking)\s+(?:the\s+)?(?:most|longest)\b"
        r"|\bmost\s+talk(?:ing)?\s+time\b",
    ),
)

#: Recording-level objects. The weak summarize markers below only count when the
#: question is about **the recording**, not about a topic inside it.
_DISCOURSE_NOUN = (
    r"(?:meetings?|recordings?|calls?|sessions?|conversations?|transcripts?|"
    r"discussions?|presentations?)"
)

#: Markers that mean "summarize" on their own, wherever they appear.
_SUMMARIZE_STRONG: tuple[tuple[str, str], ...] = (
    ("summarize-verb", r"\b(?:summar(?:ize|ise|y|ies|isation|ization))\b"),
    ("recap", r"\b(?:recap|tl;?dr|synopsis|debrief|catch me up|brief me|rundown)\b"),
    ("key-points", r"\bkey (?:points?|takeaways?|themes?|decisions?)\b"),
    # Fixed multiword artifact terms, STRONG for the same reason "key decisions"
    # is: they name the extracted artifact list itself, not a topic inside the
    # recording, so there is no reading of "what are the action items" that wants
    # ranked excerpts instead of the per-file map. Measured: 6 of 6 AMI
    # action-item questions routed to the chunk tier alone and reached full scope
    # coverage 0 times, because nothing here matched them.
    ("action-items", r"\b(?:action items?|follow[- ]ups?|next steps?)\b"),
)

#: Imperatives that make the whole question a summary request. Anchored at the
#: start, because "describe" mid-sentence ("the metric they used to describe
#: latency") is not a request for a summary.
#:
#: ⚠️ ``describe`` is also the third word QMSum's own surface rule uses to assign
#: its ``summarize`` label, so including it inflates agreement with QMSum on that
#: class. It earns its place independently — an imperative "describe the team's
#: disagreement" over a transcript is a summary request by any reading — but the
#: routing report flags summarize *recall* as not independently measured for
#: exactly this reason. Do not quote that number as evidence.
_SUMMARIZE_LEADING: tuple[tuple[str, str], ...] = (
    ("lead-describe", r"^\s*describe\b"),
    ("lead-walk-through", r"^\s*(?:walk me through|give me (?:a|the) )\b"),
    ("lead-what-happened", r"^\s*what happened (?:in|at|during)\b"),
)

#: Markers that are summarize-shaped only when aimed at a recording. Each of
#: these fired on a real QMSum **lookup** in the first run: "the pragmatic
#: overview of the project", "the main points of Marketing", "what was discussed
#: for improvement of the remote". The noun is the discriminator, not the phrase.
_SUMMARIZE_WEAK: tuple[tuple[str, str], ...] = (
    ("overview", r"\b(?:overview|high[- ]level|at a glance)\b"),
    ("main-topics", r"\bmain (?:topics?|themes?|points?)\b"),
    ("what-covered", r"\bwhat (?:was|were|got) (?:covered|discussed)\b"),
    # Artifact nouns that are summary-shaped ONLY about a recording. WEAK, not
    # STRONG, precisely because each is an ordinary lookup noun on its own:
    # "what problems did the LCD have" and "what decisions did the PM make about
    # the chip" are both lookups and must stay lookups. The discourse noun is the
    # discriminator, exactly as it is for "overview" above.
    #
    # ``decisions`` is here as well as inside ``key-points`` above: that pattern
    # requires the literal word "key", so the far commoner "what decisions were
    # made across the meetings" matched nothing.
    ("decisions", r"\bdecisions?\b"),
    ("problems-concerns", r"\b(?:problems?|concerns?|issues?|blockers?|risks?)\b"),
)

#: ``today``/``yesterday`` are deliberately absent. In a question about a
#: transcript they are far more often reported speech than a filter — "why were
#: the thanks expressed to the House of Commons today" is a lookup — and they
#: were the only temporal false positive in the first run.
#: W2.5. STRONG markers only, deliberately — recurrence changes the retrieval
#: shape entirely (a bounded, no-ranking, cross-file detector rather than a
#: chunk search), so a weak/ambiguous marker misrouting a lookup here is a
#: worse failure than the ``_SUMMARIZE_WEAK`` case, which still gets the
#: chunk tier. Every pattern here names recurrence EXPLICITLY: "keeps coming
#: up", "recurring", "a pattern/trend across meetings" — never a bare
#: "again" or "repeat", which are common enough in ordinary transcript
#: questions ("can you repeat that") to be a false-positive magnet.
_RECURRENCE_STRONG: tuple[tuple[str, str], ...] = (
    ("recurring", r"\brecurr(?:ing|ed|ence|ent)\b"),
    (
        "keeps-coming-up",
        r"\bkeeps? (?:coming up|showing up|being (?:mentioned|brought up|raised))\b",
    ),
    (
        "comes-up-repeatedly",
        r"\bcomes? up (?:repeatedly|again and again|over and over|"
        r"(?:in|across) multiple (?:meetings|recordings|calls|sessions))\b",
    ),
    ("common-theme", r"\b(?:common|recurring) (?:theme|thread|pattern)s?\b"),
    (
        "pattern-across",
        r"\b(?:pattern|trend)s? across (?:meetings|recordings|calls|sessions|conversations)\b",
    ),
    (
        "repeated-items",
        r"\brepeated (?:action items?|topics?|themes?|issues?|decisions?|requests?)\b",
    ),
    ("same-issue-again", r"\bsame (?:issue|topic|problem|question) (?:again|every time)\b"),
)

_TEMPORAL_MARKERS: tuple[tuple[str, str], ...] = (
    ("when-did", r"\bwhen (?:did|was|were|will|has|have)\b"),
    ("since", r"\bsince\b"),
    ("most-recent", r"\b(?:most recent|latest|earliest|first time|last time)\b"),
    ("relative-period", r"\b(?:this|last|past|previous|next) (?:week|month|quarter|year)\b"),
)

_MONTHS: dict[str, int] = {
    name: number
    for number, name in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}
_MONTHS.update(
    {
        abbrev: number
        for number, abbrev in enumerate(
            ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
            start=1,
        )
    }
)

_MONTH_YEAR_RE = re.compile(
    r"\b(?P<month>"
    + "|".join(sorted(_MONTHS, key=len, reverse=True))
    + r")\.?\s+(?P<year>20\d{2})\b",
    re.IGNORECASE,
)
_BARE_YEAR_RE = re.compile(r"\b(?P<year>20\d{2})\b")
_ISO_DATE_RE = re.compile(r"\b(?P<year>20\d{2})-(?P<month>0[1-9]|1[0-2])(?:-\d{2})?\b")

#: A quoted span is a request for literal words. It does not change the label —
#: "summarize what was said about 'project atlas'" is still a summary — but it
#: does mean the derived text of a digest is the wrong place to look for it.
_QUOTED_RE = re.compile(r"[\"“”']([^\"“”']{3,})[\"“”']")

#: Compiled once. Keeping the (name, pattern) pairs is what lets a decision
#: record WHICH signal fired, which is the difference between a router you can
#: debug from a log line and one you have to re-derive.
_COMPILED: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "aggregate_scoped": tuple(
        (n, re.compile(p, re.IGNORECASE)) for n, p in _AGGREGATE_HEADS_SCOPED
    ),
    "aggregate_standalone": tuple(
        (n, re.compile(p, re.IGNORECASE)) for n, p in _AGGREGATE_HEADS_STANDALONE
    ),
    "cross_corpus": tuple((n, re.compile(p, re.IGNORECASE)) for n, p in _CROSS_CORPUS),
    "summarize_strong": tuple((n, re.compile(p, re.IGNORECASE)) for n, p in _SUMMARIZE_STRONG),
    "summarize_leading": tuple((n, re.compile(p, re.IGNORECASE)) for n, p in _SUMMARIZE_LEADING),
    "summarize_weak": tuple((n, re.compile(p, re.IGNORECASE)) for n, p in _SUMMARIZE_WEAK),
    "discourse_noun": ((("discourse-noun", re.compile(rf"\b{_DISCOURSE_NOUN}\b", re.I))),),
    "temporal": tuple((n, re.compile(p, re.IGNORECASE)) for n, p in _TEMPORAL_MARKERS),
    "recurrence": tuple((n, re.compile(p, re.IGNORECASE)) for n, p in _RECURRENCE_STRONG),
}


@dataclass(frozen=True)
class TemporalHint:
    """A date constraint recovered from the question.

    Carried on **every** route, not only the ``temporal`` one: "how many
    meetings in March 2025 discussed X" is an aggregation whose whole point is
    the date filter, and dropping the hint because the label was ``aggregate``
    would answer the unfiltered question with a confident number.
    """

    year: int | None = None
    month: int | None = None
    relative: str | None = None
    matched: str = ""

    @property
    def is_empty(self) -> bool:
        return self.year is None and self.month is None and self.relative is None

    def as_metadata(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "month": self.month,
            "relative": self.relative,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class Route:
    """What the router decided, and everything needed to audit the decision."""

    intent: str = INTENT_LOOKUP
    tiers: tuple[str, ...] = (TIER_CHUNK,)
    signals: tuple[str, ...] = ()
    temporal: TemporalHint | None = None
    #: ``rules`` · ``rules:rewritten`` (the rewritten query was more specific) ·
    #: ``llm`` (the rewrite's INTENT line broke a no-signal default) ·
    #: ``default`` (nothing fired).
    source: str = "default"
    #: The question asked for literal words. The digest tier is dropped, because
    #: a digest is *selected* sentences: a phrase can be absent from it and
    #: present in the transcript, and answering "not mentioned" from a digest is
    #: the silent-wrong-answer shape this epic keeps hitting.
    literal: bool = False
    #: The active speaker scope this turn was asked under, exactly as passed to
    #: :func:`route`. Carried here — not just consumed for
    #: :func:`_apply_structure`'s digest-removal check — so a Postgres-backed
    #: aggregation shape (``aggregation_service._run_speaker_stats``), which
    #: never sees ``service.py``'s original ``speakers`` argument, can still
    #: narrow a per-speaker answer to the one name already in scope with no new
    #: parameter threaded through ``answer_aggregation``.
    speakers: tuple[str, ...] = ()
    #: W2.2. An AXIS, not a fifth intent: whether ``chat.speaker_resolver``
    #: found a UNIQUE speaker mention in the question text paired with a
    #: speaker-verb frame ("what did Dana say about pricing"). Orthogonal to
    #: ``intent``/``tiers`` — it never changes either, and it is not consulted
    #: by :func:`_apply_structure`, which only ever narrows tiers for the
    #: EXPLICIT, hard ``speakers`` scope above. A resolved mention is soft: it
    #: is evidence for a PARALLEL retrieval leg the caller may add, never for
    #: removing or narrowing what the existing tiers already return — so
    #: unlike ``speakers``, this field earns no place in ``_apply_structure``.
    speaker_focus: bool = False

    @property
    def wants_digest(self) -> bool:
        return TIER_DIGEST in self.tiers

    @property
    def wants_recurrence(self) -> bool:
        """W2.5. Whether this turn asked for the cross-meeting recurrence block.

        Derived from ``intent`` alone, exactly like :attr:`wants_digest` — the
        flag gate lives upstream, in :func:`route` (which never produces
        ``INTENT_RECURRENCE`` when ``chat.recurrence_enabled`` is off), so this
        property does not need its own flag check to stay byte-identical.
        """
        return self.intent == INTENT_RECURRENCE

    @property
    def wants_aggregate(self) -> bool:
        return TIER_AGGREGATE in self.tiers

    @property
    def wants_speaker_digest_map(self) -> bool:
        """W2.3. A speaker-scoped summarize turn: the closed routing gap.

        ``_apply_structure`` removes :data:`TIER_DIGEST` whenever an explicit
        speaker filter is active, because the INDEXED digest genuinely cannot
        answer "summarize what Alice said" — a digest carries no single-valued
        speaker field. That removal is correct and stays; what was missing is
        the fallback it should have had: a per-speaker Postgres map
        (``mapreduce.scope_speaker_digest_hits``) that filters digest
        *sentences* by their own per-sentence speaker, which the indexed
        document cannot do but the stored JSONB can. Without this property
        "summarize what Alice said" was structurally impossible — the digest
        tier was gone and nothing replaced it — even though the data to answer
        it exists.

        Derived, not stored: true exactly when a summarize turn's digest tier
        was removed for the SPEAKER reason specifically, not the literal-quote
        one (:attr:`literal`) — a quoted phrase still has no sentence-level
        speaker index to fall back to, so that case is left exactly as it was.
        ``retrieve_digests`` (the ranked leg) stays untouched either way.
        """
        return self.intent == INTENT_SUMMARIZE and bool(self.speakers) and not self.literal

    def as_metadata(self) -> dict[str, Any]:
        """The ``meta.intent`` payload persisted on the assistant message."""
        payload: dict[str, Any] = {
            "intent": self.intent,
            "tiers": list(self.tiers),
            "signals": list(self.signals),
            "source": self.source,
        }
        if self.literal:
            payload["literal"] = True
        if self.temporal is not None and not self.temporal.is_empty:
            payload["temporal"] = self.temporal.as_metadata()
        if self.speaker_focus:
            payload["speaker_focus"] = True
        if self.wants_speaker_digest_map:
            payload["speaker_digest_map"] = True
        return payload


def _fired(text: str, group: str) -> tuple[str, ...]:
    """Names of the patterns in ``group`` that match ``text``, in declared order."""
    return tuple(name for name, pattern in _COMPILED[group] if pattern.search(text))


def extract_temporal(text: str) -> TemporalHint | None:
    """Recover a date constraint, or ``None`` when the question carries none.

    Absolute dates win over relative ones: "meetings in March 2025" is exact and
    "recent meetings" is not, and a question containing both means the exact one.

    Args:
        text: The question, original or rewritten.

    Returns:
        A :class:`TemporalHint`, or ``None`` if nothing time-shaped is present.
    """
    iso = _ISO_DATE_RE.search(text)
    if iso:
        return TemporalHint(
            year=int(iso.group("year")), month=int(iso.group("month")), matched=iso.group(0)
        )

    found = _MONTH_YEAR_RE.search(text)
    if found:
        return TemporalHint(
            year=int(found.group("year")),
            month=_MONTHS[found.group("month").lower()],
            matched=found.group(0),
        )

    relative = _fired(text, "temporal")
    year = _BARE_YEAR_RE.search(text)
    if year:
        return TemporalHint(
            year=int(year.group("year")),
            relative=relative[0] if relative else None,
            matched=year.group(0),
        )
    if relative:
        return TemporalHint(relative=relative[0], matched=relative[0])
    return None


def classify(text: str, *, recurrence_enabled: bool = False) -> tuple[str, tuple[str, ...]]:
    """Label one query string from the lexicon alone.

    Precedence is ``recurrence`` > ``aggregate`` > ``summarize`` > ``temporal`` >
    ``lookup``, and it is not arbitrary. Recurrence outranks aggregate because
    its markers are the most specific in the lexicon (see
    ``_RECURRENCE_STRONG``'s docstring) and a recurrence question phrased as a
    count ("how many times has budget come up across our meetings") is still
    fundamentally about the cross-file pattern, not a single number. An
    aggregation with a date ("how many meetings in March discussed X") is an
    aggregation whose filter happens to be a date — the hint rides along on
    :class:`Route` — whereas labelling it ``temporal`` would send a counting
    question to a ranking tier that cannot count. Summarize outranks temporal
    for the same reason: "recap last month's calls" is a summary over a date
    range, not a date question.

    Args:
        text: The question.
        recurrence_enabled: ``chat.recurrence_enabled``. When ``False`` — the
            default — the recurrence lexicon is never tested at all, so this
            function cannot return ``INTENT_RECURRENCE`` and every other
            precedence decision is exactly what it was before this label
            existed. This is what makes the flag gate the PATTERNS, not just
            the shape built from them.

    Returns:
        ``(intent, signals)`` — the label and the pattern names that produced it.
        ``(lookup, ())`` when nothing fires.
    """
    clean = " ".join(str(text or "").split())
    if not clean:
        return INTENT_LOOKUP, ()

    if recurrence_enabled:
        recurrence = _fired(clean, "recurrence")
        if recurrence:
            return INTENT_RECURRENCE, recurrence

    standalone = _fired(clean, "aggregate_standalone")
    if standalone:
        return INTENT_AGGREGATE, standalone + _fired(clean, "cross_corpus")
    scoped = _fired(clean, "aggregate_scoped")
    if scoped:
        cross = _fired(clean, "cross_corpus")
        if cross:
            return INTENT_AGGREGATE, scoped + cross

    summarize = _fired(clean, "summarize_strong") + _fired(clean, "summarize_leading")
    weak = _fired(clean, "summarize_weak")
    if weak and _fired(clean, "discourse_noun"):
        summarize += weak
    if summarize:
        return INTENT_SUMMARIZE, summarize

    temporal = _fired(clean, "temporal")
    if temporal:
        return INTENT_TEMPORAL, temporal

    return INTENT_LOOKUP, ()


def parse_intent_line(raw: str) -> str | None:
    """Read an ``INTENT: <label>`` line out of a rewrite response.

    The rewriter's contract is "the rewritten query on line one"; the intent is
    an optional second line. Anything unrecognised returns ``None`` and the
    rules stand — a model that ignores the instruction must cost nothing.

    Args:
        raw: The rewrite response's full text.

    Returns:
        A member of :data:`INTENTS`, or ``None``.
    """
    for line in (raw or "").splitlines()[1:4]:
        stripped = line.strip()
        if not stripped.upper().startswith("INTENT:"):
            continue
        label = stripped.split(":", 1)[1].strip().lower().strip(".\"'")
        if label in _SPECIFICITY:
            return label
    return None


def _apply_structure(
    intent: str,
    tiers: tuple[str, ...],
    *,
    literal: bool,
    has_speaker_filter: bool,
) -> tuple[str, ...]:
    """Narrow the tiers a label asks for. Never widens, never touches the label.

    Two removals, each closing a specific hole:

    * **A speaker filter removes the digest tier.** A digest document carries no
      single-valued ``speaker`` field at all — deliberately, since a digest is
      not attributable to one person — so it cannot honour a speaker scope. Its
      indexed ``speakers`` array is also stale until the next reindex after a
      rename (the rename propagation task rewrites the chunk plane only), so
      even filtering on that would silently drop the renamed speaker's material.
    * **A quoted phrase removes the digest tier.** See :attr:`Route.literal`.

    The chunk tier is never removed, which is what bounds the cost of a misroute.
    """
    narrowed = list(tiers)
    if TIER_DIGEST in narrowed and (literal or has_speaker_filter):
        narrowed.remove(TIER_DIGEST)
    if TIER_CHUNK not in narrowed:
        narrowed.append(TIER_CHUNK)
    return tuple(narrowed)


def route(
    question: str,
    *,
    rewritten: str | None = None,
    llm_intent: str | None = None,
    speakers: list[str] | None = None,
    speaker_focus: bool = False,
    recurrence_enabled: bool = False,
) -> Route:
    """Decide the tiers for one turn.

    The rules run over the original **and** the rewritten query and the more
    specific label wins. Rewriting resolves pronouns, and in doing so it can
    lose the verb that carried the intent — "summarize that" becomes "the Q3
    revenue discussion in the Atlas kickoff", which reads as a lookup. Taking
    the maximum keeps whichever form still carries the signal.

    Args:
        question: The user's message, as typed.
        rewritten: The standalone rewrite, when one was produced. ``None`` (or a
            value equal to *question*) means only the original is classified.
        llm_intent: The label from the rewrite's ``INTENT:`` line, if any. It is
            consulted **only when the rules found nothing** — the rules are
            deterministic evidence and a model's guess must not override them,
            but it is a reasonable tiebreak for a query with no signal at all.
        speakers: Active speaker scope, which narrows the tiers (see
            :func:`_apply_structure`).
        speaker_focus: W2.2. Whether ``chat.speaker_resolver`` found a unique
            mention plus a speaker-verb frame. Carried onto :attr:`Route.speaker_focus`
            unchanged — this function does not derive it, only records it,
            since resolving a mention needs the roster (Postgres) and this
            module loads nothing and calls nothing.
        recurrence_enabled: W2.5. ``chat.recurrence_enabled``. Threaded into
            every :func:`classify` call AND into the LLM-tiebreak branch below
            — both are how a question could reach ``INTENT_RECURRENCE``, and
            flag-off must close both, not just the lexicon, or a rewrite's
            free-text ``INTENT: recurrence`` line could produce the label with
            the flag off and the "gates the patterns AND the shape" contract
            would be false for that one path.

    Returns:
        A :class:`Route`. Always includes :data:`TIER_CHUNK`.
    """
    original = " ".join(str(question or "").split())
    intent, signals = classify(original, recurrence_enabled=recurrence_enabled)
    source = "rules" if signals else "default"

    if rewritten and rewritten.strip() and rewritten.strip() != original:
        alt_intent, alt_signals = classify(rewritten, recurrence_enabled=recurrence_enabled)
        if _SPECIFICITY[alt_intent] > _SPECIFICITY[intent]:
            intent, signals, source = alt_intent, alt_signals, "rules:rewritten"

    if (
        not signals
        and llm_intent in _SPECIFICITY
        and llm_intent != INTENT_LOOKUP
        and (llm_intent != INTENT_RECURRENCE or recurrence_enabled)
    ):
        intent, signals, source = llm_intent, ("llm-intent-line",), "llm"

    temporal = extract_temporal(original)
    if temporal is None and rewritten:
        temporal = extract_temporal(rewritten)

    literal = bool(_QUOTED_RE.search(original))
    tiers = _apply_structure(
        intent,
        TIERS_BY_INTENT[intent],
        literal=literal,
        has_speaker_filter=bool(speakers),
    )
    return Route(
        intent=intent,
        tiers=tiers,
        signals=signals,
        temporal=temporal,
        source=source,
        literal=literal,
        speakers=tuple(speakers) if speakers else (),
        speaker_focus=speaker_focus,
    )
