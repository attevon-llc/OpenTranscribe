"""Effective redaction config = per-user preferences ∪ admin-forced floor.

Per-user prefs live in ``UserSetting`` (``redaction_*`` keys); the admin governance
floor lives in ``SystemSettings`` (``redaction.force_*`` keys). A forced category is
always enabled and the user cannot disable it. There are NO ``.env`` vars — defaults
are coded constants in ``app.core.constants``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field

from sqlalchemy.orm import Session

from app.core import constants as C  # noqa: N812

# Re-imported, NOT redefined (issue #545). The copy that used to live here returned "en" for
# every sentinel and echoed everything else verbatim, which is what made `detector_language_
# support` below fail OPEN. The name stays exported because `redaction/service.py` imports it
# from this module; what must not exist twice is the implementation.
from app.utils.language import normalize_language

logger = logging.getLogger(__name__)

# Detector → the categories whose MASKING depends on it. The ONE copy of this
# mapping: every fail-closed masker needs it to decide whether a detector failure
# is one the user's policy cares about, and a second copy drifts silently
# (``blocking_detector_failures`` below is the shared reader).
#
# ⚠️ ``toxicity`` maps to NOTHING, and that is the entry with a decision in it. The
# toxicity detector emits a per-segment SCORE, never a ``RedactionSpan`` — read
# ``detectors/toxicity.py``, and note that ``is_segment_toxic`` is consumed only by
# ``formatting_service`` to flag a segment in the UI. So its absence cannot leave one
# character unmasked, and making it blocking would withhold text on the strength of a
# detector that never masks any. The consequences are not hypothetical: ``toxicity``
# IS a default category, so a box without the ~500 MB toxic-bert weights would mark
# every default-configured user's file stale on every segment edit and refuse every
# LLM feature — for a gap with no text in it. The ``toxicity`` *category* still has
# maskable spans; they come from ``llm``, which is why that entry keeps all four.
# A toxicity outage is reported instead — ``skipped_detectors`` and
# ``media_file.redaction_coverage`` — which is what a missing toxicity FLAG deserves.
_DETECTOR_CATEGORIES: dict[str, set[str]] = {
    "profanity": {"profanity", "custom"},
    "pii": {"pii"},
    "toxicity": set(),
    "llm": {"pii", "toxicity", "profanity", "custom"},
}

_USER_KEYS = (
    "redaction_enabled",
    "redaction_detectors",
    "redaction_categories",
    "redaction_pii_entities",
    "redaction_style",
    "redaction_custom_words",
    "redaction_allowlist",
    "redaction_toxicity_threshold",
    "redaction_redact_before_llm",
    "redaction_default_export_redacted",
)


@dataclass
class EffectiveRedactionConfig:
    """Resolved, ready-to-use redaction config for one user."""

    enabled: bool = True
    detectors: set[str] = field(default_factory=set)
    enabled_categories: set[str] = field(default_factory=set)
    locked_categories: set[str] = field(default_factory=set)  # admin-forced, non-disableable
    pii_entities: set[str] = field(default_factory=set)
    style: str = C.DEFAULT_REDACTION_STYLE
    custom_words: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    toxicity_threshold: float = C.DEFAULT_REDACTION_TOXICITY_THRESHOLD
    redact_before_llm: bool = C.DEFAULT_REDACTION_REDACT_BEFORE_LLM
    # Admin force floor (`redaction.force_redact_before_llm`). When set, the
    # per-provider local-model exemption (`llm_guard.is_local_provider`) must be
    # ignored and masking applied regardless of where the model runs — the
    # admin is mandating masked text for every provider, not only external ones.
    redact_before_llm_locked: bool = False
    export_redacted: bool = C.DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED
    export_locked: bool = False  # admin mandates censored exports

    def reveal_categories(self, requested: bool, is_owner: bool) -> set[str]:
        """Categories an authorized owner may reveal (everything enabled except forced)."""
        if not requested or not is_owner:
            return set()
        return self.enabled_categories - self.locked_categories


def _parse_list(raw: str | None, default: list[str]) -> list[str]:
    if raw is None or raw == "":
        return list(default)
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except (ValueError, TypeError):
        pass
    # Fallback: comma-separated.
    return [p.strip() for p in raw.split(",") if p.strip()]


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return str(raw).lower() in ("true", "1", "yes", "on")


def _parse_float(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw is not None else default
    except (ValueError, TypeError):
        return default


def _load_user_prefs(db: Session, user_id: int) -> dict[str, str]:
    from app import models

    rows = (
        db.query(models.UserSetting)
        .filter(
            models.UserSetting.user_id == user_id,
            models.UserSetting.setting_key.in_(_USER_KEYS),
        )
        .all()
    )
    return {str(r.setting_key): str(r.setting_value) for r in rows}


_ADMIN_POLICY_KEYS = (
    "redaction.force_pii",
    "redaction.force_toxicity",
    "redaction.force_profanity",
    "redaction.force_pii_entities",
    "redaction.force_custom_words",
    "redaction.force_toxicity_threshold",
    "redaction.force_export_redacted",
    "redaction.force_redact_before_llm",
)


def _load_admin_policy(db: Session) -> dict:
    """Load the admin-forced governance floor from SystemSettings.

    Reads all eight policy keys in a single SELECT (via ``get_settings_map``)
    instead of one round-trip per key.
    """
    from app.services.system_settings_service import get_settings_map

    policy = get_settings_map(db, list(_ADMIN_POLICY_KEYS))

    def g(key: str) -> str | None:
        return policy.get(key)

    forced_categories: set[str] = set()
    if _parse_bool(g("redaction.force_pii"), False):
        forced_categories.add("pii")
    if _parse_bool(g("redaction.force_toxicity"), False):
        forced_categories.add("toxicity")
    if _parse_bool(g("redaction.force_profanity"), False):
        forced_categories.add("profanity")

    return {
        "forced_categories": forced_categories,
        "forced_pii_entities": set(_parse_list(g("redaction.force_pii_entities"), [])),
        "forced_custom_words": _parse_list(g("redaction.force_custom_words"), []),
        "force_toxicity_threshold": _parse_float(
            g("redaction.force_toxicity_threshold"), C.DEFAULT_REDACTION_TOXICITY_THRESHOLD
        ),
        "force_export_redacted": _parse_bool(g("redaction.force_export_redacted"), False),
        "force_redact_before_llm": _parse_bool(g("redaction.force_redact_before_llm"), False),
    }


def resolve_effective_config(db: Session, user_id: int) -> EffectiveRedactionConfig:
    """Resolve a user's effective redaction config (user prefs ∪ admin force)."""
    prefs = _load_user_prefs(db, user_id)
    admin = _load_admin_policy(db)

    user_enabled = _parse_bool(prefs.get("redaction_enabled"), C.DEFAULT_REDACTION_ENABLED)
    user_detectors = set(
        _parse_list(prefs.get("redaction_detectors"), C.DEFAULT_REDACTION_DETECTORS)
    )
    user_categories = set(
        _parse_list(prefs.get("redaction_categories"), C.DEFAULT_REDACTION_CATEGORIES)
    )
    user_pii = set(
        _parse_list(prefs.get("redaction_pii_entities"), C.DEFAULT_REDACTION_PII_ENTITIES)
    )

    forced_categories: set[str] = admin["forced_categories"]
    # A forced category implies its detector runs and its category is masked.
    detectors = set(user_detectors)
    for cat in forced_categories:
        if cat in ("profanity", "custom"):
            detectors.add("profanity")
        elif cat == "pii":
            detectors.add("pii")
        elif cat == "toxicity":
            detectors.add("toxicity")

    enabled_categories = user_categories | forced_categories
    # If the user disabled redaction entirely but admin forces something, force wins.
    enabled = user_enabled or bool(forced_categories)
    if not enabled:
        enabled_categories = set()
        detectors = set()

    toxicity_threshold = _parse_float(
        prefs.get("redaction_toxicity_threshold"), C.DEFAULT_REDACTION_TOXICITY_THRESHOLD
    )
    if "toxicity" in forced_categories:
        toxicity_threshold = min(toxicity_threshold, admin["force_toxicity_threshold"])

    return EffectiveRedactionConfig(
        enabled=enabled,
        detectors=detectors,
        enabled_categories=enabled_categories,
        locked_categories=forced_categories,
        pii_entities=user_pii | admin["forced_pii_entities"],
        style=prefs.get("redaction_style") or C.DEFAULT_REDACTION_STYLE,
        custom_words=_parse_list(prefs.get("redaction_custom_words"), [])
        + admin["forced_custom_words"],
        allowlist=_parse_list(prefs.get("redaction_allowlist"), []),
        toxicity_threshold=toxicity_threshold,
        redact_before_llm=_parse_bool(
            prefs.get("redaction_redact_before_llm"), C.DEFAULT_REDACTION_REDACT_BEFORE_LLM
        )
        or admin["force_redact_before_llm"],
        redact_before_llm_locked=admin["force_redact_before_llm"],
        export_redacted=_parse_bool(
            prefs.get("redaction_default_export_redacted"),
            C.DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED,
        )
        or admin["force_export_redacted"],
        export_locked=admin["force_export_redacted"],
    )


#: Every style, most→least protective for an EGRESS decision (never for display —
#: see the docstring on :func:`_stricter_style`). ``blur`` is deliberately LAST: it
#: embeds the ORIGINAL, unmasked text in an HTML attribute for the UI's
#: reveal-on-hover affordance (``spans.py::_placeholder``), so it is strictly less
#: protective than doing nothing category-wise wrong — it is a display style that
#: must never be allowed to win a strictest-wins union.
_STYLE_STRICTNESS: dict[str, int] = {"label": 3, "asterisks": 2, "first_letter": 1, "blur": 0}


def _stricter_style(a: str, b: str) -> str:
    """The more protective of two masking styles, for an egress-only union.

    Not a general-purpose style preference: ``blur``'s placeholder carries the
    original text (see the module-level constant above), which is fine for a
    reveal-on-hover UI and never fine for text about to leave the deployment as
    part of an LLM prompt. So a union involving ``blur`` prefers whichever style
    it is paired with, never ``blur`` itself, even though ``VALID_STYLES`` lists
    it as an ordinary choice.
    """
    return a if _STYLE_STRICTNESS.get(a, 0) >= _STYLE_STRICTNESS.get(b, 0) else b


def most_restrictive_config() -> EffectiveRedactionConfig:
    """The fail-closed policy: every category on, nothing exempted, masking mandatory.

    Used whenever a policy this deployment needs to combine with (union) cannot be
    resolved at all — a missing owner, a DB error resolving their preferences, or a
    file whose owner id could not be determined. "Most restrictive policy available"
    (task #40) means: if we cannot tell what the unreadable policy would have said,
    assume it says mask everything, rather than silently treating the unreadable
    side as if it had no opinion (which is exactly how a resolvable-but-permissive
    policy would look, and unioning with it either way could pass raw text).

    ``allowlist`` is intentionally EMPTY: an allowlist is a "never mask this" list,
    and unioning allowlists is only safe as an INTERSECTION (see
    :func:`union_effective_config`) — the empty list here is the identity element
    for that intersection, so this config exempts nothing.
    """
    return EffectiveRedactionConfig(
        enabled=True,
        detectors={"pii", "profanity", "toxicity"},
        enabled_categories={"pii", "toxicity", "profanity", "custom"},
        locked_categories=set(),
        pii_entities=set(C.REDACTION_PII_ENTITIES),
        style=C.DEFAULT_REDACTION_STYLE,
        custom_words=[],
        allowlist=[],
        toxicity_threshold=0.0,
        redact_before_llm=True,
        # Deliberately True even though no admin actually forced anything: a
        # fail-closed stand-in for an unreadable policy must not be quietly
        # unwound by the local-provider exemption either. In practice this
        # never matters for that exemption (`chat/redactor._gather` decides it
        # from the REQUESTER's config alone, before any owner lookup runs —
        # see that module's docstring), but a future caller unioning this value
        # directly must not be able to read it as "not mandated".
        redact_before_llm_locked=True,
        export_redacted=True,
        export_locked=True,
    )


def union_effective_config(
    a: EffectiveRedactionConfig, b: EffectiveRedactionConfig
) -> EffectiveRedactionConfig:
    """Strictest-wins union of two resolved policies governing the SAME egress decision.

    Task #40. Chat's egress masking used to resolve a single subject's config — first
    the plan's choice (the file owner), then issue #402's shipped choice (the
    requester) — and both are wrong in one direction: requester-subject lets a sharee
    whose own policy is permissive read PII the file's owner meant to hide,
    owner-subject ignores a stricter requester-side mandate. The fix is neither
    subject alone: mask if EITHER policy says to, with the union of what they mask.

    Every field takes the MORE PROTECTIVE of the two:

    * ``enabled`` / ``redact_before_llm`` — True if either is True (OR).
    * ``enabled_categories`` / ``locked_categories`` / ``detectors`` /
      ``pii_entities`` — the union (more masked, not less).
    * ``custom_words`` — concatenated and de-duplicated (case-insensitively, first
      occurrence wins the casing) — a word either side wants masked stays masked.
    * ``allowlist`` — the INTERSECTION, not the union. An allowlist is a "never mask
      this" exemption, so unioning it would be the exact opposite of strictest-wins:
      it would let MORE through unmasked. Only a word BOTH policies exempt survives.
    * ``style`` — the more protective placeholder (:func:`_stricter_style`); never
      ``blur``, which embeds the original text for the UI's own reveal affordance
      and is never safe for text about to leave the deployment.
    * ``toxicity_threshold`` — the lower (more sensitive) of the two.
    * ``redact_before_llm_locked`` / ``export_locked`` — OR. In production both
      operands read this off the SAME deployment-wide admin setting
      (``resolve_effective_config``'s ``admin["force_redact_before_llm"]``), so this
      is normally a no-op; it is unioned anyway so a caller handed two policies from
      different sources (e.g. one already a fail-closed stand-in) never loses the
      lock by construction.

    Args:
        a: One resolved policy (either subject).
        b: The other.

    Returns:
        A new :class:`EffectiveRedactionConfig` that is at least as protective as
        either input on every field. Every attribute read is via ``getattr`` with a
        safe-side default so a duck-typed test double (a ``SimpleNamespace`` missing
        fields this module has grown since the double was written) still unions
        correctly instead of raising.

    Note:
        ``a is b`` returns ``a`` unchanged rather than rebuilding an equal object.
        This is a real optimization (the common case — a file the requester owns —
        resolves the SAME config object for both subjects, see
        ``chat/redactor._effective_cfg_for_owner``) and it is also what keeps every
        existing single-subject caller and test byte-for-byte unaffected when a
        test double supplies one canned config for every ``resolve_effective_config``
        call: the union of a policy with itself is that exact policy, not a
        reconstruction of one that merely compares equal.
    """
    if a is b:
        return a

    enabled = bool(getattr(a, "enabled", False)) or bool(getattr(b, "enabled", False))
    enabled_categories = set(getattr(a, "enabled_categories", None) or set()) | set(
        getattr(b, "enabled_categories", None) or set()
    )
    locked_categories = set(getattr(a, "locked_categories", None) or set()) | set(
        getattr(b, "locked_categories", None) or set()
    )
    detectors = set(getattr(a, "detectors", None) or set()) | set(
        getattr(b, "detectors", None) or set()
    )
    pii_entities = set(getattr(a, "pii_entities", None) or set()) | set(
        getattr(b, "pii_entities", None) or set()
    )
    custom_words = _dedupe_casefold(
        list(getattr(a, "custom_words", None) or []) + list(getattr(b, "custom_words", None) or [])
    )
    allowlist = _intersect_casefold(
        list(getattr(a, "allowlist", None) or []), list(getattr(b, "allowlist", None) or [])
    )
    style = _stricter_style(
        str(getattr(a, "style", None) or C.DEFAULT_REDACTION_STYLE),
        str(getattr(b, "style", None) or C.DEFAULT_REDACTION_STYLE),
    )
    toxicity_threshold = min(
        float(getattr(a, "toxicity_threshold", C.DEFAULT_REDACTION_TOXICITY_THRESHOLD)),
        float(getattr(b, "toxicity_threshold", C.DEFAULT_REDACTION_TOXICITY_THRESHOLD)),
    )
    redact_before_llm = bool(getattr(a, "redact_before_llm", False)) or bool(
        getattr(b, "redact_before_llm", False)
    )
    redact_before_llm_locked = bool(getattr(a, "redact_before_llm_locked", False)) or bool(
        getattr(b, "redact_before_llm_locked", False)
    )
    export_redacted = bool(getattr(a, "export_redacted", False)) or bool(
        getattr(b, "export_redacted", False)
    )
    export_locked = bool(getattr(a, "export_locked", False)) or bool(
        getattr(b, "export_locked", False)
    )

    return EffectiveRedactionConfig(
        enabled=enabled,
        detectors=detectors,
        enabled_categories=enabled_categories,
        locked_categories=locked_categories,
        pii_entities=pii_entities,
        style=style,
        custom_words=custom_words,
        allowlist=allowlist,
        toxicity_threshold=toxicity_threshold,
        redact_before_llm=redact_before_llm,
        redact_before_llm_locked=redact_before_llm_locked,
        export_redacted=export_redacted,
        export_locked=export_locked,
    )


def _dedupe_casefold(words: list[str]) -> list[str]:
    """First-occurrence-wins de-duplication, case-insensitive."""
    seen: set[str] = set()
    out: list[str] = []
    for word in words:
        key = word.strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(word)
    return out


def _intersect_casefold(a: list[str], b: list[str]) -> list[str]:
    """Entries of ``a`` whose casefolded form also appears in ``b``, order from ``a``."""
    b_folded = {w.strip().casefold() for w in b}
    return [w for w in a if w.strip().casefold() in b_folded]


def redaction_is_in_use(db: Session) -> bool:
    """Does ANY user have redaction on, or does the admin floor force a category?

    The question a process asks before spending ~7 s and ~500 MB warming a
    detector it may never call (issue #74). Redaction is **opt-out**
    (``DEFAULT_REDACTION_ENABLED`` is False), so on most deployments the answer
    is no and nothing should be loaded.

    It is deliberately not "does anyone mask the ``pii`` category". Every inline
    masker runs :func:`detection_config_for_all`, which runs **all** detectors
    regardless of which categories a user masks — so a single user with
    ``redaction_enabled`` is enough to make Presidio load, whatever their
    categories are. Narrowing this to ``pii`` would skip the warm-up on exactly
    the deployments that still pay the cold load.

    Any admin-forced category is likewise sufficient on its own:
    :func:`resolve_effective_config` resolves ``enabled = user_enabled or
    bool(forced_categories)``, so a floor turns masking on for everyone.

    Args:
        db: Database session. Two short reads; holds nothing open.

    Returns:
        True if some user's or the admin's policy can activate masking.
    """
    from app import models

    if _load_admin_policy(db)["forced_categories"]:
        return True

    # DISTINCT over the values, not a row per user: the answer is a property of
    # the deployment, and the parse stays the one in this module rather than a
    # second truthiness rule written in SQL.
    values = (
        db.query(models.UserSetting.setting_value)
        .filter(models.UserSetting.setting_key == "redaction_enabled")
        .distinct()
        .all()
    )
    return any(_parse_bool(row[0], False) for row in values)


def blocking_detector_failures(
    failures: Iterable[str], enabled_categories: Collection[str]
) -> set[str]:
    """Which detector failures actually matter for a policy masking ``enabled_categories``.

    ``detect_segment_spans`` **swallows** a detector exception and returns the
    spans it did collect, so "found nothing" and "could not look" are the same
    return value; its ``failures`` sink (issue #324) is the only thing that tells
    them apart. Any masker that must fail closed asks this function whether a
    recorded failure is one this user's policy cares about.

    The narrowness is the point. ``pii`` is **not** in the default categories, so
    treating every failure as blocking would withhold content wholesale on every
    CPU-only deployment that has no Presidio and never asked for PII masking.
    Only a failure of a detector feeding an *enabled* category may withhold.

    ``failures`` names DETECTORS while ``enabled_categories`` names CATEGORIES;
    they coincide for ``pii``, diverge for ``profanity`` (which also produces
    ``custom``), and for ``toxicity`` do not correspond at all — it produces no
    spans, so nothing it fails to find can be left unmasked. The mapping above is
    written out rather than assumed for exactly those two cases.

    Args:
        failures: Detector names recorded by ``detect_segment_spans``.
        enabled_categories: The categories this policy masks.

    Returns:
        The subset of ``failures`` that feeds an enabled category. Empty means
        nothing the caller masks was left unchecked.
    """
    enabled = set(enabled_categories)
    return {name for name in failures if _DETECTOR_CATEGORIES.get(name, {name}) & enabled}


def detector_language_support(language: str | None) -> tuple[set[str], dict[str, str]]:
    """Which detectors support ``language``. Returns (supported_detectors, skipped{detector: reason}).

    ``profanity``/``custom`` ride the profanity wordlist's language support. The ``llm``
    detector is provider-dependent and never language-gated here.

    ⚠️ **An undeterminable language fails CLOSED — every detector stays required.** A language
    skip is not a coverage gap (``coverage.uncovered_detectors`` subtracts it first, and
    ``redaction/CLAUDE.md`` argues why: profanity and PII are English-only *by design*,
    identically on every future scan and unfixable by any operator action). That argument is
    only sound for a language we actually identified. Before #545 the normalizer never
    stripped and never validated, so ``"eng"``, ``"English"`` and ``"en "`` were compared
    verbatim against ``REDACTION_PII_LANGUAGES`` (``{"en"}``), found absent, and **dropped the
    detector** — after which coverage subtracted the skip and reported clean. The control
    turned itself off and then reported itself healthy.

    The fix is emphatically **not** "fall back to English": that maps ``"fra"`` onto ``"en"``,
    runs the English PII detector over French text and records full coverage, which is a *new*
    fail-open. ``normalize_language`` returns ``None`` for anything it cannot identify and
    ``None`` is treated as "no detector may excuse itself", so whatever the scan did not run
    surfaces as a real gap in ``coverage.py``. A *recognised* language that genuinely lacks a
    detector (``fr`` has no PII detector) keeps today's behaviour: a legitimate, reported skip.
    """
    lang = normalize_language(language)
    if lang is None:
        return set(_DETECTOR_CATEGORIES), {}
    supported: set[str] = {"llm"}
    skipped: dict[str, str] = {}
    if lang in C.REDACTION_PROFANITY_LANGUAGES:
        supported.add("profanity")
    else:
        skipped["profanity"] = lang
    if lang in C.REDACTION_PII_LANGUAGES:
        supported.add("pii")
    else:
        skipped["pii"] = lang
    if lang in C.REDACTION_TOXICITY_LANGUAGES:
        supported.add("toxicity")
    else:
        skipped["toxicity"] = lang
    return supported, skipped


def detection_config_for_all() -> dict:
    """Config used by the detection task — it always runs ALL expensive detectors and
    caches ALL categories, independent of any user/admin setting (toggling is read-time).
    """
    return {
        "pii_entities": list(C.REDACTION_PII_ENTITIES),
        "pii_confidence": C.DEFAULT_REDACTION_PII_CONFIDENCE,
        "toxicity_threshold": 0.0,  # store all scores; threshold applied at read time
    }
