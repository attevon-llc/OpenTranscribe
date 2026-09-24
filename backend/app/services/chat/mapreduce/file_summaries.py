"""The MAP step: one recording becomes one :class:`FileSummary`.

Split out of the former single-file ``mapreduce.py``. This module owns the
per-file read — ``file_facts``, the LLM-summary freshness test, and the
outer-join-not-inner / quarantine-exclusion rules that make the map correct
for a bounded scope. ``reducers.py`` (the REDUCE half) consumes the
:class:`FileSummary` list this module produces; it does not read Postgres
itself.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: SystemSettings key for the #464 tiering flag, resolved as
#: ``ChatSettings.map_tier_summaries`` (``services/chat/settings.py``) — this
#: constant exists only so callers that read the raw key (tests, admin tooling)
#: don't have to spell it twice.
MAP_TIER_SUMMARIES_SETTING_KEY = "chat.rag.map_tier_summaries"

#: SystemSettings key for the W2.3 per-speaker tiering flag, resolved as
#: ``ChatSettings.map_tier_speaker_summaries``. Same convention as the
#: constant above.
MAP_TIER_SPEAKER_SUMMARIES_SETTING_KEY = "chat.rag.map_tier_speaker_summaries"


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
    #: The EXTRACTIVE digest text, **already masked** by the caller, in EVERY
    #: mode (#532 follow-up, section 2.5). This module never sees raw index
    #: content and never masks: masking needs a session and a policy subject,
    #: and a module that quietly did it would be a second place for the
    #: fail-closed contract to drift out of step with `redactor.py`. Never
    #: holds abstractive text — see `llm_summary` below for that.
    digest: str = ""
    #: #532 follow-up. The ABSTRACTIVE text — `structured_summary_text`'s
    #: lead + decisions + action items in hybrid mode, or the plain
    #: `_summary_highlight_text` paragraph in the pre-hybrid (arm-(d)) shape —
    #: already masked, exactly like `digest`. `""` when this file's map hit
    #: carries no fresh-summary content at all (a plain digest-tier file).
    #: `digest` and `llm_summary` are two DIFFERENT KINDS of text and must
    #: never be concatenated into one field again — that is the #464/#532
    #: mixing bug this split exists to prevent (see `is_hybrid` below).
    llm_summary: str = ""
    #: #532 follow-up. This file's `digest`'s real digest-section index,
    #: start time and end time — populated ONLY when `digest` was assembled
    #: from EXACTLY ONE section (the hybrid's closing section). `None` in
    #: every other shape: a non-hybrid file's digest can join several
    #: sections (`sections_per_file` > 1), for which no single index/time is
    #: meaningful. Exists for a real per-part citation deep link (plan's
    #: conditional Unit U5), not consumed by the code-composer rendering path.
    digest_section_index: int | None = None
    digest_start_time: float | None = None
    digest_end_time: float | None = None
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
    #: #464. True when this file's map hit(s) included a FRESH LLM summary
    #: hit (`ChunkHit.is_llm_summary`) — either the pre-#532 arm-(d) shape
    #: (`llm_summary` set, `digest` empty) or the #532 hybrid shape (BOTH
    #: `llm_summary` and `digest` set — see `is_hybrid`). Before #532 this
    #: field doubled as "was `digest` itself built from a summary", which the
    #: hybrid shape makes false: `digest` is extractive-only in every mode
    #: now (see that field's own docstring). `citations.build_overview_citations`
    #: still reads this coarse flag to pick `KIND_SUMMARY` vs `KIND_DIGEST` —
    #: correct for the non-hybrid shapes; `service._overview_citation_start`'s
    #: hybrid guard is what keeps a hybrid file from ever reaching that
    #: function today (no per-part citation kind exists yet — plan's
    #: conditional Unit U5).
    is_llm_summary: bool = False

    @property
    def is_hybrid(self) -> bool:
        """#532 follow-up.

        True when this file carries BOTH an abstractive summary and an
        extractive digest section — the hybrid map entry shape, distinct from
        arm-(d)'s summary-only shape (`llm_summary` set, `digest` empty) and
        the plain digest-tier shape (`digest` set, `llm_summary` empty).
        """
        return bool(self.llm_summary) and bool(self.digest)


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
        # #532 follow-up: route each hit by KIND rather than joining every hit
        # into one field. Before the hybrid shape existed, a file's hits were
        # either ALL summary (one hit) or ALL digest (`sections_per_file`
        # sections) — never mixed — so `any()`/`all()` agreed and one join was
        # safe. The hybrid shape breaks that invariant (one summary hit PLUS
        # one closing-section hit for the same file), so `digest` and
        # `llm_summary` are now populated from their own hit subsets, never
        # from a shared join. Sections are joined in the order the digest leg
        # returned them, which is relevance order, not transcript order — said
        # here because the reader of a summary would reasonably assume
        # chronology.
        summary_hits = [hit for hit in hits if getattr(hit, "is_llm_summary", False)]
        section_hits = [hit for hit in hits if not getattr(hit, "is_llm_summary", False)]
        llm_summary_text = " ".join(
            masked_text.get(id(hit), "").strip() for hit in summary_hits
        ).strip()
        digest_text = " ".join(masked_text.get(id(hit), "").strip() for hit in section_hits).strip()
        digest_section_index = digest_start_time = digest_end_time = None
        if len(section_hits) == 1:
            # A single section can only be identified unambiguously when it is
            # the sole digest hit for this file — the hybrid's closing
            # section, or a non-hybrid file whose `sections_per_file` allowance
            # happened to be 1. A join of several sections has no single index.
            only = section_hits[0]
            digest_section_index = getattr(only, "digest_section", None)
            digest_start_time = getattr(only, "start_time", None)
            digest_end_time = getattr(only, "end_time", None)
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
                digest=digest_text,
                llm_summary=llm_summary_text,
                digest_section_index=digest_section_index,
                digest_start_time=digest_start_time,
                digest_end_time=digest_end_time,
                speaker_stats=(
                    _speaker_facts_entry(facts, speaker_focus) if speaker_focus else None
                ),
                speaker_in_roster=(
                    speaker_focus is not None and _speaker_in_roster(facts, speaker_focus)
                ),
                is_llm_summary=bool(summary_hits),
            )
        )
    return summaries


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


#: #532 follow-up plan, section 2.2: cap on decisions/action items rendered
#: into the hybrid map entry's abstractive half. Small on purpose — this is
#: one paragraph in a collection-view entry, not the full summary modal.
HYBRID_MAX_ITEMS_PER_LEAF = 3


def _fit_clause(label: str, items: list[str], remaining: int) -> str:
    """The widest PREFIX of ``items`` that fits in ``remaining`` chars, or ``""``.

    Never partial: a clause that does not fit at ``n`` items is retried at
    ``n - 1``, so an item is always rendered whole or not at all — this is
    the "cuts at item boundaries, drops whole trailing items, never cuts
    inside an item" rule from the plan's section 2.3.
    """
    for n in range(len(items), 0, -1):
        clause = f" {label}: " + "; ".join(items[:n]) + "."
        if len(clause) <= remaining:
            return clause
    return ""


def structured_summary_text(summary_data: dict[str, Any], budget_chars: int) -> str:
    """The hybrid map entry's abstractive half: lead paragraph + decisions + action items.

    Fixed fill order — lead, then decisions, then action items (plan section
    2.2) — cutting at ITEM boundaries only. A clause that does not fully fit
    is dropped whole rather than truncated mid-item, and a later clause is
    still attempted against whatever budget remains (a decisions clause that
    does not fit at all does not block action items from being tried).

    If the lead alone exceeds ``budget_chars`` it is cut at the last sentence
    boundary inside the budget — the same cut ``prompting.format_excerpts``
    uses for a ``truncated="true"`` excerpt (``prompting._cut_at_boundary``),
    reused rather than reimplemented — instead of dropping the whole entry:
    some abstractive text beats none. Returns ``""`` when there is no lead to
    render, or the budget cannot hold even a trimmed lead (``budget_chars <=
    0``); the caller (:func:`scope_digest_hits`) treats an empty render
    exactly like an absent summary and falls back to the control's digest
    sections.

    Items are extracted with :func:`app.services.chat.recurrence.normalize_leaf`
    — the module's own shape-tolerant extractor for ``key_decisions``/
    ``action_items`` entries (string or dict, several key spellings) — never
    a second extractor written here. An item ``normalize_leaf`` declines to
    parse (an unrecognised custom-prompt shape) is silently omitted, exactly
    as it is everywhere else that function is used.

    Args:
        summary_data: A file's ``MediaFile.summary_data`` (already validated
            fresh by the caller via :func:`_summary_is_fresh`).
        budget_chars: Characters available for this text. The caller derives
            it as ``entry_budget(n) - len(closing_section_text)`` per the
            plan's section 2.3 — the closing digest section is rendered
            verbatim and never truncated, so it is subtracted first.

    Returns:
        The composed text, or ``""``.
    """
    from app.services.chat.recurrence import LEAF_ACTION_ITEM
    from app.services.chat.recurrence import LEAF_KEY_DECISION
    from app.services.chat.recurrence import normalize_leaf

    lead = _summary_highlight_text(summary_data)
    if not lead or budget_chars <= 0:
        return ""

    if len(lead) > budget_chars:
        from app.services.chat.prompting import _cut_at_boundary

        return _cut_at_boundary(lead, budget_chars)

    text = lead
    remaining = budget_chars - len(text)

    for label, raw_items, leaf in (
        ("Decisions", summary_data.get("key_decisions"), LEAF_KEY_DECISION),
        ("Action items", summary_data.get("action_items"), LEAF_ACTION_ITEM),
    ):
        items: list[str] = []
        for raw in (raw_items or [])[:HYBRID_MAX_ITEMS_PER_LEAF]:
            extracted = normalize_leaf(raw, leaf)
            if extracted is not None:
                items.append(extracted[0])
        if not items:
            continue
        clause = _fit_clause(label, items, remaining)
        if clause:
            text += clause
            remaining -= len(clause)

    return text


def scope_digest_hits(
    db,
    file_uuids: list[str],
    *,
    sections_per_file: int = 1,
    use_summaries: bool = False,
    hybrid: bool = False,
    entry_budget_chars: int = 0,
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
        hybrid: #532 follow-up (``docs/design/532_hybrid_summary_synthesis_plan.md``),
            resolved ``ChatSettings.map_tier_hybrid and ChatSettings.map_tier_summaries``.
            Only consulted when ``use_summaries`` is also True. ``False`` — the
            default — reproduces the #464 (arm-(d)) shape exactly: a fresh
            summary contributes one paragraph-only hit. ``True`` composes each
            fresh-summary file's entry as
            :func:`structured_summary_text` (lead + decisions + action items)
            **plus** the file's closing digest section (``sections[-1]``,
            verbatim), REPLACING the leading ``sections[:sections_per_file]``
            the control would otherwise show — never adding to them. A file
            whose abstractive render comes back empty, or whose summary is not
            fresh, falls back to the plain digest sections exactly as the
            non-hybrid path does.
        entry_budget_chars: The per-file character ceiling a hybrid entry may
            not exceed — ``sections_budget(len(file_uuids)) *
            DIGEST_SNIPPET_CHARS`` (plan section 2.3), computed by the caller
            since this module does not import ``citations.py``. The closing
            section (verbatim, never truncated) is subtracted first; the
            remainder bounds :func:`structured_summary_text`. Unused when
            ``hybrid`` is False.

    Returns:
        A :class:`DigestScopeHits` — behaves as the list of ``ChunkHit``s (carrying
        ``digest_section``, in scope order) it always was, with a ``.coverage`` dict
        attached: ``coverage["files_without_artifacts"]`` counts files in scope with no
        ``file_facts`` row — counted, never silently dropped. ``coverage["files_no_content"]``
        counts files that DO have a ``file_facts`` row and a digest, but whose digest
        carries zero sections — a file that was genuinely looked at and had nothing to
        contribute, which is a different fact than "never consulted" and would otherwise
        be indistinguishable from it: neither an empty-sections file nor a missing-row
        file appends a hit, so without this counter both read as a silent gap to a
        caller reconciling ``len(hits)`` against ``len(file_uuids)``
        (``mapreduce.coverage.check_scope_coverage`` is that reconciliation).
        ``coverage["summary_hits"]`` (present only when ``use_summaries`` is True) counts
        files represented by a fresh summary instead of their digest — a hybrid file counts
        here too, alongside the arm-(d)-shaped ones. Present only when ``use_summaries`` is
        True, ``coverage`` also carries the #532 follow-up counters:
        ``hybrid_entries`` (fresh-summary files rendered as abstractive + closing section),
        ``summary_only_entries`` (fresh-summary files with zero digest sections to close
        with, so the entry is abstractive-only — the arm-(d) shape, reached only via the
        hybrid path since it renders a structured text even with no sections), ``entries_digest``
        (files that fell back to the plain digest sections — stale/absent summary, or
        hybrid's abstractive render came back empty), ``summary_chars`` and
        ``closing_section_chars`` (summed rendered character counts, for the per-turn
        volume instrumentation the plan's section 4.3 applied-checks read).
    """
    if not file_uuids:
        return DigestScopeHits([], {"files_without_artifacts": 0, "files_no_content": 0})
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
        return DigestScopeHits([], {"files_without_artifacts": 0, "files_no_content": 0})

    hits: list[Any] = []
    files_without_artifacts = 0
    files_no_content = 0
    summary_hits = 0
    hybrid_entries = 0
    summary_only_entries = 0
    entries_digest = 0
    summary_chars = 0
    closing_section_chars = 0
    #: Which scope uuids the query actually matched. The query outer-joins
    #: ``file_facts`` onto ``media_file`` filtered by ``MediaFile.uuid.in_(...)``,
    #: so a scope uuid with no accessible ``media_file`` row produces NO row at
    #: all and the loop below can never see it. Counting those after the loop is
    #: what keeps ``files_without_artifacts`` a complete account of the scope —
    #: without it a caller reconciling coverage sees an unexplained gap, which is
    #: exactly the failure this coverage dict exists to prevent.
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
            if hybrid:
                # #532 follow-up: REPLACE the leading sections with abstractive
                # text + the closing section, never add to them (plan 2.3).
                closing = sections[-1] if sections else None
                closing_text = str(closing.get("text") or "") if closing else ""
                abstractive_budget = max(0, entry_budget_chars - len(closing_text))
                abstractive = structured_summary_text(summary_data, abstractive_budget)
                if abstractive:
                    hits.append(
                        ChunkHit(
                            file_uuid=str(uuid),
                            file_id=int(file_id),
                            chunk_index=-1,
                            content=abstractive,
                            title=str(title or ""),
                            start_time=0.0,
                            end_time=None,
                            digest_section=len(sections),
                            is_llm_summary=True,
                        )
                    )
                    summary_hits += 1
                    summary_chars += len(abstractive)
                    if closing is not None:
                        hits.append(
                            ChunkHit(
                                file_uuid=str(uuid),
                                file_id=int(file_id),
                                chunk_index=-1 - int(closing.get("index", 0)),
                                content=closing_text,
                                title=str(title or ""),
                                start_time=float(closing.get("start_time") or 0.0),
                                end_time=closing.get("end_time"),
                                digest_section=int(closing.get("index", 0)),
                            )
                        )
                        closing_section_chars += len(closing_text)
                        hybrid_entries += 1
                    else:
                        # No sections to close with — abstractive-only, the
                        # arm-(d) shape, but reached via structured_summary_text
                        # rather than _summary_highlight_text alone (so decisions
                        # and action items still render even with zero sections).
                        summary_only_entries += 1
                    continue
                # The abstractive render came back "" (no lead at all, or a
                # budget too small to hold even a trimmed one) — an empty
                # render acts like an absent summary, same rule as the
                # non-hybrid branch below: fall through to the digest.
            else:
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
                            is_llm_summary=True,
                        )
                    )
                    summary_hits += 1
                    continue
                # An empty/unusable summary shape acts like an absent one — fall
                # through to the digest below rather than contributing nothing for
                # a file the digest tier can still cover.

        file_contributed = False
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
            file_contributed = True
        if file_contributed and use_summaries:
            # A file that fell back to the plain digest sections while tiering
            # was on — stale/absent summary, or (hybrid only) an empty
            # abstractive render. Meaningless noise when tiering is off (every
            # file takes this path trivially), so counted only alongside the
            # other use_summaries-only counters below.
            entries_digest += 1
        if not file_contributed:
            # A real ``file_facts`` row with a digest, but the digest's own
            # ``sections`` list is empty (an extractive digest that selected
            # nothing — e.g. a near-silent recording). Distinct from
            # ``files_without_artifacts`` above: this file WAS read, it just
            # had nothing to offer, and a caller reconciling coverage needs to
            # tell the two apart rather than seeing one unexplained gap.
            files_no_content += 1

    # Scope uuids the query never matched — see ``matched_uuids`` above.
    files_without_artifacts += sum(1 for u in file_uuids if str(u) not in matched_uuids)

    coverage = {
        "files_without_artifacts": files_without_artifacts,
        "files_no_content": files_no_content,
    }
    if use_summaries:
        coverage["summary_hits"] = summary_hits
        coverage["hybrid_entries"] = hybrid_entries
        coverage["summary_only_entries"] = summary_only_entries
        coverage["entries_digest"] = entries_digest
        coverage["summary_chars"] = summary_chars
        coverage["closing_section_chars"] = closing_section_chars
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
