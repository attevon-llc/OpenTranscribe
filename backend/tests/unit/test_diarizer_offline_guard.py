"""Offline-guard / bounded-load behavior for SpeakerDiarizer.load_model.

Confirmed bug (investigation for the overnight `run-dev-tests.sh --full` slowdown):
the diarizer's ``HF_HUB_OFFLINE`` checks only improved an error MESSAGE after
``Pipeline.from_pretrained`` had already returned/raised — they never prevented the
network round trip in the first place. `pyannote.audio.Pipeline.from_pretrained`
exposes no ``local_files_only`` kwarg, so the fix is
``app.utils.hf_hub_offline.force_offline_if_requested`` (mutates
``huggingface_hub.constants.HF_HUB_OFFLINE`` + drops cached sessions so a fresh one
mounts the library's ``OfflineAdapter``) plus ``load_with_timeout`` to bound the
call regardless of the flag.

These tests stub ``pyannote.audio.Pipeline`` at the point ``diarizer.py`` imports it
(``from pyannote.audio import Pipeline`` inside the function) with a fake whose
``from_pretrained`` performs a REAL huggingface_hub HTTP attempt — the same
mechanism `test_hf_hub_offline.py` proves blocks instantly under
`force_offline_if_requested`. That is the falsifiable claim: not "the load
succeeded", but "no network attempt reached the transport layer".
"""

from __future__ import annotations

import sys
import time
import types

import pytest

from app.transcription import diarizer as diarizer_mod
from app.transcription.config import TranscriptionConfig


def _install_fake_pyannote(monkeypatch, from_pretrained):
    stub_audio = types.ModuleType("pyannote.audio")
    fake_pipeline_cls = types.SimpleNamespace(from_pretrained=from_pretrained)
    stub_audio.Pipeline = fake_pipeline_cls  # type: ignore[attr-defined]
    stub_pyannote = types.ModuleType("pyannote")
    stub_pyannote.audio = stub_audio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pyannote", stub_pyannote)
    monkeypatch.setitem(sys.modules, "pyannote.audio", stub_audio)


@pytest.mark.unit
class TestOfflineGuardBlocksTheNetworkAttempt:
    def test_offline_mode_never_reaches_the_transport_layer(self, monkeypatch):
        """The behavioral proof: a real huggingface_hub HTTP attempt is blocked
        BEFORE any socket operation, for both the v4 load and its v3 fallback.
        """
        from huggingface_hub.utils._http import get_session
        from huggingface_hub.utils._http import reset_sessions

        monkeypatch.setenv("HF_HUB_OFFLINE", "1")
        attempts: list[str] = []

        def _from_pretrained(checkpoint, token=None):
            attempts.append(checkpoint)
            # A real Hub HTTP attempt — this is what must never reach the network.
            get_session().get(f"https://huggingface.co/api/models/{checkpoint}")
            raise AssertionError("unreachable: the offline adapter must raise first")

        _install_fake_pyannote(monkeypatch, _from_pretrained)
        reset_sessions()

        config = TranscriptionConfig(diarization_device="cpu", hf_token=None)
        diarizer = diarizer_mod.SpeakerDiarizer(config)

        try:
            with pytest.raises(RuntimeError) as exc_info:
                diarizer.load_model()
        finally:
            reset_sessions()

        # Both the v4 and v3 attempts ran (proving the guard didn't just skip the
        # call), and the failure reported is the offline block, not a real network
        # error (DNS/connection refused/timeout) — proving no attempt got past it.
        assert attempts == [diarizer_mod.PYANNOTE_V4_MODEL, diarizer_mod.PYANNOTE_V3_FALLBACK]
        assert "OfflineModeIsEnabled" in str(exc_info.value) or "offline mode is enabled" in str(
            exc_info.value
        )

    def test_online_mode_is_unaffected_by_the_guard(self, monkeypatch):
        """Control: with the flag unset, the same fake load succeeds normally —
        the guard must not change online behavior at all.
        """
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)

        from unittest.mock import MagicMock

        fake_pipeline = MagicMock()
        fake_pipeline.to.return_value = fake_pipeline

        def _from_pretrained(checkpoint, token=None):
            return fake_pipeline

        _install_fake_pyannote(monkeypatch, _from_pretrained)

        config = TranscriptionConfig(diarization_device="cpu", hf_token=None)
        diarizer = diarizer_mod.SpeakerDiarizer(config)
        diarizer.load_model()

        assert diarizer.is_loaded
        assert diarizer._model_name == diarizer_mod.PYANNOTE_V4_MODEL


@pytest.mark.unit
class TestLoadIsBoundedRegardlessOfOfflineFlag:
    def test_a_stalled_load_fails_fast_instead_of_hanging(self, monkeypatch):
        """THE regression: a stalled Hub round trip must not block for the
        process lifetime of the caller. `HF_HUB_OFFLINE` is deliberately left at
        its dev default (unset) here — the bound must hold even when nobody
        opted into offline mode, which is the common case in production today.
        """
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        monkeypatch.setattr(diarizer_mod, "_PIPELINE_LOAD_TIMEOUT_S", 1.0)

        def _stalling_from_pretrained(checkpoint, token=None):
            time.sleep(5)
            raise AssertionError("unreachable: the timeout must fire first")

        _install_fake_pyannote(monkeypatch, _stalling_from_pretrained)

        config = TranscriptionConfig(diarization_device="cpu", hf_token=None)
        diarizer = diarizer_mod.SpeakerDiarizer(config)

        started = time.monotonic()
        with pytest.raises(RuntimeError, match="did not complete within"):
            diarizer.load_model()
        elapsed = time.monotonic() - started

        assert elapsed < 4, f"the timeout did not bound the wait: took {elapsed:.1f}s"
