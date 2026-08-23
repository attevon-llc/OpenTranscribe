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
        ``file_facts`` row — counted, never silently dropped. ``coverage["files_no_content"]``
        counts files that DO have a ``file_facts`` row and a digest, but whose digest
        carries zero sections — a file that was genuinely looked at and had nothing to
        contribute, which is a different fact than "never consulted" and would otherwise
        be indistinguishable from it: neither an empty-sections file nor a missing-row
        file appends a hit, so without this counter both read as a silent gap to a
        caller reconciling ``len(hits)`` against ``len(file_uuids)``
        (``mapreduce.coverage.check_scope_coverage`` is that reconciliation).
        ``coverage["summary_hits"]`` (present only when ``use_summaries`` is True) counts
        files represented by a fresh summary instead of their digest.
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
