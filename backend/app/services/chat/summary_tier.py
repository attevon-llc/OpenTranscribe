"""The summary tier: which leg feeds the ``<overview>`` block, and composing it.

A bounded scope is MAPPED in full (``scope_digest_hits`` reads ``file_facts`` for every
file); the ranked digest leg is only the fallback for the unbounded "all accessible"
scope, because ranking is not mapping (see :func:`_resolve_summary_tier`). Every
summary the overview is built from went through ``mask_digests`` first, with the
turn's digest-plane kwargs.
"""

from __future__ import annotations

from typing import Any

from app.services.chat.redactor import MaskedChunk
from app.services.chat.retrieval import RetrievalResult
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit


def _resolve_summary_tier(
    *,
    decision,
    file_uuids: list[str] | None,
    settings: ChatSettings,
    session_scope,
    ranked_digests: list[ChunkHit],
    ranked_digests_masked: list[MaskedChunk],
    user_id: int,
    organization_id: int | None = None,
    mask_kwargs: dict[str, Any],
) -> tuple[list[Any], list[MaskedChunk], str | None, int, int, dict[str, int]]:
    """Decide which leg feeds the map-reduce overview's summaries.

    Split out of ``_prepare_context`` so its branching doesn't count against
    that function's own complexity — it is a single decision, made once.

    **The scope map used to run ONLY when the ranked digest leg had already
    found something** (``if result.digests: ... scope_digest_hits(...)``), so
    the coverage map died exactly when it mattered most: a bounded scope whose
    ranked search simply did not surface any digest sections. It now runs
    whenever the router asked for the digest tier AND the scope is bounded,
    independent of what the ranked leg returned.

    The MAP output is READ, not computed: level 1 ran at ingest, so a summary
    over a large scope costs no map-time work (#403 Phase 4). A BOUNDED scope
    is mapped over in full; the ranked leg is only a fallback for "all
    accessible" (``file_uuids is None``), where mapping over everything is not
    possible — there is no enumerated list to map. Ranking picks the best
    passages, mapping covers every document — using the ranked leg as the map
    produced a block headed "recordings: 8" over a 25-file scope, and an
    answer that said so.

    Args:
        decision: The router's ``Route`` for this turn.
        file_uuids: The resolved scope. ``None``/unbounded cannot be mapped.
        settings: Admin-tuned RAG knobs (``map_tier_summaries``).
        session_scope: Factory for a short-lived Postgres session.
        ranked_digests: ``result.digests`` — the OpenSearch-ranked digest leg.
        ranked_digests_masked: Those same hits, already masked.
        user_id: The requesting user, for the masker's policy resolution.
        mask_kwargs: ``mask_digests``' ``unmask_for_local`` kwarg, if set.

    Returns:
        ``(summary_hits, summary_masked, map_leg, files_without_artifacts,
        files_no_content, map_coverage)``. ``map_leg`` is ``"scope_map"`` |
        ``"speaker_scope_map"`` | ``"speaker_scope_map_empty"`` |
        ``"ranked_digests"`` | ``None`` (nothing to summarise this turn).
        ``files_without_artifacts`` and ``files_no_content`` are 0 whenever the
        map did not run or found no gap — see
        ``mapreduce.scope_digest_hits``'s own docstring for what distinguishes
        the two (never consulted vs. consulted and genuinely empty), which
        ``mapreduce.coverage.check_scope_coverage`` reads back off ``meta`` via
        these same two counts. ``map_coverage`` is ``scope_digest_hits``'s full
        ``.coverage`` dict when ``map_leg == "scope_map"`` (it carries the
        ``summary_hits``/``entries_digest`` counters — see that function's own
        docstring), and ``{}`` for every other leg.
    """
    from app.services.chat.mapreduce import scope_digest_hits
    from app.services.chat.mapreduce import scope_speaker_digest_hits
    from app.services.chat.mapreduce import sections_budget
    from app.services.chat.redactor import mask_digests

    if decision.wants_speaker_digest_map and file_uuids:
        # closes the gap `Route.wants_speaker_digest_map` documents. A
        # speaker filter already removed the INDEXED digest tier above
        # (correctly — the index has no single-valued speaker field to filter
        # on), so `decision.wants_digest` is False for exactly this case; this
        # branch is the fallback that used to be missing entirely, leaving
        # "summarize what Alice said" structurally impossible.
        with session_scope() as db:
            map_hits = scope_speaker_digest_hits(
                db,
                file_uuids,
                list(decision.speakers),
                max_sections_per_file=sections_budget(len(file_uuids)),
                use_summaries=settings.map_tier_speaker_summaries,
            )
        files_without_artifacts = int(map_hits.coverage.get("files_without_artifacts", 0))
        files_no_content = int(map_hits.coverage.get("files_no_content", 0))
        if map_hits:
            summary_masked = mask_digests(
                session_scope, map_hits, user_id, organization_id=organization_id, **mask_kwargs
            )
            return (
                map_hits,
                summary_masked,
                "speaker_scope_map",
                files_without_artifacts,
                files_no_content,
                {},
            )
        # Never a silent zero: the caller still composes an overview from this
        # (empty) map when the turn was speaker-scoped — see
        # `mapreduce._empty_speaker_focus_overview` — rather than silently
        # answering with nothing and no explanation.
        return [], [], "speaker_scope_map_empty", files_without_artifacts, files_no_content, {}

    if decision.wants_digest and file_uuids:
        with session_scope() as db:
            map_hits = scope_digest_hits(
                db,
                file_uuids,
                sections_per_file=sections_budget(len(file_uuids)),
                use_summaries=settings.map_tier_summaries,
            )
        # Counted whether or not the map produced anything, so an upgraded
        # library (files whose `file_facts` row has not been backfilled yet)
        # reports the gap on `meta` rather than the file simply being absent
        # with no signal — the coverage block already says so in the header
        # when summaries exist; this is the same count for when they don't.
        files_without_artifacts = int(map_hits.coverage.get("files_without_artifacts", 0))
        files_no_content = int(map_hits.coverage.get("files_no_content", 0))
        if map_hits:
            summary_masked = mask_digests(
                session_scope, map_hits, user_id, organization_id=organization_id, **mask_kwargs
            )
            return (
                map_hits,
                summary_masked,
                "scope_map",
                files_without_artifacts,
                files_no_content,
                dict(map_hits.coverage),
            )
        if ranked_digests:
            # The map covered nothing (every file in scope lacks a digest, or
            # the read failed) but the ranked leg still found something —
            # degrade to it rather than reporting no overview at all.
            return (
                ranked_digests,
                ranked_digests_masked,
                "ranked_digests",
                files_without_artifacts,
                files_no_content,
                {},
            )
        return [], [], None, files_without_artifacts, files_no_content, {}

    if ranked_digests:
        # Unbounded scope: keep today's ranked-leg-only behaviour. Also
        # reachable for a bounded scope the router did not route to the
        # digest tier at all but which somehow still carries ranked digest
        # hits — the same fallback the pre-decoupling code applied
        # unconditionally to any non-empty `result.digests`.
        return ranked_digests, ranked_digests_masked, "ranked_digests", 0, 0, {}
    return [], [], None, 0, 0, {}


def _overview_citation_start(settings, digest_masked: list, masked: list) -> int | None:
    """#532 arm (a): the id base for citable overview entries, or ``None`` (off).

    Excerpt markers are POSITIONS into the final chunk list, and that list can
    only SHRINK after this point (the empty-after-masking filter), so
    ``len(digest_masked) + len(masked)`` is an upper bound that can never
    collide with an excerpt id.
    """
    if not settings.overview_citable:
        return None
    return len(digest_masked) + len(masked)


def _finalize_overview_citations(overview, summaries: list) -> None:
    """#532 arm (a): attach citation payloads when the composer assigned ids.

    A no-op for every turn where the arm is off (``cited_entries`` empty) —
    payloads carry snippets and ride the Overview object, never ``meta``.
    """
    if not overview.cited_entries:
        return
    from app.services.chat.citations import build_overview_citations

    overview.citation_payloads = tuple(build_overview_citations(overview.cited_entries, summaries))


def _overview_diagnostics(summaries: list, map_coverage: dict[str, Any], overview, map_leg):
    """#532 instrumentation (plan Unit U4), scope-map turns only.

    ``{}`` when ``map_leg != "scope_map"`` — the branch lives here rather than
    at the call site so it does not count against ``_prepare_context``'s own
    complexity ceiling. Content-free counts only — never text, same rule
    ``Overview.as_metadata()`` already follows. ``summary_chars``/``digest_chars``
    are summed from the POST-masking ``summaries``.
    """
    if map_leg != "scope_map":
        return {}
    return {
        "entries_digest": int(map_coverage.get("entries_digest", 0)),
        "block_chars": len(overview.block),
        "summary_chars": sum(len(s.llm_summary) for s in summaries),
        "digest_chars": sum(len(s.digest) for s in summaries),
    }


def _build_summaries(
    *,
    decision,
    file_uuids: list[str] | None,
    settings: ChatSettings,
    session_scope,
    result: RetrievalResult,
    digest_masked: list[MaskedChunk],
    user_id: int,
    organization_id: int | None,
    digest_mask_kwargs: dict[str, bool],
    meta: dict[str, Any],
) -> tuple[list[Any], str | None, dict[str, Any], str | None]:
    """Phase 4 of ``_prepare_context``, continued: the summary tier's per-file summaries.

    Returns:
        ``(summaries, map_leg, map_coverage, speaker_focus_for_summary)``.
    """
    summaries: list[Any] = []
    # which leg feeds the overview — the scope map (bounded scope, reads
    # `file_facts` for every file) or the ranked digest leg above (unbounded
    # scope, or the map covered nothing). See `_resolve_summary_tier`'s own
    # docstring for the decoupling this replaces.
    (
        summary_hits,
        summary_masked,
        map_leg,
        files_without_artifacts,
        files_no_content,
        map_coverage,
    ) = _resolve_summary_tier(
        decision=decision,
        file_uuids=file_uuids,
        settings=settings,
        session_scope=session_scope,
        ranked_digests=result.digests,
        ranked_digests_masked=digest_masked,
        user_id=user_id,
        organization_id=organization_id,
        mask_kwargs=digest_mask_kwargs,
    )
    if files_without_artifacts:
        meta["map_files_without_artifacts"] = files_without_artifacts
    if files_no_content:
        # Same "counted whether or not the map produced anything" rule as the
        # line above, and the same reason: distinct from
        # `files_without_artifacts` (`mapreduce.scope_digest_hits`'s own
        # docstring), a file counted here WAS read and had a real digest —
        # it just had nothing in it — which `mapreduce.coverage` needs to
        # tell apart from "never consulted" when reconciling this turn's
        # scope coverage from `meta` alone.
        meta["map_files_no_content"] = files_no_content

    # Single-speaker focus only — same simplification
    # `aggregation_service._run_speaker_stats` already applies
    # (`route.speakers[0] if len(route.speakers) == 1 else None`): a map over
    # SEVERAL speakers still serves their combined content correctly (OR
    # semantics in `scope_speaker_digest_hits`), it just does not get the
    # single-name talk-time header/coverage notes below.
    speaker_focus_for_summary = decision.speakers[0] if len(decision.speakers) == 1 else None

    if summary_hits:
        from app.services.chat.mapreduce import build_file_summaries

        with session_scope() as db:
            summaries = build_file_summaries(
                db,
                summary_hits,
                masked_text={id(m.source): m.content for m in summary_masked},
                speaker_focus=speaker_focus_for_summary,
            )
    return summaries, map_leg, map_coverage, speaker_focus_for_summary


def _compose_overview(
    question: str,
    *,
    settings: ChatSettings,
    summaries: list[Any],
    map_leg: str | None,
    map_coverage: dict[str, Any],
    speaker_focus_for_summary: str | None,
    file_uuids: list[str] | None,
    digest_masked: list[MaskedChunk],
    masked: list[MaskedChunk],
    meta: dict[str, Any],
):
    """Phase 5 of ``_prepare_context``: the ``<overview>`` block, or ``None``."""
    overview = None
    # --- Phase 5: composition and diagnostics. Pure, except the language read
    # below, which takes one short session of its own. -------------------------
    #
    # The speaker map's own "never a silent zero" case (`map_leg ==
    # "speaker_scope_map_empty"`) has `summaries == []` by construction, so the
    # plain `if summaries:` gate below would skip building an overview
    # entirely — leaving the turn to answer with no explanation of why a
    # speaker-scoped summarize came back with nothing. Building one anyway,
    # for exactly this case, is what makes
    # `mapreduce._empty_speaker_focus_overview`'s explicit note reach the
    # prompt at all.
    if summaries or (map_leg == "speaker_scope_map_empty" and speaker_focus_for_summary):
        from app.services.chat.mapreduce import build_overview

        citation_start = _overview_citation_start(settings, digest_masked, masked)
        overview = build_overview(
            question,
            summaries,
            files_in_scope=len(file_uuids) if file_uuids else 0,
            speaker_focus=speaker_focus_for_summary,
            citation_start=citation_start,
        )
        _finalize_overview_citations(overview, summaries)
        overview.diagnostics.update(
            _overview_diagnostics(summaries, map_coverage, overview, map_leg)
        )
        meta["overview"] = overview.as_metadata()
        # The frontend's pre-existing "Overview source" row: which REDUCER
        # composed the block ("code" | "llm-batch"), already carried inside
        # `overview.as_metadata()["reducer"]` but promoted to a top-level key
        # because `ChatMessageMeta.svelte` reads `meta.map_source` directly,
        # not `meta.overview.reducer`.
        meta["map_source"] = overview.reducer
        # The decoupling provenance this task is actually about: which LEG
        # supplied the summaries the reducer above ran over. Not on the
        # frontend allowlist — a diagnostic for `meta`'s general-purpose bag,
        # same as `chunk_leg_reduced_to` / `chunks_dropped_quarantined` below.
        meta["map_leg"] = map_leg
    return overview
