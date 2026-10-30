"""Re-mask retrieved chunks before they reach an LLM provider.

**The OpenSearch chunk index stores transcript text UNREDACTED.** That is correct
for search (the user searching their own transcripts should find their own words),
but it means retrieval hands back raw text — including anything the owner's
redaction policy says must never leave the deployment.

So chat re-masks every retrieved chunk whenever ``enabled && redact_before_llm``
applies, using exactly the gate summarization uses (``tasks/summarization.py``),
with the admin force floor folded in by ``resolve_effective_config``. Masking is
read-time only; stored transcripts are never modified.

**The subject is STRICTEST-WINS, resolved PER FILE (task #40).** A chat turn
retrieves across a library that can span several owners, so neither "the
requester's policy" (issue #402, previously the only subject) nor "the file
owner's policy" (the original plan) is safe alone: requester-subject lets a
sharee whose own policy is permissive read PII the owner meant to hide;
owner-subject ignores a stricter requester-side mandate. For each chunk or
digest section, the effective policy is
``union(policy_of(file.owner), policy_of(requester))`` —
:func:`~app.services.redaction.config.union_effective_config` — masked if
EITHER side says to, with the union of what they mask. It is resolved **per
file**, not once for the whole turn: a single global union across a many-file
scope would over-mask every file to the strictest owner in the set, which is
exactly the failure mode a per-file resolution avoids.
:func:`~app.services.redaction.config.most_restrictive_config` is unioned in
whenever a file's owner cannot be resolved at all (missing scan row, missing
owner id, or the owner's own policy read raising) — never fall through to only
the requester's policy on that failure, which would look identical to "the
owner permits it".

This composes with, and does not replace, the LOCAL-PROVIDER exemption
(``unmask_for_local`` below): that decision is evaluated once, from the
REQUESTER's config alone, before any per-file owner lookup runs at all — see
the note on :func:`_gather`.

Primary path: rebuild the chunk from its ``TranscriptSegment`` rows, whose
detection spans are cached in JSONB — sub-millisecond, no detector runs. Fallback
for files whose detection has not completed: mask inline (slower, logged).

⚠️ **Both public maskers are TWO-PHASE: gather from Postgres, close the session,
then mask (issue #83).** They take a *session factory*, not a ``Session``, for the
same reason ``aggregation_service`` does. The fallback above runs Presidio, and a
cold ``AnalyzerEngine`` build is **~10 s**; running it inside the caller's
transaction put a chat turn ``idle in transaction`` for that long — measured
**13,898 ms** against real Postgres — which queues every ``ALTER TABLE`` behind it
and hangs an Alembic upgrade mid-release. ``redaction/warmup.py`` makes the cold
build rare, not impossible: the warm-up gate is evaluated **once** at API startup,
so a deployment that enables redaction afterwards still pays it, and a request
landing mid-warm-up waits for the remainder.

Phase A returns PLAIN DATA (:class:`_SegmentSpans`), never an ORM instance —
attribute access on a detached row re-opens a session and silently undoes the
split. See this package's CLAUDE.md. Phase A's per-item reads live in
:mod:`app.services.chat.redactor_chunk_plans` and
:mod:`app.services.chat.redactor_digest_plans`; :func:`_gather` here opens the one
session and resolves the requester's policy around them.

⚠️ **``cfg.style`` is a DISPLAY choice and this module's output is an EGRESS
surface — the two must never share a config unchanged.** ``redaction/spans.py``'s
``_placeholder`` implements ``blur`` by embedding the ORIGINAL, UNMASKED text
inside the emitted ``<span>`` markup (escaped, so it can't inject HTML) so the UI
can blur it via CSS and reveal it on hover for an authorized viewer. That is
correct for a display surface with CSS and an authorization check and a leak on
this one: everything :func:`mask_chunks`/:func:`mask_digests` return is placed
directly into an LLM prompt and sent to a provider, which is neither. Both public
maskers force the style to ``label`` at the one seam where masked text is
actually produced (:func:`_mask_inline`, :func:`_mask_from_spans` — see
:func:`_egress_style` beside them) rather than trusting ``cfg.style`` through
unchanged, so a chunk masked via the cached-span path and one that fell back to
inline detection get the same treatment and a future third masking path added
here inherits it for free.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from sqlalchemy.orm import Session

from app.services.chat import context_expansion
from app.services.chat.redactor_chunk_plans import _ChunkPlan
from app.services.chat.redactor_chunk_plans import _gather_chunk_plans
from app.services.chat.redactor_chunk_plans import _SegmentSpans
from app.services.chat.redactor_digest_plans import _DigestPlan
from app.services.chat.redactor_digest_plans import _gather_digest_plans
from app.services.search.chunk_retrieval import ChunkHit

logger = logging.getLogger(__name__)

#: A callable returning a session context manager — ``db.session_utils.session_scope``
#: in production, the test's own session in the suites. Same contract as
#: ``aggregation_queries.SessionFactory``, except that ``None`` is not accepted:
#: "no Postgres" is not a decline here, it is a policy that cannot be resolved,
#: and this module fails closed on that rather than passing text through.
SessionFactory = Callable[[], contextlib.AbstractContextManager[Session]]


@dataclass
class MaskedChunk:
    """A chunk whose text is safe to place in a prompt.

    ``was_masked`` records that the redaction policy **applied** to this chunk,
    not that anything was found in it — a chunk that failed closed carries
    ``content == ""`` and ``was_masked=True``, because the policy is precisely
    what emptied it. The safety signal is ``content``; nothing may infer
    "this text is masked" from the flag alone.
    """

    source: ChunkHit
    content: str
    was_masked: bool = False

    @property
    def file_uuid(self) -> str:
        return self.source.file_uuid

    @property
    def title(self) -> str:
        return self.source.title

    @property
    def speaker(self) -> str | None:
        return self.source.speaker

    @property
    def start_time(self) -> float:
        return self.source.start_time

    @property
    def end_time(self) -> float | None:
        return self.source.end_time

    @property
    def chunk_index(self) -> int:
        return self.source.chunk_index

    @property
    def expanded(self) -> bool:
        """Issue #526: whether ``context_expansion`` widened this chunk's own
        time range before masking. Read by :func:`citations.build_citation` so
        the citation payload can say so — see :class:`ChunkHit`'s docstring for
        why this can never disagree with ``start_time``/``end_time``."""
        return bool(getattr(self.source, "expanded", False))


@dataclass
class _MaskingInputs:
    """Everything the masking phase needs, with no session held.

    ``cfg`` is the REQUESTER's own resolved policy — used only for the
    whole-turn local-provider exemption (see :func:`_gather`) and as the
    fail-closed stand-in when ``_gather`` itself raises before any per-item
    work runs. Each item's actual masking uses its OWN effective policy, on
    its plan (:class:`_ChunkPlan`/:class:`_DigestPlan`), not this field —
    task #40 resolves strictest-wins per file, not once per turn.
    """

    cfg: Any = None
    applies: bool = False
    chunks: list[_ChunkPlan] = field(default_factory=list)
    digests: list[_DigestPlan] = field(default_factory=list)


def _gather(
    session_factory: SessionFactory,
    user_id: int,
    *,
    organization_id: int | None = None,
    chunks: list[ChunkHit] | None = None,
    digests: list[ChunkHit] | None = None,
    unmask_for_local: bool = False,
) -> _MaskingInputs:
    """Open ONE short session, read everything, close it.

    Raises whatever the factory or the config read raises; the callers turn that
    into a fail-closed result for every item, because a policy that cannot be
    resolved is not a policy that permits sending text.

    Args:
        organization_id: The REQUESTER's active tenant scope — threaded into
            their own ``resolve_effective_config`` call so a registered per-org
            redaction floor (issue #982/#987) is actually consulted (#988).
            Each file's OWNER side of the strictest-wins union resolves its own
            org from the file's row instead (see ``_effective_cfg_for_owner``).
        unmask_for_local: True when the turn's LLM config is local
            (``llm_guard.is_local_provider``) — the text never leaves the
            machine, so masking it before the call costs recall for no egress
            benefit. Only takes effect when the policy would otherwise mask
            AND the admin has not forced ``redact_before_llm``
            (``cfg.redact_before_llm_locked``): the force floor exists
            specifically to override a per-provider exemption like this one, so
            a locked policy masks for every provider, local included.

            **This decision is made from the REQUESTER's config ALONE, before
            any per-file owner lookup runs — deliberately unaffected by
            strictest-wins (task #40).** The provider rule answers "does this
            text leave the deployment at all", which strictest-wins does not
            change (see this module's docstring and `services/chat/CLAUDE.md`):
            if nothing egresses, there is no "either policy" question left to
            ask, for ANY file's owner. It is also safe to read off the
            requester alone for a mechanical reason: `redact_before_llm_locked`
            is derived purely from the deployment-wide admin floor
            (`resolve_effective_config`'s `admin["force_redact_before_llm"]`),
            identical for every user by construction, so re-deriving it from an
            owner would answer the exact same question at the cost of a lookup
            that must not happen for a fully-exempted turn at all — see
            `test_local_provider_skips_masking_when_the_policy_would_otherwise_apply`,
            which asserts zero `db.query()` calls for exactly this case.
    """
    from app.services.redaction.config import resolve_effective_config

    with session_factory() as db:
        requester_cfg = resolve_effective_config(db, user_id, organization_id=organization_id)
        if unmask_for_local and not requester_cfg.redact_before_llm_locked:
            return _MaskingInputs(cfg=requester_cfg, applies=False)

        inputs = _MaskingInputs(cfg=requester_cfg, applies=True)
        if chunks is not None:
            inputs.chunks = _gather_chunk_plans(db, chunks, user_id, requester_cfg)
        if digests is not None:
            inputs.digests = _gather_digest_plans(db, digests, user_id, requester_cfg)
        return inputs


def _egress_style(cfg: Any) -> Any:
    """Force a non-revealing masking style for provider-bound text.

    Called at the two places this module actually produces masked text
    (:func:`_mask_inline`, :func:`_mask_from_spans`) — never sprinkled at each
    caller, so a new masking path added later inherits this for free instead of
    reintroducing the bug this function fixes.

    ``blur`` is the confirmed leak: ``spans.py::_placeholder`` embeds the
    ORIGINAL text inside the emitted ``<span>`` markup so the UI can blur it via
    CSS and reveal it on hover for an authorized viewer. Sent to an LLM
    provider, that markup is not blurred at all — it's the plaintext, with tags
    around it.

    ``first_letter`` is forced too, deliberately, even though it is a smaller
    leak: it exists to let a *display* reader recognize which entity was there
    (``J*** ****``) without full disclosure, which is a display-only
    justification that does not apply to a provider — there is no reader on the
    other end who benefits from a recognizable fragment, only a smaller version
    of the same disclosure. A short secret (a 4-digit PIN, a short name) can be
    fully identified from one revealed character plus context.

    ``asterisks`` reveals no original characters — only a span's length — so it
    is arguably already egress-safe. It is forced to ``label`` anyway, for
    consistency with the one existing precedent for this exact problem,
    ``redaction/summary_redaction.py::resolve_summary_leaf_policy``, which forces
    every non-``label`` style for a rendered-prose egress-adjacent surface
    without carving out an exception for it either. Consistency here means a
    reviewer checking one call site never has to re-derive which styles are
    safe from first principles.

    Fails closed by construction: this must be forced HERE, in the module that
    actually builds provider-bound text, rather than trusted from ``cfg`` — a
    caller passing a reveal-style config through unchanged is exactly how the
    bug this function fixes shipped (``cfg`` flows in from the requester's own
    stored preference, unvalidated, at every call site above).
    """
    if getattr(cfg, "style", "label") == "label":
        return cfg
    if dataclasses.is_dataclass(cfg) and not isinstance(cfg, type):
        return dataclasses.replace(cfg, style="label")
    # Defensive fallback for a duck-typed config (e.g. a test double) that is
    # not a real dataclass instance — copy rather than mutate the caller's cfg.
    clone = copy.copy(cfg)
    clone.style = "label"
    return clone


def _mask_inline(text: str, cfg) -> str:
    """Detect and mask on the fly — for files whose cached spans aren't ready.

    Toxicity classification is skipped: it is the expensive detector and loading
    it on an interactive request would blow the latency budget. PII/profanity
    still run, which is what ``redact_before_llm`` primarily protects.

    **Runs with no database session open** (#83) — a cold Presidio build is ~10 s,
    and paying it inside a transaction is the defect
    ``scripts/audit-session-lifetime.py`` exists to catch.

    **The ``failures`` sink is what makes the fail-closed promise true.**
    ``detect_segment_spans`` *swallows* a PII-detector exception and returns
    whatever spans it managed to collect, so "found nothing" and "could not look"
    are the same return value (issue #324). Without the sink this function
    returned the chunk **verbatim** whenever Presidio was broken or absent, and
    ``mask_chunks`` then labelled it masked and sent it — on the path that
    egresses to a third-party provider. A `try`/`except` around a call that never
    raises is not a fail-closed control; the docstring said it was.

    Narrow on purpose (see ``blocking_detector_failures``): only a failure of a
    detector feeding a category this user actually masks withholds the chunk. A
    CPU-only deployment with no Presidio that never enabled ``pii`` must keep
    answering rather than lose every excerpt to a category it never asked for.
    """
    try:
        from app.services.redaction.config import blocking_detector_failures
        from app.services.redaction.config import detection_config_for_all
        from app.services.redaction.service import RedactionService

        failures: list[str] = []
        spans, _toxicity = RedactionService.detect_segment_spans(
            text, None, detection_config_for_all(), run_toxicity=False, failures=failures
        )
        blocking = blocking_detector_failures(failures, cfg.enabled_categories)
        if blocking:
            raise RuntimeError(f"detectors unavailable for enabled categories: {sorted(blocking)}")
        masked, _applied = RedactionService.mask_segment(
            text, spans, None, _egress_style(cfg), set()
        )
        return masked
    except Exception:  # noqa: BLE001
        logger.exception("Inline chunk masking failed; dropping chunk content")
        # Failing closed: an unmaskable chunk must not reach the provider.
        return ""


def _mask_from_spans(segments: list[_SegmentSpans], cfg) -> str:
    """Apply cached spans to gathered segment text. Pure — no I/O, no detectors."""
    from app.services.redaction.service import RedactionService

    parts = []
    egress_cfg = _egress_style(cfg)
    for segment in segments:
        if not segment.text:
            continue
        masked, _applied = RedactionService.mask_segment(
            segment.text, segment.redactions, segment.words, egress_cfg, set()
        )
        parts.append(masked)
    return " ".join(parts).strip()


def _apply_chunk_plan(plan: _ChunkPlan, chunk: ChunkHit) -> tuple[str, bool, bool]:
    """Mask one chunk from its plan. Returns ``(content, was_masked, used_inline)``.

    ``plan.applies`` is the strictest-wins decision (task #40) for THIS chunk's
    own file: False means neither the owner's nor the requester's policy wants
    it masked, so the content passes through untouched and ``was_masked`` is
    False — distinct from every other branch here, which fails closed on a
    problem actually MASKING it.
    """
    if not plan.applies:
        return chunk.content, False, False
    if plan.failed:
        return "", True, False
    if plan.segments is None:
        return _mask_inline(chunk.content, plan.cfg), True, True
    try:
        text = _mask_from_spans(plan.segments, plan.cfg)
    except Exception:  # noqa: BLE001
        logger.exception("Cached-span masking failed for chunk; withholding content")
        # Fail CLOSED — an unmaskable chunk contributes nothing.
        return "", True, False
    if not text:
        return _mask_inline(chunk.content, plan.cfg), True, True
    return text, True, False


# --------------------------------------------------------------------------- #
# The two public maskers. NOT interchangeable — see this package's CLAUDE.md.
# --------------------------------------------------------------------------- #


def mask_chunks(
    session_factory: SessionFactory,
    chunks: list[ChunkHit],
    user_id: int,
    *,
    organization_id: int | None = None,
    unmask_for_local: bool = False,
    expand_short_chunks: bool = False,
) -> list[MaskedChunk]:
    """Apply the STRICTEST-WINS redact-before-LLM policy to retrieved chunks.

    ⚠️ **Neither the requester alone (issue #402) nor the file owner alone**
    (the original plan). Both were wrong in one direction: requester-subject
    lets a sharee with a permissive policy read PII the owner meant to hide;
    owner-subject ignores a stricter requester-side mandate. Task #40 resolves
    ``union(owner_of(chunk.file), requester)`` **per chunk** — a turn spans
    recordings owned by several people, so there is no single subject, and a
    single global union across the whole turn would over-mask every file to
    the strictest owner in the scope. Summarization resolves the FILE OWNER
    alone (``tasks/summarization.py``) because it has exactly one file and one
    egress decision per call; chat's per-chunk union is the multi-owner
    generalization of that same egress question, not a different one.
    ``redaction/export_policy.py`` argues the general subject-choice rule.

    Two phases: **one** short session gathers the policy and every chunk's cached
    spans, then the session closes and the masking (including a possible Presidio
    load) runs with nothing held (#83).

    Args:
        session_factory: Callable returning a session context manager
            (``session_scope``). NOT a ``Session`` — this function owns the
            transaction boundary precisely so it can close it before masking.
        chunks: Chunks straight out of retrieval (unredacted index content).
        user_id: The REQUESTING user. Their effective policy (admin force floor
            included) is unioned with each chunk's own file owner's — see
            above — matching :func:`mask_digests` beside it.
        organization_id: The REQUESTER's active tenant scope (``ctx.org_id``),
            threaded into their side of the union so a registered per-org
            redaction floor (issue #982/#987) is actually consulted (#988).
        unmask_for_local: True when the turn's LLM is local
            (``redaction.llm_guard.is_local_provider``) — the excerpt text never
            leaves the machine, so masking is skipped for every chunk regardless
            of either policy, unless the admin has forced ``redact_before_llm``
            (the REQUESTER's ``cfg.redact_before_llm_locked`` — see ``_gather``
            for why the requester alone is the right and sufficient read here),
            in which case the force floor wins and masking still applies.
        expand_short_chunks: Issue #523, ``chat.context_expansion_enabled``.
            Widens any chunk under
            :data:`~app.services.chat.context_expansion.SHORT_CHUNK_WORD_THRESHOLD`
            words to its surrounding exchange, BY TIME RANGE, before anything
            below reads a single segment — so the widened ``chunk.start_time``/
            ``end_time`` this rewrites ``chunks`` with is what every downstream
            masking decision (cached-span rebuild, the fail-CLOSED branches,
            the pass-through branch below) actually sees. There is no second
            masking path for expanded text: it is masked, or withheld, exactly
            like any other excerpt from the same file. Default False — see
            ``context_expansion.py``'s module docstring for the budget bound
            that keeps this from crowding out other files' evidence.

    Returns:
        Chunks with prompt-safe text. When the policy does not apply to a given
        chunk — including when the whole turn is skipped for a local provider —
        that chunk's content is passed through untouched.
    """
    if expand_short_chunks and chunks:
        try:
            with session_factory() as db:
                chunks = context_expansion.expand_chunks(db, chunks)
        except Exception:  # noqa: BLE001 — an enhancement, never a dependency
            logger.exception("Context expansion failed; masking the un-expanded chunks instead")

    try:
        inputs = _gather(
            session_factory,
            user_id,
            organization_id=organization_id,
            chunks=chunks,
            unmask_for_local=unmask_for_local,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; masking all chunk content")
        # Fail CLOSED: if we cannot tell whether masking is required, don't send text.
        return [MaskedChunk(source=c, content="", was_masked=True) for c in chunks]

    if not inputs.applies:
        # The WHOLE TURN is exempt (local provider, admin has not forced the
        # floor) — see `_gather`'s docstring. Deliberately evaluated once, from
        # the requester's config alone, before any per-file owner lookup: the
        # provider rule answers "does this leave the deployment", which is the
        # same answer for every file in scope.
        return [MaskedChunk(source=c, content=c.content) for c in chunks]

    masked: list[MaskedChunk] = []
    inline_fallbacks = 0
    for chunk, plan in zip(chunks, inputs.chunks, strict=True):
        content, was_masked, used_inline = _apply_chunk_plan(plan, chunk)
        inline_fallbacks += int(used_inline)
        masked.append(MaskedChunk(source=chunk, content=content, was_masked=was_masked))

    if inline_fallbacks:
        logger.info(
            "Chat masking used the inline fallback for %d/%d chunks "
            "(segments unavailable — detection may still be running)",
            inline_fallbacks,
            len(chunks),
        )
    return masked


def mask_digests(
    session_factory: SessionFactory,
    digests: list[ChunkHit],
    user_id: int,
    *,
    organization_id: int | None = None,
    unmask_for_local: bool = False,
) -> list[MaskedChunk]:
    """Re-mask digest sections **through their provenance**, failing closed per sentence.

    ⚠️ **Never route a digest through :func:`mask_chunks`.** That path rebuilds a
    chunk's text from every ``TranscriptSegment`` overlapping its time range,
    which is correct for a chunk — a chunk *is* contiguous turns — and
    catastrophically wrong for a digest, whose text is a handful of
    non-contiguous *selected* sentences spanning the whole recording. The
    rebuild would return the entire span verbatim in place of a short summary:
    strictly **more** text than was asked for, emitted by a function whose name
    asserts it was masked. No reviewer looks twice at a call to ``mask_*``.

    So the unit of masking here is the **sentence**, addressed by the
    ``segment_ids`` its provenance records (#403 D3). Fail-closed is per
    sentence too: an unmaskable sentence contributes nothing and the rest of the
    section still reaches the prompt, where a chunk fails closed whole. Those are
    genuinely different contracts, which is the second reason this is not an
    overload of ``mask_chunks``.

    Two-phase like :func:`mask_chunks`: the provenance read and every sentence's
    segments are gathered in one short session, which closes before any masking
    (#83).

    Args:
        session_factory: Callable returning a session context manager.
        digests: Digest hits from ``retrieve_digests``.
        user_id: The REQUESTING user. As in :func:`mask_chunks`, their policy is
            unioned per-section with that section's own file owner's
            (strictest-wins, task #40) — never the requester alone.
        organization_id: The REQUESTER's active tenant scope — same rule as
            :func:`mask_chunks` (#988).
        unmask_for_local: Same rule as :func:`mask_chunks` — skip masking for a
            local LLM unless the admin force floor (the REQUESTER's
            ``cfg.redact_before_llm_locked``) overrides the exemption.

    Returns:
        Masked digests. A section whose provenance cannot be resolved comes back
        with empty content and is dropped by the caller, never passed through raw.
    """
    if not digests:
        return []
    try:
        inputs = _gather(
            session_factory,
            user_id,
            organization_id=organization_id,
            digests=digests,
            unmask_for_local=unmask_for_local,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not resolve redaction config; withholding all digest content")
        return [MaskedChunk(source=d, content="", was_masked=True) for d in digests]

    if not inputs.applies:
        # The WHOLE TURN is exempt — same fast path as `mask_chunks`, see
        # `_gather`'s docstring.
        return [MaskedChunk(source=d, content=d.content) for d in digests]

    masked: list[MaskedChunk] = []
    unresolvable = 0
    for digest, plan in zip(digests, inputs.digests, strict=True):
        if not plan.applies:
            # Strictest-wins (task #40): neither this SECTION's file owner nor
            # the requester wants it masked. Per-file, not per-turn — a section
            # from a permissive-owner file is not over-masked just because
            # another section in this same turn belongs to a strict owner.
            masked.append(MaskedChunk(source=digest, content=digest.content, was_masked=False))
            continue

        if plan.sentences is None:
            # No cached spans to apply. Masking the rendered section inline is
            # the only remaining option that cannot over-disclose — and if even
            # that fails, `_mask_inline` already returns "".
            unresolvable += 1
            masked.append(
                MaskedChunk(
                    source=digest,
                    content=_mask_inline(digest.content, plan.cfg),
                    was_masked=True,
                )
            )
            continue

        kept: list[str] = []
        for segments in plan.sentences:
            if not segments:
                continue
            try:
                text = _mask_from_spans(segments, plan.cfg)
            except Exception:  # noqa: BLE001
                logger.exception("Digest sentence masking failed; dropping that sentence")
                text = ""
            if text:
                kept.append(text)
        masked.append(MaskedChunk(source=digest, content=" ".join(kept).strip(), was_masked=True))

    if unresolvable:
        logger.info(
            "Digest masking fell back to inline detection for %d/%d sections "
            "(no cached provenance — detection may still be running)",
            unresolvable,
            len(digests),
        )
    return masked
