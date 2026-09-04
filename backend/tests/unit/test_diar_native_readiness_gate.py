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
