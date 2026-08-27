"""GH #595: an ``event: error`` frame must be well-formed on the wire.

``LLMService.chat_completion_stream`` already emits a ``StreamEvent(type="error", ...)``
when a call is blocked (e.g. by the SSRF guard refusing a private-network endpoint —
``tests/unit/test_ssrf_chat_completion.py::TestPrivateEndpointPolicy::
test_stream_reports_the_refusal_in_band``) or otherwise fails, and
``chat/service.py::stream_reply`` converts every such event into an SSE frame via
``sse("error", {...})``. What was never pinned anywhere is the wire shape that
conversion produces — which matters because the release-rehearsal harness's SSE parser
(``scripts/release-tests/lib/api-client.sh::ac_chat_completion``) used to drop `error`
frames silently, so a *correctly refused* LLM call read identically to a genuinely empty
answer (issue #595). This test is the control for that fix: it pins the exact bytes an
`event:`/`data:` parser depends on.
"""

from __future__ import annotations

import json

from app.services.chat.service import sse


class TestSseErrorFrameFormat:
    def test_error_frame_carries_a_readable_message(self):
        frame = sse("error", {"code": "provider_error", "message": "Generation failed."})

        lines = frame.split("\n")
        assert lines[0] == "event: error"
        assert lines[1].startswith("data: ")
        payload = json.loads(lines[1][len("data: ") :])
        assert payload == {"code": "provider_error", "message": "Generation failed."}
        # A blank line terminates the frame, per the SSE spec every parser (ours and the
        # release harness's) relies on to know where one frame ends.
        assert frame.endswith("\n\n")

    def test_ssrf_refusal_message_is_carried_through_unmodified(self):
        """The exact message ``LLMEndpointBlockedError`` raises — proved reachable in
        ``test_ssrf_chat_completion.py`` — must survive the SSE conversion, since that
        text is what tells an operator "this was refused", not "this failed silently"."""
        frame = sse(
            "error",
            {
                "code": "provider_error",
                "message": "http://mock-llm:5199/v1 is not a permitted outbound target",
            },
        )
        payload = json.loads(frame.split("\n")[1][len("data: ") :])
        assert "not a permitted outbound target" in payload["message"]
