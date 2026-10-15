"""What the API shows about how a file was processed (issue #1229).

``build_processing_steps`` is pure over the row's attributes, so these tests drive it with
a plain namespace. The cases that matter most are the honest ones: a file processed before
recording began shows what the older columns really say, marked legacy, and "not recorded"
for everything else, and the ambiguous ``pyannote`` id never gets a location.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services.file_provenance_view import build_processing_steps

STEPS = ["transcription", "diarization", "voiceprints", "summary", "topics", "speaker_names"]


def _row(**overrides):
    values = {
        "uuid": "f",
        "processing_provenance": None,
        "asr_provider": None,
        "asr_model": None,
        "diarization_provider": None,
        "diarization_model": None,
        "diarization_disabled": False,
        "embedding_mode": None,
        "summary_data": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _by_step(row) -> dict:
    rows = build_processing_steps(row)
    assert [r.step for r in rows] == STEPS
    return {r.step: r for r in rows}


def test_a_file_with_nothing_recorded_is_not_recorded_everywhere():
    steps = _by_step(_row())
    assert {s.status for s in steps.values()} == {"not_recorded"}
    assert {s.recorded for s in steps.values()} == {"none"}
    assert all(s.provider is None and s.where is None for s in steps.values())


def test_legacy_columns_are_shown_as_legacy_and_never_overclaimed():
    steps = _by_step(
        _row(
            asr_provider="deepgram",
            asr_model="nova-3",
            diarization_provider="native",
            diarization_model="pyannote/speaker-diarization-community-1",
            embedding_mode="v4",
            summary_data={"metadata": {"provider": "vllm", "model": "gemma-4-e4b"}},
        )
    )
    transcription = steps["transcription"]
    assert (transcription.status, transcription.recorded) == ("done", "legacy")
    assert (transcription.provider_display, transcription.model) == ("Deepgram", "nova-3")
    assert transcription.where == "cloud"
    assert transcription.timing is None  # never recorded for an old file

    assert steps["diarization"].where == "server"
    assert steps["diarization"].provider_display == "diar-native"

    voiceprints = steps["voiceprints"]
    assert (voiceprints.status, voiceprints.where) == ("done", "server")
    assert voiceprints.model == "pyannote/wespeaker-voxceleb-resnet34-LM"

    summary = steps["summary"]
    assert (summary.provider_display, summary.model) == ("vLLM", "gemma-4-e4b")
    # The old summary metadata never recorded the endpoint, so where it ran is unknown.
    assert summary.where is None

    assert steps["topics"].status == "not_recorded"
    assert steps["speaker_names"].status == "not_recorded"


def test_legacy_pyannote_is_ambiguous_so_its_location_stays_unknown():
    """Both the local engine and pyannote.ai wrote ``pyannote`` before #1229."""
    steps = _by_step(
        _row(asr_provider="local", asr_model="large-v3", diarization_provider="pyannote")
    )
    assert steps["transcription"].where == "server"
    assert steps["transcription"].provider_display == "Whisper"
    assert steps["diarization"].status == "done"
    assert steps["diarization"].where is None


def test_legacy_skipped_diarization_is_off_for_speakers_and_voiceprints():
    steps = _by_step(_row(asr_provider="local", diarization_disabled=True, embedding_mode="v4"))
    assert steps["diarization"].status == "off"
    assert steps["voiceprints"].status == "off"


def _doc(**steps):
    return {"v": 1, "steps": steps}


def test_a_recorded_run_reports_what_ran_and_what_has_not_run():
    row = _row(
        processing_provenance=_doc(
            transcription={
                "provider": "openai",
                "model": "whisper-1",
                "where": "cloud",
                "engine": "cloud",
                "timing": "segment",
            },
            diarization={
                "provider": "pyannote.ai",
                "model": "precision-3",
                "where": "cloud",
                "engine": "pyannote_ai",
            },
            summary={"provider": "ollama", "model": "qwen3", "where": "server"},
        ),
        # Older columns that disagree must not leak into a recorded run.
        summary_data={"metadata": {"provider": "openai", "model": "gpt"}},
    )
    steps = _by_step(row)
    assert steps["transcription"].recorded == "full"
    assert steps["transcription"].timing == "segment"
    assert steps["transcription"].provider_display == "OpenAI"
    assert steps["diarization"].provider_display == "pyannote.ai"
    assert steps["diarization"].where == "cloud"
    assert (steps["summary"].provider_display, steps["summary"].where) == ("Ollama", "server")
    for step in ("voiceprints", "topics", "speaker_names"):
        assert (steps[step].status, steps[step].recorded) == ("not_run", "full")


def test_a_recorded_run_with_speakers_off_shows_voiceprints_off():
    row = _row(
        processing_provenance=_doc(
            transcription={"provider": "local", "model": "large-v3", "where": "server"},
            diarization={"engine": "off"},
        )
    )
    steps = _by_step(row)
    assert steps["diarization"].status == "off"
    assert steps["voiceprints"].status == "off"


def test_a_later_step_on_an_old_file_falls_back_to_legacy_for_the_rest():
    """Summarized after #1229, transcribed before it: only the summary is recorded."""
    row = _row(
        asr_provider="local",
        asr_model="large-v3",
        processing_provenance=_doc(summary={"provider": "custom", "model": "m", "where": "cloud"}),
    )
    steps = _by_step(row)
    assert steps["transcription"].recorded == "legacy"
    assert steps["topics"].status == "not_recorded"
    # ``custom`` has no proper-noun name: the model alone is shown, never an invented label.
    assert (steps["summary"].provider_display, steps["summary"].model) == (None, "m")


def test_a_malformed_document_is_ignored_rather_than_served():
    row = _row(
        asr_provider="local",
        processing_provenance={"v": 1, "steps": {"summary": {"base_url": "http://x"}}},
    )
    steps = _by_step(row)
    assert steps["summary"].status == "not_recorded"
    assert steps["transcription"].recorded == "legacy"
    assert "http" not in str([s.model_dump() for s in steps.values()])
