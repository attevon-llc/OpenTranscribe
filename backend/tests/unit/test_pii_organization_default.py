"""ORGANIZATION must be masked by default, and the residual gap must be visible (#499).

A person's name goes **unmasked** whenever the shipped spaCy model labels it
``ORGANIZATION`` rather than ``PERSON``, because the default entity list excluded
``ORGANIZATION``. That reaches every live-detection surface at once — transcript
segments, search snippets, chat masking and summary masking all share one detector and
one default entity set.

MEASURED against the model this app actually configures, ``en_core_web_sm`` — **not**
the ``en_core_web_lg`` that a bare ``AnalyzerEngine()`` silently downloads:

    "Blackwell will follow up"      -> ORGANIZATION @ 0.85   <- a surname. The leak.
    "Acme Corporation", "Microsoft" -> ORGANIZATION @ 0.85   <- correct, now masked too
    "SSN", "API", "CPU"             -> ORGANIZATION @ 0.85   <- noise, now masked too
    "Dax Okonkwo", "Rivera"         -> no span at all        <- missed ENTIRELY

Two consequences the tests below pin:

1. **A confidence threshold cannot separate them.** Presidio's FAQ recommends tuning the
   acceptance threshold for exactly this, and it does not work here: every NER hit comes
   back at a flat 0.85. Raising ``pii_confidence`` only loses recall.
2. **Including ORGANIZATION does not make detection exhaustive.** Names that produce no
   span at all are unaffected by it. That residual has to be *told to the user*, not
   left to be discovered, which is why the warning string is asserted here too.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOCALES = _REPO_ROOT / "frontend" / "src" / "lib" / "i18n" / "locales"
_WARNING_KEY = "settings.contentRedaction.piiEntitiesWarning"


def test_organization_is_masked_by_default() -> None:
    """The fix: a name mislabelled ORGANIZATION must not survive the default policy."""
    from app.core.constants import DEFAULT_REDACTION_PII_ENTITIES

    assert "ORGANIZATION" in DEFAULT_REDACTION_PII_ENTITIES, (
        "ORGANIZATION is excluded from the default PII entities, so a person's name that "
        "spaCy labels ORGANIZATION (measured: 'Blackwell') ships in clear to a user who "
        "explicitly enabled PII masking"
    )


def test_the_default_covers_every_supported_entity() -> None:
    """No entity may be silently absent from the default.

    The previous default was built by *subtracting* from the supported list, which is
    how one entity came to be excluded with the reason recorded only in a comment. If a
    future entity should be off by default, that needs its own measured argument.
    """
    from app.core.constants import DEFAULT_REDACTION_PII_ENTITIES
    from app.core.constants import REDACTION_PII_ENTITIES

    missing = sorted(set(REDACTION_PII_ENTITIES) - set(DEFAULT_REDACTION_PII_ENTITIES))
    assert not missing, f"these supported PII entities are not masked by default: {missing}"


def test_pii_masking_remains_opt_in() -> None:
    """The blast radius is bounded, and that is what makes the trade acceptable.

    Masking company names and acronyms is a real cost. It is only paid by users who
    turned PII masking on — `pii` is deliberately not a default masking category. If
    that ever changes, the trade in #499 needs re-arguing, so this fails loudly.
    """
    from app.core.constants import DEFAULT_REDACTION_CATEGORIES

    assert "pii" not in DEFAULT_REDACTION_CATEGORIES, (
        "PII became a default masking category. ORGANIZATION is in the default entity "
        "list on the assumption that PII masking is opt-in, which bounded the cost of "
        "masking company names — re-read the argument in constants.py before shipping this"
    )


def test_the_confidence_threshold_is_not_being_used_as_the_lever() -> None:
    """Pin the measurement that rules out the obvious remedy.

    Every NER hit from `en_core_web_sm` scores a flat 0.85 — real names, company names
    and acronyms alike — so no value of this threshold separates them. It is kept well
    below 0.85 so that it filters nothing from this model, which is the honest state.
    Raising it toward 0.85 would silently drop real PII.
    """
    from app.core.constants import DEFAULT_REDACTION_PII_CONFIDENCE

    assert DEFAULT_REDACTION_PII_CONFIDENCE < 0.85, (
        f"pii_confidence is {DEFAULT_REDACTION_PII_CONFIDENCE}, at or above the flat 0.85 "
        "that en_core_web_sm assigns every NER hit — this drops real PII rather than noise"
    )


@pytest.mark.parametrize("locale", ["en", "de", "es", "fr", "ja", "pt", "ru", "zh"])
def test_the_residual_gap_is_disclosed_in_every_language(locale: str) -> None:
    """Users must be TOLD detection is not exhaustive, in their own language.

    Adding ORGANIZATION closes one cause of the leak. Names that produce no span at all
    are untouched by it, and a user who believes masking is complete will share an
    export that is not. A missing key renders the raw key string, which discloses
    nothing.
    """
    data = json.loads((_LOCALES / f"{locale}.json").read_text(encoding="utf-8"))

    warning = data.get(_WARNING_KEY)
    assert warning, f"{locale}.json has no {_WARNING_KEY} — that language gets no warning"
    assert len(warning) > 40, f"{locale} warning is too short to convey the caveat: {warning!r}"
    assert warning != _WARNING_KEY


def test_the_warning_is_actually_rendered() -> None:
    """A translated string nothing displays warns nobody.

    The key existing in eight files proves translation, not disclosure. This asserts the
    component references it, which is the half that reaches a user.
    """
    component = (
        _REPO_ROOT
        / "frontend"
        / "src"
        / "components"
        / "settings"
        / "ContentRedactionSettings.svelte"
    ).read_text(encoding="utf-8")

    assert _WARNING_KEY in component, (
        "the PII warning is translated but never rendered, so no user ever sees it"
    )
