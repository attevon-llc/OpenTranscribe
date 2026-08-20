"""Chat turn orchestration: retrieval → prompt → stream → persist.

Emits the SSE frames the frontend consumes (``start``, ``status``, ``sources``,
``warning``, ``delta``, ``reasoning``, ``usage``, ``done``, ``error``). The event
names are a frozen contract shared with the frontend implementation.
``reasoning`` is shaped identically to ``delta`` (``{"text": ...}``) but carries a
model's separately streamed reasoning/thinking content, rendered by the frontend
in its own collapsed-by-default block rather than mixed into the answer.

``sources`` is emitted only once the excerpt budget is known, and lists only the
excerpts that actually reached the prompt — see the comment at its yield site.

``warning`` carries a ``code`` naming exactly one way the answer ended up
ungrounded: ``context_dropped`` (excerpts were retrieved and the budget fit none
of them), ``no_context`` (nothing reached masking at all and the search itself
was not the cause — retrieval genuinely matched nothing, or every chunk failed
closed under masking), or ``retrieval_failed`` (the chunk-plane search itself
raised or had no client, so retrieval degraded to empty rather than reporting an
empty library — issue #438's open half, now closed: see
``search/chunk_retrieval.retrieve_chunks``'s ``diagnostics`` out-param). Adding a
code means teaching ``frontend/src/lib/types/chat.ts``'s ``ChatWarningCode`` about
it too.

Threading model: everything except the SSE plumbing is synchronous (OpenSearch,
SQLAlchemy, ``requests``), so the blocking stages run via Starlette's threadpool
and the provider's token stream is bridged with ``iterate_in_threadpool``.

The generator owns its own DB session rather than borrowing the request's: it
outlives the handler's dependency scope, and a stream that is still writing when
the session closes would fail at exactly the moment we most need to persist the
partial answer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncGenerator
from typing import Any

import anyio
from sqlalchemy.orm import Session
from starlette.concurrency import iterate_in_threadpool
from starlette.concurrency import run_in_threadpool

from app.core import constants as C  # noqa: N812
from app.models.chat import ROLE_ASSISTANT
from app.models.chat import STATUS_CANCELLED
from app.models.chat import STATUS_COMPLETE
from app.models.chat import STATUS_ERROR
from app.services.chat import citations as citations_mod
from app.services.chat import limits
from app.services.chat.hooks import ChatCompletionContext
from app.services.chat.hooks import fire_message_complete
from app.services.chat.language import METADATA_KEY as LANGUAGE_METADATA_KEY
from app.services.chat.language import WARNING_CODE as LANGUAGE_WARNING_CODE
from app.services.chat.language import describe_context_languages
from app.services.chat.language import warning_payload as language_warning_payload
from app.services.chat.output_redactor import OutputRedactor
from app.services.chat.prompting import build_messages
from app.services.chat.prompting import format_counted_block
from app.services.chat.redactor import MaskedChunk
from app.services.chat.redactor import mask_chunks
from app.services.chat.retrieval import retrieve_context
from app.services.chat.settings import ChatSettings
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60

# Floor for a reply budget. Small enough to honour "keep answers short", large
# enough that a clamped request still returns a usable answer rather than a
# sentence cut in half.
MIN_ANSWER_TOKENS = 256


def sse(event: str, payload: dict[str, Any]) -> str:
    """Format one SSE frame (same helper shape as the subtitle export stream)."""
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


def resolve_answer_tokens(
    *,
    requested: int | None,
    tenant_ceiling: int | None,
    default_tokens: int,
    context_window: int,
) -> int:
    """Decide how many tokens the reply may use.

    Three limits apply, in order of authority: a per-tenant cap is a hard
    ceiling the user cannot raise; the context window is a physical one; and the
    user's own preference sits underneath both. Clamping rather than rejecting
    is deliberate — providers disagree about oversized ``max_tokens`` (some 400,
    some silently truncate), and a chat should not fail because someone typed an
    ambitious number.

    The window cap is half the context, not all of it: the prompt and history
    have to fit in the same budget, and ``build_messages`` sizes the excerpt
    block against whatever this returns.

    Args:
        requested: The conversation's override, or None to use the default.
        tenant_ceiling: Per-tenant maximum, or None when no tier cap applies.
        default_tokens: The LLM config's own derived reply budget.
        context_window: The model's total context window in tokens.

    Returns:
        A positive token count safe to send as ``max_tokens``.
    """
    tokens = requested if requested is not None else default_tokens
    if tenant_ceiling is not None:
        tokens = min(tokens, tenant_ceiling)
    window_cap = max(MIN_ANSWER_TOKENS, context_window // 2)
    return max(MIN_ANSWER_TOKENS, min(tokens, window_cap))


def _title_from(question: str) -> str:
    """Derive a conversation title from its opening question."""
    clean = " ".join(question.split())
    if len(clean) <= TITLE_MAX_CHARS:
        return clean
    cut = clean[:TITLE_MAX_CHARS]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut + "…"


class ChatTurn:
    """One user message and the assistant reply it produces.

    Holds the mutable state a turn accumulates (answer text, token counts,
    diagnostics) so the streaming generator stays readable and the ``finally``
    block has one place to persist from.
    """

    def __init__(self) -> None:
        self.answer_parts: list[str] = []
        # Separate from answer_parts so the collapsible reasoning block in the UI
        # never gets reasoning tokens mixed into the rendered answer.
        self.reasoning_parts: list[str] = []
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        # Cache tokens are priced differently from ordinary input tokens — reads far
        # below, writes above — so they are carried separately rather than folded in.
        self.cache_read_tokens: int | None = None
        self.cache_write_tokens: int | None = None
        self.tokens_estimated = False
        self.finish_reason: str | None = None
        self.error: str | None = None
        self.error_code: str | None = None
        self.offered_citations: list[dict] = []
        self.metadata: dict[str, Any] = {}
        # Filled in by _finalize_turn so the trailing frames can read them.
        self.total_tokens = 0
        self.title: str | None = None

    @property
    def answer(self) -> str:
        return "".join(self.answer_parts)

    @property
    def reasoning(self) -> str:
        return "".join(self.reasoning_parts)

    def status(self) -> str:
        if self.error:
            return STATUS_ERROR
        if self.finish_reason == "cancelled":
            return STATUS_CANCELLED
        return STATUS_COMPLETE


def _drop_quarantined_hits(db: Session, hits: list[ChunkHit]) -> list[ChunkHit]:
    """Remove hits belonging to quarantined files, chunk **or** digest.

    Quarantine status is not an OpenSearch filter field — it can lag a takedown
    by a reindex — so scope resolution filters it in Postgres
    (``context_resolver._visible_files_query``) whenever a turn is explicitly
    scoped. An **unscoped** turn (``file_uuids is None``) never routes through
    that resolver at all: retrieval goes straight to the index, which still
    returns the quarantined file's chunks and digest sections. This is the
    chat-plane analogue of ``api/endpoints/search.py::_drop_quarantined_search_hits``
    and must run over both planes, since a summarize-routed turn's digest hits
    never pass through the chunk leg.

    ⚠️ **This is NOT the only place a quarantine flag is enforced in the chat
    pipeline, and an earlier revision of this docstring claiming that was
    itself the bug** (W2.0g's adversarial review). This function runs at
    ``_prepare_context`` phase 3.5 and only ever sees ``result.chunks`` /
    ``result.digests`` — the RANKED tiers. The counted/aggregation tier
    (``aggregation_service.answer_aggregation``) runs a whole phase EARLIER, at
    phase 2, and is a separate code path this function never touches:
    ``aggregation_service._accessible_scoped_files`` and
    ``aggregation_service._quarantined_among`` are the enforcement points for
    that tier, on the Postgres and OpenSearch shapes respectively. The
    map-reduce overview's bounded-scope leg (``mapreduce.scope_digest_hits``)
    is a third, independent enforcement point for the same reason — it re-reads
    ``file_facts`` directly rather than routing through this function's output.
    """
    if not hits:
        return hits
    from app.models.media import MediaFile

    uuids = {hit.file_uuid for hit in hits if hit.file_uuid}
    if not uuids:
        return hits
    quarantined = {
        str(row[0])
        for row in db.query(MediaFile.uuid)
        .filter(MediaFile.uuid.in_(uuids), MediaFile.is_quarantined.is_(True))
        .all()
    }
    if not quarantined:
        return hits
    return [hit for hit in hits if hit.file_uuid not in quarantined]


def _resolve_summary_tier(
    *,
    decision,
    file_uuids: list[str] | None,
    settings: ChatSettings,
    session_scope,
    ranked_digests: list[ChunkHit],
    ranked_digests_masked: list[MaskedChunk],
    user_id: int,
    mask_kwargs: dict[str, Any],
) -> tuple[list[Any], list[MaskedChunk], str | None, int]:
    """Decide which leg feeds the map-reduce overview's summaries (W2.1).

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
        ``(summary_hits, summary_masked, map_leg, files_without_artifacts)``.
        ``map_leg`` is ``"scope_map"`` | ``"speaker_scope_map"`` |
        ``"speaker_scope_map_empty"`` | ``"ranked_digests"`` | ``None``
        (nothing to summarise this turn). ``files_without_artifacts`` is 0
        whenever the map did not run or found no gap.
    """
    from app.services.chat.mapreduce import scope_digest_hits
    from app.services.chat.mapreduce import scope_speaker_digest_hits
    from app.services.chat.mapreduce import sections_budget
    from app.services.chat.redactor import mask_digests

    if decision.wants_speaker_digest_map and file_uuids:
        # W2.3: closes the gap `Route.wants_speaker_digest_map` documents. A
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
        if map_hits:
            summary_masked = mask_digests(session_scope, map_hits, user_id, **mask_kwargs)
            return map_hits, summary_masked, "speaker_scope_map", files_without_artifacts
        # Never a silent zero: the caller still composes an overview from this
        # (empty) map when the turn was speaker-scoped — see
        # `mapreduce._empty_speaker_focus_overview` — rather than silently
        # answering with nothing and no explanation.
        return [], [], "speaker_scope_map_empty", files_without_artifacts

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
        if map_hits:
            summary_masked = mask_digests(session_scope, map_hits, user_id, **mask_kwargs)
            return map_hits, summary_masked, "scope_map", files_without_artifacts
        if ranked_digests:
            # The map covered nothing (every file in scope lacks a digest, or
            # the read failed) but the ranked leg still found something —
            # degrade to it rather than reporting no overview at all.
            return ranked_digests, ranked_digests_masked, "ranked_digests", files_without_artifacts
        return [], [], None, files_without_artifacts

    if ranked_digests:
        # Unbounded scope: keep today's ranked-leg-only behaviour. Also
        # reachable for a bounded scope the router did not route to the
        # digest tier at all but which somehow still carries ranked digest
        # hits — the same fallback the pre-decoupling code applied
        # unconditionally to any non-empty `result.digests`.
        return ranked_digests, ranked_digests_masked, "ranked_digests", 0
    return [], [], None, 0


def _resolve_speaker_focus(
    *, question: str, user_id: int, organization_id: int | None, session_scope
):
    """Phase 1.5 of ``_prepare_context`` (W2.2, wired W2.3): a MENTIONED speaker.

    Split out so ``_prepare_context`` stays under ruff's complexity limit —
    the same reason ``_resolve_summary_tier`` documents for its own
    extraction. The caller (``_prepare_context``) is the ONE place that
    decides whether to run this at all, gated on
    ``settings.speaker_resolver_enabled`` — this function does not re-check
    the flag itself.

    Its own short session: the roster read is one bounded query, closing well
    before the LLM round trip / retrieval that follow in the phases below.
    Reads ``question`` as typed, never the rewrite —
    ``speaker_resolver.resolve_speaker_mentions`` is explicit that a rewrite
    can lose or paraphrase a name the original carried.

    Returns:
        A ``SpeakerMentionResolution`` — always, even when nothing matched, so
        the caller has one shape to read ``.as_meta()``/``.speaker_focus`` off.
    """
    from app.services.chat.speaker_resolver import resolve_speaker_mentions

    with session_scope() as db:
        return resolve_speaker_mentions(
            db, question, user_id=user_id, organization_id=organization_id
        )


def _apply_speaker_resolution(
    *,
    settings: ChatSettings,
    question: str,
    user_id: int,
    organization_id: int | None,
    session_scope,
    meta: dict[str, Any],
) -> list[str]:
    """Resolve a mentioned speaker and fold it into ``meta`` (W2.2, wired W2.3).

    A second extraction on top of :func:`_resolve_speaker_focus` — this one
    holds the branching (whether the flag is on, whether anything resolved),
    which is what pushed ``_prepare_context`` over ruff's complexity limit.
    Same reasoning :func:`_resolve_summary_tier` documents for its own split.
    Mutates ``meta`` in place, matching how ``_prepare_context`` already
    threads its own ``meta`` dict through several stages.

    Returns:
        The uniquely matched names when the turn has a resolved speaker
        focus, else ``[]`` — always a list, never ``None``, so the caller
        treats "flag off" and "nothing resolved" identically.
    """
    if not settings.speaker_resolver_enabled:
        return []
    resolution = _resolve_speaker_focus(
        question=question,
        user_id=user_id,
        organization_id=organization_id,
        session_scope=session_scope,
    )
    resolution_meta = resolution.as_meta()
    if resolution_meta:
        meta["speaker_resolution"] = resolution_meta
    return list(resolution.matched) if resolution.speaker_focus else []


def _prepare_context(
    *,
    user_id: int,
    organization_id: int | None,
    question: str,
    history: list[dict[str, str]],
    settings: ChatSettings,
    file_uuids: list[str] | None,
    speakers: list[str] | None,
    search_mode: str,
    llm,
    rewrite_enabled: bool,
) -> tuple[list[MaskedChunk], dict[str, Any], Any, Any]:
    """Run the blocking RAG stages: rewrite → route → count → retrieve → mask.

    Executed in a worker thread, and **phased**. It opens its own database
    sessions rather than accepting one, because the caller's session would then
    be held across the rewrite (an LLM round trip), the counted tier's
    OpenSearch aggregation and retrieval (OpenSearch + cross-encoder + Redis) —
    the shape that queues every ``ALTER TABLE`` behind a chat turn and hangs an
    Alembic upgrade mid-release. ``scripts/audit-session-lifetime.py`` gates it.

    The phases, in order:

    1. rewrite + route — **no session**;
    2. the counted tier — ``answer_aggregation`` takes a *factory* and opens a
       short session per Postgres statement group, so its search runs clean;
    3. retrieval — **no session**;
    4. masking and the scope map — the database phase. ``mask_chunks`` /
       ``mask_digests`` take the *factory* too (#83) and gather-then-close, so
       the inline Presidio fallback never runs inside a transaction; the scope
       map and the summaries get their own short sessions. Everything returned
       is a plain dataclass (``MaskedChunk``/``FileSummary``, never an ORM
       instance: attribute access on a detached row would silently re-open a
       session and undo the split);
    5. overview composition and diagnostics — pure, except one short session for
       the language read.

    Returns the masked chunks, diagnostics for the message metadata (ids/counts/
    timings only, never text), the counted result when the router sent the turn
    to the aggregation tier, and the overview when it asked for a summary.
    """
    from app.core.config import settings as settings_config
    from app.db.session_utils import session_scope

    meta: dict[str, Any] = {}

    # --- Phase 1: rewrite + route. An LLM round trip; NO session. -------------
    effective_query = question
    llm_intent: str | None = None
    if rewrite_enabled and history:
        from app.services.chat.query_rewriter import rewrite_query

        rewrite_started = time.monotonic()
        rewrite = rewrite_query(llm, history, question)
        effective_query = rewrite.query
        llm_intent = rewrite.intent
        if effective_query != question:
            meta["rewritten_query"] = effective_query
        meta.setdefault("timings_ms", {})["rewrite"] = int(
            (time.monotonic() - rewrite_started) * 1000
        )

    # --- Phase 1.5: resolve a speaker MENTIONED in the question text (W2.2,
    # wired W2.3). Gated on `chat.speaker_resolver_enabled`, off by default —
    # a flag-off turn takes none of this branch and stays byte-identical to
    # before it existed: no `meta["speaker_resolution"]` key, `speaker_focus`
    # stays False, `speaker_focus_names` stays empty. Its own short session
    # (see `_resolve_speaker_focus`).
    speaker_focus_names = _apply_speaker_resolution(
        settings=settings,
        question=question,
        user_id=user_id,
        organization_id=organization_id,
        session_scope=session_scope,
        meta=meta,
    )

    from app.services.chat.router import route

    decision = route(
        question,
        rewritten=effective_query if effective_query != question else None,
        llm_intent=llm_intent,
        speakers=speakers,
        speaker_focus=bool(speaker_focus_names),
    )
    meta["route"] = decision.as_metadata()

    # --- Phase 2: the counted tier. Postgres and OpenSearch, INTERLEAVED but
    # never overlapping: `answer_aggregation` takes the factory below and opens a
    # short session per statement group, so its `size: 0` search never inherits a
    # transaction.
    #
    # It runs BEFORE retrieval and is independent of it: "how many meetings
    # mention X" is answered by an aggregation over the whole library, and the
    # excerpts that follow are examples beside that number, never the thing it
    # was derived from. ROUTE, DON'T FUSE — two queries, combined here.
    counted = None
    overview = None
    if decision.wants_aggregate:
        from app.services.chat.aggregation_service import answer_aggregation
        from app.services.opensearch_service import get_opensearch_client

        counted_started = time.monotonic()
        counted = answer_aggregation(
            question,
            decision,
            session_factory=session_scope,
            client=get_opensearch_client(),
            index=settings_config.OPENSEARCH_CHUNKS_INDEX,
            user_id=user_id,
            organization_id=organization_id,
            file_uuids=file_uuids,
        )
        meta.setdefault("timings_ms", {})["aggregate"] = int(
            (time.monotonic() - counted_started) * 1000
        )
        # Recorded either way. "The router said aggregate and the mechanism
        # declined" is a different fact from "it was never an aggregation", and
        # only the metadata can tell them apart after the fact.
        meta["aggregation"] = counted.as_metadata() if counted is not None else {"declined": True}

    # A counted turn still retrieves — a reduced leg, so a misroute always has
    # evidence and every `[n]` marker still resolves to a clickable timestamp.
    # The plan's ratio: a third of the usual excerpts, because the number above
    # them is the answer and the excerpts are illustration.
    retrieval_settings = settings
    if counted is not None:
        from dataclasses import replace as _replace

        retrieval_settings = _replace(settings, final_chunks=max(1, settings.final_chunks // 3))
        meta["chunk_leg_reduced_to"] = retrieval_settings.final_chunks

    # --- Phase 3: retrieval. OpenSearch, the cross-encoder and Redis; the
    # slowest stage of the turn, and NO session. `file_uuids` is passed through
    # exactly as it arrived: `None` means "all accessible", `[]` means "match
    # nothing", and substituting one for the other leaks the whole library.
    result = retrieve_context(
        query=effective_query,
        user_id=user_id,
        organization_id=organization_id,
        file_uuids=file_uuids,
        speakers=speakers,
        settings=retrieval_settings,
        search_mode=search_mode,
        wants_digest=decision.wants_digest,
        speaker_focus_names=speaker_focus_names or None,
    )

    # --- Phase 3.5: drop quarantined hits before anything downstream sees
    # them (its own short session). See `_drop_quarantined_hits` for what this
    # does and does NOT cover — retrieval itself has no quarantine predicate,
    # so this is the enforcement point for the RANKED tiers of an unscoped turn.
    chunks_before = len(result.chunks)
    with session_scope() as db:
        result.chunks = _drop_quarantined_hits(db, result.chunks)
        result.digests = _drop_quarantined_hits(db, result.digests)
    # `result.retrieved` feeds the `no_context` warning's contract (documented
    # at the yield site below and in `services/chat/CLAUDE.md`): non-zero means
    # masking failed closed on every chunk. A quarantined chunk never reaches
    # masking at all, so leaving it counted in `retrieved` made a quarantine
    # drop impersonate a masking failure — `{"code": "no_context", "retrieved":
    # 3}` beside `chunks_dropped_empty_after_masking == 0`, two diagnostics that
    # contradicted each other and pointed at the wrong subsystem. Decrementing
    # keeps `retrieved` meaning "chunks that could have reached masking", and
    # the drop still has its own named counter so a quarantine cause is never
    # read as a bare "search returned less".
    chunks_dropped_quarantined = chunks_before - len(result.chunks)
    if chunks_dropped_quarantined:
        result.retrieved = max(0, result.retrieved - chunks_dropped_quarantined)
        meta["chunks_dropped_quarantined"] = chunks_dropped_quarantined

    # --- Phase 4: the database phase. Every session here is SHORT and every
    # value that leaves it is a plain dataclass — an ORM instance would re-open a
    # session on the first attribute read afterwards and quietly undo the split.
    #
    # The maskers take the FACTORY, not a session (#83): each one gathers its
    # cached spans, closes, and only then runs the detector. A file whose scan
    # cannot be trusted falls through to inline Presidio, and a cold analyzer
    # build is ~10 s — measured at 13.9 s `idle in transaction` when it ran
    # inside this phase's session.
    #
    # `unmask_for_local` keys egress masking off WHERE THE MODEL RUNS (owner
    # decision, 2026-08-13): a local model never has this text leave the
    # machine, so masking it costs recall for no egress benefit. Resolved ONCE
    # here (pure — reads only `llm.config`, no I/O) and threaded to every
    # masker call below rather than re-derived per call, so the three sites
    # cannot disagree about which provider this turn is actually using.
    #
    # `llm` is None for a legitimate, first-class deployment shape (#403 D6):
    # no LLM_PROVIDER configured at all still has to produce masked context for
    # the deterministic maps/keyphrase/coverage tiers, and that path never had
    # an `llm` to read a provider from. `getattr(llm, "config", None)` reaches
    # `is_local_provider`'s own None-safe duck typing (`getattr(config,
    # "provider", "")` on a None config is `""`, which resolves to "not a known
    # local provider") instead of a SECOND None check here — one fail-closed
    # decision, not two that could disagree.
    #
    # `_mask_kwargs` only includes the keyword when it is True. Every existing
    # caller of `mask_chunks`/`mask_digests` in this codebase — and every test
    # that stubs either with a positional-only signature — predates this
    # parameter; passing it unconditionally would break every one of them for
    # the overwhelmingly common case (remote provider or no provider at all)
    # where it does nothing anyway. Only a genuinely local provider needs the
    # keyword sent at all.
    from app.services.redaction.llm_guard import is_local_provider

    unmask_for_local = is_local_provider(getattr(llm, "config", None))
    _mask_kwargs = {"unmask_for_local": True} if unmask_for_local else {}

    digest_masked: list[MaskedChunk] = []
    summaries: list[Any] = []
    masked = mask_chunks(session_scope, result.chunks, user_id, **_mask_kwargs)

    if result.digests:
        # A SEPARATE masking call, not an overload. A digest is
        # non-contiguous selected sentences; `mask_chunks` would rebuild it
        # from every segment overlapping its time range and hand back the
        # whole span verbatim — more text than the digest holds, from a
        # function whose name says it masked it. `mask_digests` goes through
        # the per-sentence provenance.
        from app.services.chat.redactor import mask_digests

        digest_masked = mask_digests(session_scope, result.digests, user_id, **_mask_kwargs)

    # W2.1: which leg feeds the overview — the scope map (bounded scope, reads
    # `file_facts` for every file) or the ranked digest leg above (unbounded
    # scope, or the map covered nothing). See `_resolve_summary_tier`'s own
    # docstring for the decoupling this replaces.
    summary_hits, summary_masked, map_leg, files_without_artifacts = _resolve_summary_tier(
        decision=decision,
        file_uuids=file_uuids,
        settings=settings,
        session_scope=session_scope,
        ranked_digests=result.digests,
        ranked_digests_masked=digest_masked,
        user_id=user_id,
        mask_kwargs=_mask_kwargs,
    )
    if files_without_artifacts:
        meta["map_files_without_artifacts"] = files_without_artifacts

    # W2.3. Single-speaker focus only — same simplification
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

        overview = build_overview(
            question,
            summaries,
            files_in_scope=len(file_uuids) if file_uuids else 0,
            speaker_focus=speaker_focus_for_summary,
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
    if digest_masked:
        # Digests lead: they are the recording-level answer a summarize turn
        # asked for, and the chunk excerpts under them are the evidence. This
        # is independent of whether the OVERVIEW came from the scope map or
        # the ranked leg — the ranked digest hits are still evidence chunks
        # either way.
        masked = digest_masked + masked
    if result.digests:
        meta["digests_retrieved"] = len(result.digests)

    kept = [chunk for chunk in masked if chunk.content.strip()]
    # Masking fails CLOSED: an unmaskable chunk becomes "" and contributes
    # nothing. Without this counter that is indistinguishable from retrieval
    # returning less, which is a different defect with a different fix.
    meta["chunks_dropped_empty_after_masking"] = len(masked) - len(kept)
    masked = kept

    # RAG is English-only (see services/chat/language.py). Recording what the turn
    # could draw on is what makes that limit visible instead of silent.
    #
    # Its own short session, and it must stay one: this read used to be handed the
    # phase-4 session AFTER that `with` block had closed it, which SQLAlchemy
    # answers by silently opening a fresh transaction on a connection nothing ever
    # returns — an `idle in transaction` backend left behind by every turn, freed
    # only when the garbage collector reaches the connection.
    with session_scope() as db:
        languages = describe_context_languages(
            db,
            scope_file_uuids=file_uuids,
            grounded_file_uuids=[chunk.source.file_uuid for chunk in masked],
        )
    if languages.total_files:
        meta[LANGUAGE_METADATA_KEY] = languages.as_metadata()
    if languages.has_unsupported:
        meta[LANGUAGE_WARNING_CODE] = True

    meta["retrieved"] = result.retrieved
    meta["reranked"] = result.reranked
    meta["cache_hit"] = result.cache_hit
    # Only when true: a key most turns never carry keeps `msg_metadata` from
    # growing a permanent "search worked fine" marker on every ordinary turn.
    if result.retrieval_failed:
        meta["retrieval_failed"] = True
    meta["files_searched"] = "all" if file_uuids is None else len(file_uuids)
    if speakers:
        meta["speakers_filtered"] = list(speakers)
    timings = meta.setdefault("timings_ms", {})
    timings.update(result.timings_ms)
    return masked, meta, counted, overview


# SSE comment lines are ignored by every client but keep the connection warm.
# Without them, retrieval (OpenSearch + cross-encoder + masking) and then the
# wait for a first token can put ZERO bytes on the wire for well over nginx's
# default 60s proxy_read_timeout, and the proxy closes the stream mid-answer.
_KEEPALIVE = ": keepalive\n\n"
_KEEPALIVE_INTERVAL_S = 15


async def _with_keepalive(awaitable, queue: asyncio.Queue):
    """Await ``awaitable``, pushing a keepalive frame into ``queue`` every 15s."""
    task = asyncio.ensure_future(awaitable)
    while True:
        done, _ = await asyncio.wait({task}, timeout=_KEEPALIVE_INTERVAL_S)
        if done:
            return task.result()
        queue.put_nowait(_KEEPALIVE)


async def _drain(queue: asyncio.Queue):
    """Yield everything buffered in ``queue`` without blocking."""
    while not queue.empty():
        yield queue.get_nowait()


def _resolve_output_policy(user_id: int):
    """The requesting user's effective redaction config, or None if unresolvable.

    Its own short-lived session: this runs on every turn including
    ``use_context=False`` ones, which never open the retrieval session at all.
    ``None`` is not "no redaction" — ``OutputRedactor`` reads it as "mask
    everything", because being unable to resolve the policy must not mean
    sending generated text out unexamined.
    """
    from app.db.session_utils import session_scope
    from app.services.redaction.config import resolve_effective_config

    try:
        with session_scope() as db:
            return resolve_effective_config(db, user_id)
    except Exception:  # noqa: BLE001 — the redactor fails closed on None
        logger.exception("Could not resolve the output redaction policy for user %s", user_id)
        return None


async def _redact_delta(redactor: OutputRedactor, text: str) -> str:
    """Buffer one generated delta; return what is safe to put on the wire now.

    Returns ``""`` while a sentence is still arriving. Detection is CPU-bound
    (Presidio), so it goes to a thread — buffering itself is string work and
    stays on the loop.
    """
    if not redactor.active:
        return text
    span = redactor.buffer(text)
    if not span:
        return ""
    # Annotated: run_in_threadpool is typed as returning Any, and returning it
    # straight out of a str-declared function trips no-any-return.
    # OutputRedactor.mask genuinely returns str.
    masked: str = await run_in_threadpool(redactor.mask, span)
    return masked


def _record_output_redaction(
    turn: ChatTurn, answer: OutputRedactor | None, reasoning: OutputRedactor | None
) -> None:
    """Stamp what output redaction did on the turn's metadata.

    Only when it was active, so a deployment with redaction off carries no new
    key. ``withheld_spans`` is the one that matters: a sentence replaced by the
    failsafe placeholder is otherwise indistinguishable from a short answer.
    """
    if answer is None or not answer.active:
        return
    withheld = answer.withheld_spans + (reasoning.withheld_spans if reasoning else 0)
    turn.metadata["output_redaction"] = {
        "masked_spans": answer.masked_spans + (reasoning.masked_spans if reasoning else 0),
        "withheld_spans": withheld,
    }
    turn.metadata.setdefault("timings_ms", {})["output_redaction"] = answer.mask_ms + (
        reasoning.mask_ms if reasoning else 0
    )
    if withheld:
        logger.warning(
            "Chat output redaction withheld %d generated span(s): detectors were "
            "unavailable for an enabled category, so the text was replaced rather than sent",
            withheld,
        )


async def _flush_redactor(redactor: OutputRedactor | None) -> str:
    """Mask and return the unemitted tail. Idempotent — later calls return ``""``."""
    if redactor is None:
        return ""
    tail = redactor.drain()
    if not tail:
        return ""
    if not redactor.active:
        return tail
    masked_tail: str = await run_in_threadpool(redactor.mask, tail)
    return masked_tail


async def _finalize_turn(
    *,
    turn: ChatTurn,
    llm,
    messages: list[dict[str, str]],
    masked_count: int,
    conversation_id: int,
    conversation_uuid: str,
    assistant_message_uuid: str,
    user_id: int,
    organization_id: int | None,
    is_first_exchange: bool,
    question: str,
    started: float,
    use_context: bool,
) -> None:
    """Persist, meter and audit one turn. Safe to call during teardown.

    Called from the streaming generator's ``finally`` so a cancelled or
    disconnected stream still records what it produced. Every step is
    individually contained: a failure here must never propagate into the
    teardown path and mask the original exception.
    """
    # Token fallback for providers that report nothing (notably "custom"
    # OpenAI-clones, which can't be sent stream_options.include_usage).
    if turn.prompt_tokens is None and turn.completion_tokens is None:
        try:
            turn.prompt_tokens = llm.estimate_tokens("".join(m["content"] for m in messages))
            # Reasoning counts, even though it is not the answer (issue #439). The
            # model generated and was billed for those tokens; separating them out
            # of `answer` so they stop rendering must not also delete them from the
            # meter. Providers that report usage already include them.
            turn.completion_tokens = llm.estimate_tokens(turn.answer + turn.reasoning)
            turn.tokens_estimated = True
        except Exception:  # noqa: BLE001
            logger.exception("Token estimation failed")

    turn.total_tokens = (turn.prompt_tokens or 0) + (turn.completion_tokens or 0)
    turn.metadata.setdefault("timings_ms", {})["total"] = int((time.monotonic() - started) * 1000)
    used_citations = citations_mod.extract_used_citations(turn.answer, turn.offered_citations)

    try:
        turn.title = await run_in_threadpool(
            _persist_reply,
            conversation_id,
            assistant_message_uuid,
            turn,
            used_citations,
            turn.total_tokens,
            llm,
            is_first_exchange,
            question,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to persist chat reply %s", assistant_message_uuid)

    limits.clear_cancel(assistant_message_uuid)

    try:
        fire_message_complete(
            ChatCompletionContext(
                conversation_uuid=conversation_uuid,
                message_uuid=assistant_message_uuid,
                user_id=user_id,
                organization_id=organization_id,
                provider=str(llm.config.provider.value),
                model=str(llm.config.model),
                prompt_tokens=turn.prompt_tokens or 0,
                completion_tokens=turn.completion_tokens or 0,
                total_tokens=turn.total_tokens,
                tokens_estimated=turn.tokens_estimated,
                retrieved_chunks=masked_count,
                success=not turn.error,
                cache_read_tokens=turn.cache_read_tokens or 0,
                cache_write_tokens=turn.cache_write_tokens or 0,
                use_context=use_context,
            )
        )
    except Exception:  # noqa: BLE001 — hooks are contained by contract
        logger.exception("Chat completion hook raised past its guard")

    _audit_message(
        user_id=user_id,
        organization_id=organization_id,
        conversation_uuid=conversation_uuid,
        message_uuid=assistant_message_uuid,
        llm=llm,
        turn=turn,
        chunk_count=masked_count,
        total_tokens=turn.total_tokens,
    )


class ChatService:
    """Streams one assistant reply and persists both sides of the exchange."""

    @staticmethod
    async def stream_reply(  # noqa: C901 — a linear pipeline reads better whole
        *,
        conversation_id: int,
        conversation_uuid: str,
        user_id: int,
        organization_id: int | None,
        question: str,
        history: list[dict[str, str]],
        file_uuids: list[str] | None,
        speakers: list[str] | None,
        scope_files_dropped: int = 0,
        settings: ChatSettings,
        use_context: bool,
        system_prompt: str,
        search_mode: str,
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        enable_thinking: bool | None = None,
        llm,
        on_teardown=None,
        assistant_message_uuid: str,
        user_message_uuid: str,
        is_first_exchange: bool,
    ) -> AsyncGenerator[str, None]:
        """Yield SSE frames for one turn.

        Args:
            conversation_id: Owning conversation's primary key.
            conversation_uuid: Public id echoed in the ``start`` frame.
            user_id: Message author (also the redaction-policy subject).
            organization_id: Tenant stamp for metering and audit.
            question: The user's message (already persisted by the endpoint).
            history: Prior turns, oldest first.
            file_uuids: Resolved scope; None = all accessible transcripts.
            speakers: Restrict retrieval to these speakers' turns.
            scope_files_dropped: How many of the caller's EXPLICIT file picks
                (never collections/tags) `resolve_scope_file_uuids` could not
                resolve into `file_uuids` — inaccessible, deleted, or
                quarantined. Stamped onto `msg_metadata` so a picker offering
                files the scope then silently discards (most visible for an
                admin, whose picker is tenant-wide while scope resolution has
                no admin bypass) is visible in the reply rather than only in
                a smaller `files_searched`.
            settings: Admin-tuned RAG knobs.
            use_context: False skips retrieval entirely (pure-LLM chat).
            system_prompt: Fully layered system prompt.
            search_mode: Retrieval mode for this turn.
            temperature: Per-conversation override, or None for the config default.
            max_tokens: Per-conversation answer-length override, clamped below.
            top_p: Per-conversation nucleus-sampling override, omitted when None.
            enable_thinking: ``False`` sends the measured "reasoning off" arm;
                ``None`` (the default) builds the payload exactly as it is built
                today, keeping vLLM's issue-#439 activation intact. Already
                gated on the model's measured capability by the endpoint --
                never derive it from a raw user preference here.
            on_teardown: Called exactly once inside the shielded finally, however
                the turn ends. Used to release the concurrency slot: a wrapping
                generator's own ``finally`` does NOT reliably run when Starlette
                tears this one down on client disconnect, so a slot released
                there leaks on every Stop.
            llm: The caller's ``LLMService``.
            assistant_message_uuid: Pre-allocated id for the reply.
            user_message_uuid: Id of the already-persisted user message.
            is_first_exchange: Whether to derive a title from this question.

        Yields:
            SSE frame strings.
        """
        turn = ChatTurn()
        if scope_files_dropped:
            turn.metadata["scope_files_dropped"] = scope_files_dropped
        keepalive_q: asyncio.Queue = asyncio.Queue()
        cancel_event = threading.Event()
        started = time.monotonic()

        yield sse(
            "start",
            {
                "conversation_uuid": conversation_uuid,
                "user_message_uuid": user_message_uuid,
                "assistant_message_uuid": assistant_message_uuid,
            },
        )

        masked: list[MaskedChunk] = []
        messages: list[dict[str, str]] = []
        # A counted answer is not "context" in the excerpt sense: it survives an
        # empty retrieval, so it is initialised here and not inside the branch.
        counted_block = ""
        overview_block = ""
        reached_end = False
        # Declared out here because the shielded `finally` flushes them: a turn
        # torn down mid-sentence still has to persist the buffered tail.
        answer_redactor: OutputRedactor | None = None
        reasoning_redactor: OutputRedactor | None = None
        try:
            if use_context:
                # Query rewriting is a separate LLM round trip, so it is worth
                # its own stage. Retrieval and reranking happen inside one
                # threadpool call and are reported together as "retrieving" —
                # the frontend renders every pre-generation stage with the same
                # label anyway, so splitting them further would add machinery
                # for no visible difference.
                will_rewrite = settings.query_rewrite_enabled and bool(history)
                yield sse("status", {"stage": "rewriting" if will_rewrite else "retrieving"})

                # No `session_scope` here on purpose: `_prepare_context` is
                # phased and opens its own short sessions. Wrapping it would put
                # the turn back to holding one transaction across the rewrite,
                # the counted tier's aggregation and retrieval.
                def _prep():
                    return _prepare_context(
                        user_id=user_id,
                        organization_id=organization_id,
                        question=question,
                        history=history,
                        settings=settings,
                        file_uuids=file_uuids,
                        speakers=speakers,
                        search_mode=search_mode,
                        llm=llm,
                        rewrite_enabled=settings.query_rewrite_enabled,
                    )

                masked, meta, counted, overview = await _with_keepalive(
                    run_in_threadpool(_prep), keepalive_q
                )
                async for frame in _drain(keepalive_q):
                    yield frame
                turn.metadata.update(meta)
                counted_block = format_counted_block(counted)
                overview_block = overview.block if overview is not None else ""

                # W2.2/W2.3: a speaker mention that matched more than one
                # roster entry resolves to NO filter (never a guess) — surface
                # the candidates so the user can disambiguate, exactly like
                # `context_dropped`/language warn elsewhere in this turn.
                ambiguous_speakers = (meta.get("speaker_resolution") or {}).get("ambiguous") or []
                if ambiguous_speakers:
                    yield sse(
                        "warning",
                        {"code": "ambiguous_speaker", "candidates": list(ambiguous_speakers)},
                    )

            # Resolve the answer budget BEFORE building the prompt: build_messages
            # reserves context for the reply, so raising max_tokens after the fact
            # would let prompt + answer overrun the window.
            answer_tokens = resolve_answer_tokens(
                requested=max_tokens,
                tenant_ceiling=settings.max_output_tokens,
                default_tokens=llm.response_tokens,
                context_window=llm.user_context_window,
            )

            # The prompt is assembled BEFORE the `sources` frame goes out, and the
            # frame carries only the excerpts that survived the budget. Emitting
            # it earlier meant the UI could render clickable citations for
            # excerpts the model was never given — an answer that looks sourced
            # but is not grounded in the cited material (issue #384).
            prompt_diagnostics: dict[str, int] = {}
            messages, excerpt_ids = build_messages(
                system_prompt=system_prompt,
                chunks=masked,
                history=history,
                question=question,
                context_window=llm.user_context_window,
                response_tokens=answer_tokens,
                max_history_turns=settings.history_max_turns,
                diagnostics=prompt_diagnostics,
                counted_block=counted_block,
                overview_block=overview_block,
            )
            chunks_used = len(excerpt_ids)
            turn.metadata["chunks_used"] = chunks_used
            turn.metadata.update(prompt_diagnostics)

            if use_context:
                turn.offered_citations = citations_mod.build_offered_citations(masked, excerpt_ids)
                yield sse("sources", {"citations": turn.offered_citations})

                if not masked:
                    # Nothing reached the prompt at all. The model will answer "I
                    # don't have enough information", which is indistinguishable
                    # from a grounded negative — and, worse, from a working search
                    # that simply found nothing. Retrieval degrades to an empty
                    # list on ANY failure (issue #438: an OpenSearch 503 during a
                    # reindex produced a confident "I don't know" over a corpus
                    # full of matching material), so the counters are the only
                    # evidence the user can be shown. `retrieved` separates the
                    # remaining two: 0 means the search returned nothing,
                    # non-zero means masking failed closed on every chunk.
                    #
                    # `retrieval_failed` is a THIRD, more specific fact
                    # `_prepare_context` now threads out of `retrieve_chunks`'s
                    # `diagnostics` param: the search backend itself raised or was
                    # unreachable, rather than genuinely returning zero hits. That
                    # is worth its own code — "your library has nothing about
                    # this" and "search was down" call for different next steps —
                    # so the two are mutually exclusive branches here, exactly
                    # like `context_dropped`/`no_context` below.
                    retrieval_failed = bool(turn.metadata.get("retrieval_failed"))
                    code = "retrieval_failed" if retrieval_failed else "no_context"
                    turn.metadata[code] = True
                    logger.warning(
                        "Chat turn %s answered with NO excerpts (%s): retrieved=%s, "
                        "files_searched=%s — the reply is ungrounded",
                        assistant_message_uuid,
                        code,
                        turn.metadata.get("retrieved", 0),
                        turn.metadata.get("files_searched", 0),
                    )
                    yield sse(
                        "warning",
                        {
                            "code": code,
                            "retrieved": turn.metadata.get("retrieved", 0),
                            "files_searched": turn.metadata.get("files_searched", 0),
                        },
                    )
                elif not excerpt_ids:
                    # Retrieval found material and the budget left no room for any
                    # of it. Say so rather than answering ungrounded behind a
                    # normal-looking reply.
                    turn.metadata["context_dropped"] = True
                    logger.warning(
                        "Chat turn %s dropped all %d retrieved excerpts: no room in "
                        "the %d-token context window after %d reserved answer tokens",
                        assistant_message_uuid,
                        len(masked),
                        llm.user_context_window,
                        answer_tokens,
                    )
                    yield sse(
                        "warning",
                        {"code": "context_dropped", "retrieved": len(masked)},
                    )

                # Answering anyway is deliberate: a library is usually mixed, and
                # refusing every question because one recording is Spanish would
                # be worse than useless. The turn says what it could not serve
                # well and answers from the rest.
                language_warning = language_warning_payload(turn.metadata)
                if language_warning is not None:
                    logger.info(
                        "Chat turn %s spans unsupported RAG languages %s "
                        "(%d files, %d of unknown language)",
                        assistant_message_uuid,
                        language_warning["languages"],
                        language_warning["files"],
                        language_warning["unknown_files"],
                    )
                    yield sse("warning", language_warning)

            # Redaction of the model's OWN words. Offset masking covers the text
            # we gave it; nothing covered the text it writes about that material
            # until this. Resolved here rather than inside `_prepare_context`
            # because a `use_context=False` turn never runs that stage and its
            # answer is just as visible — which is also why it sits OUTSIDE the
            # use_context block above, unlike the language warning.
            output_policy = await run_in_threadpool(_resolve_output_policy, user_id)
            answer_redactor = OutputRedactor(output_policy)
            reasoning_redactor = OutputRedactor(output_policy)

            yield sse("status", {"stage": "generating"})

            kwargs: dict[str, Any] = {"max_tokens": answer_tokens}
            if temperature is not None:
                kwargs["temperature"] = temperature
            # Omitted entirely when unset: not every provider accepts top_p, and
            # sending a "default" would override a provider-side tuned value.
            if top_p is not None:
                kwargs["top_p"] = top_p
            # Only ever narrows: None leaves the provider payload untouched.
            if enable_thinking is not None:
                kwargs["enable_thinking"] = enable_thinking

            first_token_deadline = time.monotonic() + C.DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S
            got_first_token = False

            stream = llm.chat_completion_stream(messages, cancel_event=cancel_event, **kwargs)
            last_beat = time.monotonic()
            async for event in iterate_in_threadpool(stream):
                # Long gaps between tokens (or before the first) must not look
                # like a dead connection to an intermediary.
                if time.monotonic() - last_beat >= _KEEPALIVE_INTERVAL_S:
                    last_beat = time.monotonic()
                    yield _KEEPALIVE
                # A user Stop may arrive over a separate connection.
                if not cancel_event.is_set() and limits.is_cancelled(assistant_message_uuid):
                    cancel_event.set()

                if event.type == "delta":
                    # The watchdog below measures the PROVIDER's first token, so
                    # it is satisfied here — before the redactor may hold this
                    # delta back. Keying it off emission instead would read a
                    # buffered first sentence as a stalled model and kill it.
                    if not got_first_token:
                        got_first_token = True
                        turn.metadata.setdefault("timings_ms", {})["first_token"] = int(
                            (time.monotonic() - started) * 1000
                        )
                    safe = await _redact_delta(answer_redactor, event.text)
                    if safe:
                        turn.answer_parts.append(safe)
                        yield sse("delta", {"text": safe})
                elif event.type == "reasoning":
                    # The model IS actively responding once reasoning starts, so this
                    # satisfies the first-token watchdog exactly like an answer delta —
                    # otherwise a reasoning-heavy response looks "stalled" and gets
                    # killed by the timeout below while tokens are visibly arriving.
                    if not got_first_token:
                        got_first_token = True
                        turn.metadata.setdefault("timings_ms", {})["first_token"] = int(
                            (time.monotonic() - started) * 1000
                        )
                    # Reasoning is rendered too (a collapsed block, not a hidden
                    # one), so it is a display surface and gets its own buffer.
                    safe = await _redact_delta(reasoning_redactor, event.text)
                    if safe:
                        turn.reasoning_parts.append(safe)
                        yield sse("reasoning", {"text": safe})
                elif event.type == "usage":
                    turn.prompt_tokens = event.prompt_tokens
                    turn.completion_tokens = event.completion_tokens
                    turn.cache_read_tokens = event.cache_read_tokens
                    turn.cache_write_tokens = event.cache_write_tokens
                elif event.type == "error":
                    turn.error = event.message
                    turn.error_code = "provider_error"
                    yield sse("error", {"code": "provider_error", "message": event.message})
                elif event.type == "done":
                    turn.finish_reason = event.finish_reason

                if not got_first_token and time.monotonic() > first_token_deadline:
                    cancel_event.set()
                    turn.error = "The model did not start responding in time."
                    turn.error_code = "timeout"
                    yield sse("error", {"code": "timeout", "message": turn.error})
                    break

            # The last sentence usually has no trailing whitespace to prove it
            # ended, so it is still in the buffer. Flushing here (rather than
            # only in the shielded teardown) is what puts it on the wire; the
            # teardown flush is idempotent and only covers a torn-down turn.
            tail = await _flush_redactor(answer_redactor)
            if tail:
                turn.answer_parts.append(tail)
                yield sse("delta", {"text": tail})
            reasoning_tail = await _flush_redactor(reasoning_redactor)
            if reasoning_tail:
                turn.reasoning_parts.append(reasoning_tail)
                yield sse("reasoning", {"text": reasoning_tail})

        except Exception as exc:  # noqa: BLE001 — surface as a frame, never a 500
            logger.exception("Chat stream failed for conversation %s", conversation_uuid)
            turn.error = str(exc)
            turn.error_code = "provider_error"
            yield sse("error", {"code": "provider_error", "message": "Generation failed."})
        else:
            reached_end = True
        finally:
            cancel_event.set()

            # Persistence, metering and audit MUST run here, not after the try.
            #
            # A client disconnect (closed tab, navigation, or the Stop button —
            # which aborts the fetch before its cancel POST lands) makes Starlette
            # cancel this task, delivering GeneratorExit/CancelledError at a
            # `yield`. Both are BaseException, so `except Exception` above does not
            # catch them and any code placed AFTER the try block would simply never
            # run — silently losing the partial answer, skipping the cloud metering
            # hook (letting a user consume provider tokens unbilled by aborting
            # just before completion), and dropping the audit record.
            #
            # The cancel scope is shielded so the DB write survives the very
            # cancellation that triggered it. It contains no `yield`, which is what
            # makes awaiting here legal during generator teardown.
            if not reached_end and not turn.error:
                # Torn down mid-stream: record it as the cancellation it is.
                turn.finish_reason = "cancelled"

            with anyio.CancelScope(shield=True):
                # A turn torn down mid-sentence still holds a buffered tail. It
                # cannot be yielded (there is no client left, and this scope
                # contains no `yield` by design) but it IS part of the answer,
                # so it is masked and persisted — the user sees it on reload.
                # `drain()` is idempotent, so a normal exit does not duplicate it.
                try:
                    tail = await _flush_redactor(answer_redactor)
                    if tail:
                        turn.answer_parts.append(tail)
                    reasoning_tail = await _flush_redactor(reasoning_redactor)
                    if reasoning_tail:
                        turn.reasoning_parts.append(reasoning_tail)
                except Exception:  # noqa: BLE001 — never mask the real outcome
                    logger.exception("Chat output redaction teardown flush failed")
                _record_output_redaction(turn, answer_redactor, reasoning_redactor)

                try:
                    await _finalize_turn(
                        turn=turn,
                        llm=llm,
                        messages=messages,
                        masked_count=len(masked),
                        conversation_id=conversation_id,
                        conversation_uuid=conversation_uuid,
                        assistant_message_uuid=assistant_message_uuid,
                        user_id=user_id,
                        organization_id=organization_id,
                        is_first_exchange=is_first_exchange,
                        question=question,
                        started=started,
                        use_context=use_context,
                    )
                finally:
                    # Its OWN finally: finalisation can raise (the conversation may
                    # have been deleted underneath a disconnected stream), and a
                    # release placed after it would then never run — leaking the
                    # slot exactly in the case this hook exists to cover.
                    if on_teardown is not None:
                        try:
                            on_teardown()
                        except Exception:  # noqa: BLE001 — never mask the real outcome
                            logger.exception("Chat stream teardown hook failed")

        # Only reachable on a normal (non-cancelled) exit — there is no client
        # left to receive these frames otherwise.
        if not turn.error:
            yield sse(
                "usage",
                {
                    "prompt_tokens": turn.prompt_tokens or 0,
                    "completion_tokens": turn.completion_tokens or 0,
                    "total_tokens": turn.total_tokens,
                    "estimated": turn.tokens_estimated,
                },
            )

        done_payload: dict[str, Any] = {"finish_reason": turn.finish_reason or "stop"}
        if turn.title:
            done_payload["title"] = turn.title
        yield sse("done", done_payload)


def _persist_reply(
    conversation_id: int,
    assistant_message_uuid: str,
    turn: ChatTurn,
    used_citations: list[dict],
    total_tokens: int,
    llm,
    is_first_exchange: bool,
    question: str,
) -> str | None:
    """Write the assistant message and bump the conversation. Returns a new title, if set."""
    import uuid as uuid_pkg
    from datetime import UTC
    from datetime import datetime

    from app.db.session_utils import session_scope
    from app.models.chat import ChatConversation
    from app.models.chat import ChatMessage

    title: str | None = None
    with session_scope() as db:
        message = ChatMessage(
            uuid=uuid_pkg.UUID(assistant_message_uuid),
            conversation_id=conversation_id,
            role=ROLE_ASSISTANT,
            content=turn.answer,
            reasoning_content=turn.reasoning or None,
            citations=used_citations or None,
            msg_metadata=turn.metadata or None,
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            total_tokens=total_tokens,
            tokens_estimated=turn.tokens_estimated,
            provider=str(llm.config.provider.value),
            model=str(llm.config.model),
            status=turn.status(),
            error=turn.error,
        )
        db.add(message)

        conversation = (
            db.query(ChatConversation).filter(ChatConversation.id == conversation_id).first()
        )
        if conversation is not None:
            conversation.last_message_at = datetime.now(UTC)
            if is_first_exchange and not conversation.title:
                title = _title_from(question)
                conversation.title = title
    return title


def _audit_message(
    *,
    user_id: int,
    organization_id: int | None,
    conversation_uuid: str,
    message_uuid: str,
    llm,
    turn: ChatTurn,
    chunk_count: int,
    total_tokens: int,
) -> None:
    """Record the exchange on the compliance trail — metadata only, never content."""
    try:
        from app.auth.audit import AuditEventType
        from app.auth.audit import AuditOutcome
        from app.auth.audit import audit_logger

        audit_logger.log(
            event_type=AuditEventType.CHAT_MESSAGE_SEND,
            outcome=AuditOutcome.FAILURE if turn.error else AuditOutcome.SUCCESS,
            user_id=user_id,
            organization_id=organization_id,
            error_code=turn.error_code,
            details={
                "conversation_uuid": conversation_uuid,
                "message_uuid": message_uuid,
                "provider": str(llm.config.provider.value),
                "model": str(llm.config.model),
                "retrieved_chunks": chunk_count,
                "total_tokens": total_tokens,
                "tokens_estimated": turn.tokens_estimated,
                "finish_reason": turn.finish_reason,
            },
        )
    except Exception:  # noqa: BLE001 — auditing must not break delivery
        logger.exception("Chat audit write failed")
