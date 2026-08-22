"""The per-speaker map (W2.3) — "summarize what Alice said".

Split out of the former single-file ``mapreduce.py``. Reads
``file_facts.digest`` sentence-by-sentence, since the INDEXED digest has no
single-valued speaker field to filter on at all (``Route.wants_speaker_digest_map``).
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from app.services.chat.mapreduce.file_summaries import DigestScopeHits

logger = logging.getLogger(__name__)


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
            :func:`~app.services.chat.mapreduce.file_summaries.scope_digest_hits`
            documents — an unbounded scope cannot be mapped over.
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
        meaning as ``scope_digest_hits``) and ``files_with_no_speaker_match``
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

    from app.services.chat.mapreduce.file_summaries import _summary_is_fresh

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
