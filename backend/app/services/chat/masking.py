"""Egress masking for one chat turn: the keyword arguments every masker call shares.

SECURITY. These builders decide whether retrieved transcript text is masked before an
LLM sees it. ``unmask_for_local`` is sent only when
``redaction.llm_guard.is_local_provider`` classifies THIS turn's provider as local.
That classification fails closed (any ambiguity, an unknown provider or no LLM at all
reads as remote, so masking applies), and an admin's
``redaction.force_redact_before_llm`` floor still wins inside ``redactor._gather``. The
chunk plane and the digest plane get deliberately different dicts: see
:func:`_build_digest_mask_kwargs`.

Resolved once per turn by ``service._mask_evidence`` and handed to every masker call,
so the call sites cannot disagree about which provider the turn is using.
"""

from __future__ import annotations

from typing import Any


def _build_digest_mask_kwargs(llm: Any) -> dict[str, bool]:
    """Masker kwargs for the DIGEST plane — deliberately narrower than the chunk plane.

    ⚠️ ``expand_short_chunks`` MUST NOT appear here, and the reason is the same one
    that keeps ``mask_chunks`` and ``mask_digests`` separate functions at all:
    ``mask_chunks`` addresses text by **time range**, ``mask_digests`` by
    **provenance** (each sentence's own ``segment_ids``). Read-time expansion widens a
    hit's time window to its surrounding exchange, which is meaningful for a chunk and
    is the over-disclosure trap for a digest — a digest rebuilt from a widened span
    returns material the digest never held, from a function whose name says it masked it.

    Passing the chunk-plane kwargs to ``mask_digests`` was a real, measured outage, not a
    hypothetical: every turn that routed through the digest tier died with
    ``mask_digests() got an unexpected keyword argument 'expand_short_chunks'`` and a
    ``provider_error`` frame, while chunk-only turns were unaffected — so 4 of 14 probe
    questions returned nothing and it read as a coverage regression rather than a crash.
    The unit suite did not catch it because no test drove the digest path with expansion
    on; ``test_chat_digest_masking.py`` now does.
    """
    kwargs: dict[str, bool] = {}
    if _unmask_for_local(llm):
        kwargs["unmask_for_local"] = True
    return kwargs


def _unmask_for_local(llm: Any) -> bool:
    """Whether this turn's provider is local, so egress masking can be skipped."""
    from app.services.redaction.llm_guard import is_local_provider

    return bool(is_local_provider(getattr(llm, "config", None)))


def _build_mask_kwargs(llm: Any, settings: Any) -> dict[str, bool]:
    """The keyword arguments every masker call in one turn shares.

    Extracted from ``_prepare_context`` so the two decisions below are testable
    on their own and so the caller stays under the complexity ceiling — not as
    tidying. Both are resolved ONCE per turn and threaded to every masker rather
    than re-derived per call, so the three sites cannot disagree about which
    provider this turn is using or whether expansion is on.

    ``unmask_for_local`` keys egress masking off WHERE THE MODEL RUNS (owner
    decision, 2026-08-13): a local model never has this text leave the machine,
    so masking it costs recall for no egress benefit.

    ⚠️ ``llm`` is None for a legitimate, first-class deployment shape (#403 D6):
    no ``LLM_PROVIDER`` configured at all still has to produce masked context for
    the deterministic map/keyphrase/coverage tiers, and that path never had an
    ``llm`` to read a provider from. ``getattr(llm, "config", None)`` reaches
    ``is_local_provider``'s own None-safe duck typing instead of a SECOND None
    check here — one fail-closed decision, not two that could disagree.

    ⚠️ Keys are included ONLY when True. Every pre-existing caller of
    ``mask_chunks``/``mask_digests`` — and every test that stubs either with a
    positional-only signature — predates both parameters; passing them
    unconditionally would break all of them for the overwhelmingly common case
    (remote provider, expansion off) where they do nothing anyway.

    ``expand_short_chunks`` (#523) widens a chunk under
    ``context_expansion.SHORT_CHUNK_WORD_THRESHOLD`` words to its surrounding
    exchange BEFORE masking, inside ``mask_chunks`` itself, so the
    strictest-wins policy applies to every widened word. Flag-gated, default ON
    as of the #523 A/B (2026-09-21) (``chat.context_expansion_enabled``).

    ⚠️ THIS IS THE CHUNK PLANE ONLY. The digest plane takes
    :func:`_build_digest_mask_kwargs`, which omits ``expand_short_chunks`` — see its
    docstring for why feeding it to ``mask_digests`` is both a TypeError and a
    conceptual over-disclosure. These returned dicts are deliberately NOT interchangeable;
    do not re-merge them into one.
    """
    kwargs: dict[str, bool] = {}
    if _unmask_for_local(llm):
        kwargs["unmask_for_local"] = True
    if getattr(settings, "context_expansion_enabled", False):
        kwargs["expand_short_chunks"] = True
    return kwargs
