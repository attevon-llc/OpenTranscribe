"""The SSRF guard must cover ``chat_completion`` — the path that actually runs.

``validate_connection`` and ``health_check`` validated (and, since the pinning work,
**pinned**) the operator-supplied ``base_url``. ``chat_completion`` and
``chat_completion_stream`` did neither. So the endpoint was checked when an admin pressed
"Test connection" and unchecked on every real summarization, topic extraction, speaker
identification, redaction and chat request afterwards — a guard on the button, not on the
door (issue #444).

Three separate failures are proved here, each with its own control:

1. **Rebinding.** A hostname that answers a public address once and ``127.0.0.1`` after
   passes the check and is dialled at the second answer. Two real servers on 127.0.0.1 and
   127.0.0.2 plus a resolver stub that answers differently on each call make "which server
   replied" the assertion — a test that only inspected the URL would pass against code
   that computed a pinned URL and then threw it away.
2. **Redirects.** A PUBLIC endpoint that passes validation and answers
   ``302 Location: http://169.254.169.254/`` reaches cloud instance metadata with no DNS
   control at all. ``test_a_followed_redirect_escapes_the_pin_entirely`` is the control:
   same request, flag flipped, and it lands on the *other* server.
3. **Streaming.** Pinning must not buy security by breaking chat.
   ``test_stream_is_incremental_through_the_pin`` asserts the first token arrives while the
   server is still holding the connection open, so a fix that buffered the whole body (and
   would blow the caller's first-token watchdog) fails here.

⚠️ **The harness trap.** anyio/httpx ASCII-encode the host before calling ``getaddrinfo``,
so a stub comparing against a ``str`` silently delegates to the real resolver and the
control then fails with NXDOMAIN, proving nothing. ``_fake_getaddrinfo`` decodes bytes —
the same fix ``test_ssrf_connection_pinning.py`` documents.
"""

from __future__ import annotations

import http.server
import json
import socket
import threading
from collections.abc import Iterator
from typing import Any
from typing import cast

import pytest

FIRST_ANSWER = "127.0.0.1"
SECOND_ANSWER = "127.0.0.2"
REBIND_HOST = "rebind.test"

#: Ceiling on how long the streaming handler will wait to be released. Only a deadlock
#: reaches it; the test releases the server as soon as it has seen the first token.
STREAM_RELEASE_TIMEOUT_S = 10.0


class _LLMHandler(http.server.BaseHTTPRequestHandler):
    """A minimal OpenAI-compatible chat endpoint that reports which server answered.

    **HTTP/1.1 with real chunked framing on the streaming branch**, because that is what
    every SSE provider does and it is the only shape that streams. Under HTTP/1.0
    read-until-close, `http.client` satisfies `read(amt)` from a *buffered* reader and
    blocks for the full 512-byte `iter_lines` chunk or EOF — so the first token does not
    surface until the server hangs up, and the incremental test measures the harness
    rather than the code. Measured: 0.40s to first token unchunked, ~0.00s chunked.

    The streaming branch then **blocks on `server.release`** rather than sleeping, so
    "the client saw a token before the rest of the body existed" is a causal fact the
    test establishes, not a timing threshold it hopes holds on a loaded machine.
    """

    protocol_version = "HTTP/1.1"
    server_version = "SsrfChatTest/1.0"

    # -- helpers ---------------------------------------------------------------
    @property
    def _which(self) -> str:
        return cast("str", cast("Any", self.server).which)

    def _record(self, method: str) -> None:
        cast("Any", self.server).seen.append((method, self.path, self.headers.get("Host", "")))

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- handlers --------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        """Only reached by a client that FOLLOWED a redirect (302 turns POST into GET)."""
        self._record("GET")
        self._json({"served_by": self._which, "followed": True})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        self._record("POST")
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")

        if "/redirect-to-metadata/" in self.path:
            self._redirect("http://169.254.169.254/latest/meta-data/")
            return
        if "/redirect-onward/" in self.path:
            port = cast("tuple[str, int]", self.server.server_address)[1]
            self._redirect(f"http://{REBIND_HOST}:{port}/followed")
            return

        if payload.get("stream"):
            self._stream()
            return

        self._json(
            {
                "choices": [
                    {"message": {"content": self._which}, "finish_reason": "stop"},
                ],
                "usage": {"total_tokens": 7},
            }
        )

    def _chunk(self, data: bytes) -> None:
        """Write one HTTP chunk. `wfile` is unbuffered, so this hits the socket now."""
        self.wfile.write(b"%X\r\n%s\r\n" % (len(data), data))

    def _stream(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        # First token goes out immediately, then the connection is held open. A client
        # that buffers the whole response cannot observe this gap.
        self._chunk(f'data: {{"choices":[{{"delta":{{"content":"{self._which}"}}}}]}}\n\n'.encode())
        cast("Any", self.server).release.wait(STREAM_RELEASE_TIMEOUT_S)
        self._chunk(b'data: {"choices":[{"delta":{"content":"-tail"}}]}\n\n')
        self._chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")

    def log_message(self, *args: Any) -> None:
        return None


class _QuietServer(http.server.ThreadingHTTPServer):
    """Swallows the broken-pipe traceback the cancellation test provokes by design."""

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


def _serve(bind_address: str, port: int = 0):
    server = _QuietServer((bind_address, port), _LLMHandler)
    server.daemon_threads = True
    cast("Any", server).which = bind_address
    cast("Any", server).seen = []
    # Set by default so every test EXCEPT the incremental one streams straight through.
    cast("Any", server).release = threading.Event()
    cast("Any", server).release.set()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


@pytest.fixture
def rebinding_llm(monkeypatch) -> Iterator[tuple[int, list[str], Any, Any]]:
    """Two chat endpoints on two loopback addresses, plus a rebinding resolver.

    Yields ``(port, calls, first_server, second_server)``. ``calls`` records every lookup
    of ``rebind.test``, so a test can prove there was exactly ONE resolution; the servers
    expose ``.seen`` so a test can prove which one was actually reached.
    """
    first, port = _serve(FIRST_ANSWER)
    second, _ = _serve(SECOND_ANSWER, port)
    calls: list[str] = []
    real_getaddrinfo = socket.getaddrinfo

    def _fake_getaddrinfo(host, prt, *args, **kwargs):
        # bytes, not str: anyio ASCII-encodes the host. Comparing against the str alone
        # delegates to the real resolver and the control fails with NXDOMAIN instead of
        # proving anything.
        name = host.decode("ascii") if isinstance(host, bytes) else host
        if name != REBIND_HOST:
            return real_getaddrinfo(host, prt, *args, **kwargs)
        calls.append(name)
        answer = FIRST_ANSWER if len(calls) == 1 else SECOND_ANSWER
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answer, prt))]

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)
    try:
        yield port, calls, first, second
    finally:
        first.shutdown()
        second.shutdown()


def _service(base_url: str, *, allow_private: bool, monkeypatch):
    """An `LLMService` pointed at *base_url*, with the private-endpoint flag set."""
    from app.core.config import settings
    from app.services import llm_service as llm_module

    monkeypatch.setattr(settings, "LLM_ALLOW_PRIVATE_ENDPOINTS", allow_private, raising=False)
    return llm_module.LLMService(
        llm_module.LLMConfig(
            provider=llm_module.LLMProvider.CUSTOM,
            model="test-model",
            api_key="sk-test",
            base_url=base_url,
        )
    )


def _deltas(events) -> str:
    return "".join(e.text for e in events if e.type == "delta")


# ── 1. The connection lands on the address that was validated ───────────────────────


class TestChatCompletionIsPinned:
    """`chat_completion` must reach the FIRST DNS answer — the one it judged."""

    def test_chat_completion_reaches_the_validated_address(self, rebinding_llm, monkeypatch):
        port, calls, first, second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=True, monkeypatch=monkeypatch
        )
        try:
            response = service.chat_completion([{"role": "user", "content": "hi"}])
        finally:
            service.close()

        # Both servers answer 200, so a re-resolving implementation would also "work".
        # WHICH server replied is the whole assertion.
        assert response.content == FIRST_ANSWER, (
            "the request reached the address the SECOND DNS answer named — it re-resolved"
        )
        assert calls == [REBIND_HOST], "there must be exactly ONE resolution"
        assert [m for m, _p, _h in second.seen] == [], "the rebound server was never reached"
        assert [m for m, _p, _h in first.seen] == ["POST"]
        # Virtual hosting still works: the origin sees the name it was asked for.
        assert first.seen[0][2] == f"{REBIND_HOST}:{port}"

    def test_stream_reaches_the_validated_address(self, rebinding_llm, monkeypatch):
        port, calls, first, second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=True, monkeypatch=monkeypatch
        )
        try:
            events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))
        finally:
            service.close()

        assert _deltas(events) == f"{FIRST_ANSWER}-tail"
        assert events[-1].type == "done"
        assert calls == [REBIND_HOST]
        assert second.seen == []

    def test_the_session_is_built_once_not_per_request(self, rebinding_llm, monkeypatch):
        """Two calls, one resolution: fewer lookups is strictly fewer rebinding windows.

        It also keeps connection pooling — the LLM redaction detector issues one
        `chat_completion` per transcript segment against a single service instance.
        """
        port, calls, _first, _second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=True, monkeypatch=monkeypatch
        )
        try:
            service.chat_completion([{"role": "user", "content": "one"}])
            service.chat_completion([{"role": "user", "content": "two"}])
            list(service.chat_completion_stream([{"role": "user", "content": "three"}]))
        finally:
            service.close()

        assert calls == [REBIND_HOST]

    def test_an_unpinned_client_reaches_the_second_answer(self, rebinding_llm, monkeypatch):
        """The control that makes the three tests above mean something.

        This is the OLD `chat_completion`, reproduced deliberately: resolve once to judge,
        then hand the HOSTNAME to `requests`. If the loopback pair could not tell the two
        answers apart, every assertion above would be vacuous.
        """
        import requests

        from app.utils.url_validation import resolve_pinned_target

        port, calls, _first, second = rebinding_llm
        url = f"http://{REBIND_HOST}:{port}/v1/chat/completions"

        target, reason = resolve_pinned_target(url, allow_private=True)
        assert target is not None and reason == ""  # validation passed, on answer #1

        with requests.Session() as session:  # the defect: dial the NAME, not the address
            body = session.post(url, json={"model": "m", "messages": []}, timeout=5).json()

        assert body["choices"][0]["message"]["content"] == SECOND_ANSWER
        assert len(calls) > 1, "an unpinned client resolves a second time, by definition"
        assert [m for m, _p, _h in second.seen] == ["POST"]


# ── 2. Redirects: one hop only ──────────────────────────────────────────────────────


class TestRedirectsAreNotFollowed:
    """A pin covers ONE hop. Without this, no DNS control is needed at all."""

    def test_chat_completion_does_not_follow_a_redirect_to_metadata(
        self, rebinding_llm, monkeypatch
    ):
        port, _calls, first, _second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/redirect-to-metadata/v1",
            allow_private=True,
            monkeypatch=monkeypatch,
        )
        try:
            # `match=`, not a bare `Exception`: a bare one is satisfied by a typo in the
            # setup line above just as well as by the refusal this test claims to prove.
            with pytest.raises(Exception, match=r"LLM API error: 302"):
                service.chat_completion([{"role": "user", "content": "hi"}])
        finally:
            service.close()

        # The 302 came back to us as an error, NOT chased to 169.254.169.254.
        assert [m for m, _p, _h in first.seen] == ["POST"], "no second hop was attempted"

    def test_stream_does_not_follow_a_redirect_to_metadata(self, rebinding_llm, monkeypatch):
        port, _calls, first, _second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/redirect-to-metadata/v1",
            allow_private=True,
            monkeypatch=monkeypatch,
        )
        try:
            events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))
        finally:
            service.close()

        assert len(events) == 1
        assert events[0].type == "error"
        assert "302" in events[0].message
        assert [m for m, _p, _h in first.seen] == ["POST"]

    def test_a_followed_redirect_escapes_the_pin_entirely(self, rebinding_llm, monkeypatch):
        """The control: prove `allow_redirects=False` is what stops it, not luck.

        Same pinned request, flag flipped. The client re-resolves the Location — which is
        the point — and lands on the OTHER server. Without this test,
        `allow_redirects=False` could be deleted and the two above would still pass against
        any endpoint that did not actually redirect.
        """
        from app.utils.url_validation import pinned_requests_session
        from app.utils.url_validation import resolve_pinned_target

        port, _calls, first, second = rebinding_llm
        url = f"http://{REBIND_HOST}:{port}/redirect-onward/v1/chat/completions"
        target, _ = resolve_pinned_target(url, allow_private=True)
        assert target is not None

        with pinned_requests_session(target) as session:
            response = session.post(
                target.url,
                json={"model": "m", "messages": []},
                headers=target.headers,
                timeout=5,
                allow_redirects=True,  # what the production code must NOT do
            )

        assert response.status_code == 200
        assert response.json()["served_by"] == SECOND_ANSWER
        assert [p for _m, p, _h in first.seen] == ["/redirect-onward/v1/chat/completions"]
        assert [p for _m, p, _h in second.seen] == ["/followed"], (
            "a followed redirect leaves the pinned address — that is why it is refused"
        )


# ── 3. Streaming is not broken by the pin ───────────────────────────────────────────


class TestStreamingStillStreams:
    def test_stream_is_incremental_through_the_pin(self, rebinding_llm, monkeypatch):
        """The first token must arrive while the rest of the body does not yet exist.

        This is the falsifiable form of "pinning did not break streaming", and it is
        **causal, not timed**: the server blocks after its first chunk until this test
        releases it, so an implementation that read the whole response before yielding
        deadlocks rather than passing slowly. Such an implementation would satisfy the
        content assertions in `test_stream_reaches_the_validated_address` exactly as well,
        and would then blow the chat service's first-token watchdog on a slow model.
        """
        port, _calls, first, _second = rebinding_llm
        first.release.clear()  # hold the connection open after the first chunk
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=True, monkeypatch=monkeypatch
        )
        texts: list[str] = []
        held_when_first_token_arrived = None
        try:
            for event in service.chat_completion_stream([{"role": "user", "content": "hi"}]):
                if event.type != "delta":
                    continue
                if not texts:
                    held_when_first_token_arrived = not first.release.is_set()
                    first.release.set()  # let the server finish
                texts.append(event.text)
        finally:
            first.release.set()
            service.close()

        assert texts == [FIRST_ANSWER, "-tail"]
        assert held_when_first_token_arrived is True, (
            "the first token surfaced only after the whole body was available — the "
            "response was buffered, not streamed"
        )

    def test_cancellation_still_closes_the_stream_early(self, rebinding_llm, monkeypatch):
        """The Stop button: the pinned session must not defeat mid-stream cancellation."""
        port, _calls, _first, _second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=True, monkeypatch=monkeypatch
        )
        cancel = threading.Event()
        events = []
        try:
            for event in service.chat_completion_stream(
                [{"role": "user", "content": "hi"}], cancel_event=cancel
            ):
                events.append(event)
                if event.type == "delta":
                    cancel.set()  # user hits Stop after the first token
        finally:
            service.close()

        assert _deltas(events) == FIRST_ANSWER, "output was not truncated at the Stop"
        assert events[-1].type == "done"
        assert events[-1].finish_reason == "cancelled"


# ── 4. The refusal itself, and the LAN configuration that must keep working ─────────


class TestPrivateEndpointPolicy:
    """`LLM_ALLOW_PRIVATE_ENDPOINTS` is honoured exactly as on the other call sites."""

    def test_loopback_endpoint_is_refused_when_the_flag_is_off(self, rebinding_llm, monkeypatch):
        port, _calls, first, second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=False, monkeypatch=monkeypatch
        )
        try:
            with pytest.raises(Exception, match="not a permitted outbound target") as excinfo:
                service.chat_completion([{"role": "user", "content": "hi"}])
        finally:
            service.close()

        assert type(excinfo.value).__name__ == "LLMEndpointBlockedError"
        # The reason ("Loopback address: ...") is logged, never returned: it would turn
        # the error message into a network scanner.
        assert "127.0.0" not in str(excinfo.value)
        assert first.seen == [] and second.seen == [], "nothing was dialled"

    def test_stream_reports_the_refusal_in_band(self, rebinding_llm, monkeypatch):
        """A streaming response has already committed its status line, so it is a frame."""
        port, _calls, first, _second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=False, monkeypatch=monkeypatch
        )
        try:
            events = list(service.chat_completion_stream([{"role": "user", "content": "hi"}]))
        finally:
            service.close()

        assert len(events) == 1
        assert events[0].type == "error"
        assert "not a permitted outbound target" in events[0].message
        assert first.seen == []

    def test_the_same_endpoint_works_with_the_flag_on(self, rebinding_llm, monkeypatch):
        """The control for the two above, and the requirement in its own right.

        A self-hosted Ollama/vLLM on the LAN is a legitimate configuration. Without this,
        a "fix" that refused every endpoint unconditionally would pass the refusal tests.
        """
        port, _calls, first, _second = rebinding_llm
        service = _service(
            f"http://{REBIND_HOST}:{port}/v1", allow_private=True, monkeypatch=monkeypatch
        )
        try:
            response = service.chat_completion([{"role": "user", "content": "hi"}])
        finally:
            service.close()

        assert response.content == FIRST_ANSWER
        assert [m for m, _p, _h in first.seen] == ["POST"]

    @pytest.mark.parametrize(
        "host",
        [
            "169.254.169.254",  # AWS/Azure/GCP IMDS
            "100.100.100.200",  # Alibaba IMDS, in RFC 6598 shared address space
        ],
    )
    def test_metadata_is_refused_even_with_the_flag_on(self, host, monkeypatch):
        """`allow_private=True` loosens the address RANGE, never the metadata carve-out.

        100.100.100.200 is the load-bearing case: RFC 6598 is neither `is_private` nor
        `is_global`, so before the carve-out listed it explicitly it passed in BOTH modes.
        """
        service = _service(f"http://{host}/v1", allow_private=True, monkeypatch=monkeypatch)
        try:
            with pytest.raises(Exception, match="not a permitted outbound target") as excinfo:
                service.chat_completion([{"role": "user", "content": "hi"}])
        finally:
            service.close()

        assert type(excinfo.value).__name__ == "LLMEndpointBlockedError"
