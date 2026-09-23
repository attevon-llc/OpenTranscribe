"""Per-tenant limit resolvers (cloud-edition seam).

Two registry hooks the proprietary cloud layer overrides to apply per-org
(per-subscription-tier) policy on top of core's global defaults. Both default
to a community no-op (return ``None`` -> "no override, use the global value"),
so the open-source / self-host edition behaves EXACTLY as before — there is no
behavior change unless the cloud layer registers a resolver.

Why a resolver and not a column:
  * core stays vendor-neutral — it has no concept of subscription tiers, and
    the billing schema is closed-source (``organization_billing`` lives in the
    cloud Alembic chain). A resolver lets the cloud compute the value from its
    own billing state without core importing any cloud module.
  * mirrors the capability resolver (``app.core.capabilities``): core ships a
    community resolver; the cloud replaces it at activation.

Hook 1 — **retention override** (``resolve_retention_days``). core's file
retention is a single global ``files.retention_days`` SystemSetting. A cloud
tenant on a higher tier may keep files longer (or a free tier shorter). The
cleanup task asks the resolver per organization; ``None`` means "use the global
SystemSetting".

Hook 2 — **upload limits** (``resolve_upload_limits``). core enforces one global
ceiling (15 GB / 4 h). A cloud tier may allow more or less. The resolver returns
a :class:`TenantUploadLimits` (max bytes / max duration seconds, either field
``None`` = no per-tenant override for that dimension); ``None`` for the whole
result means "no override, use the global ceiling".

Hook 3 — **redaction floor** (``resolve_redaction_floor``). core's redaction
governance floor is a single global set of ``redaction.force_*`` SystemSettings
(``services.redaction.config``), with no per-tenant path at all. A regulated
cloud tenant may need PII forced on for its own users while the rest of the
deployment is left alone. The resolver returns a :class:`RedactionFloor` that is
UNIONED with the global floor at mask time — it can only ever add, never relax
what the operator already mandated; ``None`` means "no tenant floor".
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import only for typing — core must stay import-light here
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantUploadLimits:
    """Per-tenant upload ceiling (either dimension ``None`` = no override)."""

    max_file_bytes: int | None = None
    max_duration_seconds: int | None = None


@dataclass(frozen=True)
class TenantChatLimits:
    """Per-tenant chat ceilings (any field ``None`` = no override for that dimension).

    Deliberately generic vocabulary — no tier names, no prices, no currency. core
    knows only that *some* caller may cap these dimensions; what a "starter" plan
    is, and what it costs, stays in the cloud layer.

    ``max_output_tokens`` and ``max_retrieved_chunks`` are the two levers that
    actually bound the cost of a single message. Retrieved excerpts dominate input
    tokens in a RAG chat — a user typing five words can still send thousands of
    tokens — so capping chunks matters at least as much as capping the answer.
    """

    messages_per_hour: int | None = None
    max_concurrent_streams: int | None = None
    max_output_tokens: int | None = None
    max_retrieved_chunks: int | None = None


@dataclass(frozen=True)
class RedactionFloor:
    """Per-tenant redaction governance floor — a FLOOR, never a ceiling.

    One field per global ``redaction.force_*`` SystemSetting, so a cloud
    resolver expresses tenant policy in exactly the vocabulary core's admin UI
    already uses (``services.redaction.config._load_admin_policy`` is the one
    place that reads those keys, and this mirrors its shape).

    **Every field can only ADD.** The read surface unions this with the global
    floor: categories/entities/words are merged, ``force_toxicity_threshold``
    takes the lower (more sensitive) value, and the booleans OR. A tenant
    override that could *unset* something the deployment's operator mandated
    would be a governance hole, not a feature, so no field is capable of it.

    Collections are ``frozenset``/``tuple`` rather than ``set``/``list``: the
    dataclass is frozen, and a mutable member would let a caller edit a floor
    the resolver may well be caching and handing to the next tenant too.

    Attributes:
        forced_categories: Categories always masked for this tenant, from the
            same vocabulary as the global floor (``pii``, ``toxicity``,
            ``profanity``). Forcing one also forces its detector to run.
        forced_pii_entities: Presidio entity names always masked.
        forced_custom_words: Literal words always masked.
        force_toxicity_threshold: ``None`` = no opinion. A value is a MAXIMUM:
            the effective threshold becomes the lower of this and the user's,
            matching how the global floor already behaves.
        force_export_redacted: Mandate censored exports for this tenant.
        force_redact_before_llm: Mandate masked text to LLM providers, INCLUDING
            local ones — this is the same lock as the global
            ``redaction.force_redact_before_llm``, which always beats the
            per-provider local-model exemption (see
            ``services.redaction.config.EffectiveRedactionConfig``).
    """

    forced_categories: frozenset[str] = frozenset()
    forced_pii_entities: frozenset[str] = frozenset()
    forced_custom_words: tuple[str, ...] = ()
    force_toxicity_threshold: float | None = None
    force_export_redacted: bool = False
    force_redact_before_llm: bool = False


# Resolver signatures. ``organization_id`` is None for personal/no-org scope
# (community edition is ALWAYS None here), in which case a community resolver
# returns None and the global value applies.
RetentionResolver = Callable[[int | None], int | None]
UploadLimitsResolver = Callable[[int | None], TenantUploadLimits | None]
# Reports the SMALLEST per-org retention override currently in effect (days),
# or None when no override is shorter than the global value. The cleanup task
# uses it to WIDEN its candidate query window: a candidate must merely be older
# than the shortest retention anyone has — the per-file expiry check then
# applies each file's own effective retention, so longer-retention tenants'
# files are kept without any help from this hook. (Keying the window on the
# LARGEST override — the pre-0.5.0 shape — was doubly wrong: short-override
# files were never candidates until the global age, and global files were
# never candidates until the longest override's age.) Community: None.
MinRetentionResolver = Callable[[], int | None]
ChatLimitsResolver = Callable[[int | None], TenantChatLimits | None]
# Returns the set of model identifiers a tenant may select, or ``None`` for "no
# restriction". An EMPTY set is meaningful and distinct from None: it means the
# tenant may use no model at all, which is how a suspended account is expressed.
AllowedModelsResolver = Callable[[int | None], set[str] | None]
# Takes the caller's live ``Session`` as well as the org, unlike every resolver
# above: the ONLY read surface is ``resolve_effective_config``, which already
# holds a session open for the user-prefs and admin-floor reads, and a floor
# resolver that opened a second one would add a connection to a hot path that
# runs on every mask. It is a read-only borrow — a resolver must not write or
# commit on it (see ``usage_service.record_event`` for what a stray rollback on
# a borrowed session costs). ``organization_id`` is None for personal scope
# (ALWAYS None in community), in which case the community resolver returns None.
RedactionFloorResolver = Callable[["Session", int | None], "RedactionFloor | None"]


def _community_retention_resolver(_org_id: int | None) -> int | None:
    return None  # no override -> global files.retention_days applies


def _community_min_retention_resolver() -> int | None:
    return None  # no override is shorter than the global value


def _community_upload_limits_resolver(_org_id: int | None) -> TenantUploadLimits | None:
    return None  # no override -> global ceiling applies


def _community_chat_limits_resolver(_org_id: int | None) -> TenantChatLimits | None:
    return None  # no override -> the admin's SystemSettings values apply


def _community_allowed_models_resolver(_org_id: int | None) -> set[str] | None:
    return None  # no restriction -> any configured model may be selected


def _community_redaction_floor_resolver(_db: Session, _org_id: int | None) -> RedactionFloor | None:
    return None  # no tenant floor -> the global redaction.force_* settings apply


_retention_resolver: RetentionResolver = _community_retention_resolver
_min_retention_resolver: MinRetentionResolver = _community_min_retention_resolver
_upload_limits_resolver: UploadLimitsResolver = _community_upload_limits_resolver
_chat_limits_resolver: ChatLimitsResolver = _community_chat_limits_resolver
_allowed_models_resolver: AllowedModelsResolver = _community_allowed_models_resolver
_redaction_floor_resolver: RedactionFloorResolver = _community_redaction_floor_resolver


def set_retention_resolver(
    resolver: RetentionResolver,
    min_resolver: MinRetentionResolver | None = None,
) -> None:
    """Replace the retention resolver (registered by the cloud layer).

    ``min_resolver`` (optional) reports the smallest override in effect so the
    cleanup candidate window can include early-expiring tenants' files;
    defaults to the community no-op.
    """
    global _retention_resolver, _min_retention_resolver
    logger.info("Retention resolver overridden (cloud edition)")
    _retention_resolver = resolver
    if min_resolver is not None:
        _min_retention_resolver = min_resolver


def set_upload_limits_resolver(resolver: UploadLimitsResolver) -> None:
    """Replace the upload-limits resolver (registered by the cloud layer)."""
    global _upload_limits_resolver
    logger.info("Upload-limits resolver overridden (cloud edition)")
    _upload_limits_resolver = resolver


def set_chat_limits_resolver(resolver: ChatLimitsResolver) -> None:
    """Replace the chat-limits resolver (registered by the cloud layer)."""
    global _chat_limits_resolver
    logger.info("Chat-limits resolver overridden (cloud edition)")
    _chat_limits_resolver = resolver


def set_allowed_models_resolver(resolver: AllowedModelsResolver) -> None:
    """Replace the allowed-models resolver (registered by the cloud layer)."""
    global _allowed_models_resolver
    logger.info("Allowed-models resolver overridden (cloud edition)")
    _allowed_models_resolver = resolver


def set_redaction_floor_resolver(resolver: RedactionFloorResolver) -> None:
    """Replace the redaction-floor resolver (registered by the cloud layer)."""
    global _redaction_floor_resolver
    logger.info("Redaction-floor resolver overridden (cloud edition)")
    _redaction_floor_resolver = resolver


def reset_resolvers() -> None:
    """Restore the community resolvers (primarily for tests)."""
    global _retention_resolver, _min_retention_resolver, _upload_limits_resolver
    global _chat_limits_resolver, _allowed_models_resolver, _redaction_floor_resolver
    _retention_resolver = _community_retention_resolver
    _min_retention_resolver = _community_min_retention_resolver
    _upload_limits_resolver = _community_upload_limits_resolver
    _chat_limits_resolver = _community_chat_limits_resolver
    _allowed_models_resolver = _community_allowed_models_resolver
    _redaction_floor_resolver = _community_redaction_floor_resolver


def min_retention_override_days() -> int | None:
    """Smallest per-org retention override in effect (days), or None.

    Best-effort: any error returns None (candidate window stays global).
    """
    try:
        return _min_retention_resolver()
    except Exception:
        logger.exception("min-retention resolver failed; not widening cleanup window")
        return None


def resolve_retention_days(organization_id: int | None) -> int | None:
    """Per-org retention override in days, or ``None`` to use the global setting.

    Best-effort: a misbehaving resolver never breaks cleanup — on any error we
    fall back to the global value (return None).
    """
    try:
        return _retention_resolver(organization_id)
    except Exception:
        logger.exception("retention resolver failed; falling back to global setting")
        return None


def resolve_upload_limits(organization_id: int | None) -> TenantUploadLimits | None:
    """Per-org upload ceiling, or ``None`` to use the global ceiling.

    Best-effort: a misbehaving resolver never blocks uploads — on any error we
    fall back to the global ceiling (return None).
    """
    try:
        return _upload_limits_resolver(organization_id)
    except Exception:
        logger.exception("upload-limits resolver failed; falling back to global ceiling")
        return None


def resolve_chat_limits(organization_id: int | None) -> TenantChatLimits | None:
    """Per-org chat ceilings, or ``None`` to use the admin's global settings.

    Best-effort in the same sense as the other resolvers: a misbehaving resolver
    must not break chat. But note the asymmetry — falling back to "no override"
    here is *permissive*, so a cloud deployment that needs a hard guarantee should
    enforce it in its own before-message hook (which can fail the request), not
    rely on this returning a ceiling.
    """
    try:
        return _chat_limits_resolver(organization_id)
    except Exception:
        logger.exception("chat-limits resolver failed; falling back to global settings")
        return None


def resolve_allowed_models(organization_id: int | None) -> set[str] | None:
    """Model identifiers this tenant may select, or ``None`` for no restriction.

    An empty set means "no model permitted" and is deliberately distinct from
    ``None``; a suspended tenant is expressed that way.

    On resolver failure this returns ``None`` (no restriction) rather than an
    empty set: a broken resolver should not lock every tenant out of the product.
    A deployment that would rather fail closed enforces that in its before-message
    hook, where a request can be rejected outright.
    """
    try:
        return _allowed_models_resolver(organization_id)
    except Exception:
        logger.exception("allowed-models resolver failed; allowing any model")
        return None


def resolve_redaction_floor(db: Session, organization_id: int | None) -> RedactionFloor | None:
    """Per-org redaction floor to UNION with the global one, or ``None`` for no addition.

    Best-effort like the other resolvers, and the fallback here is the least
    alarming of the set: ``None`` does not mean "no redaction", it means "nothing
    on top of the deployment's own ``redaction.force_*`` floor", which is exactly
    what every self-host deployment already gets. A broken cloud resolver
    therefore degrades to core's global policy rather than to no policy.

    It is still a degradation, not a guarantee: a deployment that must never mask
    less than a tenant's contracted floor should make that floor the GLOBAL one,
    because only the global floor is read from a source that cannot raise.

    Args:
        db: The caller's live session, borrowed read-only (see
            ``RedactionFloorResolver``). Never written to or committed by core.
        organization_id: Tenant scope, or None for personal scope.

    Returns:
        The tenant's floor, or None for "no per-tenant addition".
    """
    try:
        return _redaction_floor_resolver(db, organization_id)
    except Exception:
        logger.exception("redaction-floor resolver failed; falling back to the global floor")
        return None
