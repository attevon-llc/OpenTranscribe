"""``speaker_count_hints`` in the ASR catalog says what each provider module SENDS (#1224).

The catalog is the pipeline plan's source for "does this engine use your speaker range".
A value copied from vendor docs is exactly the error to prevent (AssemblyAI documented
min/max for months while the module sent only ``speakers_expected``; #1226 wired the range
through the installed SDK's ``SpeakerOptions``), so the expected set is derived
from the provider module itself: every ``<config>.min_speakers`` / ``max_speakers`` /
``num_speakers`` the module reads. Several vendors' request builders sit behind SDK
clients that cannot be driven without a network double per vendor; the two that can be
driven cheaply (pyannote.ai's job body, AssemblyAI's SDK config) are driven as a check
that the source scan agrees with what reaches the wire.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.asr.factory import ASR_PROVIDER_CATALOG
from app.services.asr.types import ASRConfig
from app.services.pipeline_plan import ALL_HINTS

_APP = Path(__file__).resolve().parents[2] / "app"
_ATTR_TO_HINT = {"min_speakers": "min", "max_speakers": "max", "num_speakers": "num"}
_CLOUD = sorted(p for p in ASR_PROVIDER_CATALOG if p != "local")


def _hints_read_by(source: str) -> frozenset[str]:
    return frozenset(
        _ATTR_TO_HINT[node.attr]
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr in _ATTR_TO_HINT
    )


def _module_hints(relative: str) -> frozenset[str]:
    return _hints_read_by((_APP / relative).read_text())


def test_every_provider_and_model_declares_its_hints():
    assert len(ASR_PROVIDER_CATALOG) >= 10, "the catalog lost its providers"
    for provider_id, entry in ASR_PROVIDER_CATALOG.items():
        hints = entry.get("speaker_count_hints")
        assert isinstance(hints, list), provider_id
        assert set(hints) <= ALL_HINTS, provider_id
        for model in entry["models"]:
            assert set(model.get("speaker_count_hints", hints)) <= ALL_HINTS


@pytest.mark.parametrize("provider_id", _CLOUD)
def test_catalog_hints_equal_what_the_module_reads(provider_id):
    declared = frozenset(ASR_PROVIDER_CATALOG[provider_id]["speaker_count_hints"])
    assert declared == _module_hints(f"services/asr/{provider_id}_provider.py")


def test_the_scan_fires_and_stays_clean():
    """Control: a scan that matched nothing would make every 'no hints' row pass."""
    assert _hints_read_by("def f(config):\n    return config.max_speakers") == {"max"}
    assert _hints_read_by("def f(config):\n    return config.language") == frozenset()


def test_pyannote_ai_diarizer_honours_all_three():
    """The resolver gives pyannote.ai ALL_HINTS; its module must read all three."""
    assert _module_hints("services/diarization/pyannote_provider.py") == ALL_HINTS


def test_local_catalog_names_the_in_process_engine_hints():
    """Local hints depend on the admin's engine; the catalog names the PyAnnote set."""
    assert frozenset(ASR_PROVIDER_CATALOG["local"]["speaker_count_hints"]) == ALL_HINTS


def test_pyannote_job_body_carries_the_hints_the_scan_found(tmp_path):
    from tests.unit.test_pyannote_provider import _run_transcribe_capture_job_body

    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    num = _run_transcribe_capture_job_body(tmp_path / "a", ASRConfig(num_speakers=3))
    ranged = _run_transcribe_capture_job_body(
        tmp_path / "b", ASRConfig(min_speakers=2, max_speakers=5)
    )
    sent = {k for k in ("numSpeakers", "minSpeakers", "maxSpeakers") if k in num or k in ranged}
    assert sent == {"numSpeakers", "minSpeakers", "maxSpeakers"}


def test_assemblyai_sends_the_count_or_else_the_range(tmp_path):
    from tests.unit.test_assemblyai_provider import _fake_transcript
    from tests.unit.test_assemblyai_provider import _fake_utterance
    from tests.unit.test_assemblyai_provider import _run_transcribe

    transcript = _fake_transcript(utterances=[_fake_utterance("hi", 0, 500, "A")])
    counted = ASRConfig(min_speakers=2, max_speakers=7, num_speakers=4, vocabulary=None)
    _result, call_kwargs = _run_transcribe(tmp_path / "count", counted, transcript)
    sdk_config = call_kwargs["config"]
    assert sdk_config.speakers_expected == 4
    assert sdk_config.speaker_options is None

    ranged = ASRConfig(min_speakers=2, max_speakers=7, vocabulary=None)
    _result, call_kwargs = _run_transcribe(tmp_path / "range", ranged, transcript)
    sdk_config = call_kwargs["config"]
    assert sdk_config.speakers_expected is None
    assert sdk_config.speaker_options.min_speakers_expected == 2
    assert sdk_config.speaker_options.max_speakers_expected == 7
    assert frozenset(ASR_PROVIDER_CATALOG["assemblyai"]["speaker_count_hints"]) == {
        "min",
        "max",
        "num",
    }


def test_assemblyai_sends_no_range_when_speakers_are_off(tmp_path):
    from tests.unit.test_assemblyai_provider import _fake_transcript
    from tests.unit.test_assemblyai_provider import _run_transcribe

    config = ASRConfig(min_speakers=2, max_speakers=7, enable_diarization=False, vocabulary=None)
    _result, call_kwargs = _run_transcribe(tmp_path, config, _fake_transcript(text="hi"))
    assert call_kwargs["config"].speaker_options is None
