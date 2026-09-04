"""diar-native WAV handoff (issue #661) + sidecar_ready() TTL cache (probe-cost fix).

Two independent things pinned here, both inside diarizer_native.py:

1. NativeSpeakerDiarizer.diarize() must reuse an already-materialized shared-volume WAV
   instead of re-encoding the same audio a second time, when the caller has one on a path
   this sidecar can also reach — but must still fall back to writing its own copy when no
   such path is supplied (the single-process run() path never has one). It must never delete
   a WAV it did not create.
2. sidecar_ready() now carries a short TTL cache keyed on base_url, because it used to fire a
   live 5s-timeout HTTP GET on every call and #665 added a second per-job call site on top of
   ModelManager's existing one. The cache must serve repeated calls (including negative
   results) from memory within the TTL, re-probe once the TTL lapses, and be forcibly
   invalidated by reset_readiness_cache() — the hook test_diar_native_readiness_gate.py's
   real-HTTP-server suite needs between phases that flip server state within one test.

The sidecar interaction in part 1 is stubbed at the same `post_json` seam
test_diarizer_native_embed_window.py uses — the property under test is diarize()'s own
filesystem behaviour, not the wire format. Part 2 follows test_diar_native_readiness_gate.py's
convention of a real http.server instead of mocking urllib, because the failure mode being
guarded is a real HTTP round trip either happening or not.
"""

from __future__ import annotations

import contextlib
import http.server
import os
import socket
import threading

import numpy as np
import pytest

import app.transcription.diarizer_native as diarizer_native
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.diarizer_native import reset_readiness_cache
from app.transcription.diarizer_native import sidecar_ready

#: All diar-native tests that stand up a real HTTP server, or drive diarizer_native's
#: module-level state, run on ONE xdist worker.
#:
#: `_free_port()` binds port 0, reads the number, then CLOSES the socket and returns it — so
#: between that close and the caller's `HTTPServer((host, port))` bind, another worker can be
#: handed the same ephemeral port. Seven modules use that helper and none were grouped, which
#: is why a DIFFERENT diar test failed on each full-suite run while every one of them passed in
#: isolation. Same remedy the repo already uses for tests sharing mutable global state
#: (backend/tests/CLAUDE.md's `--dist loadgroup` note); here the shared state is the machine's
#: ephemeral-port pool plus diarizer_native's readiness caches.
pytestmark = pytest.mark.xdist_group("diar_native_state")


class _Config:
    num_speakers = None
    enable_overlap_detection = False
    enable_native_embeddings = False


def _fake_diarize_reply(url: str, payload: dict, timeout: float) -> dict:
    return {
        "exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
        "segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}],
    }


class TestWavHandoff:
    def test_a_pre_existing_wav_is_reused_and_no_second_copy_is_written(
        self, tmp_path, monkeypatch
    ):
        """The whole point of #661: a caller-supplied WAV must go straight to the sidecar."""
        engine_shared = tmp_path / "engine-shared"
        engine_shared.mkdir()
        diar_scratch = tmp_path / "diar-native-scratch"
        monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
        monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(diar_scratch))

        pre_wav = engine_shared / "task-abc123.wav"
        pre_wav.write_bytes(b"not real WAV bytes, post_json is stubbed and never reads them")

        sent: list[dict] = []

        def fake_post_json(url, payload, timeout):
            sent.append(payload)
            return _fake_diarize_reply(url, payload, timeout)

        monkeypatch.setattr(diarizer_native, "post_json", fake_post_json)

        diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://fake-sidecar")
        diarizer.is_loaded = True
        audio = np.zeros(16_000, dtype=np.float32)

        result, overlap_info, embeddings = diarizer.diarize(audio, wav_path=str(pre_wav))

        assert len(sent) == 1
        assert sent[0]["wav_path"] == str(pre_wav), (
            "the supplied WAV must be posted as-is, not copied under a new name"
        )
        # Filesystem assertion, not a mock: nothing was ever written into the sidecar's own
        # scratch directory for this call.
        assert not diar_scratch.exists(), (
            "reusing a supplied WAV must not create the sidecar's own scratch directory at all"
        )
        assert len(result) == 1

    def test_a_supplied_wav_is_never_deleted_by_diarize(self, tmp_path, monkeypatch):
        engine_shared = tmp_path / "engine-shared"
        engine_shared.mkdir()
        monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
        monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-native-scratch"))

        pre_wav = engine_shared / "task-keepme.wav"
        pre_wav.write_bytes(b"stage 1's wav; a later stage still needs this file")

        monkeypatch.setattr(diarizer_native, "post_json", _fake_diarize_reply)

        diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://fake-sidecar")
        diarizer.is_loaded = True
        audio = np.zeros(16_000, dtype=np.float32)

        diarizer.diarize(audio, wav_path=str(pre_wav))

        assert pre_wav.exists(), (
            "diarize() must only ever unlink a WAV it created itself, never a caller-supplied one"
        )

    def test_without_a_pre_existing_wav_one_is_written_and_the_job_still_succeeds(
        self, tmp_path, monkeypatch
    ):
        """The single-process run() path (stages.py's _GpuStage) has no shared-volume WAV at
        all — diarize(audio) with no wav_path must keep writing (and cleaning up) its own
        copy exactly as before #661.
        """
        diar_scratch = tmp_path / "diar-native-scratch"
        monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(diar_scratch))
        # No _SHARED_VOLUME_ROOT patch needed: wav_path is never passed, so the reuse branch
        # can never be taken regardless of what that prefix is set to.

        seen: dict = {}

        def fake_post_json(url, payload, timeout):
            # The write must already be done by the time the HTTP call happens.
            seen["wav_path"] = payload["wav_path"]
            seen["existed_during_call"] = os.path.isfile(payload["wav_path"])
            return _fake_diarize_reply(url, payload, timeout)

        monkeypatch.setattr(diarizer_native, "post_json", fake_post_json)

        diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://fake-sidecar")
        diarizer.is_loaded = True
        audio = np.zeros(16_000, dtype=np.float32)

        result, overlap_info, embeddings = diarizer.diarize(audio)

        assert seen["existed_during_call"] is True
        assert seen["wav_path"].startswith(str(diar_scratch))
        assert not os.path.exists(seen["wav_path"]), (
            "a WAV this call created for itself must be cleaned up in the finally block"
        )
        assert len(result) == 1


# -- retry policy: retry a 422 (sidecar couldn't open the path), never a timeout ------------


_422_then_ok_hits = {"count": 0}


class _Counting422ThenOKHandler(http.server.BaseHTTPRequestHandler):
    """First /diarize POST answers 422 ("could not open the path"); every later one 200s."""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler method name
        _422_then_ok_hits["count"] += 1
        if _422_then_ok_hits["count"] == 1:
            self.send_response(422)
            self.end_headers()
            self.wfile.write(b"{}")
            return
        body = b'{"exclusive_segments": [{"start": 0.0, "end": 1.0, "speaker": "SPEAKER_00"}], "segments": []}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


@contextlib.contextmanager
def _accept_and_hang_server():
    """A real listening socket that accepts a connection and never answers it.

    Deliberately not http.server: a handler would parse the request and could reply fast.
    This reproduces a genuinely WEDGED sidecar — TCP connects, then nothing — so the client
    can only ever fail by hitting its own read timeout, exactly the case the retry-on-any-
    exception bug doubled.
    """
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    port = srv.getsockname()[1]
    srv.listen(5)
    accepted = {"count": 0}
    stop = threading.Event()

    def _serve() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                # The listening socket was closed from the main thread as part of teardown
                # (stop.set() + srv.close() race with a blocked accept()) — expected, not a
                # real failure.
                break
            accepted["count"] += 1
            # Never read/respond/close — the client's own timeout is what ends this.

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}", accepted
    finally:
        stop.set()
        with contextlib.suppress(OSError):
            srv.close()


class TestRetryPolicy:
    """Issue #656/#661 audit: a timeout must never be retried; a 422 must be."""

    def test_a_422_is_retried_with_a_fresh_copy(self, tmp_path, monkeypatch):
        _422_then_ok_hits["count"] = 0
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Counting422ThenOKHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            engine_shared = tmp_path / "engine-shared"
            engine_shared.mkdir()
            monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))

            pre_wav = engine_shared / "task-422.wav"
            pre_wav.write_bytes(b"not a real wav; the sidecar is real but doesn't read it")

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=f"http://127.0.0.1:{port}")
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            result, _, _ = diarizer.diarize(audio, wav_path=str(pre_wav))

            assert _422_then_ok_hits["count"] == 2, (
                "a 422 (sidecar reachable but couldn't open THIS path) must be retried once "
                "with a freshly written copy"
            )
            assert len(result) == 1
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_timeout_is_never_retried(self, tmp_path, monkeypatch):
        """A wedged sidecar must fail over after exactly ONE request, not two.

        Before this fix, any exception (including a timeout) on the staged-WAV attempt
        triggered a second attempt — a fresh copy POSTed to the same wedged sidecar — which
        doubles the worst-case hang from one _TIMEOUT_S to two before PyAnnote takes over.
        """
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 0.3)
        with _accept_and_hang_server() as (base_url, accepted):
            engine_shared = tmp_path / "engine-shared"
            engine_shared.mkdir()
            monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))
            monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", _FakeFallback)

            pre_wav = engine_shared / "task-timeout.wav"
            pre_wav.write_bytes(b"staged wav; the sidecar never reads it, it never answers")

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=base_url)
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            result, _, _ = diarizer.diarize(audio, wav_path=str(pre_wav))

            assert accepted["count"] == 1, (
                "a timeout must NOT be retried — a second attempt at the same sidecar and "
                "the same _TIMEOUT_S ceiling can only double the worst-case hang before the "
                "PyAnnote fallback takes over"
            )
            assert len(result) == 1, "must still degrade to the PyAnnote fallback, not raise"

    def test_a_failed_own_copy_post_leaves_no_wav_behind(self, tmp_path, monkeypatch):
        """A failed own-copy POST must not orphan the WAV it wrote.

        ``_post_own_copy`` ends ``return self._post_diarize(own_wav), own_wav``. Python
        evaluates the POST first, so when it raises, the tuple is never constructed and the
        caller's ``own_wav`` stays ``None`` — making its ``finally``'s ``if own_wav:``
        cleanup a no-op. Nothing sweeps ``_SHARED_DIR``, so at 32 KB per audio-second this
        orphaned ~460 MB per failed 4-hour job onto the ``diar-native-tmp`` volume.

        No ``wav_path`` is passed, so the reuse branch is skipped entirely and the own-copy
        path is the only one exercised — the file under test is one this call created.
        """
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 0.3)
        with _accept_and_hang_server() as (base_url, accepted):
            diar_scratch = tmp_path / "diar-scratch"
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(diar_scratch))
            monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", _FakeFallback)

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=base_url)
            diarizer.is_loaded = True

            result, _, _ = diarizer.diarize(np.zeros(16_000, dtype=np.float32))

            assert accepted["count"] == 1, "the own-copy POST should have been attempted once"
            assert len(result) == 1, "must still degrade to the PyAnnote fallback, not raise"
            orphans = sorted(diar_scratch.glob("diar_*.wav"))
            assert orphans == [], (
                f"a failed own-copy POST orphaned {len(orphans)} WAV(s) in the sidecar "
                f"scratch dir: {[p.name for p in orphans]}. Nothing sweeps that directory, "
                "so every failed job leaks its full decoded audio onto the volume."
            )

    def test_a_timeout_while_overlapped_raises_instead_of_retrying_or_falling_back(
        self, tmp_path, monkeypatch
    ):
        """Same timeout, but allow_local_fallback=False (the overlapped-diarization thread,
        issue #665): still exactly one request, and it must raise rather than build the
        in-process fallback on this thread.
        """
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 0.3)
        with _accept_and_hang_server() as (base_url, accepted):
            engine_shared = tmp_path / "engine-shared"
            engine_shared.mkdir()
            monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))

            pre_wav = engine_shared / "task-timeout2.wav"
            pre_wav.write_bytes(b"staged wav")

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=base_url)
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            with pytest.raises(RuntimeError):
                diarizer.diarize(audio, wav_path=str(pre_wav), allow_local_fallback=False)

            assert accepted["count"] == 1


_429_hits = {"count": 0}


class _429Handler(http.server.BaseHTTPRequestHandler):
    """Every /diarize POST answers 429 with a Retry-After header — a live backpressure signal."""

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler method name
        _429_hits["count"] += 1
        self.send_response(429)
        self.send_header("Retry-After", "30")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass


class TestSidecarFailureClassification:
    """Issue #656 Step 3: backpressure (429/503) must be classified separately from a "bad
    path" 4xx, must NOT cost a wasted own-copy write, and must carry Retry-After through.
    """

    def test_a_429_is_not_retried_with_a_fresh_copy(self, tmp_path, monkeypatch):
        _429_hits["count"] = 0
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _429Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            engine_shared = tmp_path / "engine-shared"
            engine_shared.mkdir()
            diar_scratch = tmp_path / "diar-scratch"
            monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(diar_scratch))

            pre_wav = engine_shared / "task-429.wav"
            pre_wav.write_bytes(b"staged wav; the sidecar answers 429 without reading it")

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=f"http://127.0.0.1:{port}")
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            from app.transcription.diarizer_native import DiarSidecarUnavailableError

            # allow_local_fallback=False (the overlap thread) always wraps in a plain
            # RuntimeError regardless of classification — see that flag's docstring — so the
            # classified exception is the wrapper's __cause__, exactly what
            # _AsyncDiarization.failed_unavailable (engine/stages.py) inspects.
            with pytest.raises(RuntimeError) as excinfo:
                diarizer.diarize(audio, wav_path=str(pre_wav), allow_local_fallback=False)

            assert _429_hits["count"] == 1, (
                "a 429 must NOT be retried with a freshly written copy — the sidecar just "
                "said back off, wasting a full WAV write plus a second admission attempt "
                "against the thing that told us to wait would be exactly wrong"
            )
            assert not diar_scratch.exists(), "no own-copy WAV should ever have been written"
            cause = excinfo.value.__cause__
            assert isinstance(cause, DiarSidecarUnavailableError)
            assert cause.reason == "backpressure"
            assert cause.retry_after == 30.0
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_a_saturated_sidecar_raises_timeout_reason_not_unreachable(self, tmp_path, monkeypatch):
        """A wedged (not dead) sidecar must classify as "timeout", never "unreachable" —
        an operator debugging the two needs different fixes (restart vs. recreate)."""
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 0.3)
        with _accept_and_hang_server() as (base_url, accepted):
            engine_shared = tmp_path / "engine-shared"
            engine_shared.mkdir()
            monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))

            pre_wav = engine_shared / "task-saturated.wav"
            pre_wav.write_bytes(b"staged wav; the sidecar never answers")

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=base_url)
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            from app.transcription.diarizer_native import DiarSidecarUnavailableError

            with pytest.raises(RuntimeError) as excinfo:
                diarizer.diarize(audio, wav_path=str(pre_wav), allow_local_fallback=False)

            cause = excinfo.value.__cause__
            assert isinstance(cause, DiarSidecarUnavailableError)
            assert cause.reason == "timeout"
            assert accepted["count"] == 1

    def test_connection_refused_raises_unreachable_reason(self, tmp_path, monkeypatch):
        """A genuinely closed port — process gone — must classify as "unreachable"."""
        monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()  # bound-then-released: guaranteed ECONNREFUSED

        diarizer = NativeSpeakerDiarizer(_Config(), base_url=f"http://127.0.0.1:{port}")
        diarizer.is_loaded = True
        audio = np.zeros(16_000, dtype=np.float32)

        from app.transcription.diarizer_native import DiarSidecarUnavailableError

        with pytest.raises(RuntimeError) as excinfo:
            diarizer.diarize(audio, allow_local_fallback=False)

        cause = excinfo.value.__cause__
        assert isinstance(cause, DiarSidecarUnavailableError)
        assert cause.reason == "unreachable"

    def test_a_422_is_still_not_classified_as_sidecar_unavailable(self, tmp_path, monkeypatch):
        """A 422 stays a plain retry-with-fresh-copy path — it must never be classified as
        DiarSidecarUnavailableError, or a Celery task would retry it (Step 5) when a fresh copy
        already fixes it in-process."""
        _422_then_ok_hits["count"] = 0
        httpd = http.server.HTTPServer(("127.0.0.1", 0), _Counting422ThenOKHandler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            engine_shared = tmp_path / "engine-shared"
            engine_shared.mkdir()
            monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
            monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))

            pre_wav = engine_shared / "task-422b.wav"
            pre_wav.write_bytes(b"staged wav")

            diarizer = NativeSpeakerDiarizer(_Config(), base_url=f"http://127.0.0.1:{port}")
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            result, _, _ = diarizer.diarize(
                audio, wav_path=str(pre_wav), allow_local_fallback=False
            )
            assert len(result) == 1
            assert _422_then_ok_hits["count"] == 2
        finally:
            httpd.shutdown()
            httpd.server_close()


def _db_unavailable_for_require_sidecar() -> None:
    raise RuntimeError("db unavailable in test")


class TestRequireSidecarPolicy:
    """Issue #656 Step 6: engine.diarizer_require_sidecar makes the fail-hard policy
    exercisable before #572 deletes the fallback outright."""

    def test_default_off_reproduces_todays_fallback(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", raising=False)
        monkeypatch.setattr(
            "app.db.session_utils.session_scope", _db_unavailable_for_require_sidecar
        )
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 0.3)
        monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", _FakeFallback)
        with _accept_and_hang_server() as (base_url, _accepted):
            diarizer = NativeSpeakerDiarizer(_Config(), base_url=base_url)
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            result, _, _ = diarizer.diarize(audio)
            assert len(result) == 1
            assert diarizer.last_provider == "pyannote"

    def test_enabled_raises_instead_of_falling_back(self, monkeypatch):
        monkeypatch.setenv("ENGINE_DIARIZER_REQUIRE_SIDECAR", "true")
        monkeypatch.setattr(
            "app.db.session_utils.session_scope", _db_unavailable_for_require_sidecar
        )
        monkeypatch.setattr(diarizer_native, "_TIMEOUT_S", 0.3)
        built: list[object] = []

        class _CountingFake(_FakeFallback):
            def __init__(self, config):
                super().__init__(config)
                built.append(self)

        monkeypatch.setattr("app.transcription.diarizer.SpeakerDiarizer", _CountingFake)
        with _accept_and_hang_server() as (base_url, _accepted):
            diarizer = NativeSpeakerDiarizer(_Config(), base_url=base_url)
            diarizer.is_loaded = True
            audio = np.zeros(16_000, dtype=np.float32)

            from app.transcription.diarizer_native import DiarSidecarUnavailableError

            with pytest.raises(DiarSidecarUnavailableError):
                diarizer.diarize(audio)

            assert built == [], (
                "engine.diarizer_require_sidecar=true must never build the PyAnnote "
                "fallback — that is the whole point of the setting"
            )


class _FakeFallback:
    """Minimal PyAnnote-shaped stand-in, local to the retry-policy tests."""

    def __init__(self, config):
        self.config = config
        self.is_loaded = False

    def load_model(self) -> None:
        self.is_loaded = True

    def unload_model(self) -> None:
        self.is_loaded = False

    def diarize(self, audio):
        from app.transcription.diarize_result import DiarizeResult

        return (
            DiarizeResult(
                start=np.array([0.0]),
                end=np.array([1.0]),
                speaker=np.array(["SPEAKER_00"], dtype=object),
            ),
            {"count": 0, "duration": 0.0, "regions": []},
            {},
        )


# -- E0 observability: log which case fired (reused / mismatched-prefix / missing-file) -----


class TestReuseObservability:
    def test_logs_when_the_staged_path_is_not_under_the_shared_prefix(
        self, tmp_path, monkeypatch, caplog
    ):
        import logging

        engine_shared = tmp_path / "engine-shared"
        engine_shared.mkdir()
        monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
        monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))
        monkeypatch.setattr(diarizer_native, "post_json", _fake_diarize_reply)

        # A path that exists but lives OUTSIDE the configured shared prefix — the E0 mismatch
        # this stale-.env install shape produces.
        elsewhere = tmp_path / "elsewhere.wav"
        elsewhere.write_bytes(b"a wav not under the shared prefix")

        diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://fake-sidecar")
        diarizer.is_loaded = True
        audio = np.zeros(16_000, dtype=np.float32)

        caplog.set_level(logging.INFO)
        diarizer.diarize(audio, wav_path=str(elsewhere))

        assert any(
            "NOT used" in r.message and "not under the shared volume root" in r.message
            for r in caplog.records
        ), "a mismatched shared-volume root must be logged, not silently absorbed"

    def test_logs_when_reuse_actually_fires(self, tmp_path, monkeypatch, caplog):
        import logging

        engine_shared = tmp_path / "engine-shared"
        engine_shared.mkdir()
        monkeypatch.setattr(diarizer_native, "_SHARED_VOLUME_ROOT", str(engine_shared))
        monkeypatch.setattr(diarizer_native, "_SHARED_DIR", str(tmp_path / "diar-scratch"))
        monkeypatch.setattr(diarizer_native, "post_json", _fake_diarize_reply)

        pre_wav = engine_shared / "task-ok.wav"
        pre_wav.write_bytes(b"a properly staged wav")

        diarizer = NativeSpeakerDiarizer(_Config(), base_url="http://fake-sidecar")
        diarizer.is_loaded = True
        audio = np.zeros(16_000, dtype=np.float32)

        caplog.set_level(logging.DEBUG)
        diarizer.diarize(audio, wav_path=str(pre_wav))

        assert any("reusing staged WAV" in r.message for r in caplog.records), (
            "a successful reuse must also be observable, not just its absence"
        )


# -- sidecar_ready() TTL cache -------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_counting_readyz_handler(state: dict):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
            if self.path == "/readyz":
                state["hits"] += 1
                self.send_response(state["code"])
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return _Handler


@contextlib.contextmanager
def _counting_sidecar(state: dict):
    """A real HTTP sidecar stand-in that counts /readyz hits; yields its base URL."""
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _make_counting_readyz_handler(state))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()


class TestSidecarReadyTTLCache:
    def test_two_calls_within_the_ttl_issue_exactly_one_http_request(self, monkeypatch):
        monkeypatch.setattr(diarizer_native, "_READY_CACHE_TTL_S", 60.0)
        reset_readiness_cache()
        state = {"hits": 0, "code": 200}
        with _counting_sidecar(state) as url:
            assert sidecar_ready(url) is True
            assert sidecar_ready(url) is True
            assert state["hits"] == 1, "the second call within the TTL must be served from cache"
        reset_readiness_cache()

    def test_a_negative_result_is_cached_too(self, monkeypatch):
        """Negative results are the expensive case this cache exists to stop re-paying."""
        monkeypatch.setattr(diarizer_native, "_READY_CACHE_TTL_S", 60.0)
        reset_readiness_cache()
        state = {"hits": 0, "code": 503}
        with _counting_sidecar(state) as url:
            assert sidecar_ready(url) is False
            assert sidecar_ready(url) is False
            assert state["hits"] == 1
        reset_readiness_cache()

    def test_an_entry_is_stamped_after_the_probe_not_before(self, monkeypatch):
        """A SLOW probe must still yield a cache hit — the born-expired regression.

        Stamping the entry with a clock read taken *before* the probe makes it as old as
        the probe took. Against an unreachable sidecar the probe runs the full connect
        timeout, so with a TTL shorter than that every entry is born expired and the cache
        serves nothing — in the one situation it exists for. This drives a probe that takes
        longer than the TTL and asserts the second caller is still served from cache; with
        the pre-fix ordering it issues a second request.
        """
        clock = {"now": 1000.0}
        monkeypatch.setattr(diarizer_native.time, "monotonic", lambda: clock["now"])
        # The TTL must be SHORTER than the probe for this to discriminate. With a longer
        # one both orderings produce a hit — the first draft of this test used TTL 6 against
        # a 5 s probe and passed against the bug it was written to catch.
        monkeypatch.setattr(diarizer_native, "_READY_CACHE_TTL_S", 3.0)
        reset_readiness_cache()

        calls: list[str] = []

        def _slow_probe(url: str) -> bool:
            calls.append(url)
            clock["now"] += 5.0  # an unreachable sidecar burns the whole connect timeout
            return False

        monkeypatch.setattr(diarizer_native, "_sidecar_ready_uncached", _slow_probe)
        assert diarizer_native.sidecar_ready("http://example.invalid") is False
        assert diarizer_native.sidecar_ready("http://example.invalid") is False
        assert len(calls) == 1, (
            "the second call re-probed: the entry was stamped with a pre-probe clock read, "
            "so it was born 5s old against a 3s TTL — the cache serves nothing in the exact "
            "case it exists for"
        )
        reset_readiness_cache()

    def test_liveness_is_cached_independently_of_readiness(self, monkeypatch):
        """`/healthz` is probed twice per failing job and used to be uncached entirely."""
        monkeypatch.setattr(diarizer_native, "_READY_CACHE_TTL_S", 60.0)
        reset_readiness_cache()
        health: list[str] = []

        def _probe(url: str) -> bool:
            health.append(url)
            return True

        monkeypatch.setattr(diarizer_native, "_sidecar_healthy_uncached", _probe)
        assert diarizer_native.sidecar_healthy("http://example.invalid") is True
        assert diarizer_native.sidecar_healthy("http://example.invalid") is True
        assert len(health) == 1, "liveness must be cached too, not just readiness"
        reset_readiness_cache()

    def test_the_ttl_can_never_be_configured_below_the_probe_timeout(self, run_in_clean_process):
        """A TTL under the probe timeout makes every entry born expired, so it is floored.

        Exercises the REAL module-level resolution (``max(env value, floor)`` in
        ``diarizer_native.py``) by configuring a too-small value in a fresh interpreter,
        rather than comparing the two already-floored module constants against each other —
        which passes even with the flooring logic deleted outright.

        ⚠️ IN A CHILD PROCESS, never ``importlib.reload`` in this one. Reloading
        ``diarizer_native`` rebinds its classes and module-level caches, while every other
        module that did ``from app.transcription.diarizer_native import X`` keeps the ORIGINAL
        object — so ``isinstance`` checks and identity comparisons across the boundary quietly
        stop matching. Reloading a second time to "restore" does not help; it makes a third
        set of objects.

        Measured: an earlier version of this test reloaded in-process and broke
        ``test_diarizer_engine_selection.py::TestOverlapMidJobFailureDegradesSafely::
        test_release_and_fallback_happen_on_the_main_thread_after_transcription`` — but only
        when the two modules landed on the same xdist worker, so it presented as a different
        diar test failing on each full-suite run while all of them passed in isolation.
        Bisected to this reload by running the grouped modules pairwise.
        """
        out = run_in_clean_process(
            "from app.transcription import diarizer_native as d;"
            "print(f'{d._READY_CACHE_TTL_S},{d._PROBE_TIMEOUT_S}')",
            DIAR_NATIVE_READY_CACHE_TTL_S="1",
        )
        ttl, probe = (float(x) for x in out.strip().split(","))
        assert ttl > probe, (
            f"a configured TTL (1s) below the probe timeout was not floored: {ttl} <= {probe}"
        )
        assert ttl == probe + 1.0, f"expected the floor value ({probe + 1.0}), got {ttl}"

    def test_a_call_after_the_ttl_lapses_issues_a_fresh_request(self, monkeypatch):
        """Advance the clock rather than sleeping on it.

        A real ``time.sleep`` here would be a fixed wait racing a real HTTP round-trip on a
        loaded machine: too short and it flakes, too long and it taxes every future run.
        Driving ``time.monotonic`` makes the expiry exact and the test instant — and the
        cache reads the clock through the module, so replacing that one function is enough.
        """
        clock = {"now": 1000.0}
        monkeypatch.setattr(diarizer_native.time, "monotonic", lambda: clock["now"])
        monkeypatch.setattr(diarizer_native, "_READY_CACHE_TTL_S", 10.0)
        reset_readiness_cache()
        state = {"hits": 0, "code": 200}
        with _counting_sidecar(state) as url:
            assert sidecar_ready(url) is True
            assert state["hits"] == 1

            clock["now"] += 9.0  # still inside the window
            assert sidecar_ready(url) is True
            assert state["hits"] == 1, "a call inside the TTL must not re-probe"

            clock["now"] += 2.0  # now past it
            assert sidecar_ready(url) is True
            assert state["hits"] == 2, (
                "an expired cache entry must trigger a real re-probe, not reuse a stale verdict "
                "— this is the half of the no-pinning contract that keeps a recovered sidecar "
                "reachable without a worker restart"
            )
        reset_readiness_cache()

    def test_reset_readiness_cache_forces_an_immediate_recheck(self, monkeypatch):
        """The hook the orchestrator must wire into test_diar_native_readiness_gate.py: that
        suite flips a real server between states within a single test, and without clearing
        the cache between phases a verdict cached before the flip would mask the new state
        until the TTL lapsed.
        """
        monkeypatch.setattr(diarizer_native, "_READY_CACHE_TTL_S", 60.0)
        reset_readiness_cache()
        state = {"hits": 0, "code": 200}
        with _counting_sidecar(state) as url:
            assert sidecar_ready(url) is True
            assert state["hits"] == 1

            state["code"] = 503  # the sidecar's state changes mid-test
            assert sidecar_ready(url) is True, "still cached — the flip alone must not be visible"

            reset_readiness_cache()
            assert sidecar_ready(url) is False, "after reset the new state must be visible at once"
            assert state["hits"] == 2
        reset_readiness_cache()


class TestSidecarDiagnostics:
    """Issue #656 Step 9: sidecar_diagnostics() is the live "can it serve right now" probe
    behind the admin stats `diarization.sidecar` sub-object — a DIFFERENT question from
    #706's per-file `diarization_provider` column, and must never be derived from it."""

    def test_reports_live_and_ready_against_a_real_healthy_server(self, monkeypatch):
        reset_readiness_cache()
        monkeypatch.setenv("DIAR_NATIVE_MAX_INFLIGHT", "3")
        srv = http.server.HTTPServer(("127.0.0.1", 0), _HealthzAndReadyzHandler)
        port = srv.server_address[1]
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            info = diarizer_native.sidecar_diagnostics(f"http://127.0.0.1:{port}")
            assert info["live"] is True
            assert info["ready"] is True
            assert info["max_inflight"] == 3
            assert info["url"] == f"http://127.0.0.1:{port}"
        finally:
            srv.shutdown()
            srv.server_close()
            reset_readiness_cache()

    def test_reports_dead_against_a_real_closed_port(self, monkeypatch):
        reset_readiness_cache()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        port = srv.getsockname()[1]
        srv.close()

        info = diarizer_native.sidecar_diagnostics(f"http://127.0.0.1:{port}")
        assert info["live"] is False
        assert info["ready"] is False
        reset_readiness_cache()


class _HealthzAndReadyzHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
        if self.path in ("/healthz", "/readyz"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            body = b'{"models_state": "ready"}'
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass
