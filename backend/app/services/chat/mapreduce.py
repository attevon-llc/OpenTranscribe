"""Map-reduce over digests — the "1000 transcripts" path (#403 Stage 4, Phase 4).

The problem in the owner's words: *"If I have 1000 transcripts and someone says
'give me a summary of all the transcripts', that is impossible to feed to an
LLM."* And the shape of the answer, also his: *"it's very rarely that a Claude or
ChatGPT chat session is one single large chat session — it's multiple small fast
quick calls combined into one master result."*

This is the industry-standard **map-reduce / `tree_summarize`** pattern, and the
digest plane it maps over is a **DocumentSummaryIndex**. Named here so the names
travel with the code.

## Two levels, and the first one is already paid for

```
transcript chunks  ──(TextRank, at ingest, NO LLM)──▶  file digest      ← the MAP
file digests       ──(code, or N small bounded calls)─▶  collection view ← the REDUCE
```

Level 1 is `services/ingest_artifacts` and it ran when the file was ingested, so
a summary over 1,000 recordings costs **zero** map-time work. That is the whole
reason the extractive digest was built deterministically: a map step that needed
an LLM would make the corpus-scale case exactly as impossible as it was before.

**Not a recursive tree.** Two levels only, deliberately — RAPTOR-style recursive
clustering is interesting for a corpus with no natural structure, and ours has
one: real meeting and speaker-turn boundaries. Recording it as measured future
work rather than scope.

## Two reducers, one interface, and the no-LLM one is FIRST CLASS

:class:`CodeComposer` renders the collection view in code — N recordings, date
span, total duration, speaker roster, recurring keyphrases, then per recording a
title, date and its extractive digest. It is not a degraded fallback: **D6** makes
the `LLM_PROVIDER`-empty deployment first class, and this is what gives it an
answer to "summarize this collection" at all.

:class:`BatchReducer` spends an LLM, in **many small bounded calls** rather than
one impossible one — the owner's framing, implemented literally.

## What it produces, and why that is not a third summarization path

Both reducers return an ``<overview>`` **context block**, not an answer. The turn's
existing streaming call is the final reduce, which means:

* one summarization path, not three — `media_file.summary_data` (the file page)
  is untouched, and the retired `transcript_summaries` index (#67) is not read or
  written here;
* the answer still streams, and `[n]` citations still resolve, because the
  reduced chunk leg runs alongside;
* the model composes the master result, which is the job it is good at.

## Assembly is concatenation-only

Same rule as `prompting.py` and for the same reason: a recording title
containing ``{evil}`` would raise or interpolate under `str.format`.

⚠️ **This section used to claim every value reaching a prompt here went
through ``prompting._sanitize_attribute`` first. That was false, and the gap
was a real cross-user prompt-injection surface.** Per-file titles and digests
in the listed-recordings section always went through it — but the CORPUS
HEADER's speaker roster and recurring-keyphrase list (`_corpus_header`) were
interpolated into the ``<overview>`` block completely unsanitized. Speaker
display names are OWNER-controlled on a shared recording, so on a
multi-tenant deployment a name containing `` </overview><synthesis> `` was
attacker-controlled text landing, unescaped, in the highest-trust part of the
prompt the model is told to treat as authoritative (base rule 12).

Fixed: the roster and keyphrases now go through
``prompting._sanitize_body_text`` — the BODY-safe sanitizer, not the
attribute one. That distinction matters here specifically: the attribute
sanitizer caps at 120 chars, which is fine for one title but would silently
truncate a roster of a dozen names or a keyphrase list mid-render if applied
per-block instead of per-value. Per-file titles and digests in the listed
section keep using ``_sanitize_attribute``, unchanged.
"""

from __future__ import annotations

import difflib
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

logger = logging.getLogger(__name__)

#: Recordings listed individually before the block starts eliding. The corpus
#: header above them is always complete, so an elided list still reports the true
#: total — a partial list read as complete is the silent-wrong-answer shape this
#: whole stage exists to remove.
MAX_LISTED_FILES = 25

#: Files per LLM call in :class:`BatchReducer`. Small on purpose: "many small
#: fast quick calls" is the design, and a batch large enough to be slow is a
#: batch large enough to be truncated.
DEFAULT_BATCH_FILES = 8

#: Hard ceiling on reduce calls for one turn, whatever the scope. 500 files at 8
#: per batch is 63 calls; this caps the bill and the latency at something a chat
#: turn can survive, and the block says when it bit.
MAX_REDUCE_CALLS = 12

#: Rough character budget the scope map targets when deciding how many leading
#: digest sections to pull PER FILE (`scope_digest_hits`'s `sections_per_file`).
#: The REAL excerpt budget is only known once the model's context window and
#: reply-token reservation are resolved (`prompting.build_messages`), which
#: runs far downstream of the map step — this is a coarse pre-budget so a scope
#: of many files does not fetch three sections each only to have most of them
#: trimmed away later by `prompting._trim_evidence_blocks`.
DEFAULT_MAP_BUDGET_CHARS = 12000

_OVERVIEW_OPEN = "<overview>\n"
_OVERVIEW_CLOSE = "</overview>\n\n"


def sections_budget(files: int, budget_chars: int = DEFAULT_MAP_BUDGET_CHARS) -> int:
    """How many leading digest sections per file the scope map should pull.

    ``max(1, min(3, budget_chars // files))``: never less than one section —
    every file in a bounded scope must contribute something to the map — and
    never more than three, so a small scope's per-file allowance cannot balloon
    unbounded. Shrinks as the scope grows, so a 25-file "summarize everything"
    turn does not fetch three sections apiece only to see most of them dropped
    by the excerpt-budget trim that runs later in the pipeline.

    Args:
        files: Number of files in the resolved scope. Zero or negative is
            treated as one, so a caller need not special-case an empty scope.
        budget_chars: The coarse pre-budget. Defaults to
            :data:`DEFAULT_MAP_BUDGET_CHARS`.

    Returns:
        An integer in ``[1, 3]``.
    """
    return max(1, min(3, budget_chars // max(1, files)))


@dataclass(frozen=True)
class FileSummary:
    """One recording's contribution — the MAP output, read not computed.

    Every field here already exists on disk when a file finishes ingesting. The
    map step is a database read.
    """

    file_uuid: str
    title: str = ""
    recorded_at: str | None = None
    duration: float | None = None
    speakers: tuple[str, ...] = ()
    keyphrases: tuple[str, ...] = ()
    #: The digest text, **already masked** by the caller. This module never sees
    #: raw index content and never masks: masking needs a session and a policy
    #: subject, and a module that quietly did it would be a second place for the
    #: fail-closed contract to drift out of step with `redactor.py`.
    digest: str = ""
    #: W2.3. When this summary was built for a speaker-scoped map
    #: (`scope_speaker_digest_hits`), the focus speaker's own
    #: `file_facts.facts["speakers"]` entry (`total_time`/`turn_count`/
    #: `longest_turn`) — `None` when no speaker focus was requested, or the
    #: focus speaker has no stats entry in this file at all.
    speaker_stats: dict[str, Any] | None = None
    #: W2.3. True when the focus speaker's canonical name IS in this file's
    #: `facts["roster"]`, independent of whether `speaker_stats`/`digest`
    #: above actually carry anything — kept separate so "named in the roster
    #: but nothing came back" is a coverage note the reducer can render,
    #: rather than being indistinguishable from "not in this file at all".
    speaker_in_roster: bool = False


@dataclass
class Overview:
    """A rendered collection view, plus what it cost and what it left out."""

    block: str = ""
    reducer: str = ""
    #: Recordings the map actually covered.
    files_total: int = 0
    #: Recordings the user's scope contains. When this exceeds ``files_total``
    #: the block SAYS SO. Reporting the covered count as though it were the scope
    #: is how a summary states "8 sessions" over a scope of 25 — measured, in the
    #: first end-to-end run of this module.
    files_in_scope: int = 0
    files_listed: int = 0
    llm_calls: int = 0
    truncated: bool = False
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def as_metadata(self) -> dict[str, Any]:
        """Counts only, never content — the same rule the rest of chat follows."""
        return {
            "reducer": self.reducer,
            "files_total": self.files_total,
            "files_listed": self.files_listed,
            "llm_calls": self.llm_calls,
            "truncated": self.truncated,
            **self.diagnostics,
        }


def _clock(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    return f"{hours}h {remainder // 60:02d}m" if hours else f"{remainder // 60}m"


def _speaker_facts_entry(facts: dict[str, Any], speaker_name: str) -> dict[str, Any] | None:
    """This file's `facts["speakers"]` entry for `speaker_name`, or None."""
    wanted = speaker_name.strip().casefold()
    for entry in facts.get("speakers") or []:
        if isinstance(entry, dict) and str(entry.get("name") or "").strip().casefold() == wanted:
            return entry
    return None


def _speaker_in_roster(facts: dict[str, Any], speaker_name: str) -> bool:
    wanted = speaker_name.strip().casefold()
    return any(str(name).strip().casefold() == wanted for name in facts.get("roster") or [])


def build_file_summaries(
    db, digests, *, masked_text: dict[int, str], speaker_focus: str | None = None
) -> list[FileSummary]:
    """Group masked digest hits by file and attach each file's stored facts.

    Args:
        db: Session, for the `file_facts` read. ``None`` yields summaries with
            digest text only — degraded but never wrong.
        digests: Digest ``ChunkHit``s, in rank order.
        masked_text: ``id(hit) -> masked content``. Passed in rather than read off
            the hit so this module cannot be handed unmasked text by accident.
        speaker_focus: W2.3. A single active focus speaker's canonical name —
            same convention `aggregation_service._run_speaker_stats` uses
            (``route.speakers[0] if len(route.speakers) == 1 else None``).
            When set, each summary's `speaker_stats`/`speaker_in_roster` are
            populated from this file's own `facts["speakers"]`/`facts["roster"]`
            so the reducer can render a talk-time header and an honest
            coverage note instead of a silent empty answer.

    Returns:
        One :class:`FileSummary` per distinct file, in the order the digest leg
        ranked them — best file first, which is the order the block lists them in.
    """
    ordered: dict[str, list] = {}
    for hit in digests:
        ordered.setdefault(str(hit.file_uuid), []).append(hit)
    if not ordered:
        return []

    facts_by_file: dict[str, dict[str, Any]] = {}
    if db is not None:
        facts_by_file = _load_facts(db, [hit[0].file_id for hit in ordered.values()])

    summaries: list[FileSummary] = []
    for file_uuid, hits in ordered.items():
        payload = facts_by_file.get(str(hits[0].file_id), {})
        facts = payload.get("facts") or {}
        keyphrases = payload.get("keyphrases") or {}
        # Sections are joined in the order the digest leg returned them, which is
        # relevance order, not transcript order. Said here because the reader of a
        # summary would reasonably assume chronology.
        text = " ".join(masked_text.get(id(hit), "").strip() for hit in hits).strip()
        summaries.append(
            FileSummary(
                file_uuid=file_uuid,
                title=str(hits[0].title or ""),
                recorded_at=(
                    str(facts.get("recorded_at"))[:10] if facts.get("recorded_at") else None
                ),
                duration=facts.get("duration_seconds"),
                speakers=tuple(str(name) for name in (facts.get("roster") or [])),
                keyphrases=tuple(
                    str(entry.get("phrase", "")) for entry in (keyphrases.get("phrases") or [])[:5]
                ),
                digest=text,
                speaker_stats=(
                    _speaker_facts_entry(facts, speaker_focus) if speaker_focus else None
                ),
                speaker_in_roster=(
                    speaker_focus is not None and _speaker_in_roster(facts, speaker_focus)
                ),
            )
        )
    return summaries


class DigestScopeHits(list):
    """The map's hits, PLUS the coverage of the map itself.

    A ``list`` subclass rather than a ``(hits, coverage)`` tuple: ``service.py``'s call
    site (``map_hits = scope_digest_hits(...)``, then ``if map_hits:`` and
    ``mask_digests(session_scope, map_hits, ...)``) is outside this change's file set and
    already treats the return value as a plain list. A tuple would silently break that
    call — iterating it, or truthiness-testing it, would see the ``(list, dict)`` pair
    instead of the hits. Subclassing keeps every existing use (truthiness, iteration,
    ``len()``, equality against a bare ``[]``) working unmodified while making
    ``coverage`` available to a caller that wants it.
    """

    def __init__(self, hits: list, coverage: dict[str, int]) -> None:
        super().__init__(hits)
        self.coverage = coverage


#: SystemSettings key for the #464 tiering flag, resolved as
#: ``ChatSettings.map_tier_summaries`` (``services/chat/settings.py``) — this
#: constant exists only so callers that read the raw key (tests, admin tooling)
#: don't have to spell it twice.
MAP_TIER_SUMMARIES_SETTING_KEY = "chat.rag.map_tier_summaries"

#: SystemSettings key for the W2.3 per-speaker tiering flag, resolved as
#: ``ChatSettings.map_tier_speaker_summaries``. Same convention as the
#: constant above.
MAP_TIER_SPEAKER_SUMMARIES_SETTING_KEY = "chat.rag.map_tier_speaker_summaries"


def _summary_is_fresh(
    summary_status: Any, summary_data: Any, digest_fingerprint: str | None
) -> bool:
    """Whether a file's LLM summary is trustworthy enough to replace its digest.

    **Mismatch OR absent fingerprint ⇒ stale ⇒ the caller falls back to the
    digest.** A summary generated before ``tasks/summarization.py`` started
    stamping ``metadata.source_fingerprint`` (every summary that predates #464)
    therefore self-heals to the digest tier instead of being trusted on faith —
    a stale summary silently describing a transcript that has since been
    edited, re-diarized, or had a speaker renamed is worse than no summary at
    all, because unlike an absent one it *looks* authoritative.

    ``digest_fingerprint`` is ``file_facts.source_fingerprint``, computed by the
    exact same ``ingest_artifacts.service.source_fingerprint`` function over the
    exact same ordered-segment shape — the one automatically-current freshness
    baseline available without a second stored copy of "when was this last
    generated".
    """
    if summary_status != "completed" or not summary_data or not digest_fingerprint:
        return False
    metadata = summary_data.get("metadata") or {}
    stored_fingerprint = metadata.get("source_fingerprint")
    return bool(stored_fingerprint) and stored_fingerprint == digest_fingerprint


def _summary_highlight_text(summary_data: dict[str, Any]) -> str:
    """The prose to represent a file by in the map tier.

    ``brief_summary`` is preferred over ``bluf`` when both exist — it is
    normally the fuller paragraph, and the map already renders one entry per
    file rather than a one-line bottom-line. Custom prompts are validated for
    NOTHING (``llm_service._parse_summary_response``'s own docstring), so
    neither key is guaranteed; falling through to ``""`` is what makes an
    unusable summary shape act exactly like an absent one to the caller.
    """
    return str(summary_data.get("brief_summary") or summary_data.get("bluf") or "").strip()


def scope_digest_hits(
    db,
    file_uuids: list[str],
    *,
    sections_per_file: int = 1,
    use_summaries: bool = False,
) -> DigestScopeHits:
    """One digest per file **for every file in scope** — the actual MAP step.

    ⚠️ **This is not the ranked digest leg, and the difference is a measured
    defect, not a preference.** ``retrieve_digests`` returns the top-K digest
    *sections* by relevance, and sections cluster: asked for 50 over a 25-file
    scope it returned 50 sections drawn from **8 files**. Composing an overview
    from that produced a block headed "recordings: 8" and an answer that
    confidently reported *"8 vendor review board sessions"* over a scope of 25.

    The mistake was conflating two different operations. Ranking picks the best
    passages; **mapping covers every document, by definition** — that is what the
    "map" in map-reduce means. So for a bounded scope the map reads
    ``file_facts`` for each file directly and ignores relevance entirely.

    Returns ``ChunkHit``s rather than raw rows so the result goes through the
    *same* ``redactor.mask_digests`` path as the ranked leg. A second masking
    implementation for the same text is how a fail-closed contract drifts.

    ⚠️ **Excludes quarantined files, unconditionally.** ``file_uuids`` is trusted
    scope by contract (see above) and for a bounded, explicitly-resolved scope
    that is already quarantine-clean (``context_resolver._visible_files_query``
    excludes one for every caller, admin included). But this function is also
    reachable with a scope resolved for a DIFFERENT permission profile than the
    caller who eventually reads the map, and re-deriving that agreement here —
    rather than trusting every caller to have already enforced it — is what
    keeps this rule from silently drifting out of step the way
    ``service._drop_quarantined_hits``'s docstring once claimed it could not:
    the ranked digest leg (``retrieve_digests``) is dropped at phase 3.5 and,
    without this filter, the MAP leg here would serve the same file's digest
    sections regardless. Filtering by predicate rather than a second post-fetch
    matches ``_accessible_scoped_files``'s approach in ``aggregation_service.py``.

    ⚠️ **Outer join, not inner.** A file completed before ``file_facts`` (v390)
    existed — or one the periodic backfill (``tasks/search_maintenance_task``)
    has not reached yet — has no ``FileFacts`` row at all. An INNER JOIN made
    such a file vanish from the map with no signal: not "covered with an empty
    digest", just silently absent, which is indistinguishable from the file
    never having been in scope. The outer join finds the file and reports it in
    ``coverage["files_without_artifacts"]`` instead.

    ⚠️ **Tiering (#464, flag ``chat.rag.map_tier_summaries``, coded default
    OFF).** When ``use_summaries`` is True, a file whose LLM summary is FRESH
    (:func:`_summary_is_fresh`) contributes a summary-derived hit instead of a
    digest section — one file, one hit, one better-written paragraph rather
    than ``sections_per_file`` extractive sections. Absent, unconfigured,
    failed, or **stale** (fingerprint mismatch or missing) summaries fall back
    to the digest exactly as before; the flag can only ever ADD a hit shape,
    never remove the digest fallback's coverage guarantee. With the flag off —
    the default — this function's query and output are byte-identical to
    before #464.

    The summary hit's ``digest_section`` is set to ``len(sections)`` — one
    PAST the file's last real digest section index — deliberately, not to a
    real section number. Downstream, ``mask_digests``/``redactor._gather``
    re-masks every hit this function returns through the digest plane's
    provenance lookup (``_digest_sentences``), which matches a hit's
    ``digest_section`` against ``file_facts.digest["sections"][i]["index"]``.
    An out-of-range index can never coincidentally match a real section — so
    provenance resolution declines for a summary hit exactly as it does for
    any digest whose provenance cannot be resolved, and masking falls through
    to that path's existing, already fail-closed-safe inline fallback rather
    than either (a) matching a real section and substituting the WRONG file
    content, or (b) needing a second "this hit is pre-masked, skip me" contract
    plumbed through ``redactor.py`` and ``ChunkHit`` — a real change, and one
    outside this module.

    Args:
        db: Session.
        file_uuids: The resolved scope. Bounded — an unbounded scope cannot be
            mapped over and must use the ranked leg instead.
        sections_per_file: Leading digest sections per file. One is usually
            enough for a collection view and keeps the block inside its budget.
        use_summaries: Resolved ``ChatSettings.map_tier_summaries``. ``False`` —
            the default — reproduces pre-#464 behaviour exactly.

    Returns:
        A :class:`DigestScopeHits` — behaves as the list of ``ChunkHit``s (carrying
        ``digest_section``, in scope order) it always was, with a ``.coverage`` dict
        attached: ``coverage["files_without_artifacts"]`` counts files in scope with no
        ``file_facts`` row — counted, never silently dropped. ``coverage["summary_hits"]``
        (present only when ``use_summaries`` is True) counts files represented by a fresh
        summary instead of their digest.
    """
    if not file_uuids:
        return DigestScopeHits([], {"files_without_artifacts": 0})
    from app.models.file_facts import FileFacts
    from app.models.media import MediaFile
    from app.services.search.chunk_retrieval import ChunkHit

    columns: list[Any] = [MediaFile.id, MediaFile.uuid, MediaFile.title, FileFacts.digest]
    if use_summaries:
        # Only requested when the flag is set: keeps the flag-off query — and
        # every existing mock of it — byte-identical to before #464.
        columns += [FileFacts.source_fingerprint, MediaFile.summary_status, MediaFile.summary_data]

    try:
        rows = (
            db.query(*columns)
            .outerjoin(FileFacts, FileFacts.media_file_id == MediaFile.id)
            .filter(MediaFile.uuid.in_(list(file_uuids)))
            .filter(MediaFile.is_quarantined.is_(False))
            .all()
        )
    except Exception:  # noqa: BLE001 — a missing map degrades the answer, never breaks it
        logger.exception("Could not read file_facts for the scope map")
        return DigestScopeHits([], {"files_without_artifacts": 0})

    hits: list[Any] = []
    files_without_artifacts = 0
    summary_hits = 0
    # #403 Stage-6 gate: which uuids the MEDIA query above actually matched — a
    # document's uuid never appears in `media_file`, so anything left over
    # after this loop is either a document or genuinely gone.
    matched_uuids: set[str] = set()
    for row in rows:
        if use_summaries:
            file_id, uuid, title, digest, fingerprint, summary_status, summary_data = row
        else:
            file_id, uuid, title, digest = row
            fingerprint = summary_status = summary_data = None
        matched_uuids.add(str(uuid))

        if digest is None:
            files_without_artifacts += 1
            continue

        sections = (digest or {}).get("sections", [])

        if use_summaries and _summary_is_fresh(summary_status, summary_data, fingerprint):
            text = _summary_highlight_text(summary_data)
            if text:
                hits.append(
                    ChunkHit(
                        file_uuid=str(uuid),
                        file_id=int(file_id),
                        chunk_index=-1,
                        content=text,
                        title=str(title or ""),
                        start_time=0.0,
                        end_time=None,
                        digest_section=len(sections),
                    )
                )
                summary_hits += 1
                continue
            # An empty/unusable summary shape acts like an absent one — fall
            # through to the digest below rather than contributing nothing for
            # a file the digest tier can still cover.

        for section in sections[:sections_per_file]:
            hits.append(
                ChunkHit(
                    file_uuid=str(uuid),
                    file_id=int(file_id),
                    chunk_index=-1 - int(section.get("index", 0)),
                    content=str(section.get("text") or ""),
                    title=str(title or ""),
                    start_time=float(section.get("start_time") or 0.0),
                    end_time=section.get("end_time"),
                    digest_section=int(section.get("index", 0)),
                )
            )

    # The DOCUMENT half of a mixed collection (#403 Stage-6 gate). Only for
    # uuids the media query did not match — the two tables share no uuid
    # namespace, so this can never double-count a recording, and it also keeps
    # every EXISTING media-only caller (and every test mocking the media query
    # chain above) byte-identical: this second query only ever runs when the
    # scope actually contains a non-media uuid.
    remaining = [u for u in file_uuids if str(u) not in matched_uuids]
    if remaining:
        doc_hits, doc_without_artifacts = _document_scope_hits(db, remaining, sections_per_file)
        hits.extend(doc_hits)
        files_without_artifacts += doc_without_artifacts

    coverage = {"files_without_artifacts": files_without_artifacts}
    if use_summaries:
        coverage["summary_hits"] = summary_hits
    return DigestScopeHits(hits, coverage)


def _document_scope_hits(
    db, file_uuids: list[str], sections_per_file: int
) -> tuple[list[Any], int]:
    """The document arm of the #403 Stage-6 mixed-collection gate.

    Delegates the join to :func:`ingest_artifacts.scope.scope_facts_for_uuids`
    — it already gets the "outer join, not inner" and the
    ``document -> file_facts.document_id`` join right — rather than restating
    that logic a second time, and converts its ``document``-kind hits into the
    same :class:`ChunkHit` shape the media arm above produces, tagged
    ``source_kind="document"`` (:attr:`ChunkHit.is_document`) so every reader
    downstream — ``mask_digests`` chief among them — can tell the two apart.

    Documents have no speaker roster or duration (unlike a recording's
    ``FileSummary.speakers``/``duration``); :func:`build_file_summaries`
    already tolerates an empty roster/None duration for exactly this reason,
    and ``#464``'s LLM-summary tiering (``use_summaries``) is media-only —
    ``Document`` carries no ``summary_data``/``summary_status`` at all, so
    this arm always falls through to the digest sections.

    Returns:
        ``(hits, files_without_artifacts)`` — never raises; a read failure
        degrades to ``([], len(file_uuids))`` so one broken arm cannot take
        down a map that the media half already answered.
    """
    from app.services.ingest_artifacts.scope import scope_facts_for_uuids
    from app.services.search.chunk_retrieval import ChunkHit

    try:
        coverage = scope_facts_for_uuids(db, file_uuids)
    except Exception:  # noqa: BLE001 — the document half degrades, never breaks the turn
        logger.exception("Could not read file_facts for the document half of the scope map")
        return [], len(file_uuids)

    hits: list[Any] = []
    for hit in coverage.hits:
        if hit.kind != "document":
            # This arm is only ever called with uuids the media query above
            # did NOT match, so a "media"-kind hit here should not occur —
            # skip defensively rather than assume the caller's input was
            # constructed correctly.
            continue
        sections = (hit.digest or {}).get("sections", [])
        for section in sections[:sections_per_file]:
            hits.append(
                ChunkHit(
                    file_uuid=hit.uuid,
                    file_id=hit.source_id,
                    chunk_index=-1 - int(section.get("index", 0)),
                    content=str(section.get("text") or ""),
                    title=hit.title,
                    start_time=float(section.get("start_time") or 0.0),
                    end_time=section.get("end_time"),
                    digest_section=int(section.get("index", 0)),
                    source_kind="document",
                )
            )
    return hits, coverage.files_without_artifacts


# --------------------------------------------------------------------------- #
# The per-speaker map (W2.3) — "summarize what Alice said" (Route.wants_
# speaker_digest_map). Reads file_facts.digest sentence-by-sentence, since the
# INDEXED digest has no single-valued speaker field to filter on at all.
# --------------------------------------------------------------------------- #


def _sentence_speaker_in(sentence: dict[str, Any], wanted: set[str]) -> bool:
    """Whether one stored digest sentence belongs to a wanted speaker.

    ``wanted`` is already casefolded and stripped of every spelling in
    :data:`~app.utils.speaker_labels.UNKNOWN_SPEAKER_LABELS` — "who said X"
    about an undiarized slot is not a mention anyone could scope to.
    """
    speaker = str(sentence.get("speaker") or "").strip()
    return bool(speaker) and speaker.casefold() in wanted


def _speaker_summary_entry(
    summary_data: dict[str, Any], speaker_name: str
) -> dict[str, Any] | None:
    """This file's ``speakers_analysis[]`` entry for ``speaker_name``, or None.

    A two-rung ladder — exact casefold, then best fuzzy match — rather than
    reusing ``speaker_resolver.match_candidate`` directly: that function
    matches free text against a whole roster and resolves ambiguity to "no
    filter", which is right for a question typed in prose. Here the caller
    already knows exactly which canonical name it wants (this map's own
    ``speaker_names``), so a single best-fuzzy-match is the correct shape.
    """
    from app.services.chat.speaker_resolver import FUZZY_MATCH_THRESHOLD

    entries = summary_data.get("speakers_analysis") or []
    wanted = speaker_name.strip().casefold()
    if not wanted:
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("speaker") or "").strip().casefold() == wanted:
            return entry
    best: tuple[float, dict[str, Any]] | None = None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("speaker") or "").strip()
        if not name:
            continue
        ratio = difflib.SequenceMatcher(None, wanted, name.casefold()).ratio()
        if ratio >= FUZZY_MATCH_THRESHOLD and (best is None or ratio > best[0]):
            best = (ratio, entry)
    return best[1] if best else None


def _owner_matched_action_items(summary_data: dict[str, Any], speaker_name: str) -> list[str]:
    """Action items whose ``assigned_to`` names ``speaker_name`` (same ladder)."""
    from app.services.chat.speaker_resolver import FUZZY_MATCH_THRESHOLD

    items = summary_data.get("action_items") or []
    wanted = speaker_name.strip().casefold()
    if not wanted:
        return []
    matched: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        owner = str(item.get("assigned_to") or "").strip()
        if not owner:
            continue
        ratio = difflib.SequenceMatcher(None, wanted, owner.casefold()).ratio()
        if owner.casefold() == wanted or ratio >= FUZZY_MATCH_THRESHOLD:
            text = str(item.get("text") or "").strip()
            if text:
                matched.append(text)
    return matched


def _speaker_summary_highlight_text(summary_data: dict[str, Any], speaker_name: str) -> str:
    """The prose to represent one speaker's contribution in the LLM map tier (#464-style).

    Mirrors ``_summary_highlight_text``'s "unusable shape acts like absent"
    rule: no matching ``speakers_analysis`` entry AND no owner-matched action
    item returns ``""``, which the caller treats exactly like "this file's
    summary said nothing about them" — falling through to the digest tier
    rather than contributing an empty line.

    ⚠️ Masking subject is UNRESOLVED, deliberately left as-is here: this text
    is masked by the SAME call `mask_digests` already makes for every digest
    hit — under the REQUESTING user's policy, matching what this package's
    CLAUDE.md records as the shipped (not-yet-reconsidered) subject for the
    whole digest/summary tier, per issue #402's chunk-tier precedent. Nothing
    here decides that question; it only produces text for the same masking
    call every other digest hit already goes through.
    """
    parts: list[str] = []
    entry = _speaker_summary_entry(summary_data, speaker_name)
    if entry:
        role = str(entry.get("role") or "").strip()
        if role:
            parts.append(f"({role})")
        parts.extend(
            str(point).strip()
            for point in (entry.get("key_contributions") or [])
            if str(point).strip()
        )
    action_items = _owner_matched_action_items(summary_data, speaker_name)
    if action_items:
        parts.append("Action items: " + "; ".join(action_items))
    return " ".join(parts).strip()


def _speaker_summary_text_for_any(summary_data: dict[str, Any], speaker_names: list[str]) -> str:
    """OR across every requested speaker: whatever the summary said about any of them."""
    parts = []
    for name in speaker_names:
        text = _speaker_summary_highlight_text(summary_data, name)
        if text:
            parts.append(f"{name}: {text}")
    return " ".join(parts).strip()


def scope_speaker_digest_hits(
    db,
    file_uuids: list[str],
    speaker_names: list[str],
    *,
    max_sections_per_file: int = 3,
    use_summaries: bool = False,
) -> DigestScopeHits:
    """The per-speaker map: closes ``Route.wants_speaker_digest_map``'s gap.

    ``_apply_structure`` strips the INDEXED digest tier whenever a speaker
    filter is active — correctly, since a digest carries no single-valued
    speaker field — but until this function nothing replaced it, so
    "summarize what Alice said" was structurally impossible even though the
    data to answer it exists: ``file_facts.digest`` stores a ``speaker`` on
    every SENTENCE. This reads that directly, filtering each real section's
    stored sentences down to just the requested speaker(s)' own words (OR
    semantics — a sentence matches if it belongs to ANY of ``speaker_names``),
    and emits one hit per (file, real section that had a match) — never a
    synthetic section divorced from the stored data, so masking's per-sentence
    provenance lookup still resolves through a REAL section index.

    ⚠️ **THE MASKING SEAM.** This function only decides what feeds the hit's
    own (pre-mask) ``content`` — used verbatim only when masking does not
    apply. Masking comes back to ``file_facts.digest`` independently via
    ``redactor.mask_digests`` and re-reads the WHOLE real section fresh,
    because that section may hold other speakers' sentences too. Without a
    matching filter on that side, "a summary of Alice" would come back
    quoting Bob. That second filter lives in
    ``redactor._digest_sentences_from_row``, keyed off ``ChunkHit.speaker`` —
    set below to every requested name, pipe-joined — so it can be re-applied
    with no session held. See that function's docstring and
    ``tests/unit/test_chat_digest_masking.py``'s must-fire guard.

    Args:
        db: Session.
        file_uuids: The resolved scope. Bounded, same precondition
            :func:`scope_digest_hits` documents — an unbounded scope cannot be
            mapped over.
        speaker_names: The requested speakers, already canonical display
            labels (``Route.speakers``, or the resolver's matched names).
        max_sections_per_file: Cap on how many of a file's real sections may
            contribute, so a speaker who talks throughout a long recording
            cannot balloon the block past its budget.
        use_summaries: ``ChatSettings.map_tier_speaker_summaries`` (#W2.3,
            mirrors #464). When True, a file whose LLM summary is FRESH
            contributes its ``speakers_analysis[]`` entry (plus owner-matched
            action items) instead of digest sentences. Stale, absent, or
            unusable summaries fall back to the digest exactly as before, per
            file — never removing the digest fallback's coverage guarantee.

    Returns:
        A :class:`DigestScopeHits`. ``.coverage`` carries
        ``files_without_artifacts`` (no ``file_facts`` row at all — same
        meaning as :func:`scope_digest_hits`) and ``files_with_no_speaker_match``
        (a digest exists but no sentence was attributed to any requested
        speaker) — surfaced so an empty or partial answer always says why,
        never silently. Documents are never included: they have no speakers.
    """
    empty_coverage = {"files_without_artifacts": 0, "files_with_no_speaker_match": 0}
    if not file_uuids or not speaker_names:
        return DigestScopeHits([], dict(empty_coverage))

    from app.models.file_facts import FileFacts
    from app.models.media import MediaFile
    from app.services.search.chunk_retrieval import ChunkHit
    from app.utils.speaker_labels import UNKNOWN_SPEAKER_LABELS

    unknown = {label.casefold() for label in UNKNOWN_SPEAKER_LABELS}
    requested = [str(n).strip() for n in speaker_names if str(n).strip()]
    wanted = {n.casefold() for n in requested} - unknown
    if not wanted:
        return DigestScopeHits([], dict(empty_coverage))
    # One filter string, computed once — every hit this call builds carries the
    # SAME requested set, so `_digest_sentences_from_row` re-derives an
    # identical filter regardless of which hit it is masking.
    speaker_filter = " | ".join(sorted(requested))

    columns: list[Any] = [MediaFile.id, MediaFile.uuid, MediaFile.title, FileFacts.digest]
    if use_summaries:
        columns += [FileFacts.source_fingerprint, MediaFile.summary_status, MediaFile.summary_data]

    try:
        rows = (
            db.query(*columns)
            .outerjoin(FileFacts, FileFacts.media_file_id == MediaFile.id)
            .filter(MediaFile.uuid.in_(list(file_uuids)))
            .filter(MediaFile.is_quarantined.is_(False))
            .all()
        )
    except Exception:  # noqa: BLE001 — a missing map degrades the answer, never breaks it
        logger.exception("Could not read file_facts for the speaker scope map")
        return DigestScopeHits([], dict(empty_coverage))

    hits: list[Any] = []
    files_without_artifacts = 0
    files_with_no_match = 0
    summary_hits = 0
    for row in rows:
        if use_summaries:
            file_id, uuid, title, digest, fingerprint, summary_status, summary_data = row
        else:
            file_id, uuid, title, digest = row
            fingerprint = summary_status = summary_data = None

        if digest is None:
            files_without_artifacts += 1
            continue

        sections = (digest or {}).get("sections", [])

        if use_summaries and _summary_is_fresh(summary_status, summary_data, fingerprint):
            summary_text = _speaker_summary_text_for_any(summary_data, requested)
            if summary_text:
                hits.append(
                    ChunkHit(
                        file_uuid=str(uuid),
                        file_id=int(file_id),
                        chunk_index=-1,
                        content=summary_text,
                        title=str(title or ""),
                        speaker=speaker_filter,
                        start_time=0.0,
                        end_time=None,
                        digest_section=len(sections),
                    )
                )
                summary_hits += 1
                continue
            # A fresh summary that said nothing about any requested speaker —
            # fall through to the digest sentences rather than reporting
            # nothing for a file the digest tier can still cover.

        matched_any = False
        included = 0
        for section in sections:
            if included >= max_sections_per_file:
                break
            sentences = section.get("sentences") or []
            matched = [s for s in sentences if _sentence_speaker_in(s, wanted)]
            if not matched:
                continue
            matched_any = True
            included += 1
            starts = [float((s.get("provenance") or {}).get("start_time") or 0.0) for s in matched]
            ends = [float((s.get("provenance") or {}).get("end_time") or 0.0) for s in matched]
            hits.append(
                ChunkHit(
                    file_uuid=str(uuid),
                    file_id=int(file_id),
                    chunk_index=-1 - int(section.get("index", 0)),
                    content=" ".join(str(s.get("text") or "") for s in matched).strip(),
                    title=str(title or ""),
                    speaker=speaker_filter,
                    start_time=min(starts),
                    end_time=max(ends),
                    digest_section=int(section.get("index", 0)),
                )
            )
        if not matched_any:
            files_with_no_match += 1

    coverage = {
        "files_without_artifacts": files_without_artifacts,
        "files_with_no_speaker_match": files_with_no_match,
    }
    if use_summaries:
        coverage["summary_hits"] = summary_hits
    return DigestScopeHits(hits, coverage)


def _load_facts(db, file_ids: list[int]) -> dict[str, dict[str, Any]]:
    """`str(file_id) -> {facts, keyphrases}` for the files in scope. One query."""
    try:
        from app.models.file_facts import FileFacts

        rows = (
            db.query(FileFacts.media_file_id, FileFacts.facts, FileFacts.keyphrases)
            .filter(FileFacts.media_file_id.in_(list(file_ids)))
            .all()
        )
    except Exception:  # noqa: BLE001 — a summary without facts is degraded, not broken
        logger.exception("Could not load file_facts for the overview; composing without them")
        return {}
    return {str(row[0]): {"facts": row[1] or {}, "keyphrases": row[2] or {}} for row in rows}


def _corpus_header(summaries: list[FileSummary], files_in_scope: int = 0) -> list[str]:
    """The facts that are true of the whole scope, and are exact.

    The roster and keyphrase list are sanitized with
    ``prompting._sanitize_body_text`` — NOT ``_sanitize_attribute`` — because
    speaker display names are OWNER-controlled on a shared recording: the
    person who named "Dana" is not necessarily the person chatting, so an
    unescaped ``</overview><synthesis>`` in a display name would be cross-user
    prompt injection into the highest-trust block the prompt assembles. See
    the module docstring's "Assembly is concatenation-only" section for why
    this is the body-safe sanitizer and not the attribute one.
    """
    from app.services.chat.prompting import _sanitize_body_text

    dates = sorted(s.recorded_at for s in summaries if s.recorded_at)
    total_seconds = sum(float(s.duration or 0.0) for s in summaries)
    roster: dict[str, None] = {}
    for summary in summaries:
        for name in summary.speakers:
            roster.setdefault(_sanitize_body_text(name), None)

    counts: dict[str, int] = {}
    for summary in summaries:
        for phrase in summary.keyphrases:
            safe_phrase = _sanitize_body_text(phrase)
            counts[safe_phrase] = counts.get(safe_phrase, 0) + 1
    # Recurring means recurring: a phrase from one recording is that recording's
    # topic, not the collection's.
    recurring = sorted(
        (phrase for phrase, n in counts.items() if n > 1),
        key=lambda phrase: (-counts[phrase], phrase),
    )[:8]

    covered = len(summaries)
    if files_in_scope and files_in_scope > covered:
        lines = [
            f"recordings summarised here: {covered} of {files_in_scope} in scope "
            f"(the other {files_in_scope - covered} have no digest available)"
        ]
    else:
        lines = [f"recordings: {covered}"]
    if dates:
        lines.append(
            f"dates: {dates[0]} to {dates[-1]}" if dates[0] != dates[-1] else f"date: {dates[0]}"
        )
    if total_seconds > 0:
        lines.append(f"total duration: {_clock(total_seconds)}")
    if roster:
        shown = list(roster)[:12]
        more = f", +{len(roster) - len(shown)} more" if len(roster) > len(shown) else ""
        lines.append(f"speakers ({len(roster)}): {', '.join(shown)}{more}")
    if recurring:
        lines.append(f"recurring topics: {', '.join(recurring)}")
    return lines


def _speaker_focus_header(
    speaker_focus: str,
    summaries: list[FileSummary],
    files_in_scope: int = 0,  # noqa: ARG001
) -> list[str]:
    """The speaker-focus header: talk time / turns / longest monologue.

    W2.3. Also the "never a silent zero" line for a speaker-scoped map: a file
    whose roster names the focus speaker but whose digest contributed no
    stats/content for them gets an explicit coverage note here, rather than
    the whole answer just being short with no explanation. ``files_in_scope``
    is accepted (unused) for signature symmetry with ``_corpus_header``.
    """
    from app.services.chat.prompting import _sanitize_body_text

    safe_name = _sanitize_body_text(speaker_focus)
    lines = [f"focus speaker: {safe_name}"]

    with_stats: list[dict[str, Any]] = []
    for summary in summaries:
        if summary.speaker_stats is not None:
            with_stats.append(summary.speaker_stats)
    if with_stats:
        total_seconds = sum(float(stats.get("total_time") or 0.0) for stats in with_stats)
        total_turns = sum(int(stats.get("turn_count") or 0) for stats in with_stats)
        longest = max(float(stats.get("longest_turn") or 0.0) for stats in with_stats)
        lines.append(
            f"talk time across {len(with_stats)} recording(s) with stats: "
            f"{_clock(total_seconds)}, {total_turns} turns, "
            f"longest single turn {_clock(longest)}"
        )

    # A file whose roster names this speaker but whose digest carries neither
    # stats nor content for them: the extractive digest never selected one of
    # their sentences, OR the row predates a speaker rename — facts/digest are
    # regenerated TOGETHER on a fingerprint change, so a genuinely stale row
    # would still name the OLD label and simply would not match `roster` here
    # at all, which is why this note names both possibilities rather than
    # asserting either.
    uncovered = [
        s for s in summaries if s.speaker_in_roster and not s.speaker_stats and not s.digest
    ]
    if uncovered:
        lines.append(
            f"{len(uncovered)} recording(s) list {safe_name} in the roster but have no "
            "matching content here (the digest may not have selected their sentences, "
            "or the digest may predate a speaker rename)"
        )
    return lines


def _empty_speaker_focus_overview(reducer_name: str, speaker_focus: str) -> Overview:
    """Never a silent zero: no file in scope matched the focus speaker at all."""
    from app.services.chat.prompting import _sanitize_body_text

    safe_name = _sanitize_body_text(speaker_focus)
    block = (
        _OVERVIEW_OPEN
        + f"focus speaker: {safe_name}\n"
        + "no recording in scope has digest content attributed to this speaker.\n"
        + _OVERVIEW_CLOSE
    )
    return Overview(block=block, reducer=reducer_name)


class CodeComposer:
    """The NO-LLM reducer. Renders the collection view in code (**D6**).

    First class, not a fallback: the `LLM_PROVIDER`-empty deployment gets a real
    answer to "summarize this collection" from this path, and every fact in it is
    exact because it was counted rather than generated.
    """

    name = "code"

    def reduce(
        self,
        question: str,
        summaries: list[FileSummary],
        files_in_scope: int = 0,
        *,
        speaker_focus: str | None = None,
        **_kwargs,
    ) -> Overview:  # noqa: ARG002
        if not summaries:
            if speaker_focus:
                return _empty_speaker_focus_overview(self.name, speaker_focus)
            return Overview(reducer=self.name)
        from app.services.chat.prompting import _sanitize_attribute
        from app.services.chat.prompting import _sanitize_body_text

        lines = (
            list(_speaker_focus_header(speaker_focus, summaries, files_in_scope))
            if (speaker_focus)
            else []
        )
        if lines:
            lines.append("")
        lines.extend(_corpus_header(summaries, files_in_scope))
        listed = summaries[:MAX_LISTED_FILES]
        if listed:
            lines.append("")
            for summary in listed:
                title = _sanitize_attribute(summary.title) or "Untitled recording"
                date = f" ({summary.recorded_at})" if summary.recorded_at else ""
                lines.append(f"- {title}{date}")
                if summary.digest:
                    # BODY-safe, not the 120-char attribute sanitizer: a digest is
                    # prose, not a short discrete value, and the attribute cap
                    # silently shredded it mid-sentence for anything longer than
                    # a title. `_sanitize_body_text` defuses the same breakout
                    # attempts with no length cap — see its docstring and the
                    # module docstring's "Assembly is concatenation-only" note.
                    lines.append(f"  {_sanitize_body_text(summary.digest)}")
        hidden = len(summaries) - len(listed)
        if hidden > 0:
            lines.append(
                f"({hidden} further recordings are in scope and counted above but not "
                "listed individually here.)"
            )
        return Overview(
            block=_OVERVIEW_OPEN + "\n".join(lines) + "\n" + _OVERVIEW_CLOSE,
            reducer=self.name,
            files_total=len(summaries),
            files_in_scope=files_in_scope or len(summaries),
            files_listed=len(listed),
            truncated=hidden > 0,
        )


_BATCH_SYSTEM = (
    "You are condensing summaries of several recordings into a shorter briefing.\n"
    "For EACH recording, keep its title and one sentence of what it covered. Keep "
    "every recording — never drop one, never merge two.\n"
    "Use only the material given. Add nothing. No preamble."
)


class BatchReducer:
    """The LLM reducer: **many small bounded calls**, never one impossible one.

    Batches the file summaries, condenses each batch in its own call, and
    concatenates the results in code. The final reduce is the turn's existing
    streaming call — so this adds no third summarization path and the answer
    still streams with working citations.

    Falls back to :class:`CodeComposer` on any failure, per batch. A summary that
    silently lost a third of its recordings because one call timed out is the
    failure this whole tier exists to remove, so a failed batch contributes its
    code-composed form rather than nothing.
    """

    name = "llm-batch"

    def __init__(self, llm, *, batch_files: int = DEFAULT_BATCH_FILES) -> None:
        self.llm = llm
        self.batch_files = max(1, int(batch_files))

    def reduce(
        self,
        question: str,
        summaries: list[FileSummary],
        files_in_scope: int = 0,
        *,
        speaker_focus: str | None = None,
        **_kwargs,
    ) -> Overview:
        if not summaries:
            # Preserves the exact pre-W2.3 shape (`reducer == self.name`) when
            # there is no speaker focus; only a speaker-scoped empty result
            # needs the "never a silent zero" note, which is CodeComposer's.
            if speaker_focus:
                return _empty_speaker_focus_overview(self.name, speaker_focus)
            return Overview(reducer=self.name)
        composer = CodeComposer()
        if self.llm is None:
            return composer.reduce(question, summaries, files_in_scope, speaker_focus=speaker_focus)

        batches = [
            summaries[i : i + self.batch_files] for i in range(0, len(summaries), self.batch_files)
        ]
        capped = batches[:MAX_REDUCE_CALLS]
        lines = (
            list(_speaker_focus_header(speaker_focus, summaries, files_in_scope))
            if (speaker_focus)
            else []
        )
        if lines:
            lines.append("")
        lines.extend(_corpus_header(summaries, files_in_scope))
        lines.append("")

        calls = 0
        failures = 0
        for batch in capped:
            rendered = self._condense(batch)
            if rendered is None:
                failures += 1
                rendered = self._plain(batch)
            else:
                calls += 1
            lines.append(rendered)

        covered = sum(len(batch) for batch in capped)
        hidden = len(summaries) - covered
        if hidden > 0:
            lines.append(
                f"({hidden} further recordings are in scope and counted above but were "
                f"not condensed: the {MAX_REDUCE_CALLS}-call ceiling was reached.)"
            )
        return Overview(
            block=_OVERVIEW_OPEN + "\n".join(lines) + "\n" + _OVERVIEW_CLOSE,
            reducer=self.name,
            files_total=len(summaries),
            files_in_scope=files_in_scope or len(summaries),
            files_listed=covered,
            llm_calls=calls,
            truncated=hidden > 0,
            diagnostics={"batches": len(capped), "batch_failures": failures},
        )

    def _plain(self, batch: list[FileSummary]) -> str:
        from app.services.chat.prompting import _sanitize_attribute

        out = []
        for summary in batch:
            title = _sanitize_attribute(summary.title) or "Untitled recording"
            date = f" ({summary.recorded_at})" if summary.recorded_at else ""
            out.append(f"- {title}{date}")
            if summary.digest:
                out.append(f"  {_sanitize_attribute(summary.digest)}")
        return "\n".join(out)

    def _condense(self, batch: list[FileSummary]) -> str | None:
        """One bounded call over one batch. ``None`` on any failure."""
        payload = self._plain(batch)
        messages = [
            {"role": "system", "content": _BATCH_SYSTEM},
            # Concatenation only — titles and digests are untrusted text.
            {"role": "user", "content": "Recordings:\n" + payload},
        ]
        try:
            response = self.llm.chat_completion(messages, max_tokens=400, temperature=0.1)
        except Exception as exc:  # noqa: BLE001 — one failed batch must not lose the rest
            logger.info(f"Overview batch condense failed, using the composed form: {exc}")
            return None
        text = str(getattr(response, "content", "") or "").strip()
        return text or None


def build_overview(
    question: str,
    summaries: list[FileSummary],
    *,
    llm=None,
    use_llm: bool = False,
    batch_files: int = DEFAULT_BATCH_FILES,
    files_in_scope: int = 0,
    speaker_focus: str | None = None,
) -> Overview:
    """Reduce file summaries to one collection view.

    Args:
        question: The user's question. Carried for the reducer interface; the
            code composer deliberately ignores it, because every fact it renders
            is true of the scope regardless of what was asked.
        summaries: The map output.
        llm: The caller's ``LLMService``, or None.
        use_llm: Whether to spend calls. ``False`` — the default — is a complete
            answer, not a degraded one (**D6**).
        batch_files: Recordings per call.
        files_in_scope: How many recordings the user's scope contains. When it
            exceeds what the map covered, the block says so instead of reporting
            the covered count as the total.
        speaker_focus: W2.3. A single active focus speaker's canonical name,
            when this overview was built from ``scope_speaker_digest_hits``.
            Renders a talk-time header and the "never a silent zero" coverage
            notes; ``None`` reproduces the pre-W2.3 block exactly.

    Returns:
        An :class:`Overview`. Empty ``block`` when there is nothing to summarise.
    """
    reducer = BatchReducer(llm, batch_files=batch_files) if (use_llm and llm) else CodeComposer()
    return reducer.reduce(question, summaries, files_in_scope, speaker_focus=speaker_focus)
