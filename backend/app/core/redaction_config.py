"""The resolved redaction policy for one user.

Lives in ``app.core`` so lower layers (``app.utils``) can type against it; it is built and
combined by :mod:`app.services.redaction.config`, which still exposes the name.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field

from app.core import redaction_defaults


@dataclass
class EffectiveRedactionConfig:
    """Resolved, ready-to-use redaction config for one user."""

    enabled: bool = True
    detectors: set[str] = field(default_factory=set)
    enabled_categories: set[str] = field(default_factory=set)
    locked_categories: set[str] = field(default_factory=set)  # admin-forced, non-disableable
    pii_entities: set[str] = field(default_factory=set)
    style: str = redaction_defaults.DEFAULT_REDACTION_STYLE
    custom_words: list[str] = field(default_factory=list)
    allowlist: list[str] = field(default_factory=list)
    toxicity_threshold: float = redaction_defaults.DEFAULT_REDACTION_TOXICITY_THRESHOLD
    redact_before_llm: bool = redaction_defaults.DEFAULT_REDACTION_REDACT_BEFORE_LLM
    # Admin force floor (`redaction.force_redact_before_llm`). When set, the
    # per-provider local-model exemption (`llm_guard.is_local_provider`) must be
    # ignored and masking applied regardless of where the model runs — the
    # admin is mandating masked text for every provider, not only external ones.
    redact_before_llm_locked: bool = False
    export_redacted: bool = redaction_defaults.DEFAULT_REDACTION_DEFAULT_EXPORT_REDACTED
    export_locked: bool = False  # admin mandates censored exports

    @property
    def masks_anything(self) -> bool:
        """Whether this policy would mask any text at read time.

        The single definition of "redaction is enabled" for the UI and the rescan
        action: the master switch is on (user preference or admin force) AND at least
        one category is enabled. Enabled-with-no-categories masks nothing, so offering
        a scan there is as pointless as with the switch off.
        """
        return bool(self.enabled and self.enabled_categories)

    def reveal_categories(self, requested: bool, is_owner: bool) -> set[str]:
        """Categories an authorized owner may reveal (everything enabled except forced)."""
        if not requested or not is_owner:
            return set()
        return self.enabled_categories - self.locked_categories
