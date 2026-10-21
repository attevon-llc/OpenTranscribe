"""The provider allow-list as a pipeline plan error (issue #1246, WP6).

An administrator can limit which cloud providers users may send audio to. The decision is
the plan's, so dispatch, set-active and the speaker-source PUT refuse without a check of
their own. Each case names the step the error sits on and has a control that stays clean.
"""

from __future__ import annotations

import pytest

from app.services.pipeline_plan import ISSUE_MESSAGES
from app.services.pipeline_plan import PlanInputs
from app.services.pipeline_plan import resolve_pipeline_plan
from tests.unit.test_pipeline_plan import _errors
from tests.unit.test_pipeline_plan import _inputs

NOT_ALLOWED_ASR = "asr_provider_not_allowed"
NOT_ALLOWED_DIAR = "diarizer_provider_not_allowed"


def _plan(asr: str, source: str, **overrides):
    return resolve_pipeline_plan(_inputs(asr, source, **overrides))


def test_both_flags_default_to_allowed():
    fields = PlanInputs.__dataclass_fields__
    assert fields["asr_provider_allowed"].default is True
    assert fields["diarizer_provider_allowed"].default is True


def test_cloud_transcription_not_allowed_is_a_transcription_error():
    plan = _plan("cloud_speakers", "provider", asr_provider_allowed=False)
    assert _errors(plan) == {NOT_ALLOWED_ASR}
    [issue] = plan.blocking()
    assert issue.step == "transcription"
    assert issue.severity == "error"


def test_allowed_cloud_transcription_is_unchanged():
    assert _errors(_plan("cloud_speakers", "provider", asr_provider_allowed=True)) == set()


@pytest.mark.parametrize("asr", ["local_gpu", "local_cpu"])
def test_local_transcription_is_never_blocked_by_the_flag(asr):
    assert _errors(_plan(asr, "provider", asr_provider_allowed=False)) == set()


@pytest.mark.parametrize("source", ["asr_config", "pyannote"])
def test_cloud_detector_not_allowed_is_a_diarization_error(source):
    plan = _plan("local_gpu", source, diarizer_provider_allowed=False)
    assert _errors(plan) == {NOT_ALLOWED_DIAR}
    [issue] = plan.blocking()
    assert issue.step == "diarization"


@pytest.mark.parametrize("source", ["asr_config", "pyannote"])
def test_allowed_cloud_detector_is_unchanged(source):
    assert _errors(_plan("local_gpu", source, diarizer_provider_allowed=True)) == set()


@pytest.mark.parametrize("source", ["provider", "local", "off"])
def test_detectors_that_are_not_a_vendor_ignore_the_diarizer_flag(source):
    assert _errors(_plan("local_gpu", source, diarizer_provider_allowed=False)) == set()


def test_transcription_provider_that_also_detects_speakers_reports_only_the_asr_error():
    # The detector IS the transcription provider (engine "asr"): one egress, one error.
    plan = _plan(
        "cloud_speakers",
        "asr_config",
        asr_provider_allowed=False,
        diarizer_provider_allowed=False,
        asr_provider="assemblyai",
        asr_model="universal",
    )
    assert _errors(plan) == {NOT_ALLOWED_ASR}


def test_both_codes_have_a_fixed_sentence_and_block():
    from app.services.pipeline_plan import BLOCKING_CODES

    for code in (NOT_ALLOWED_ASR, NOT_ALLOWED_DIAR):
        assert code in ISSUE_MESSAGES
        assert code in BLOCKING_CODES
    assert ISSUE_MESSAGES[NOT_ALLOWED_ASR] == (
        "Your administrator has not enabled this transcription provider."
    )
    assert ISSUE_MESSAGES[NOT_ALLOWED_DIAR] == (
        "Your administrator has not enabled this speaker detection provider."
    )
