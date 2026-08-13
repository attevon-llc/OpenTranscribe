#!/usr/bin/env python3
"""A fake OpenAI-compatible LLM, for exercising chat without a real model.

Everything except the model is real when you point OpenTranscribe at this:
retrieval runs against your actual transcripts, redaction masking applies,
citations are built from real chunk metadata, the SSE stream is a real stream,
and usage/persistence take their normal paths. Only the token generation is
canned — which is exactly the part that needs a GPU or an API key.

Why this and not a stub inside the app: a mock in production code would have to
be reachable from a code path that ships, and the repo's own rule is that mocks
live in test fixtures only. This is an external server the `custom` provider
talks to over HTTP, so nothing about the app changes.

Usage:
    python scripts/mock-llm-server.py [--port 5199]

Then in OpenTranscribe → Settings → AI:
    Provider: Custom (OpenAI-compatible)
    Base URL: http://172.19.0.1:5199/v1     # the docker bridge gateway
    Model:    mock-gpt
    API key:  anything non-empty

The reply deliberately cites [1] and [2] so citation rendering and the source
cards are exercised, and streams token-by-token with a small delay so the
typing animation, the stop button, and the first-token watchdog all behave as
they would against a real provider.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = 'mock-gpt'

# Scenario models. The requested `model` selects the behaviour, so a test picks
# a failure mode by configuring a provider rather than by patching code — which
# means the app exercises its REAL error handling, not a mocked branch.
#
#   mock-gpt        normal reply with [1]/[2] citations, markdown and a code block
#   mock-echo       echoes the exact prompt it received; lets a test assert what the
#                   app actually sent (masking applied? prompt layers in order?)
#   mock-empty      completes with no content — the "model returned nothing" path
#   mock-error      HTTP 500 before any token — the provider_error path
#   mock-slow       stalls past the first-token watchdog — the timeout path
#   mock-reasoning  streams a reasoning phase (delta.reasoning_content, the vLLM
#                   dialect this app's OpenAI-compatible parser reads) before the
#                   same [1]/[2] answer as mock-gpt — exercises the collapsible
#                   reasoning display end to end with no GPU or real model
SCENARIOS = ('mock-gpt', 'mock-echo', 'mock-empty', 'mock-error', 'mock-slow', 'mock-reasoning')

# Long enough to trip DEFAULT_CHAT_FIRST_TOKEN_TIMEOUT_S without hanging a suite.
SLOW_FIRST_TOKEN_DELAY_S = 45

# Streamed word-by-word, BEFORE the reply, on a separate wire field
# (delta.reasoning_content) so it renders in the frontend's collapsible
# "Thinking" block instead of the answer bubble. Deliberately mentions the
# excerpts too, so a glance at the expanded block looks like real deliberation.
REASONING_TEMPLATE = (
    'The user is asking about **{topic}**. Let me look at what was actually '
    'retrieved before answering.\n\n'
    'Excerpt [1] sets up the context, and excerpt [2] adds a clarifying detail '
    "that changes how I should phrase this. I don't see an explicit decision "
    'recorded anywhere in what came back, so I should say that plainly instead '
    'of guessing at one.'
)

# Streamed word-by-word. Mentions [1]/[2] so the citation pipeline is exercised;
# the backend maps those to the chunks it actually retrieved.
REPLY_TEMPLATE = (
    'Based on the transcript excerpts provided, here is what I found.\n\n'
    'The discussion covers several points relevant to your question about '
    '**{topic}**. The speakers return to it more than once [1], and a later '
    'passage adds detail that clarifies the earlier remark [2].\n\n'
    'Key points:\n\n'
    '- The first excerpt establishes the context [1]\n'
    '- A follow-up exchange expands on it [2]\n'
    '- No decision is recorded in the retrieved passages\n\n'
    '> This is mock output from `scripts/mock-llm-server.py` — no real model '
    'was consulted. Connect a provider in Settings → AI for real answers.\n\n'
    '```python\n'
    '# Code blocks render with syntax highlighting and a copy button.\n'
    "print('hello from the mock LLM')\n"
    '```\n'
)


def _topic_from(messages: list[dict]) -> str:
    """Echo the user's question back so the reply looks responsive."""
    for message in reversed(messages):
        if message.get('role') == 'user':
            content = str(message.get('content') or '')
            # The chat prompt packs excerpts and the question into one turn;
            # the question is the last non-empty line.
            lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
            if lines:
                question = lines[-1]
                question = re.sub(r'^(question|user)\s*:\s*', '', question, flags=re.I)
                return question[:80]
    return 'your question'


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # noqa: A002 - quieter console
        print(f'  {self.address_string()} {fmt % args}')

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # Model discovery — the app lists models for OpenAI-compatible providers.
        if self.path.rstrip('/').endswith('/models'):
            self._json(
                {
                    'object': 'list',
                    'data': [
                        {'id': name, 'object': 'model', 'owned_by': 'mock'} for name in SCENARIOS
                    ],
                }
            )
            return
        self._json({'status': 'ok', 'model': MODEL_ID})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}

        messages = body.get('messages') or []
        model = str(body.get('model') or MODEL_ID)

        if model == 'mock-error':
            # A provider that IS configured but fails. The app must surface this
            # as an error frame, unlike "no provider configured".
            self._json({'error': {'message': 'Mock provider failure', 'type': 'server_error'}}, 500)
            return

        if model == 'mock-slow':
            time.sleep(SLOW_FIRST_TOKEN_DELAY_S)

        if model == 'mock-echo':
            # Verbatim, so a test can assert on the prompt the app really built:
            # whether redaction masked the excerpts, and whether the system
            # prompt layers arrived in the documented order.
            text = '\n\n'.join(f'[{m.get("role")}]\n{m.get("content") or ""}' for m in messages)
        elif model == 'mock-empty':
            text = ''
        else:
            text = REPLY_TEMPLATE.format(topic=_topic_from(messages))

        reasoning = (
            REASONING_TEMPLATE.format(topic=_topic_from(messages))
            if model == 'mock-reasoning'
            else None
        )

        if not body.get('stream'):
            message: dict = {'role': 'assistant', 'content': text}
            if reasoning:
                message['reasoning_content'] = reasoning
            self._json(
                {
                    'id': 'mock-1',
                    'object': 'chat.completion',
                    'model': model,
                    'choices': [
                        {
                            'index': 0,
                            'message': message,
                            'finish_reason': 'stop',
                        }
                    ],
                    'usage': {
                        'prompt_tokens': 1234,
                        'completion_tokens': len(text) // 4,
                        'total_tokens': 1234 + len(text) // 4,
                    },
                }
            )
            return

        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        # Close the connection when the stream ends. On HTTP/1.1 this response
        # carries neither Content-Length nor chunked framing, so "end of body"
        # can only mean "socket closed" — with keep-alive the client blocks past
        # [DONE] until it hits a read timeout, which the app then surfaces as a
        # spurious provider_error. Real providers chunk-encode; we close instead.
        self.send_header('Connection', 'close')
        self.close_connection = True
        self.end_headers()

        def frame(payload: dict) -> None:
            self.wfile.write(f'data: {json.dumps(payload)}\n\n'.encode())
            self.wfile.flush()

        try:
            if reasoning:
                # Reasoning arrives BEFORE any answer content, on its own delta
                # field — real reasoning-capable servers finish "thinking" before
                # they start the answer, and the frontend's first-token watchdog
                # must treat this phase as the model already having responded.
                for token in re.findall(r'\S+\s*', reasoning):
                    frame(
                        {
                            'id': 'mock-1',
                            'object': 'chat.completion.chunk',
                            'model': model,
                            'choices': [{'index': 0, 'delta': {'reasoning_content': token}}],
                        }
                    )
                    time.sleep(0.02)

            for token in re.findall(r'\S+\s*', text):
                frame(
                    {
                        'id': 'mock-1',
                        'object': 'chat.completion.chunk',
                        'model': model,
                        'choices': [{'index': 0, 'delta': {'content': token}}],
                    }
                )
                time.sleep(0.02)  # visible streaming, not an instant dump

            frame(
                {
                    'id': 'mock-1',
                    'object': 'chat.completion.chunk',
                    'model': model,
                    'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}],
                }
            )
            # Usage arrives on a final usage-only chunk, matching what a real
            # provider sends when stream_options.include_usage was accepted —
            # this is what exercises the usage-tracking path end to end.
            frame(
                {
                    'id': 'mock-1',
                    'object': 'chat.completion.chunk',
                    'model': model,
                    'choices': [],
                    'usage': {
                        'prompt_tokens': 1234,
                        'completion_tokens': len(text) // 4,
                        'total_tokens': 1234 + len(text) // 4,
                    },
                }
            )
            self.wfile.write(b'data: [DONE]\n\n')
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # The user pressed Stop, or the browser navigated away. Expected.
            print('  client disconnected mid-stream (Stop pressed?)')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=5199)
    parser.add_argument('--host', default='0.0.0.0')  # noqa: S104 - dev-only tool
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f'Mock LLM listening on http://{args.host}:{args.port}/v1')
    print(f'  Base URL for containers: http://172.19.0.1:{args.port}/v1')
    print(f'  Model: {MODEL_ID}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopped')


if __name__ == '__main__':
    main()
