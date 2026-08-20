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


def normalize_language(language: str | None) -> str:
    """Normalize a transcript language to a 2-letter code; '' / 'auto' / None → 'en'."""
    if not language or language.lower() in ("auto", "und", "unknown"):
        return "en"
    return language.lower().split("-")[0].split("_")[0]


def detector_language_support(language: str | None) -> tuple[set[str], dict[str, str]]:
    """Which detectors support ``language``. Returns (supported_detectors, skipped{detector: reason}).

    ``profanity``/``custom`` ride the profanity wordlist's language support. The ``llm``
    detector is provider-dependent and never language-gated here.
    """
    lang = normalize_language(language)
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
