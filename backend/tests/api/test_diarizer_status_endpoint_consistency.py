"""Regression test for issue #672's second half.

``/admin/stats`` (``app/api/endpoints/admin.py``) and ``/system/stats``
(``app/api/endpoints/system.py`` via ``app/utils/stats_helpers.py``) used to run TWO
divergent diarizer-backend resolvers. Proven divergence before the fix: with
``ENGINE_DIARIZER_BACKEND=typo``, ``/admin/stats`` reported the raw, unvalidated string
("typo diarization engine") while ``/system/stats`` reported the validated, fail-safe
resolution ("native"'s description) for the identical misconfiguration.

Both endpoints now call the same ``describe_diarizer_status()``
(``app/transcription/diarizer_native.py``), so this pins the agreement down at the HTTP
response level — not just at the unit level, where an import mistake in one endpoint
could still reintroduce a second implementation without either module-level suite
noticing.
"""

from __future__ import annotations

import socket

import pytest
from fastapi import status


def _free_port() -> int:
    """A real TCP port on localhost, bound then released — guaranteed unreachable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _db_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force TranscriptionConfig._resolve_diarizer_backend's DB read to fail, so only the
    env var set by each test decides the configured backend — independent of whatever
    ``engine.diarizer_backend`` row happens to exist on the real dev database this suite
    runs against."""

    def _boom():
        raise RuntimeError("db unavailable in test")

    monkeypatch.setattr("app.db.session_utils.session_scope", _boom)


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    from app.transcription.diarizer_native import reset_readiness_cache

    reset_readiness_cache()
    yield
    reset_readiness_cache()


def test_admin_and_system_stats_agree_on_a_bad_config_value(
    client, admin_token_headers, user_token_headers, monkeypatch: pytest.MonkeyPatch
):
    """The proven-divergence scenario from issue #672's finding, reproduced and closed."""
    monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "typo")
    _db_unavailable(monkeypatch)
    # Sidecar unreachable, so "native"'s fail-safe default also exercises the
    # effective/fallback fields identically on both endpoints.
    monkeypatch.setattr(
        "app.transcription.diarizer_native._DEFAULT_URL",
        f"http://127.0.0.1:{_free_port()}",
    )

    admin_resp = client.get("/api/admin/stats", headers=admin_token_headers)
    system_resp = client.get("/api/system/stats", headers=user_token_headers)

    assert admin_resp.status_code == status.HTTP_200_OK
    assert system_resp.status_code == status.HTTP_200_OK

    admin_diar = admin_resp.json()["models"]["diarization"]
    system_diar = system_resp.json()["models"]["diarization"]

    # Neither panel may echo the raw, unvalidated "typo" value — both resolve through the
    # same validated, fail-safe path.
    assert admin_diar["configured_backend"] == "native"
    assert system_diar["configured_backend"] == "native"
    assert admin_diar["effective_backend"] == system_diar["effective_backend"] == "pyannote"
    assert admin_diar["using_fallback"] is True
    assert system_diar["using_fallback"] is True
    assert admin_diar["description"] == system_diar["description"]
    assert admin_diar["configured_description"] == system_diar["configured_description"]


def test_admin_and_system_stats_agree_when_native_is_configured_and_reachable(
    client, admin_token_headers, user_token_headers, monkeypatch: pytest.MonkeyPatch
):
    """Same resolver, opposite branch: configured == effective, no fallback in play."""
    import contextlib
    import http.server
    import threading
    from collections.abc import Iterator

    def _make_handler():
        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - stdlib handler method name
                self.send_response(200)
                self.end_headers()

            def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
                pass

        return _Handler

    @contextlib.contextmanager
    def _sidecar() -> Iterator[str]:
        port = _free_port()
        httpd = http.server.HTTPServer(("127.0.0.1", port), _make_handler())
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            with contextlib.suppress(Exception):
                httpd.shutdown()
                httpd.server_close()

    monkeypatch.setenv("ENGINE_DIARIZER_BACKEND", "native")
    _db_unavailable(monkeypatch)

    with _sidecar() as url:
        monkeypatch.setattr("app.transcription.diarizer_native._DEFAULT_URL", url)

        admin_resp = client.get("/api/admin/stats", headers=admin_token_headers)
        system_resp = client.get("/api/system/stats", headers=user_token_headers)

    assert admin_resp.status_code == status.HTTP_200_OK
    assert system_resp.status_code == status.HTTP_200_OK

    admin_diar = admin_resp.json()["models"]["diarization"]
    system_diar = system_resp.json()["models"]["diarization"]

    assert admin_diar["configured_backend"] == system_diar["configured_backend"] == "native"
    assert admin_diar["effective_backend"] == system_diar["effective_backend"] == "native"
    assert admin_diar["using_fallback"] is False
    assert system_diar["using_fallback"] is False
