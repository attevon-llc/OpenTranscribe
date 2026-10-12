"""The pipeline plan resolver over every cell of the validity matrix (issue #1224).

Rows: transcription (local GPU, local CPU, cloud with speakers, cloud text-only with word
or segment timing, cloud with no timing). Columns: the stored speaker-detection source.
Two tables: the full image and lite (cloud-only, decided 2026-10-11). Each cell pins what
actually runs (engine, execution, where) and exactly which errors refuse it, so a resolver
that collapsed any column (e.g. "pyannote.ai with local Whisper runs locally", the bug
this replaces) fails by name.

The resolver is pure, so every cell runs without a database.
"""

from __future__ import annotations

import json

import pytest

from app.core.constants import CeleryQueues
from app.services.pipeline_plan import ISSUE_MESSAGES
from app.services.pipeline_plan import DiarizerConfigInfo
from app.services.pipeline_plan import PipelinePlan
from app.services.pipeline_plan import PlanInputs
from app.services.pipeline_plan import resolve_pipeline_plan

ALL = frozenset({"min", "max", "num"})

#: Transcription profiles, named by what matters to the matrix.
ASR = {
    "local_gpu": {
        "asr_provider": "local",
        "asr_model": "large-v3-turbo",
        "asr_returns_speakers": True,
        "asr_speaker_hints": ALL,
        "asr_timing": "word",
    },
    "local_cpu": {
        "asr_provider": "local",
        "asr_model": "large-v3-turbo",
        "asr_returns_speakers": True,
        "asr_speaker_hints": ALL,
        "asr_timing": "word",
        "cpu_whisper_model": "tiny",
    },
    "cloud_speakers": {
        "asr_provider": "deepgram",
        "asr_model": "nova-3",
        "asr_returns_speakers": True,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "word",
    },
    "cloud_text_word": {
        "asr_provider": "textonly",
        "asr_model": "t1",
        "asr_returns_speakers": False,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "word",
    },
    "cloud_text_segment": {
        "asr_provider": "openai",
        "asr_model": "whisper-1",
        "asr_returns_speakers": False,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "segment",
    },
    "cloud_no_timing": {
        "asr_provider": "openai",
        "asr_model": "gpt-4o-transcribe",
        "asr_returns_speakers": False,
        "asr_speaker_hints": frozenset(),
        "asr_timing": "none",
    },
}

SPEAKER_CONFIG = DiarizerConfigInfo(
    provider="assemblyai",
    model="universal",
    uuid="11111111-1111-1111-1111-111111111111",
    returns_speakers=True,
    speaker_hints=frozenset({"num"}),
)


def _inputs(asr: str, source: str, mode: str = "full", **overrides) -> PlanInputs:
    base: dict = {
        "deployment_mode": mode,
        "asr_config_uuid": None,
        "diarization_source": source,
        "has_pyannote_key": True,
        "pyannote_model": "precision-3",
        "diarization_config": SPEAKER_CONFIG if source == "asr_config" else None,
        **ASR[asr],
    }
    base.update(overrides)
    return PlanInputs(**base)


def _errors(plan: PipelinePlan) -> set[str]:
    return {i.code for i in plan.blocking()}


NO_TIME = "asr_no_timing_for_speakers"
LOCAL_ASR = "local_asr_unavailable"
LOCAL_DIAR = "local_diarization_unavailable"
AUTO_LITE = "automatic_needs_speaker_provider"

# (asr, source) -> (engine, execution, where, errors)
FULL = {
    ("local_gpu", "provider"): ("local", "inline_local", "server", set()),
    ("local_gpu", "local"): ("local", "inline_local", "server", set()),
    ("local_gpu", "pyannote"): ("pyannote_ai", "post_stage_cloud", "cloud", set()),
    ("local_gpu", "asr_config"): ("asr_config", "post_stage_cloud", "cloud", set()),
    ("local_gpu", "off"): ("off", "none", None, set()),
    ("local_cpu", "provider"): ("local", "post_stage_local", "server", set()),
    ("local_cpu", "local"): ("local", "post_stage_local", "server", set()),
    ("local_cpu", "pyannote"): ("pyannote_ai", "post_stage_cloud", "cloud", set()),
    ("local_cpu", "asr_config"): ("asr_config", "post_stage_cloud", "cloud", set()),
    ("local_cpu", "off"): ("off", "none", None, set()),
    ("cloud_speakers", "provider"): ("asr", "inline_asr", "cloud", set()),
    ("cloud_speakers", "local"): ("local", "post_stage_local", "server", set()),
    ("cloud_speakers", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", set()),
    ("cloud_speakers", "asr_config"): ("asr_config", "parallel_cloud", "cloud", set()),
    ("cloud_speakers", "off"): ("off", "none", None, set()),
    ("cloud_text_word", "provider"): ("local", "post_stage_local", "server", set()),
    ("cloud_text_word", "local"): ("local", "post_stage_local", "server", set()),
    ("cloud_text_word", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", set()),
    ("cloud_text_word", "asr_config"): ("asr_config", "parallel_cloud", "cloud", set()),
    ("cloud_text_word", "off"): ("off", "none", None, set()),
    ("cloud_text_segment", "provider"): ("local", "post_stage_local", "server", set()),
    ("cloud_text_segment", "local"): ("local", "post_stage_local", "server", set()),
    ("cloud_text_segment", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", set()),
    ("cloud_text_segment", "asr_config"): ("asr_config", "parallel_cloud", "cloud", set()),
    ("cloud_text_segment", "off"): ("off", "none", None, set()),
    ("cloud_no_timing", "provider"): ("local", "post_stage_local", "server", {NO_TIME}),
    ("cloud_no_timing", "local"): ("local", "post_stage_local", "server", {NO_TIME}),
    ("cloud_no_timing", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", {NO_TIME}),
    ("cloud_no_timing", "asr_config"): ("asr_config", "parallel_cloud", "cloud", {NO_TIME}),
    ("cloud_no_timing", "off"): ("off", "none", None, set()),
}

LITE = {
    ("local_gpu", "provider"): ("local", "none", "server", {LOCAL_ASR}),
    ("local_gpu", "local"): ("local", "none", "server", {LOCAL_ASR, LOCAL_DIAR}),
    ("local_gpu", "pyannote"): ("pyannote_ai", "post_stage_cloud", "cloud", {LOCAL_ASR}),
    ("local_gpu", "asr_config"): ("asr_config", "post_stage_cloud", "cloud", {LOCAL_ASR}),
    ("local_gpu", "off"): ("off", "none", None, {LOCAL_ASR}),
    ("local_cpu", "provider"): ("local", "none", "server", {LOCAL_ASR}),
    ("local_cpu", "local"): ("local", "none", "server", {LOCAL_ASR, LOCAL_DIAR}),
    ("local_cpu", "pyannote"): ("pyannote_ai", "post_stage_cloud", "cloud", {LOCAL_ASR}),
    ("local_cpu", "asr_config"): ("asr_config", "post_stage_cloud", "cloud", {LOCAL_ASR}),
    ("local_cpu", "off"): ("off", "none", None, {LOCAL_ASR}),
    ("cloud_speakers", "provider"): ("asr", "inline_asr", "cloud", set()),
    ("cloud_speakers", "local"): ("local", "none", "server", {LOCAL_DIAR}),
    ("cloud_speakers", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", set()),
    ("cloud_speakers", "asr_config"): ("asr_config", "parallel_cloud", "cloud", set()),
    ("cloud_speakers", "off"): ("off", "none", None, set()),
    ("cloud_text_word", "provider"): ("local", "none", "server", {AUTO_LITE}),
    ("cloud_text_word", "local"): ("local", "none", "server", {LOCAL_DIAR}),
    ("cloud_text_word", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", set()),
    ("cloud_text_word", "asr_config"): ("asr_config", "parallel_cloud", "cloud", set()),
    ("cloud_text_word", "off"): ("off", "none", None, set()),
    ("cloud_text_segment", "provider"): ("local", "none", "server", {AUTO_LITE}),
    ("cloud_text_segment", "local"): ("local", "none", "server", {LOCAL_DIAR}),
    ("cloud_text_segment", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", set()),
    ("cloud_text_segment", "asr_config"): ("asr_config", "parallel_cloud", "cloud", set()),
    ("cloud_text_segment", "off"): ("off", "none", None, set()),
    ("cloud_no_timing", "provider"): ("local", "none", "server", {AUTO_LITE, NO_TIME}),
    ("cloud_no_timing", "local"): ("local", "none", "server", {LOCAL_DIAR, NO_TIME}),
    ("cloud_no_timing", "pyannote"): ("pyannote_ai", "parallel_cloud", "cloud", {NO_TIME}),
    ("cloud_no_timing", "asr_config"): ("asr_config", "parallel_cloud", "cloud", {NO_TIME}),
    ("cloud_no_timing", "off"): ("off", "none", None, set()),
}

SOURCES = ("provider", "local", "pyannote", "asr_config", "off")


def test_both_tables_cover_every_cell():
    cells = {(asr, source) for asr in ASR for source in SOURCES}
    assert set(FULL) == cells
    assert set(LITE) == cells


@pytest.mark.parametrize(("mode", "table"), [("full", FULL), ("lite", LITE)])
@pytest.mark.parametrize(("asr", "source"), [(a, s) for a in ASR for s in SOURCES])
def test_every_cell_of_the_validity_matrix(mode, table, asr, source):
    engine, execution, where, errors = table[(asr, source)]
    plan = resolve_pipeline_plan(_inputs(asr, source, mode))

    assert plan.diarization.engine == engine
    assert plan.diarization.execution == execution
    assert plan.diarization.where == where
    assert _errors(plan) == errors
    assert plan.diarization.source == source


def test_pyannote_with_local_whisper_is_a_cloud_post_stage_not_the_local_diarizer():
    """The #1224 bug: pyannote.ai + local Whisper silently ran the local diarizer."""
    plan = resolve_pipeline_plan(_inputs("local_gpu", "pyannote"))

    assert plan.diarization.execution != "inline_local"
    assert plan.diarization.execution == "post_stage_cloud"
    assert plan.diarization.provider == "pyannote.ai"
    assert plan.diarization.queue == CeleryQueues.CLOUD_ASR
    assert plan.blocking() == ()


def test_columns_differ_for_the_same_transcription():
    """Control: a resolver that ignored the source would make these identical."""
    executions = {
        source: resolve_pipeline_plan(_inputs("cloud_speakers", source)).diarization.execution
        for source in SOURCES
    }
    assert len(set(executions.values())) == 4  # asr_config and pyannote share parallel_cloud
    assert executions["pyannote"] == executions["asr_config"] == "parallel_cloud"


# --- the issue codes outside the matrix ------------------------------------------------


def test_pyannote_without_a_key_is_refused_for_every_transcription():
    for asr in ("local_gpu", "cloud_speakers", "cloud_text_segment"):
        plan = resolve_pipeline_plan(_inputs(asr, "pyannote", has_pyannote_key=False))
        assert "pyannote_key_missing" in _errors(plan), asr


def test_asr_config_missing_or_text_only_is_refused():
    missing = resolve_pipeline_plan(
        _inputs("cloud_speakers", "asr_config", diarization_config=None)
    )
    assert _errors(missing) == {"diarizer_config_missing"}

    text_only = DiarizerConfigInfo("openai", "whisper-1", "u", returns_speakers=False)
    no_speakers = resolve_pipeline_plan(
        _inputs("local_gpu", "asr_config", diarization_config=text_only)
    )
    assert _errors(no_speakers) == {"diarizer_config_no_speakers"}


def test_asr_config_naming_the_transcription_model_uses_its_own_speakers():
    same = DiarizerConfigInfo("deepgram", "nova-3", "u", returns_speakers=True)
    plan = resolve_pipeline_plan(_inputs("cloud_speakers", "asr_config", diarization_config=same))

    assert plan.diarization.engine == "asr"
    assert plan.diarization.execution == "inline_asr"
    codes = {i.code for i in plan.issues}
    assert "diarizer_same_as_transcription" in codes
    assert "diarizer_double_billing" not in codes


def test_a_second_vendor_as_diarizer_warns_about_billing():
    plan = resolve_pipeline_plan(_inputs("cloud_text_segment", "asr_config"))
    warnings = {i.code for i in plan.issues if i.severity == "warning"}
    assert "diarizer_double_billing" in warnings
    assert plan.diarization.config_uuid == SPEAKER_CONFIG.uuid
    assert plan.diarization.provider == "assemblyai"


def test_automatic_with_a_text_only_model_says_it_uses_this_server():
    plan = resolve_pipeline_plan(_inputs("cloud_text_word", "provider"))
    info = {i.code for i in plan.issues if i.severity == "info"}
    assert "automatic_uses_server" in info
    control = resolve_pipeline_plan(_inputs("cloud_speakers", "provider"))
    assert "automatic_uses_server" not in {i.code for i in control.issues}


def test_segment_timing_warns_only_when_turns_are_merged_into_segments():
    whisper_1 = resolve_pipeline_plan(_inputs("cloud_text_segment", "pyannote"))
    assert "segment_timing_only" in {i.code for i in whisper_1.issues}
    assert whisper_1.diarization.merge == "segment"

    word = resolve_pipeline_plan(_inputs("cloud_text_word", "pyannote"))
    assert "segment_timing_only" not in {i.code for i in word.issues}
    assert word.diarization.merge == "word"

    off = resolve_pipeline_plan(_inputs("cloud_text_segment", "off"))
    assert off.diarization.merge == "none"
    assert off.issues == ()


def test_locked_source_is_reported():
    plan = resolve_pipeline_plan(_inputs("local_gpu", "provider", source_locked=True))
    assert ("source_locked", "info") in {(i.code, i.severity) for i in plan.issues}


def test_every_emitted_code_has_a_fixed_sentence():
    emitted = set()
    for mode in ("full", "lite"):
        for asr in ASR:
            for source in SOURCES:
                for key in (True, False):
                    plan = resolve_pipeline_plan(
                        _inputs(asr, source, mode, has_pyannote_key=key, source_locked=True)
                    )
                    emitted |= {i.code for i in plan.issues}
    assert emitted, "no issue emitted at all: the sweep proves nothing"
    assert emitted <= set(ISSUE_MESSAGES)
    for code in emitted:
        message = ISSUE_MESSAGES[code]
        assert message.endswith("."), code
        assert "{" not in message, code


def test_unknown_source_is_a_programming_error():
    with pytest.raises(ValueError, match="unknown diarization source"):
        resolve_pipeline_plan(_inputs("local_gpu", "pyannote-typo"))


# --- speaker-count hints ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("asr", "source", "backend", "expected"),
    [
        ("local_gpu", "local", "native", frozenset()),
        ("local_gpu", "local", "pyannote", ALL),
        ("local_gpu", "pyannote", "native", ALL),
        ("cloud_speakers", "provider", "native", frozenset()),
        ("cloud_speakers", "asr_config", "native", frozenset({"num"})),
        ("cloud_speakers", "off", "native", frozenset()),
    ],
)
def test_speaker_hints_follow_the_engine_that_runs(asr, source, backend, expected):
    plan = resolve_pipeline_plan(_inputs(asr, source, diarizer_backend=backend))
    assert plan.diarization.speaker_hints == expected
    ignored = "speaker_hints_ignored" in {i.code for i in plan.issues}
    assert ignored == (source != "off" and not expected)


# --- queues and topology ---------------------------------------------------------------


def test_queues_per_transcription_engine():
    assert resolve_pipeline_plan(_inputs("local_gpu", "local")).transcription.queue == "gpu"
    assert (
        resolve_pipeline_plan(_inputs("local_cpu", "off")).transcription.queue
        == CeleryQueues.CPU_TRANSCRIBE
    )
    assert (
        resolve_pipeline_plan(_inputs("cloud_speakers", "off")).transcription.queue
        == CeleryQueues.CLOUD_ASR
    )


def test_gpu_split_only_for_inline_local_diarization():
    inline = resolve_pipeline_plan(_inputs("local_gpu", "local", gpu_split=True))
    assert inline.transcription.queue == CeleryQueues.GPU_TRANSCRIBE
    assert inline.diarization.queue == CeleryQueues.GPU_DIARIZE

    post = resolve_pipeline_plan(_inputs("local_gpu", "pyannote", gpu_split=True))
    assert post.transcription.queue == CeleryQueues.GPU
    assert post.diarization.queue == CeleryQueues.CLOUD_ASR


def test_lite_plans_no_local_speaker_detection_anywhere():
    """#1228: lite's old rediarize-on-CPU route is gone. Full keeps the local post stage
    (the control); on lite no cell plans a local execution or a queue for it."""
    full = resolve_pipeline_plan(_inputs("cloud_speakers", "local"))
    assert (full.diarization.execution, full.diarization.queue) == (
        "post_stage_local",
        CeleryQueues.GPU,
    )

    local_cells = 0
    for asr in ASR:
        for source in SOURCES:
            plan = resolve_pipeline_plan(_inputs(asr, source, "lite"))
            assert plan.diarization.execution not in ("inline_local", "post_stage_local")
            if plan.diarization.engine == "local":
                local_cells += 1
                assert plan.diarization.queue is None
                assert plan.blocking(), (asr, source)
    assert local_cells, "no lite cell asked for this server: the sweep proves nothing"


def test_post_stage_queue_refuses_this_servers_engine_on_lite():
    from app.core.exceptions import PipelinePlanRefusedError
    from app.services.pipeline_plan import post_stage_queue

    assert post_stage_queue("local", "full") == CeleryQueues.GPU
    assert post_stage_queue("pyannote_ai", "lite") == CeleryQueues.CLOUD_ASR
    assert post_stage_queue("asr_config", "lite") == CeleryQueues.CLOUD_ASR
    with pytest.raises(PipelinePlanRefusedError) as refused:
        post_stage_queue("local", "lite")
    assert refused.value.code == "local_diarization_unavailable"
    assert str(refused.value) == ISSUE_MESSAGES["local_diarization_unavailable"]


# --- voiceprints: always on this server, after every detector --------------------------


@pytest.mark.parametrize("mode", ["full", "lite"])
@pytest.mark.parametrize(("asr", "source"), [(a, s) for a in ASR for s in SOURCES])
def test_voiceprints_run_on_this_server_after_every_detector(mode, asr, source):
    plan = resolve_pipeline_plan(_inputs(asr, source, mode))
    step = plan.speaker_id
    assert step.where == "server"
    if plan.diarization.execution == "none":
        # Off, or (lite) a local choice nothing runs for: no turns, so no voiceprints.
        assert (step.runs, step.execution, step.queue) == (False, "none", None)
    elif plan.diarization.execution == "inline_local":
        assert (step.runs, step.execution, step.queue) == (True, "inline_native", None)
    else:
        assert (step.runs, step.execution, step.queue) == (True, "embedding_stage", "cpu")


def test_plan_round_trips_through_the_task_context():
    plan = resolve_pipeline_plan(_inputs("cloud_text_segment", "asr_config", source_locked=True))
    context = json.loads(json.dumps(plan.to_context()))
    assert PipelinePlan.from_context(context) == plan
