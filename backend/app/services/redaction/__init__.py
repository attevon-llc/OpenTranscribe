"""Content redaction package.

Detect profane / offensive / toxic words and PII in transcripts, cache the
detection *spans* (the original transcript text is never modified), and apply
masking as a cheap read-time transform at every display/export surface.

Public surface:
- ``RedactionSpan`` / ``apply_redactions`` — span model + the read-time masker.
- ``EffectiveRedactionConfig`` / ``resolve_effective_config`` — per-user prefs
  unioned with the admin-forced floor.
- ``RedactionService`` — orchestrates detection (cached) + read-time masking.

Heavy ML dependencies (presidio, gliner, detoxify, torch) are imported lazily
inside the detector modules so this package imports cleanly in the API process
and in unit tests without those libraries installed.
"""

from app.services.redaction.config import EffectiveRedactionConfig
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.spans import RedactionSpan
from app.services.redaction.spans import apply_redactions

__all__ = [
    "RedactionSpan",
    "apply_redactions",
    "EffectiveRedactionConfig",
    "resolve_effective_config",
]
