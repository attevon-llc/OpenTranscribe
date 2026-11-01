"""The PyAnnote group of download-models.py must not depend on transcribing fake audio.

Reproduced in the published ``opentranscribe-backend:v0.6.0`` image on CPU: the group
transcribed 30 s of synthetic sine waves through whisperx before diarizing. whisperx's VAD
found no speech in it ("No active speech found in audio"), ``FasterWhisperPipeline.transcribe``
passed an empty batch to transformers 5.x, and ``transformers/pipelines/base.py`` raised
``IndexError: list index out of range`` from ``is_valid_message(inputs[0])``. The downloader
exited 1 with "Failed to download PyAnnote models: list index out of range".

The whisperx pipeline here is the REAL ``whisperx.asr.FasterWhisperPipeline`` over the real
transformers ``Pipeline.__call__``; only the VAD's network forward pass is replaced (by a
``Silero`` whose ``__call__`` finds no speech, keeping its real ``merge_chunks``), so the old
code fails with exactly the reported error and no weights are needed. The Hub is the other
substitution: ``snapshot_download`` and ``Pipeline.from_pretrained`` record their calls.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DOWNLOADER_PY = REPO_ROOT / "scripts" / "download-models.py"
COMMUNITY_1 = "pyannote/speaker-diarization-community-1"


@pytest.fixture
def downloader():
    """``download-models.py`` loaded as a module, with torch.load restored afterwards."""
    torch = pytest.importorskip("torch")
    saved_load, saved_empty_cache = torch.load, torch.mps.empty_cache
    spec = importlib.util.spec_from_file_location("download_models_pyannote", DOWNLOADER_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        torch.load, torch.mps.empty_cache = saved_load, saved_empty_cache


def _no_speech_whisperx_pipeline():
    """A real whisperx FasterWhisperPipeline whose VAD finds no speech in its input."""
    asr = pytest.importorskip("whisperx.asr")
    from whisperx.vads import Silero

    class _NoSpeechSilero(Silero):
        def __init__(self):  # skip torch.hub: the forward pass is what is replaced
            pass

        def __call__(self, audio, **kwargs):
            return []

    tokenizer = SimpleNamespace(language_code="en", task="transcribe")
    return asr.FasterWhisperPipeline(
        model=object(),
        vad=_NoSpeechSilero(),
        vad_params={"vad_onset": 0.5, "vad_offset": 0.363},
        options=None,
        tokenizer=tokenizer,
        language="en",
    )


@pytest.fixture
def hub(monkeypatch, downloader):
    """Record the Hub calls the group makes, with a token and access already granted."""
    huggingface_hub = pytest.importorskip("huggingface_hub")
    pipeline_cls = pytest.importorskip("pyannote.audio").Pipeline

    calls: dict[str, list] = {"snapshot": [], "load": []}
    loaded = SimpleNamespace(name="pipeline")

    def _snapshot(repo_id, token=None, **kwargs):
        calls["snapshot"].append((repo_id, token))
        return "/cache/snapshot"

    def _from_pretrained(repo_id, token=None, **kwargs):
        calls["load"].append((repo_id, token))
        return calls.get("load_result", loaded)

    monkeypatch.setenv("HUGGINGFACE_TOKEN", "hf_test_token")
    monkeypatch.setattr(downloader, "validate_gated_model_access", lambda: (True, None))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", _snapshot)
    monkeypatch.setattr(pipeline_cls, "from_pretrained", _from_pretrained)
    return calls


@pytest.fixture
def whisperx_finds_no_speech(monkeypatch):
    """Any ``whisperx.load_model`` returns the real pipeline over a VAD that finds no speech."""
    whisperx = pytest.importorskip("whisperx")
    monkeypatch.setattr(whisperx, "load_model", lambda *a, **k: _no_speech_whisperx_pipeline())


def test_pyannote_group_succeeds_when_the_audio_has_no_speech(
    downloader, hub, whisperx_finds_no_speech
):
    result = downloader.download_pyannote_models()["pyannote"]

    assert result["status"] == "downloaded", result
    assert result["model"] == COMMUNITY_1
    assert hub["snapshot"] == [(COMMUNITY_1, "hf_test_token")]
    assert hub["load"] == [(COMMUNITY_1, "hf_test_token")], "must load via the backend's call"


def test_pipeline_the_token_cannot_read_is_a_gated_access_failure(downloader, hub, capsys):
    hub["load_result"] = None

    result = downloader.download_pyannote_models()["pyannote"]

    assert result["status"] == "failed"
    assert COMMUNITY_1 in result["error"]
    assert "GATED MODEL ACCESS ERROR" in capsys.readouterr().out
