"""Tier 1 of the privacy policy: the pipeline plan schedules no voiceprint step (issue #1280).

``PlanInputs.voiceprints_allowed`` is the policy's answer for the file's owner and
organization. Every cell of the matrix has an allowed control beside the denied case, so a plan
that ignored the field, or one that dropped the voiceprint step for everybody, fails by name.
Speakers are still told apart within the recording: only the voiceprint step goes.
"""

from __future__ import annotations

import json

import pytest

from app.services.pipeline_plan import ISSUE_MESSAGES
from app.services.pipeline_plan import PipelinePlan
from app.services.pipeline_plan import resolve_pipeline_plan
from tests.unit.test_pipeline_plan import ASR
from tests.unit.test_pipeline_plan import SOURCES
from tests.unit.test_pipeline_plan import _inputs

CODE = "voiceprints_disabled"
CELLS = [(a, s, m) for a in ASR for s in SOURCES for m in ("full", "lite")]


@pytest.mark.parametrize(("asr", "source", "mode"), CELLS)
def test_voiceprints_disallowed_plans_no_speaker_id(asr, source, mode):
    allowed = resolve_pipeline_plan(_inputs(asr, source, mode))
    denied = resolve_pipeline_plan(_inputs(asr, source, mode, voiceprints_allowed=False))

    step = denied.speaker_id
    assert (step.runs, step.execution, step.queue) == (False, "none", None)

    # Everything that is not the voiceprint step is exactly what it was.
    assert denied.transcription == allowed.transcription
    assert denied.diarization == allowed.diarization
    assert denied.blocking() == allowed.blocking()

    voiceprints_were_planned = allowed.speaker_id.execution != "none"
    codes = [i.code for i in denied.issues]
    if voiceprints_were_planned:
        # Control: with the policy on, a step IS planned and nothing is said about it.
        assert allowed.speaker_id.runs is True
        assert CODE not in [i.code for i in allowed.issues]
        assert codes.count(CODE) == 1
        issue = next(i for i in denied.issues if i.code == CODE)
        assert (issue.severity, issue.step) == ("info", "speaker_id")
    else:
        # Nothing would have run (speaker detection off, or refused): nothing to disable,
        # so no notice either.
        assert CODE not in codes


@pytest.mark.parametrize(("asr", "source", "mode"), CELLS)
def test_an_allowed_plan_is_unchanged_by_the_new_field(asr, source, mode):
    default = resolve_pipeline_plan(_inputs(asr, source, mode))
    explicit = resolve_pipeline_plan(_inputs(asr, source, mode, voiceprints_allowed=True))
    assert default == explicit
    assert CODE not in [i.code for i in default.issues]


def test_the_inline_native_cell_is_the_one_that_loses_its_matching():
    allowed = resolve_pipeline_plan(_inputs("local_gpu", "local"))
    denied = resolve_pipeline_plan(_inputs("local_gpu", "local", voiceprints_allowed=False))
    assert allowed.speaker_id.execution == "inline_native"
    assert denied.speaker_id.execution == "none"
    # Diarization itself still runs on this server, inline.
    assert denied.diarization.execution == "inline_local"


def test_the_notice_is_informational_and_never_blocks_a_run():
    plan = resolve_pipeline_plan(_inputs("cloud_speakers", "provider", voiceprints_allowed=False))
    assert plan.blocking() == ()
    assert ISSUE_MESSAGES[CODE]


def test_a_denied_plan_round_trips_through_the_task_context():
    plan = resolve_pipeline_plan(_inputs("local_gpu", "local", voiceprints_allowed=False))
    context = json.loads(json.dumps(plan.to_context()))
    assert PipelinePlan.from_context(context) == plan
    assert PipelinePlan.from_context(context).speaker_id.execution == "none"
