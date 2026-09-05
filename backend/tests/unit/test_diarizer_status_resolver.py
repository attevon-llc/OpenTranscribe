"""``describe_diarizer_status`` — the single admin/stats resolver for the diarization
engine (issue #672's second half, adversarial-audit follow-up).

Before this fix there were TWO divergent resolvers doing the same job: `admin.py`
(`/admin/stats`) called the UNVALIDATED `engine_settings._resolve_setting` with its own
hand-written description table, and `stats_helpers.py` (`/system/stats`) called the
validated, fail-safe `TranscriptionConfig._resolve_diarizer_backend` with a SECOND
hand-written description table. With `ENGINE_DIARIZER_BACKEND=typo`, `/admin/stats`
reported `"typo diarization engine"` while `/system/stats` reported diar-native's
description — two panels disagreeing about the identical misconfiguration.

And even the validated resolver only ever answered "what is CONFIGURED".
`ModelManager._build_diarizer` falls back from native to the in-process PyAnnote fork
silently whenever the sidecar cannot serve, so a deployment configured for native could
report "native" on both panels while every job actually ran on PyAnnote. That silent
divergence — not merely the duplication — is what `describe_diarizer_status` exists to
surface via its ``configured``/``effective``/``using_fallback`` triple.

These tests drive the resolver against REAL TCP state (a real HTTP server, a real closed
port) rather than a patched exception — same rationale as `test_diarizer_engine_selection.py`
and `test_diar_native_readiness_gate.py`: the failure mode being guarded is a wrong status
code being interpreted, so a mock that agrees with itself would prove nothing.
"""

from __future__ import annotations

import contextlib
import http.server
import logging
import socket
import threading
from collections.abc import Iterator

import pytest

from app.transcription.diarizer_native import _ENGINE_DESCRIPTIONS
from app.transcription.diarizer_native import describe_diarizer_status
from app.transcription.diarizer_native import reset_readiness_cache

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
    """A real TCP port on localhost, bound then released — not merely an unused number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _make_handler(readyz: int):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
            if self.path == "/healthz":
                self.send_response(200)
                self.end_headers()
            elif self.path == "/readyz":
                self.send_response(readyz)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # silence request logging in test output

    return _Handler


@contextlib.contextmanager
def _sidecar(readyz: int) -> Iterator[str]:
    """A real HTTP sidecar stand-in; yields its base URL."""
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _make_handler(readyz))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        with contextlib.suppress(Exception):
            httpd.shutdown()
            httpd.server_close()


def _db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force TranscriptionConfig._resolve_diarizer_backend's DB read to fail.

    Deterministic stand-in for "no admin has configured this" — the same helper
    `test_diarizer_engine_selection.py` uses, so this suite's env-var-only cases are not
    at the mercy of whatever `engine.diarizer_backend` row happens to exist on the real
    dev database it runs against.
    """

    def _boom():
        raise RuntimeError("db unavailable in test")

    monkeypatch.setattr("app.db.session_utils.session_scope", _boom)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """The sidecar_ready() TTL cache is module-global; isolate every test in this file."""
    reset_readiness_cache()
    yield
    reset_readiness_cache()


class TestConfiguredAndReachable:
    """ "I chose native and it is working" — configured == effective, no divergence."""

    def test_native_configured_and_ready_reports_native_as_effective(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "native")
        _db_unavailable(monkeypatch)
        with _sidecar(readyz=200) as url:
            monkeypatch.setattr("app.transcription.diarizer_native._DEFAULT_URL", url)
            status = describe_diarizer_status()

        assert status["configured"] == "native"
        assert status["effective"] == "native"
        assert status["using_fallback"] is False
        assert status["configured_description"] == _ENGINE_DESCRIPTIONS["native"]
        assert status["effective_description"] == _ENGINE_DESCRIPTIONS["native"]

    def test_pyannote_configured_is_always_its_own_effective_engine_without_probing(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Pinned pyannote never falls back to anything (there is no engine below it), so
        the resolver must not even pay a sidecar probe to answer."""
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "pyannote")
        _db_unavailable(monkeypatch)
        probed: list[bool] = []

        def _fake_ready(base_url=None):
            probed.append(True)
            return True

        monkeypatch.setattr("app.transcription.diarizer_native.sidecar_ready", _fake_ready)

        status = describe_diarizer_status()

        assert status["configured"] == "pyannote"
        assert status["effective"] == "pyannote"
        assert status["using_fallback"] is False
        assert probed == [], "a pinned-pyannote deployment must not pay a sidecar probe"


class TestConfiguredNativeButUnreachable:
    """The case that matters: #672's silent-fallback divergence, made visible."""

    def test_unreachable_sidecar_reports_pyannote_as_the_effective_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Real unreachability (a bound-then-released port -> ECONNREFUSED), not a
        patched exception."""
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "native")
        _db_unavailable(monkeypatch)
        monkeypatch.setattr(
            "app.transcription.diarizer_native._DEFAULT_URL",
            f"http://127.0.0.1:{_free_port()}",
        )

        status = describe_diarizer_status()

        assert status["configured"] == "native"
        assert status["effective"] == "pyannote"
        assert status["using_fallback"] is True
        assert status["configured_description"] == _ENGINE_DESCRIPTIONS["native"]
        assert status["effective_description"] == _ENGINE_DESCRIPTIONS["pyannote"]

    def test_unprovisioned_sidecar_reachable_but_not_ready_also_reports_the_fallback(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Answering /healthz is not the same as being able to serve. A sidecar with an
        unverified/broken models directory answers 503 on /readyz while still fully
        reachable — the exact state that made the old configured-only answer misleading."""
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "native")
        _db_unavailable(monkeypatch)
        with _sidecar(readyz=503) as url:
            monkeypatch.setattr("app.transcription.diarizer_native._DEFAULT_URL", url)
            status = describe_diarizer_status()

        assert status["configured"] == "native"
        assert status["effective"] == "pyannote"
        assert status["using_fallback"] is True


class TestUnknownConfiguredValue:
    def test_invalid_env_value_fails_safe_to_native_with_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ):
        """The divergence this resolver replaces: the OLD admin.py path reported the raw
        'typo-backend' string verbatim (no validation) while the OLD stats_helpers.py path
        reported 'native' (validated, fail-safe) for the identical misconfiguration. There
        is now only one resolver, so 'configured' can never again be the unvalidated raw
        value — it fails safe exactly like TranscriptionConfig._resolve_diarizer_backend
        does, because that is what it calls.
        """
        monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "typo-backend")
        _db_unavailable(monkeypatch)
        monkeypatch.setattr(
            "app.transcription.diarizer_native._DEFAULT_URL",
            f"http://127.0.0.1:{_free_port()}",
        )
        caplog.set_level(logging.WARNING)

        status = describe_diarizer_status()

        assert status["configured"] == "native"
        assert status["configured"] != "typo-backend"
        assert any("Unknown diarizer_backend" in r.message for r in caplog.records)


class TestDescriptionTableIsSingleSourced:
    def test_every_valid_backend_has_exactly_one_description(self):
        from app.transcription.engine.backends import VALID_DIARIZER_BACKENDS

        assert set(_ENGINE_DESCRIPTIONS) == set(VALID_DIARIZER_BACKENDS)
        # Every description is unique — two engines must never read as the same thing.
        assert len(set(_ENGINE_DESCRIPTIONS.values())) == len(_ENGINE_DESCRIPTIONS)
