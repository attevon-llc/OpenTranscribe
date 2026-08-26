"""Diarizer engine selection + failover (issue #58 / #60).

Three mechanisms used to select the diarizer: ``DiarizationProviderFactory`` (a different
axis — diarization SOURCE per user, see its own CLAUDE.md), the ``engine/backends`` registry
(previously unused/dead), and an ad-hoc ``DIARIZER_ENGINE`` env read duplicated in
``model_manager.py`` (x2) and ``engine/stages.py``. The env reads are now gone; the single
decision point is ``TranscriptionConfig.diarizer_backend`` — resolved DB
(``engine.diarizer_backend``) -> env (``ENGINE_DIARIZER_BACKEND``) -> default ``"native"`` by
``TranscriptionConfig._resolve_diarizer_backend``, and validated against
``engine.backends.VALID_DIARIZER_BACKENDS`` (which also gates the admin API — see
``tests/test_engine_settings.py``).

The failover only fires when the diar-native sidecar is unreachable — i.e. approximately
never in normal operation, which is exactly the shape that rots silently. The tests below
drive it with REAL TCP unreachability (a closed port / a server that is shut down mid-test),
not a patched exception, and assert the fallback is both functional (produces a valid
diarization result) and observable (logged), never silent. The expensive collaborator being
substituted is the GPU/PyAnnote weights load (``FakeSpeakerDiarizer``) — a real object with
its own behavior, not a mock asserting a call happened.
"""

from __future__ import annotations

import contextlib
import http.server
import logging
import socket
import threading
from typing import cast

import numpy as np
import pytest

from app.transcription.config import TranscriptionConfig
from app.transcription.diarize_result import DiarizeResult
from app.transcription.diarizer import SpeakerDiarizer
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.engine import stages as stages_mod
from app.transcription.engine.backends import VALID_DIARIZER_BACKENDS
from app.transcription.engine.backends import get_diarizer_backend
from app.transcription.engine.backends.diarizers.native_backend import NativeBackend
from app.transcription.engine.backends.diarizers.pyannote_backend import PyAnnoteBackend
from app.transcription.model_manager import ModelManager


def _free_port() -> int:
    """A real TCP port on localhost, bound then released — not merely an unused number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class FakeSpeakerDiarizer:
    """Stand-in for the PyAnnote fork: same public surface, no GPU/model weights.

    A real object with real (if canned) behavior — substituting only the expensive
    collaborator — so a test using it exercises actual control flow rather than asserting
    an internal call happened.
    """

    def __init__(self, config):
        self.config = config
        self.is_loaded = False
        self.diarize_calls = 0

    def load_model(self) -> None:
        self.is_loaded = True

    def unload_model(self) -> None:
        self.is_loaded = False

    def diarize(self, audio):
        self.diarize_calls += 1
        return (
            DiarizeResult(
                start=np.array([0.0]),
                end=np.array([1.0]),
                speaker=np.array(["SPEAKER_00"], dtype=object),
            ),
            {"count": 0, "duration": 0.0, "regions": []},
            {"SPEAKER_00": np.array([0.1, 0.2], dtype=np.float32)},
        )

    def embed_window(self, audio, start, end):
        return np.array([0.1, 0.2], dtype=np.float32)


class _HealthzOnlyHandler(http.server.BaseHTTPRequestHandler):
    """A real, minimal HTTP server: 200 on GET /healthz, 404 on anything else."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        if self.path == "/healthz":
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # silence request logging in test output


@pytest.fixture
def healthz_server():
    """A real /healthz server on a real free port, in a background thread.

    Yields (port, httpd) so a test can shut it down mid-test to produce REAL
    unreachability (connection refused) rather than a patched exception.
    """
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _HealthzOnlyHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield port, httpd
    finally:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()


def _db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force TranscriptionConfig._resolve_diarizer_backend's DB read to fail.

    Deterministic stand-in for "no admin has configured this" that does not depend on
    (or risk mutating) whatever engine.diarizer_backend row happens to exist on the real
    dev database this suite runs against.
    """

    def _boom() -> None:
        raise RuntimeError("db unavailable in test")

    monkeypatch.setattr("app.db.session_utils.session_scope", _boom)


# ---------------------------------------------------------------------------
# Registry (#58): the single validated vocabulary
# ---------------------------------------------------------------------------


class TestDiarizerBackendRegistry:
    def test_native_and_pyannote_are_the_only_registered_backends(self):
        assert set(VALID_DIARIZER_BACKENDS) == {"native", "pyannote"}

    def test_get_diarizer_backend_native_returns_native_backend(self):
        backend = get_diarizer_backend("native")
        assert isinstance(backend, NativeBackend)

    def test_get_diarizer_backend_pyannote_returns_pyannote_backend(self):
        backend = get_diarizer_backend("pyannote")
        assert isinstance(backend, PyAnnoteBackend)

    def test_get_diarizer_backend_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown diarizer backend"):
            get_diarizer_backend("nemo-not-registered")


# ---------------------------------------------------------------------------
# TranscriptionConfig.diarizer_backend resolution: default, override, validation
# ---------------------------------------------------------------------------


class TestDiarizerBackendResolution:
    def test_dataclass_default_is_native(self):
        """No I/O: the coded field default itself is native."""
        assert TranscriptionConfig().diarizer_backend == "native"

    def test_native_selected_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("ENGINE_DIARIZER_BACKEND", raising=False)
        _db_unavailable(monkeypatch)
        assert TranscriptionConfig._resolve_diarizer_backend() == "native"

    def test_explicit_override_is_honoured(self, monkeypatch: pytest.MonkeyPatch):
        """pyannote can still be pinned directly via ENGINE_DIARIZER_BACKEND."""
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "pyannote")
        _db_unavailable(monkeypatch)
        assert TranscriptionConfig._resolve_diarizer_backend() == "pyannote"

    def test_unknown_value_falls_back_to_native_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "some-future-engine")
        _db_unavailable(monkeypatch)
        caplog.set_level(logging.WARNING)
        result = TranscriptionConfig._resolve_diarizer_backend()
        assert result == "native"
        assert any("Unknown diarizer_backend" in r.message for r in caplog.records)

    def test_from_environment_wires_diarizer_backend(self, monkeypatch: pytest.MonkeyPatch):
        """The field actually reaches a fully-built TranscriptionConfig, not just the helper."""
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "pyannote")
        _db_unavailable(monkeypatch)
        cfg = TranscriptionConfig.from_environment()
        assert cfg.diarizer_backend == "pyannote"


# ---------------------------------------------------------------------------
# _overlap_diarization_enabled: only meaningful for the sidecar engine
# ---------------------------------------------------------------------------


class TestOverlapDiarizationEnabled:
    def test_disabled_for_pyannote_backend(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DIAR_OVERLAP", raising=False)
        tc = TranscriptionConfig(diarizer_backend="pyannote")
        assert stages_mod._overlap_diarization_enabled(tc) is False

    def test_enabled_for_native_backend_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("DIAR_OVERLAP", raising=False)
        tc = TranscriptionConfig(diarizer_backend="native")
        assert stages_mod._overlap_diarization_enabled(tc) is True

    def test_disabled_when_diar_overlap_env_is_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("DIAR_OVERLAP", "0")
        tc = TranscriptionConfig(diarizer_backend="native")
        assert stages_mod._overlap_diarization_enabled(tc) is False


# ---------------------------------------------------------------------------
# ModelManager._diarizer_current: cache-reuse decision, independent of config_hash
# ---------------------------------------------------------------------------


class TestDiarizerCurrent:
    def test_native_selected_and_healthy_keeps_native_cached(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(
            "app.transcription.diarizer_native.sidecar_healthy", lambda *a, **kw: True
        )
        config = TranscriptionConfig(diarizer_backend="native")
        cached = cast(SpeakerDiarizer, NativeSpeakerDiarizer(config))
        assert ModelManager._diarizer_current(cached, config) is True

    def test_native_selected_but_unhealthy_swaps_and_logs(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        monkeypatch.setattr(
            "app.transcription.diarizer_native.sidecar_healthy", lambda *a, **kw: False
        )
        config = TranscriptionConfig(diarizer_backend="native")
        cached = cast(SpeakerDiarizer, NativeSpeakerDiarizer(config))
        caplog.set_level(logging.WARNING)
        assert ModelManager._diarizer_current(cached, config) is False
        assert any("unreachable" in r.message for r in caplog.records)

    def test_pyannote_selected_rejects_native_cached(self, monkeypatch: pytest.MonkeyPatch):
        """Pinning pyannote explicitly means a cached native instance is always stale —
        and no sidecar probe should even run for a pinned-pyannote config."""
        probed: list[int] = []

        def _fake_healthy(*_a: object, **_kw: object) -> bool:
            probed.append(1)
            return True

        monkeypatch.setattr("app.transcription.diarizer_native.sidecar_healthy", _fake_healthy)
        config = TranscriptionConfig(diarizer_backend="pyannote")
        cached = cast(SpeakerDiarizer, NativeSpeakerDiarizer(config))
        assert ModelManager._diarizer_current(cached, config) is False
        assert probed == []

    def test_pyannote_selected_and_pyannote_cached_is_current(self):
        config = TranscriptionConfig(diarizer_backend="pyannote")
        cached = cast(SpeakerDiarizer, FakeSpeakerDiarizer(config))
        assert ModelManager._diarizer_current(cached, config) is True


# ---------------------------------------------------------------------------
# End-to-end failover: REAL unreachability, functional AND observable
# ---------------------------------------------------------------------------


class TestRealFailover:
    def test_load_time_fallback_engages_on_unreachable_sidecar(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """ModelManager._build_diarizer: nothing listens on the sidecar port at all.

        A real closed TCP port (ECONNREFUSED), not a patched exception. The fallback must
        both work (produce a usable diarizer) and be observable (logged).
        """
        closed_port = _free_port()  # bound-then-released: guaranteed nothing is listening
        monkeypatch.setattr(
            "app.transcription.diarizer_native._DEFAULT_URL",
            f"http://127.0.0.1:{closed_port}",
        )
        monkeypatch.setattr("app.transcription.model_manager.SpeakerDiarizer", FakeSpeakerDiarizer)

        config = TranscriptionConfig(diarizer_backend="native")
        manager = ModelManager()  # fresh instance — not the process-wide singleton

        caplog.set_level(logging.WARNING)
        diarizer = manager._build_diarizer(config)

        assert isinstance(diarizer, FakeSpeakerDiarizer), (
            "sidecar unreachable at load time must fall back to the PyAnnote-shaped engine"
        )
        assert diarizer.is_loaded

        audio = np.zeros(16000, dtype=np.float32)
        result, overlap_info, embeddings = diarizer.diarize(audio)
        assert isinstance(result, DiarizeResult)
        assert len(result) == 1
        assert overlap_info == {"count": 0, "duration": 0.0, "regions": []}
        assert embeddings is not None

        assert any("Native diarizer unavailable" in r.message for r in caplog.records), (
            "an invisible load-time failover means running degraded for weeks unnoticed"
        )

    def test_midjob_fallback_engages_when_sidecar_dies_after_load(
        self,
        healthz_server,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path,
    ):
        """NativeSpeakerDiarizer.diarize(): sidecar answers /healthz at load time, then is
        killed for real before /diarize is called — a genuine mid-job loss, not a patched
        exception."""
        port, httpd = healthz_server
        monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", FakeSpeakerDiarizer)
        monkeypatch.setattr("app.transcription.diarizer_native._SHARED_DIR", str(tmp_path))

        config = TranscriptionConfig(diarizer_backend="native")
        native = NativeSpeakerDiarizer(config, base_url=f"http://127.0.0.1:{port}")
        native.load_model()
        assert native.is_loaded

        # Kill the sidecar for real: releasing the OS socket makes the next connection
        # attempt fail with a genuine ECONNREFUSED.
        httpd.shutdown()
        httpd.server_close()

        audio = np.zeros(16000, dtype=np.float32)
        caplog.set_level(logging.WARNING)
        result, overlap_info, embeddings = native.diarize(audio)

        assert isinstance(result, DiarizeResult)
        assert len(result) == 1
        assert isinstance(native._fallback, FakeSpeakerDiarizer), (
            "mid-job loss must fall back to the same PyAnnote-shaped engine the caller "
            "already understands"
        )

        assert any("falling back to PyAnnote" in r.message for r in caplog.records), (
            "a silent mid-job failover means the slow path runs for weeks with no signal"
        )

        # A second call re-attempts the sidecar (diarize() has no short-circuit, unlike
        # embed_window()) and must fail over again rather than raising — same dead port,
        # same outcome.
        result2, _, _ = native.diarize(audio)
        assert isinstance(result2, DiarizeResult)

    def test_midjob_fallback_engages_when_the_scratch_volume_is_unwritable(
        self,
        healthz_server,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        tmp_path,
    ):
        """NativeSpeakerDiarizer.diarize(): the shared scratch dir cannot be written to.

        A genuine `PermissionError` from a real unwritable directory (chmod 0o555), not
        a patched exception — the same failure shape the `/tmp/diar-native` named-volume
        ownership bug produced in production (the volume landed root-owned on first
        creation, so the non-root worker's WAV write failed). Before this fix, only the
        `/diarize` HTTP call was wrapped in the fallback handler; a write failure
        propagated out of `diarize()` uncaught and hard-failed the whole transcription
        instead of degrading to PyAnnote the way an unreachable sidecar does.
        """
        port, httpd = healthz_server
        monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", FakeSpeakerDiarizer)

        unwritable_dir = tmp_path / "readonly-scratch"
        unwritable_dir.mkdir(mode=0o555)
        monkeypatch.setattr("app.transcription.diarizer_native._SHARED_DIR", str(unwritable_dir))

        config = TranscriptionConfig(diarizer_backend="native")
        native = NativeSpeakerDiarizer(config, base_url=f"http://127.0.0.1:{port}")
        native.load_model()
        assert native.is_loaded

        try:
            audio = np.zeros(16000, dtype=np.float32)
            caplog.set_level(logging.WARNING)
            result, overlap_info, embeddings = native.diarize(audio)

            assert isinstance(result, DiarizeResult)
            assert len(result) == 1
            assert isinstance(native._fallback, FakeSpeakerDiarizer), (
                "an unwritable scratch volume must fall back to the same PyAnnote-shaped "
                "engine the caller already understands, exactly like a sidecar loss"
            )
            assert any("falling back to PyAnnote" in r.message for r in caplog.records), (
                "a silent failover on a permission error means the slow path runs for "
                "weeks with no signal, same as a silent sidecar failover"
            )
        finally:
            # tmp_path's own cleanup needs write access to remove the directory.
            unwritable_dir.chmod(0o755)
            httpd.shutdown()
            httpd.server_close()
