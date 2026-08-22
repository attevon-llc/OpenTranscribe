"""Cross-meeting recurrence detection — "what keeps coming up across our meetings" (W2.5).

Pure logic only. Every function here takes plain data and returns plain data —
no database session, no OpenSearch client, no LLM call. The I/O half (reading
``media_file.summary_data``/``file_facts.keyphrases`` for a bounded scope,
resolving the redaction policy, and masking each item **per file**) lives in
``aggregation_service.py``; this module never sees raw index or transcript
content, only the text its caller hands it.

## Sources, and why keyphrases are always included

Two kinds of item feed the detector:

1. **Fingerprint-fresh ``summary_data`` items** — ``action_items``,
   ``key_decisions``, ``follow_up_items`` — when an LLM summary exists and is
   current (same freshness test ``mapreduce._summary_is_fresh`` already uses:
   a stale or absent summary contributes nothing).
2. **``file_facts.keyphrases``**, unconditionally. This is the #403 **D6**
   degraded mode: keyphrases are generated with no LLM at all
   (``ingest_artifacts.keyphrases``), so recurrence detection still works on a
   deployment with ``LLM_PROVIDER`` empty — the same first-class-not-fallback
   posture ``mapreduce.CodeComposer`` documents for the overview tier.

## The shape-tolerant normalizer (Task 1)

The **default** summary prompt (``core/default_prompts.py``, the
``action_items`` block) emits
``{item, owner, due_date, priority, context, mentioned_timestamp}``.
``schemas/summary.py``'s ``ActionItem`` model declares a **different** shape —
``{text, assigned_to, due_date, priority, context, status}`` — and that model
is exported but **dead**: nothing validates or renders it (``SummaryData`` is
``extra="allow"`` and accepts either shape, or neither). So the normalizer
tries, in order, ``item`` → ``text`` → ``description`` for the text and
``owner`` → ``assigned_to`` for who it is assigned to, and separately accepts
a **bare string** (``follow_up_items`` genuinely are strings; a custom prompt's
``key_decisions`` entries are dicts keyed ``decision``). A shape this
normalizer does not recognise — a fully custom prompt's own field names, which
``SummaryData``'s ``extra="allow"`` legitimizes — yields an EMPTY token set
rather than a guess, and an empty token set is never grouped with anything
(see :func:`token_set` and :func:`build_inverted_index`).

**"Open vs completed" is a disclosed heuristic, not a fact.** The default
``action_items`` shape carries no ``status`` field at all — only
``schemas/summary.py``'s dead ``ActionItem`` model has one, and nothing
produces it. Nothing in this module infers or reports a completion state; a
caller rendering these groups must say the corpus does not track it, not imply
that "recurring" means "still open".

## Grouping: an inverted index, never all pairs

500 files × ~20 items/file is ~10,000 items; comparing every pair is ~5×10⁷
Jaccard computations for one chat turn, so this builds a token → item-index
posting list and only ever compares items that already share at least one
token (see :func:`candidate_pairs`). Deterministic throughout: item order is
preserved from the caller, ties break on ``(file_uuid, item index)``, and the
union-find root is always the lower index.

## Language: declines for no-space scripts, and Arabic is NOT one of them

Token-set Jaccard needs *word* boundaries. ``ingest_artifacts.textrank.tokenize``
already gives Arabic a real Snowball stemmer and a real NLTK stopword list
(``_SNOWBALL_LANG_MAP``/``_STOPWORD_LANG_MAP`` both carry ``"ar"``), and Arabic
script is space-delimited, so recurrence detection over Arabic text works
exactly like any other supported language. Chinese, Japanese and Korean are
not space-delimited at all: with no separators, ``textrank._TOKEN_RE`` matches
a whole contiguous run of CJK characters as ONE token — the exact failure mode
``services/chat/CLAUDE.md`` records for punkt sentence splitting on Chinese
transcripts ("a whole transcript became one chunk"). Rather than silently
producing single-token, never-matching item sets, :data:`NO_SPACE_LANGUAGES`
is checked up front and those items are excluded with disclosure
(:attr:`RecurrenceResult.declined_languages`), never fed through the tokenizer
at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from app.services.ingest_artifacts.textrank import tokenize

logger = logging.getLogger(__name__)

#: Label for this shape, used the same way ``aggregation.SHAPE_*`` constants
#: are — as a metadata/diagnostics tag, not as a member of
#: ``aggregation.SHAPES``. Recurrence is not a *counted* answer (it produces no
#: single number `aggregation.AggregationResult` could carry); it is an
#: evidence block alongside ``<overview>``, so it does not join the five
#: numeric aggregation shapes' machinery in ``aggregation.py``/
#: ``aggregation_service.answer_aggregation``. See ``recurrence_service`` in
#: ``aggregation_service.py`` for where this label is actually used.
SHAPE_RECURRENCE = "recurrence"

#: Leaf kinds this module knows how to normalize. ``keyphrase`` is the D6
#: no-LLM source; the other three come from a fresh ``summary_data``.
LEAF_ACTION_ITEM = "action_items"
LEAF_KEY_DECISION = "key_decisions"
LEAF_FOLLOW_UP = "follow_up_items"
LEAF_KEYPHRASE = "keyphrase"

#: dict keys tried, in order, per leaf kind. ``key_decisions`` tries
#: ``decision`` first (the default prompt's own key) then falls back to the
#: action-item spellings, because a custom prompt may reuse them.
_TEXT_KEYS: dict[str, tuple[str, ...]] = {
    LEAF_ACTION_ITEM: ("item", "text", "description"),
    LEAF_KEY_DECISION: ("decision", "item", "text", "description"),
    LEAF_FOLLOW_UP: ("item", "text", "description"),
    LEAF_KEYPHRASE: ("phrase",),
}
_OWNER_KEYS: tuple[str, ...] = ("owner", "assigned_to")

#: Languages with no inter-word whitespace. Excluded from grouping, with
#: disclosure — see the module docstring's "Language" section. Deliberately a
#: short, explicit, hand-picked set (matching the spirit of
#: ``search/chunking_service._NO_SPACE_SCRIPT``, not imported from it: that
#: module solves a different problem — per-character word counting in mixed
#: text — and pulling it in here would couple a pure, dependency-free module
#: to the chunker's heavier regex machinery for two constants' worth of value).
#: Thai/Lao/Khmer/Burmese are included for the same reason even though no
#: current summary language config produces them (``LLM_OUTPUT_LANGUAGES``
#: does not) — ``file_facts.keyphrases`` carries the TRANSCRIPT's language,
#: which can be anything Whisper transcribes.
NO_SPACE_LANGUAGES: frozenset[str] = frozenset({"zh", "ja", "ko", "th", "lo", "km", "my"})

#: Groups below this Jaccard threshold on their token sets are not merged.
DEFAULT_SIMILARITY_THRESHOLD = 0.5

#: Bounded scope only (matches the aggregation tier's posture) — an unbounded
#: "summarize my whole library" recurrence question is refused, never
#: truncated silently, by whichever caller resolves the scope. This cap is a
#: second, cheaper backstop against a scope that resolved to something huge:
#: rather than decline outright, a caller may choose to run recurrence over
#: the first ``item_cap`` items and disclose the truncation
#: (:attr:`RecurrenceResult.truncated`).
DEFAULT_ITEM_CAP = 1500

#: A token appearing in more items than this is treated as too common to be
#: discriminative for candidate generation — the recurrence-detection analog
#: of a stopword. Without this cap, one very common content word (a client
#: name mentioned in every meeting) would make that token's posting list
#: O(n), and comparing every pair sharing it would reintroduce the O(n^2)
#: blowup the inverted index exists to avoid. Skipped tokens still count
#: toward Jaccard for any pair that DOES get compared via a rarer shared
#: token; this only bounds how many pairs get GENERATED as candidates.
MAX_POSTING_LIST = 200


def normalize_leaf(raw: Any, leaf: str) -> tuple[str, str | None] | None:
    """Extract ``(text, owner)`` from one summary-data leaf item, or decline.

    Args:
        raw: One entry from ``action_items``/``key_decisions``/
            ``follow_up_items`` (dict or string) or one
            ``keyphrases["phrases"]`` entry (dict with a ``phrase`` key).
        leaf: One of :data:`LEAF_ACTION_ITEM`, :data:`LEAF_KEY_DECISION`,
            :data:`LEAF_FOLLOW_UP`, :data:`LEAF_KEYPHRASE`.

    Returns:
        ``(text, owner)`` — ``owner`` is ``None`` when the leaf carries none
        (every leaf but action items) or none was recognised. ``None`` when no
        usable text could be extracted — a bare string with no content, an
        unrecognised dict shape, or a value of the wrong type entirely. This
        is not an error: a fully custom prompt's own field names legitimately
        produce no match, and the caller (:func:`detect_recurring_items`) must
        simply not group what was never extracted.
    """
    if isinstance(raw, str):
        text = raw.strip()
        return (text, None) if text else None
    if not isinstance(raw, dict):
        return None

    text = ""
    for key in _TEXT_KEYS.get(leaf, ()):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            break
    if not text:
        return None

    owner: str | None = None
    for key in _OWNER_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            owner = value.strip()
            break
    return text, owner


def token_set(text: str, language: str | None) -> frozenset[str]:
    """Stemmed, stopword-filtered token set for one item's (already-masked) text.

    Returns an empty set — never raises — for a declined language
    (:data:`NO_SPACE_LANGUAGES`) or empty text; both mean "this item cannot
    participate in grouping" and :func:`build_inverted_index` already treats
    an empty token set as ungroupable.
    """
    lang = (language or "en").strip().lower() or "en"
    if not text or lang in NO_SPACE_LANGUAGES:
        return frozenset()
    return frozenset(tokenize(text, lang, stem=True))


@dataclass(frozen=True)
class SourceItem:
    """One already-masked item, ready for grouping.

    Constructed by the caller (``aggregation_service.py``) AFTER
    :func:`normalize_leaf` extracted the text/owner and the I/O-side masker
    applied the redaction policy — this module never sees raw content.
    """

    file_uuid: str
    text: str
    leaf: str
    language: str | None = None
    owner: str | None = None


@dataclass(frozen=True)
class _Candidate:
    """A :class:`SourceItem` plus its precomputed token set, for internal use."""

    item: SourceItem
    tokens: frozenset[str]


@dataclass(frozen=True)
class RecurrenceGroup:
    """One cluster of items judged to be the same thing, recurring across files."""

    #: Deterministic pick: the item at the lowest ``(file_uuid, member index)``
    #: in the group — never "whichever the graph walk visited first", which
    #: would depend on dict/set iteration order.
    representative_text: str
    member_count: int
    file_uuids: tuple[str, ...]
    owners: tuple[str, ...]
    #: The single leaf kind if every member shares one, else ``"mixed"`` — an
    #: action item and a keyphrase can legitimately land in the same group
    #: (the words recur even though the source differs), and collapsing that
    #: to one leaf's label would misdescribe half the group's evidence.
    leaf: str

    def as_metadata(self) -> dict[str, Any]:
        return {
            "member_count": self.member_count,
            "files": len(self.file_uuids),
            "leaf": self.leaf,
        }


@dataclass(frozen=True)
class RecurrenceResult:
    """The detector's full output, with the disclosures a caller must render."""

    groups: tuple[RecurrenceGroup, ...] = ()
    #: True when more than :data:`DEFAULT_ITEM_CAP` (or the caller's own cap)
    #: items were offered and the excess was dropped before grouping.
    truncated: bool = False
    #: Items actually considered (after the cap, before language exclusion).
    considered: int = 0
    #: Items excluded because their language is in :data:`NO_SPACE_LANGUAGES`.
    declined_for_language: int = 0
    #: The distinct declined language codes, sorted — for the honesty note's
    #: wording ("zh, ja excluded"), not just a count.
    declined_languages: tuple[str, ...] = ()
    #: Candidate PAIRS actually Jaccard-compared — the number that proves this
    #: is not all-pairs. Exposed so a test can assert it directly rather than
    #: inferring it from timing.
    comparisons: int = 0
    #: I/O-side diagnostics this PURE module cannot compute itself — set by
    #: the caller (``aggregation_service.answer_recurrence``) via
    #: ``dataclasses.replace`` after :func:`detect_recurring_items` returns.
    #: Keys used today: ``masking_failed_files`` (a file dropped whole because
    #: its policy could not be resolved/applied — issue #402's fail-closed
    #: contract) and ``files_without_summary_or_keyphrases`` (a file in scope
    #: that contributed nothing at all). Never contains item content.
    coverage: dict[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """Diagnostics for ``msg_metadata`` — counts only, never item content."""
        return {
            "shape": SHAPE_RECURRENCE,
            "groups": len(self.groups),
            "truncated": self.truncated,
            "considered": self.considered,
            "declined_for_language": self.declined_for_language,
            "declined_languages": list(self.declined_languages),
            "comparisons": self.comparisons,
            **self.coverage,
        }


def build_inverted_index(candidates: list[_Candidate]) -> dict[str, list[int]]:
    """``token -> sorted [item indices]`` posting list, skipping empty token sets.

    An item with an empty token set (declined language, or text that reduced
    to nothing after stopword removal) contributes no postings and therefore
    can never become a candidate pair — "an empty token set is never
    grouped", literally: it has no entry to be looked up by.
    """
    index: dict[str, list[int]] = {}
    for i, candidate in enumerate(candidates):
        for token in candidate.tokens:
            index.setdefault(token, []).append(i)
    return index


def candidate_pairs(index: dict[str, list[int]]) -> set[tuple[int, int]]:
    """Item-index pairs sharing at least one token — candidate generation.

    **Not all-pairs.** Only pairs that share a token are ever considered, and
    :data:`MAX_POSTING_LIST` bounds the worst case further: a token common
    enough to appear in more than that many items is skipped entirely, on the
    same reasoning a stopword is skipped for TF-IDF — ubiquity makes it
    non-discriminative, and without the cap its posting list alone would cost
    ``O(k^2)`` pairs for ``k`` items sharing it.

    Args:
        index: Output of :func:`build_inverted_index`.

    Returns:
        A set of ``(i, j)`` with ``i < j`` — deduplicated, since two items can
        share more than one token.
    """
    pairs: set[tuple[int, int]] = set()
    for postings in index.values():
        if len(postings) < 2 or len(postings) > MAX_POSTING_LIST:
            continue
        ordered = sorted(postings)
        for a in range(len(ordered)):
            for b in range(a + 1, len(ordered)):
                pairs.add((ordered[a], ordered[b]))
    return pairs


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity of two token sets. ``0.0`` when both are empty."""
    if not a or not b:
        return 0.0
    union = len(a | b)
    if union == 0:
        return 0.0
    return len(a & b) / union


class _UnionFind:
    """Deterministic disjoint-set: the root of a merged pair is always the LOWER index."""

    def __init__(self, n: int) -> None:
        self._parent = list(range(n))

    def find(self, x: int) -> int:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]
            x = self._parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        # Lower index wins as root — deterministic regardless of pair order,
        # which the candidate set does not guarantee (it comes from a dict of
        # posting lists, iterated in insertion order).
        if ra < rb:
            self._parent[rb] = ra
        else:
            self._parent[ra] = rb


def _group_leaf(items: list[SourceItem]) -> str:
    leaves = {item.leaf for item in items}
    return leaves.pop() if len(leaves) == 1 else "mixed"


def detect_recurring_items(
    items: list[SourceItem],
    *,
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    item_cap: int = DEFAULT_ITEM_CAP,
) -> RecurrenceResult:
    """Group items recurring across at least two distinct files.

    Args:
        items: Already-masked source items, one per (file, extracted leaf
            entry). Order is whatever the caller gathered them in; this
            function does not depend on a particular order for correctness,
            only for the deterministic tie-break below.
        similarity_threshold: Minimum Jaccard on stemmed token sets for two
            items to be considered the same recurring thing.
        item_cap: Hard ceiling on items considered. Excess items (by input
            order — the caller should already have applied its own file-scope
            ordering) are dropped and :attr:`RecurrenceResult.truncated` is
            set; this is a backstop, not a substitute for the caller
            declining an unbounded scope outright.

    Returns:
        A :class:`RecurrenceResult`. Empty ``groups`` is a valid, honest
        outcome — nothing recurred, or every item declined for language.
    """
    truncated = len(items) > item_cap
    bounded = items[:item_cap] if truncated else list(items)

    declined_languages: set[str] = set()
    declined_count = 0
    candidates: list[_Candidate] = []
    for item in bounded:
        lang = (item.language or "en").strip().lower() or "en"
        if lang in NO_SPACE_LANGUAGES:
            declined_languages.add(lang)
            declined_count += 1
            candidates.append(_Candidate(item=item, tokens=frozenset()))
            continue
        candidates.append(_Candidate(item=item, tokens=token_set(item.text, lang)))

    index = build_inverted_index(candidates)
    pairs = candidate_pairs(index)

    uf = _UnionFind(len(candidates))
    for i, j in pairs:
        if jaccard(candidates[i].tokens, candidates[j].tokens) >= similarity_threshold:
            uf.union(i, j)

    clusters: dict[int, list[int]] = {}
    for idx, candidate in enumerate(candidates):
        if not candidate.tokens:
            # Never grouped — matches "an empty token set is never grouped"
            # literally, even in the (impossible in practice, since an empty
            # token set posts no candidates) case a defensive check is worth
            # keeping.
            continue
        clusters.setdefault(uf.find(idx), []).append(idx)

    groups: list[RecurrenceGroup] = []
    for member_indices in clusters.values():
        members = [candidates[i].item for i in member_indices]
        file_uuids = tuple(sorted({m.file_uuid for m in members}))
        if len(file_uuids) < 2:
            # Recurring means CROSS-FILE. Two mentions of the same thing in
            # one meeting are not a recurrence — they are one topic.
            continue
        # Deterministic representative: lowest (file_uuid, original index)
        # among the group's members, never "first visited by the union-find
        # walk" (which depends on dict/set iteration order upstream).
        ordered = sorted(
            zip(member_indices, members, strict=True), key=lambda pair: (pair[1].file_uuid, pair[0])
        )
        representative = ordered[0][1]
        owners = tuple(sorted({m.owner for m in members if m.owner}))
        groups.append(
            RecurrenceGroup(
                representative_text=representative.text,
                member_count=len(members),
                file_uuids=file_uuids,
                owners=owners,
                leaf=_group_leaf(members),
            )
        )

    # Deterministic output order: most files first, then alphabetically by
    # representative text — a stable, re-derivable order rather than
    # insertion order off a dict keyed by union-find root.
    groups.sort(key=lambda g: (-len(g.file_uuids), g.representative_text))

    return RecurrenceResult(
        groups=tuple(groups),
        truncated=truncated,
        considered=len(bounded),
        declined_for_language=declined_count,
        declined_languages=tuple(sorted(declined_languages)),
        comparisons=len(pairs),
    )
