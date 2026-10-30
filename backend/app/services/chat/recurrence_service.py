"""Cross-meeting recurrence, I/O half: read, mask and hand items to the pure detector.

The pure grouping logic lives in :mod:`app.services.chat.recurrence`; this is the half that reads
``summary_data`` / ``file_facts.keyphrases`` from Postgres and masks every item PER FILE before it
reaches the detector. Postgres-only, no OpenSearch, and separate from ``answer_aggregation``:
recurrence produces an EVIDENCE BLOCK (``<recurrence>``, rendered by
``prompting.format_recurrence_block``) rather than one counted number, so it does not join
``aggregation.SHAPES`` or ``choose_shape``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from typing import Any

from app.services.chat import recurrence
from app.services.chat.aggregation_queries import SessionFactory
from app.services.chat.aggregation_queries import _accessible_scoped_files
from app.services.chat.aggregation_queries import _short_session
from app.services.chat.router import Route

if TYPE_CHECKING:
    from app.services.redaction.config import EffectiveRedactionConfig

logger = logging.getLogger(__name__)


def _mask_summary_text(text: str, cfg: EffectiveRedactionConfig) -> str:
    """Mask one short summary-derived string (an action item, a keyphrase, ...).

    A thin alias for :func:`redaction.summary_redaction.mask_summary_leaf`, which
    is THE masker for summary prose. An action item has no ``segment_ids``
    provenance to rebuild from (unlike a chunk or a digest sentence), so it is
    detected live exactly like a summary leaf — the same shape, and therefore the
    same implementation.

    ⚠️ This used to be a second implementation, and it was not equivalent: it ran
    ``detection_config_for_all()`` directly, which skipped the **custom-wordlist**
    spans, skipped the label-style forcing (``blur`` emits markup the client does
    not sanitize for this surface), and constructed Presidio even for a
    profanity-only user. A user's custom redaction words survived into recurrence
    output. Do not reintroduce a local masker here.

    Raises:
        SummaryMaskingUnavailableError: a ``RuntimeError`` subclass, so the
            caller's per-file ``except RuntimeError`` still drops the WHOLE
            file's items rather than half-masking one and passing it through.
    """
    from app.services.redaction.summary_redaction import mask_summary_leaf

    return mask_summary_leaf(text, cfg)


#: ``(text, owner, leaf, language)`` — one recurrence-eligible item before masking.
_Entry = tuple[str, str | None, str, str | None]


def _collect_entries(
    summary_status: Any, summary_data: Any, fingerprint: Any, keyphrases: Any
) -> list[_Entry]:
    """Every recurrence-eligible item one file offers: fresh summary leaves plus keyphrases."""
    from app.services.chat.mapreduce import _summary_is_fresh

    collected: list[_Entry] = []

    if _summary_is_fresh(summary_status, summary_data, fingerprint):
        # Summaries follow LLM_OUTPUT_LANGUAGES, NOT the transcript
        # language — read from metadata, never guessed from the file.
        summary_language = (summary_data.get("metadata") or {}).get("language")
        for leaf in (
            recurrence.LEAF_ACTION_ITEM,
            recurrence.LEAF_KEY_DECISION,
            recurrence.LEAF_FOLLOW_UP,
        ):
            for raw in summary_data.get(leaf) or []:
                normalized = recurrence.normalize_leaf(raw, leaf)
                if normalized is None:
                    continue
                text, owner = normalized
                collected.append((text, owner, leaf, summary_language))

    # D6: keyphrases are ALWAYS included — the no-LLM source, so
    # recurrence works with `llm=None`/no provider configured at all.
    kp = keyphrases or {}
    kp_language = kp.get("language")
    for raw in kp.get("phrases") or []:
        normalized = recurrence.normalize_leaf(raw, recurrence.LEAF_KEYPHRASE)
        if normalized is None:
            continue
        text, _owner = normalized
        collected.append((text, None, recurrence.LEAF_KEYPHRASE, kp_language))

    return collected


def _mask_entries(
    items_by_file: dict[str, list[_Entry]],
    cfg: EffectiveRedactionConfig,
    applies: bool,
) -> tuple[list[recurrence.SourceItem], int]:
    """Mask every file's entries; a file whose masking fails is dropped WHOLE (fail closed)."""
    out: list[recurrence.SourceItem] = []
    failed_files = 0
    for file_uuid, entries in items_by_file.items():
        try:
            file_items: list[recurrence.SourceItem] = []
            for text, owner, leaf, language in entries:
                masked = _mask_summary_text(text, cfg) if applies else text
                file_items.append(
                    recurrence.SourceItem(
                        file_uuid=file_uuid,
                        text=masked,
                        leaf=leaf,
                        language=language,
                        owner=owner,
                    )
                )
            out.extend(file_items)
        except Exception:  # noqa: BLE001 — fail CLOSED: drop the whole file, never half-mask it
            logger.exception(
                "Masking failed for file %s in recurrence; dropping its items", file_uuid
            )
            failed_files += 1
    return out, failed_files


def gather_recurrence_source_items(
    session_factory: SessionFactory | None,
    user_id: int,
    organization_id: int | None,
    file_uuids: list[str] | None,
    *,
    unmask_for_local: bool = False,
) -> tuple[list[recurrence.SourceItem], dict[str, Any]]:
    """Read, normalize and mask every recurrence-eligible item across the scope.

    **Bounded scope only** — ``file_uuids`` must be a real, non-empty list,
    never ``None`` ("all accessible"). Same precondition
    ``mapreduce.scope_digest_hits`` documents, for the identical reason: this
    reads ``file_facts``/``summary_data`` for every file in scope directly
    (a MAP, not a ranked search), so an unbounded scope cannot be answered —
    it declines rather than silently capping to *some* subset of "everything".

    **Sources, per file**: fingerprint-fresh ``summary_data`` items
    (``action_items``, ``key_decisions``, ``follow_up_items`` — freshness via
    the same ``mapreduce._summary_is_fresh`` test the map tier already uses)
    PLUS, unconditionally, ``file_facts.keyphrases`` — the #403 **D6**
    no-LLM source, so recurrence detection works with no LLM provider
    configured at all.

    **Masking (issue #402).** The redaction policy is resolved ONCE, for the
    REQUESTING user — the same subject ``mask_chunks``/``mask_digests``
    already use, and deliberately not re-decided here (see
    ``services/chat/CLAUDE.md``'s "recorded for a later phase" section: the
    plan text argues for the file OWNER's policy on a summary tier, but the
    shipped code uses the requester's, and this module follows what shipped,
    not what was planned). A file whose masking cannot be completed is
    dropped **whole** — every item it offered — and counted in
    ``coverage["masking_failed_files"]``; the failure is never partial and the
    raw text never substitutes for a masked value.

    Args:
        session_factory: Opens a short Postgres session, or ``None`` to
            decline (no Postgres, no answer).
        user_id: The requesting user — enforced via
            ``_accessible_scoped_files`` and the masking policy subject.
        organization_id: Active tenant, or ``None`` for personal scope.
        file_uuids: The resolved, bounded scope. ``None`` or empty declines.
        unmask_for_local: Same per-provider exemption ``mask_chunks``/
            ``mask_digests`` apply — skipped when the admin has forced
            ``redact_before_llm`` (``cfg.redact_before_llm_locked``).

    Returns:
        ``(items, coverage)`` — masked :class:`recurrence.SourceItem`\\ s ready
        for :func:`recurrence.detect_recurring_items`, and an I/O-side
        coverage dict (``masking_failed_files``,
        ``files_without_summary_or_keyphrases``, and ``declined`` when the
        call could not even start).
    """
    coverage: dict[str, Any] = {
        "masking_failed_files": 0,
        "files_without_summary_or_keyphrases": 0,
    }
    if not file_uuids:
        coverage["declined"] = "recurrence needs a bounded, non-empty scope"
        return [], coverage

    with _short_session(session_factory) as db:
        if db is None:
            coverage["declined"] = "no database session"
            return [], coverage

        from app.models.file_facts import FileFacts
        from app.models.media import MediaFile
        from app.services.redaction.config import resolve_effective_config

        try:
            cfg = resolve_effective_config(db, user_id, organization_id=organization_id)
        except Exception:  # noqa: BLE001 — an unresolvable policy declines, never sends raw text
            logger.exception("Could not resolve redaction config for recurrence")
            coverage["declined"] = "redaction policy could not be resolved"
            return [], coverage

        applies = bool(cfg.enabled and cfg.redact_before_llm)
        if applies and unmask_for_local and not cfg.redact_before_llm_locked:
            applies = False

        scoped_predicate = _accessible_scoped_files(db, user_id, organization_id, file_uuids)
        rows = (
            db.query(
                MediaFile.uuid,
                MediaFile.summary_status,
                MediaFile.summary_data,
                FileFacts.source_fingerprint,
                FileFacts.keyphrases,
            )
            .select_from(MediaFile)
            .outerjoin(FileFacts, FileFacts.media_file_id == MediaFile.id)
            .filter(scoped_predicate)
            .all()
        )

        items_by_file: dict[str, list[_Entry]] = {}
        files_without_source = 0
        for uuid, summary_status, summary_data, fingerprint, keyphrases in rows:
            collected = _collect_entries(summary_status, summary_data, fingerprint, keyphrases)
            if not collected:
                files_without_source += 1
                continue
            items_by_file[str(uuid)] = collected

        coverage["files_without_summary_or_keyphrases"] = files_without_source

    out, failed_files = _mask_entries(items_by_file, cfg, applies)

    coverage["masking_failed_files"] = failed_files
    return out, coverage


def answer_recurrence(
    route: Route,
    *,
    session_factory: SessionFactory | None,
    user_id: int,
    organization_id: int | None = None,
    file_uuids: list[str] | None = None,
    recurrence_enabled: bool = False,
    unmask_for_local: bool = False,
    similarity_threshold: float = recurrence.DEFAULT_SIMILARITY_THRESHOLD,
    item_cap: int = recurrence.DEFAULT_ITEM_CAP,
) -> recurrence.RecurrenceResult | None:
    """Answer a cross-meeting recurrence question, or decline.

    **Flag-gated at THIS layer too, independently of the router.** ``chat.
    recurrence_enabled`` must gate "the patterns AND the shape":
    the router never PRODUCES ``Route.wants_recurrence`` with the flag off,
    but this function also refuses to build the block if somehow called with
    the flag off anyway — so no single call site's omission can leak the
    shape past the flag. With the flag off, or a non-recurrence route, this
    returns ``None`` having done **zero** I/O, which is what keeps the
    feature byte-identical to absent when disabled.

    Args:
        route: The router's decision. Only :attr:`Route.wants_recurrence`
            questions reach the detector.
        session_factory: Opens a short Postgres session; ``None`` declines.
        user_id: The requesting user (masking subject and access filter).
        organization_id: Active tenant, or ``None`` for personal scope.
        file_uuids: The resolved scope. Must be bounded (a real list) — see
            :func:`gather_recurrence_source_items`.
        recurrence_enabled: ``chat.recurrence_enabled``, resolved by the
            caller from :class:`~app.services.chat.settings.ChatSettings`.
        unmask_for_local: Per-provider masking exemption, threaded straight
            through to the gather step.
        similarity_threshold: Forwarded to
            :func:`recurrence.detect_recurring_items`.
        item_cap: Forwarded to :func:`recurrence.detect_recurring_items`.

    Returns:
        A :class:`recurrence.RecurrenceResult`, or ``None`` when the flag or
        the route says this question does not want one. A non-``None`` result
        with zero ``groups`` is still meaningful — it means the detector ran
        and found nothing recurring, which is different from never running.
    """
    if not recurrence_enabled or not route.wants_recurrence:
        return None

    items, coverage = gather_recurrence_source_items(
        session_factory,
        user_id,
        organization_id,
        file_uuids,
        unmask_for_local=unmask_for_local,
    )
    result = recurrence.detect_recurring_items(
        items, similarity_threshold=similarity_threshold, item_cap=item_cap
    )
    from dataclasses import replace

    return replace(result, coverage=coverage)
