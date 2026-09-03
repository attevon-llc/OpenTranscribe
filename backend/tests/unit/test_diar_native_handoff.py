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

import app.transcription.diarizer_native as diarizer_native
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.diarizer_native import reset_readiness_cache
from app.transcription.diarizer_native import sidecar_ready


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
        monkeypatch.setattr(diarizer_native, "_ENGINE_SHARED_DIR", str(engine_shared))
        monkeypatch.setattr(diarizer_native, "_ENGINE_SHARED_PREFIX", str(engine_shared) + "/")
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
        monkeypatch.setattr(diarizer_native, "_ENGINE_SHARED_DIR", str(engine_shared))
        monkeypatch.setattr(diarizer_native, "_ENGINE_SHARED_PREFIX", str(engine_shared) + "/")
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
        # No _ENGINE_SHARED_DIR patch needed: wav_path is never passed, so the reuse branch
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
