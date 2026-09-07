"""The sidecar routing predicate must be READINESS, not liveness (diar-native #679).

diar-native's ``/healthz`` returns **200 in every model state**, including "no models
provisioned" and "models known to be unusable". That is deliberate on the sidecar's side:
the compose healthcheck gates on that endpoint, so a container that has not provisioned
yet must not be marked unhealthy and fail ``compose up --wait`` for the whole stack.

The consequence, which this module exists to pin down, is that "answers /healthz" does not
imply "can diarize". Until ``sidecar_ready`` existed, engine selection asked ``/healthz``,
so a sidecar with a broken models directory read as healthy, the native engine was selected
anyway, and the failure surfaced at request time instead of at the point where the
in-process PyAnnote fallback was still available to choose.

Measured against the real diar-native 0.3.1 image with an unmarked models directory, which
is the exact state these servers imitate::

    GET /healthz  -> 200  {"status":"ok","models_verified":false,"models_state":"unverified"}
    GET /readyz   -> 503

Every server below is a REAL ``http.server`` on a real port, following
``test_diarizer_engine_selection.py``: the failure mode being guarded is a wrong status code
being interpreted, so a patched exception would assert the mock rather than the behaviour.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import socket
import threading
from collections.abc import Iterator

import pytest

from app.services.native_embedding_client import native_embedding_available
from app.transcription.diarizer_native import NativeSpeakerDiarizer
from app.transcription.diarizer_native import reset_readiness_cache
from app.transcription.diarizer_native import sidecar_healthy
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# The three sidecar states that matter. `readyz` is the status code /readyz returns;
# None means the route does not exist at all, which is what a pre-0.3.0 sidecar does.
_UNPROVISIONED = {
    "status": "ok",
    "models_verified": False,
    "models_state": "unverified",
    "models_reason": "No provisioning marker (diar-provision.json) in /models.",
}
_READY = {"status": "ok", "models_verified": True, "models_state": "verified"}


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """Drop the TTL-cached probe verdicts around every test in this module.

    ``sidecar_ready``/``sidecar_healthy`` memoise per (endpoint, url) for several seconds.
    Every test here stands up its own server on an ephemeral port, so today the keys happen
    to differ — but that is the OS's port allocation protecting the suite, not the suite
    protecting itself: pin ``_free_port`` to one value and
    ``test_embedding_path_agrees_with_the_diarization_path`` fails on a stale verdict.
    Clearing explicitly makes the isolation the module's own property.
    """
    reset_readiness_cache()
    yield
    reset_readiness_cache()


def _make_handler(readyz: int | None, health_body: dict):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
            if self.path == "/healthz":
                payload = json.dumps(health_body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/readyz" and readyz is not None:
                self.send_response(readyz)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return _Handler


@contextlib.contextmanager
def _sidecar(readyz: int | None, health_body: dict) -> Iterator[str]:
    """A real HTTP sidecar stand-in; yields its base URL."""
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _make_handler(readyz, health_body))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()


class TestReadinessIsNotLiveness:
    def test_unprovisioned_sidecar_is_healthy_but_not_ready(self):
        """The whole point: 200 on /healthz, 503 on /readyz, and they must disagree.

        This is the case that was previously invisible. If sidecar_ready ever collapses
        back into sidecar_healthy, this is the assertion that fails.
        """
        with _sidecar(readyz=503, health_body=_UNPROVISIONED) as url:
            assert sidecar_healthy(url) is True, "liveness must still be true, by design"
            assert sidecar_ready(url) is False, "readiness must reflect the 503"

    def test_provisioned_sidecar_is_both(self):
        with _sidecar(readyz=200, health_body=_READY) as url:
            assert sidecar_healthy(url) is True
            assert sidecar_ready(url) is True

    def test_unreachable_sidecar_is_neither(self):
        """Real unreachability: the server is shut down before the probe."""
        with _sidecar(readyz=200, health_body=_READY) as url:
            pass  # context manager has now stopped the server
        assert sidecar_healthy(url) is False
        assert sidecar_ready(url) is False

    def test_pre_0_3_0_sidecar_without_readyz_falls_back_to_liveness(self):
        """A 404 on /readyz means "cannot say", not "not ready".

        /readyz landed in diar-native 0.3.0. Treating its absence as a negative would
        disable the native engine outright for anyone still pinned to an older image,
        turning a compatibility gap into an outage.
        """
        with _sidecar(readyz=None, health_body=_UNPROVISIONED) as url:
            assert sidecar_ready(url) is True

    def test_not_ready_is_logged_with_the_sidecar_s_own_reason(self, caplog):
        """A silent fallback is the failure mode; the reason must reach the operator."""
        with _sidecar(readyz=503, health_body=_UNPROVISIONED) as url, caplog.at_level("WARNING"):
            assert sidecar_ready(url) is False
        assert "not ready" in caplog.text
        assert "unverified" in caplog.text
        assert "No provisioning marker" in caplog.text


class TestRoutingUsesReadiness:
    def test_load_model_refuses_an_unprovisioned_sidecar(self):
        """Refusing here routes the caller to PyAnnote while that is still a choice.

        Before this change load_model checked /healthz, so it succeeded against a sidecar
        that could not diarize and the job failed later, mid-transcription.
        """
        with _sidecar(readyz=503, health_body=_UNPROVISIONED) as url:
            diarizer = NativeSpeakerDiarizer(config=None, base_url=url)
            with pytest.raises(RuntimeError, match="not ready"):
                diarizer.load_model()
            assert diarizer.is_loaded is False

    def test_load_model_accepts_a_ready_sidecar(self):
        with _sidecar(readyz=200, health_body=_READY) as url:
            diarizer = NativeSpeakerDiarizer(config=None, base_url=url)
            diarizer.load_model()
            assert diarizer.is_loaded is True

    def test_load_model_error_carries_the_reason(self):
        """ "Not ready" alone sends the operator to the logs; the reason should be inline."""
        with _sidecar(readyz=503, health_body=_UNPROVISIONED) as url:
            diarizer = NativeSpeakerDiarizer(config=None, base_url=url)
            with pytest.raises(RuntimeError, match="No provisioning marker"):
                diarizer.load_model()

    def test_embedding_path_agrees_with_the_diarization_path(self):
        """/embed_window runs the same weights, so it cannot serve when they are unusable.

        These two predicates are shared deliberately. If they diverge, speaker embeddings
        get routed to a sidecar the diarizer has already rejected.
        """
        with _sidecar(readyz=503, health_body=_UNPROVISIONED) as url:
            assert native_embedding_available(url) is False
        with _sidecar(readyz=200, health_body=_READY) as url:
            assert native_embedding_available(url) is True


# ---------------------------------------------------------------------------
# Per-request device routing (issue #679): "device" must be sent ONLY when the
# sidecar's own /healthz advertises it — never blind, since the sidecar's request
# structs have no deny_unknown_fields and an old sidecar would silently ignore an
# unrecognised "device" key and answer 200 having run on CUDA anyway.
# ---------------------------------------------------------------------------


def _make_embed_handler(health_body: dict, embed_requests: list[dict]):
    """A real sidecar stand-in serving /healthz and /embed_window.

    Records every /embed_window request body into *embed_requests* so a test can assert
    on exactly what was sent, rather than trusting a mock to agree with itself.
    """

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
            if self.path == "/healthz":
                payload = json.dumps(health_body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            elif self.path == "/readyz":
                self.send_response(200)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler method name
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/embed_window":
                embed_requests.append(body)
                payload = json.dumps({"embedding": [0.1] * 256}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return _Handler


@contextlib.contextmanager
def _embed_sidecar(health_body: dict, embed_requests: list[dict]) -> Iterator[str]:
    port = _free_port()
    httpd = http.server.HTTPServer(
        ("127.0.0.1", port), _make_embed_handler(health_body, embed_requests)
    )
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()


class TestDeviceRoutingGate:
    """#679 per-request device routing, keyed on LOADED devices — not capability.

    ⚠️ These assertions were INVERTED once. The first version keyed on
    ``supported_devices`` and asserted True for ``["cuda", "cpu"]``. That field is a
    build-time capability advertisement; the set actually loaded is chosen at start-up by
    ``DIAR_DEVICES`` and reported separately as ``devices``. Measured against the live
    sidecar, an ordinary GPU deployment answers::

        "devices":           ["cuda"]
        "supported_devices": ["cuda", "cpu"]

    so the old predicate returned True everywhere, the app sent ``device: "cpu"``, and
    ``/embed_window`` replied ``400 device 'cpu' is not loaded; this server is serving
    [cuda]`` — which ``embed_waveform`` turns into ``None``, i.e. EVERY speaker embedding
    silently fell back to in-process PyAnnote on a sidecar reporting healthy. The tests
    below pin the corrected reading; the capability field must never gate a request.
    """

    def test_probe_true_when_cpu_is_actually_loaded(self):
        from app.transcription.diarizer_native import sidecar_supports_cpu_device

        with _sidecar(readyz=200, health_body={**_READY, "devices": ["cpu"]}) as url:
            assert sidecar_supports_cpu_device(url) is True

    def test_probe_false_when_cpu_is_only_a_capability_not_loaded(self):
        """The regression: capability says cpu, the running server serves only cuda."""
        from app.transcription.diarizer_native import sidecar_supports_cpu_device

        body = {**_READY, "devices": ["cuda"], "supported_devices": ["cuda", "cpu"]}
        with _sidecar(readyz=200, health_body=body) as url:
            assert sidecar_supports_cpu_device(url) is False, (
                "keyed on supported_devices instead of devices — this is the shape that "
                "made every /embed_window call 400 on a GPU deployment"
            )

    def test_probe_false_when_the_field_is_absent(self):
        """The pre-#679 shape: /healthz answers, but says nothing about devices."""
        from app.transcription.diarizer_native import sidecar_supports_cpu_device

        with _sidecar(readyz=200, health_body=_READY) as url:
            assert sidecar_supports_cpu_device(url) is False

    def test_probe_true_when_both_devices_are_loaded(self):
        from app.transcription.diarizer_native import sidecar_supports_cpu_device

        with _sidecar(readyz=200, health_body={**_READY, "devices": ["cuda", "cpu"]}) as url:
            assert sidecar_supports_cpu_device(url) is True

    def test_embed_window_sends_device_cpu_when_cpu_is_loaded(self):
        """The positive case: a sidecar actually SERVING cpu is asked to use it."""
        from app.services.native_embedding_client import embed_waveform

        requests: list[dict] = []
        with _embed_sidecar({**_READY, "devices": ["cuda", "cpu"]}, requests) as url:
            audio = _ramp_local(160_000)
            out = embed_waveform(audio, base_url=url)
        assert out is not None
        assert len(requests) == 1
        assert requests[0].get("device") == "cpu"

    def test_embed_window_omits_device_when_cpu_is_only_a_capability(self):
        """The regression case end to end: capability advertised, cpu NOT loaded.

        Sending ``device: "cpu"`` here is what produced the 400 and the silent PyAnnote
        fallback. The request must go out with no ``device`` key so the sidecar serves it
        on its loaded default.
        """
        from app.services.native_embedding_client import embed_waveform

        requests: list[dict] = []
        body = {**_READY, "devices": ["cuda"], "supported_devices": ["cuda", "cpu"]}
        with _embed_sidecar(body, requests) as url:
            audio = _ramp_local(160_000)
            out = embed_waveform(audio, base_url=url)
        assert out is not None
        assert len(requests) == 1
        assert "device" not in requests[0]

    def test_embed_window_never_sends_device_to_a_sidecar_that_does_not_advertise_it(self):
        """The gate itself: an OLD sidecar must never receive an unrecognised key.

        diar-server's request structs have no deny_unknown_fields, so a sidecar that
        cannot honour "device" would silently ignore it and run on CUDA anyway,
        answering 200 — indistinguishable from success. The only safe behaviour is to
        never send the key at all when it was not advertised.
        """
        from app.services.native_embedding_client import embed_waveform

        requests: list[dict] = []
        with _embed_sidecar(_READY, requests) as url:  # no devices field at all
            audio = _ramp_local(160_000)
            out = embed_waveform(audio, base_url=url)
        assert out is not None
        assert len(requests) == 1
        assert "device" not in requests[0]


def _ramp_local(n: int):
    import numpy as np

    return np.linspace(-1.0, 1.0, n, dtype=np.float32)
