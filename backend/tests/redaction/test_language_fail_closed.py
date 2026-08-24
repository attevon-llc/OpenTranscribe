"""Two ways redaction reported itself healthy while having examined nothing.

Both have the same shape — the control turns itself off and then reports itself clean — and
both are closed by the same rule: **a detector may only be subtracted from the coverage
report for a reason that is still true tomorrow.**

**B5 — an unrecognised language silently disabled PII, profanity and toxicity.**
``detector_language_support`` normalized its input with a parser that never stripped and never
validated, so ``"eng"``/``"English"``/``"en "`` fell straight through, were found absent from
``REDACTION_PII_LANGUAGES`` (``{"en"}``), and the detector was dropped. ``coverage.py``
subtracts a language skip *first* — correctly, because profanity and PII are English-only by
design and no operator action can change that — so the surface reported **no gap**. The fix is
not "fall back to English": that maps ``"fra"`` onto ``"en"``, runs the English PII detector
over French text and records full coverage, which is a *new* fail-open. It is that an
unidentifiable value normalizes to ``None`` and ``None`` fails CLOSED — every detector stays
required, so whatever the scan did not run shows up as a real gap.

**B6 — the ``llm`` detector was recorded as covered even when it raised.**
``detectors/llm.py`` swallows any provider fault and returns ``{}``, which is byte-identical to
"the model found nothing". ``detect_and_store`` appended ``"llm"`` to ``redaction_coverage``
whenever the owner had selected it, so a misconfigured provider read as a completed scan with
zero findings.

Every assertion here is paired with a control that keeps the fix narrow: a *recognised*
language that genuinely lacks a detector (``fr`` has no PII detector) must still produce a
legitimate, reported skip, so "return every detector for every language" cannot pass; and a
working LLM detector must still be recorded as covered.
"""

from __future__ import annotations

import uuid as uuid_pkg
from typing import Any

import pytest

from app.services.llm_service import LLMService
from app.services.redaction.config import detector_language_support
from app.services.redaction.config import resolve_effective_config
from app.services.redaction.coverage import uncovered_detectors
from app.services.redaction.detectors import pii_presidio
from app.services.redaction.detectors import toxicity as tox
from app.services.redaction.service import RedactionService

SEGMENT_TEXT = "call me on 555-867-5309 about the invoice"


# ------------------------------------------------------------------------ fixtures


class _NullAnalyzer:
    """Presidio present and finding nothing — keeps the weights out of the fast suite."""

    def analyze(self, text: str, language: str) -> list:  # noqa: ARG002
        return []


class _FakeLLMResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeLLMService:
    """Same stand-in shape as ``tests/unit/test_redaction_llm_detector.py``."""

    def __init__(self, content: str = "[]", *, raise_on_chat: bool = False) -> None:
        self._content = content
        self._raise_on_chat = raise_on_chat

    def chat_completion(self, messages: list[dict[str, str]], **kwargs: Any) -> _FakeLLMResponse:  # noqa: ARG002
        if self._raise_on_chat:
            raise RuntimeError("simulated LLM transport failure")
        return _FakeLLMResponse(self._content)

    def close(self) -> None:
        return None


@pytest.fixture
def quiet_detectors(monkeypatch):
    """Presidio and toxicity both available and both finding nothing."""
    monkeypatch.setattr(pii_presidio, "_get_analyzer", lambda _use_gliner: _NullAnalyzer())
    monkeypatch.setattr(tox, "score_text", lambda _text, _lang: None)
    monkeypatch.setattr(tox, "score_texts", lambda texts, _lang: [None] * len(texts))


def _install_llm(monkeypatch, service) -> None:
    """``detect_with_llm`` imports ``LLMService`` inline, so the class is the seam."""
    monkeypatch.setattr(
        LLMService, "create_from_settings", staticmethod(lambda user_id=None: service)
    )


def _set_prefs(db_session, user, **prefs: str) -> None:
    from app import models

    for key, value in prefs.items():
        db_session.add(models.UserSetting(user_id=user.id, setting_key=key, setting_value=value))
    db_session.flush()


@pytest.fixture
def pii_masking_user(db_session, normal_user):
    """An owner who opted into PII masking — which is what makes a PII gap blocking."""
    _set_prefs(
        db_session,
        normal_user,
        redaction_enabled="true",
        redaction_categories='["profanity", "toxicity", "custom", "pii"]',
        redaction_redact_before_llm="true",
    )
    return normal_user


@pytest.fixture
def llm_detector_user(db_session, normal_user):
    """An owner who additionally opted into the LLM detector (it is off by default)."""
    _set_prefs(
        db_session,
        normal_user,
        redaction_enabled="true",
        redaction_categories='["profanity", "toxicity", "custom", "pii"]',
        redaction_detectors='["profanity", "pii", "toxicity", "llm"]',
        redaction_redact_before_llm="true",
    )
    return normal_user


def _seed_unscanned_file(db_session, user, language: str | None = "en"):
    from app.core.enums import FileStatus
    from app.models.media import MediaFile
    from app.models.media import TranscriptSegment

    media = MediaFile(
        uuid=uuid_pkg.uuid4(),
        user_id=user.id,
        filename=f"faildosed-{uuid_pkg.uuid4().hex[:8]}.wav",
        storage_path=f"redaction-language-test/{uuid_pkg.uuid4().hex}",
        file_size=1,
        content_type="audio/wav",
        language=language,
        status=FileStatus.COMPLETED,
    )
    db_session.add(media)
    db_session.flush()
    db_session.add(
        TranscriptSegment(
            uuid=uuid_pkg.uuid4(),
            media_file_id=media.id,
            start_time=0.0,
            end_time=5.0,
            text=SEGMENT_TEXT,
            redactions=None,
        )
    )
    db_session.flush()
    return media


# ------------------------------------------------------------- B5: the language fail-open


@pytest.mark.parametrize("stored", ["eng", "English", "en ", " en", "en-US"])
def test_a_provider_spelling_of_english_still_supports_the_pii_detector(stored) -> None:
    """THE DEFECT. Each of these silently dropped PII, profanity AND toxicity.

    They are not exotic — ``en `` is a trailing space, ``eng`` is ISO 639-2, and ``English``
    is what more than one cloud provider returns.
    """
    supported, skipped = detector_language_support(stored)

    assert "pii" in supported, skipped
    assert "profanity" in supported, skipped
    assert "toxicity" in supported, skipped


@pytest.mark.parametrize("stored", [None, "", "auto", "und", "unknown", "Klingon", "zz"])
def test_an_undeterminable_language_keeps_every_detector_required(stored) -> None:
    """Fail CLOSED: "we could not determine the language" is not "this detector is exempt".

    A skip is subtracted from the coverage report before anything else, on the argument that
    an English-only detector could never have run for e.g. Spanish and never will. That
    argument does not hold for a value nobody could parse, so the detector stays required and
    whatever the scan did not run is reported as a real gap.
    """
    supported, skipped = detector_language_support(stored)

    assert {"pii", "profanity", "toxicity", "llm"} <= supported, skipped
    assert skipped == {}, "an unparseable value must not be recorded as a legitimate skip"


def test_a_recognised_language_without_a_detector_still_skips_legitimately() -> None:
    """CONTROL. "Return every detector for every language" must NOT pass this.

    French has no PII detector and no profanity wordlist — a declared, permanent product
    limit, identical on every future scan — so it is still a reported skip and still
    subtracted from coverage. It DOES have toxicity (``REDACTION_TOXICITY_LANGUAGES``), which
    is what makes this a real discrimination rather than a blanket answer.
    """
    supported, skipped = detector_language_support("fr")

    assert "pii" not in supported
    assert "profanity" not in supported
    assert skipped["pii"] == "fr"
    assert "toxicity" in supported, "not a blanket 'nothing is supported' answer either"


def test_an_undeterminable_language_reports_a_real_coverage_gap(
    db_session, pii_masking_user
) -> None:
    """The consequence, at the surface that was reporting clean.

    A scan that ran without PII on a file whose language nobody could parse used to be
    subtracted from the report and read as fully covered.
    """
    media = _seed_unscanned_file(db_session, pii_masking_user, language="Klingon")
    media.redaction_coverage = ["profanity", "toxicity"]
    db_session.flush()
    cfg = resolve_effective_config(db_session, pii_masking_user.id)

    assert "pii" in uncovered_detectors(media, cfg)


def test_a_recognised_unsupported_language_reports_no_gap(db_session, pii_masking_user) -> None:
    """CONTROL for the test above, and for the rule ``coverage.py`` is built on.

    Treating a declared capability limit as a gap would withhold every non-English transcript
    from every LLM feature, permanently — a different decision from this one.
    """
    media = _seed_unscanned_file(db_session, pii_masking_user, language="fr")
    media.redaction_coverage = ["toxicity"]
    db_session.flush()
    cfg = resolve_effective_config(db_session, pii_masking_user.id)

    assert uncovered_detectors(media, cfg) == set()


def test_a_scan_of_an_english_spelling_now_actually_runs_pii(
    db_session, pii_masking_user, quiet_detectors
) -> None:
    """End to end through the real ``detect_and_store``: ``eng`` is scanned like ``en``."""
    media = _seed_unscanned_file(db_session, pii_masking_user, language="eng")

    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert result["status"] == "done", result
    assert media.redaction_coverage == ["profanity", "pii", "toxicity"], media.redaction_coverage


# --------------------------------------------------- B6: the llm detector that never ran


def test_an_llm_detector_that_raised_is_not_recorded_as_covered(
    db_session, llm_detector_user, quiet_detectors, monkeypatch
) -> None:
    """THE DEFECT. ``detect_with_llm`` swallows the fault and returns ``{}``.

    ``{}`` is byte-identical to "the model examined every segment and found nothing", so
    ``"llm"`` was appended to ``redaction_coverage`` regardless and the coverage surface
    reported clean — the same failure mode as B5, three commits away.
    """
    _install_llm(monkeypatch, _FakeLLMService(raise_on_chat=True))
    media = _seed_unscanned_file(db_session, llm_detector_user)

    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert "llm" not in (media.redaction_coverage or []), media.redaction_coverage


def test_a_working_llm_detector_is_recorded_as_covered(
    db_session, llm_detector_user, quiet_detectors, monkeypatch
) -> None:
    """CONTROL. Same path, same config, opposite outcome — driven only by the provider.

    Without this, "never record llm" would pass the test above.
    """
    _install_llm(monkeypatch, _FakeLLMService(content="[]"))
    media = _seed_unscanned_file(db_session, llm_detector_user)

    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert "llm" in (media.redaction_coverage or []), media.redaction_coverage


def test_an_llm_provider_that_is_not_configured_at_all_is_not_covered(
    db_session, llm_detector_user, quiet_detectors, monkeypatch
) -> None:
    """The other swallowed branch: no provider resolves, so no segment is ever examined."""
    _install_llm(monkeypatch, None)
    media = _seed_unscanned_file(db_session, llm_detector_user)

    RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert "llm" not in (media.redaction_coverage or []), media.redaction_coverage


def test_an_llm_outage_is_reported_and_leaves_a_gap_rather_than_failing_the_file(
    db_session, llm_detector_user, quiet_detectors, monkeypatch
) -> None:
    """The disposition, asserted where it is decided.

    ``failed`` is not an inert label — ``llm_guard`` turns it into a permanent, non-retryable
    refusal that would break summarization, speaker-ID and topic extraction for the file. A
    provider outage is the ``DetectorUnavailableError`` shape (re-running installs nothing),
    so it is reported as a skip and recorded as a coverage gap, which every fail-closed masker
    already reads.
    """
    _install_llm(monkeypatch, _FakeLLMService(raise_on_chat=True))
    media = _seed_unscanned_file(db_session, llm_detector_user)

    result = RedactionService.detect_and_store(db_session, media.id)

    db_session.refresh(media)
    assert result["status"] == "done", result
    assert "llm" in result["skipped_detectors"], result
    cfg = resolve_effective_config(db_session, llm_detector_user.id)
    assert "llm" in uncovered_detectors(media, cfg)
