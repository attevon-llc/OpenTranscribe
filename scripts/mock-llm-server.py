#!/usr/bin/env python3
"""A fake OpenAI-compatible LLM, for exercising chat and the JSON-shaped LLM tasks without a model.

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
import collections
import hashlib
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL_ID = 'mock-gpt'

# Scenario models. The requested `model` selects the behaviour, so a test picks
# a failure mode by configuring a provider rather than by patching code — which
# means the app exercises its REAL error handling, not a mocked branch.
#
#   mock-gpt        normal reply with [1]/[2] citations, markdown and a code block; a request
#                   whose system prompt spells out the topic / speaker-ID / summary JSON schema
#                   gets that canned JSON instead (STRUCTURED_REQUEST_MARKERS)
#   mock-echo       echoes the exact prompt it received; lets a test assert what the
#                   app actually sent (masking applied? prompt layers in order?)
#   mock-empty      completes with no content — the "model returned nothing" path
#   mock-error      HTTP 500 before any token — the provider_error path
#   mock-slow       stalls past the first-token watchdog — the timeout path
#   mock-reasoning  streams a reasoning phase (delta.reasoning_content, the vLLM
#                   dialect this app's OpenAI-compatible parser reads) before the
#                   same [1]/[2] answer as mock-gpt — exercises the collapsible
#                   reasoning display end to end with no GPU or real model.
#                   Reasoning only SEPARATES when the request asked for thinking;
#                   see THINKING_END_TOKEN below for why that condition is real
#   mock-summary-json   a constant, schema-valid BLUF summary (the default summary prompt's
#   mock-topics-json    output format), a constant tag/collection suggestion, and a constant
#   mock-speakers-json  speaker-identification answer. Each is the exact JSON shape the
#                       matching parser reads, so the summarization, topic-extraction and
#                       speaker-ID tasks run for real against it. Each reply also carries
#                       ECHO_FIELD, the SHA-256 of the request's system message, so a test
#                       can tell WHICH prompt produced WHICH reply (a slice call and the
#                       merge call differ only in their system message). Wiring only: the
#                       body is a constant, so it says nothing about summary quality.
SCENARIOS = (
    'mock-gpt',
    'mock-echo',
    'mock-empty',
    'mock-error',
    'mock-slow',
    'mock-reasoning',
    'mock-summary-json',
    'mock-topics-json',
    'mock-speakers-json',
)

#: Key carrying the system-message digest inside a JSON scenario's body. No parser reads it:
#: the topic and speaker parsers pick fields by name, and a summary keeps any key a custom
#: prompt produces, so on a persisted summary it names the call that produced it.
ECHO_FIELD = 'mock_system_sha256'

#: Every chat request the server has seen, newest last, readable at ``GET /_mock/requests``
#: and cleared with ``POST /_mock/requests/clear``. Lets a test assert how many calls a task
#: made and what each one carried without patching the code under test. Bounded: a long-lived
#: container must not grow with every chat turn.
REQUEST_LOG_LIMIT = 200
REQUEST_LOG: collections.deque[dict] = collections.deque(maxlen=REQUEST_LOG_LIMIT)

SUMMARY_BODY = {
    'bluf': 'Mock summary: the group reviewed the plan and agreed on the next steps.',
    'brief_summary': (
        'A canned summary returned by scripts/mock-llm-server.py. It has the shape of the '
        'default summary prompt so the summarization task can persist and index it.'
    ),
    'major_topics': [
        {
            'topic': 'Mock topic',
            'summary': 'A canned discussion of the plan.',
            'key_points': ['The plan was reviewed', 'The next steps were agreed'],
            'timestamp_range': '[00:00] - [00:10]',
        }
    ],
    'action_items': [
        {
            'item': 'Send the follow-up notes',
            'owner': 'Not specified',
            'due_date': 'Not specified',
            'priority': 'medium',
            'context': 'The group asked for written notes.',
            'mentioned_timestamp': '[00:05]',
        }
    ],
    'key_decisions': [
        {
            'decision': 'Proceed with the plan as reviewed',
            'context': 'No objections were raised.',
            'impact': 'Work continues unchanged.',
            'stakeholders': [],
            'timestamp': '[00:08]',
        }
    ],
    'speakers_analysis': [],
    'follow_up_items': ['Confirm the schedule'],
    'overall_sentiment': 'neutral',
    'content_type_detected': 'meeting',
}

TOPICS_BODY = {
    'suggested_collections': [
        {
            'name': 'Mock Planning Sessions',
            'confidence': 0.9,
            'rationale': 'Canned collection suggestion.',
        }
    ],
    'suggested_tags': [
        {'name': 'mock-planning', 'confidence': 0.9, 'rationale': 'Canned high-confidence tag.'},
        {
            'name': 'mock-schedule',
            'confidence': 0.6,
            'rationale': 'Canned moderate-confidence tag.',
        },
    ],
}

# Two predictions, so a file with either one or two diarized speakers gets at least one
# suggestion; a label the file does not have is simply not stored.
SPEAKERS_BODY = {
    'speaker_predictions': [
        {
            'speaker_label': 'SPEAKER_00',
            'predicted_name': 'Alex Morgan',
            'confidence': 0.85,
            'reasoning': 'Canned reasoning.',
            'evidence_types': ['name_mention'],
        },
        {
            'speaker_label': 'SPEAKER_01',
            'predicted_name': 'Priya Natarajan',
            'confidence': 0.7,
            'reasoning': 'Canned reasoning.',
            'evidence_types': ['role_reference'],
        },
    ],
    'overall_confidence': 'medium',
    'analysis_notes': 'Canned analysis from the mock LLM.',
}

JSON_SCENARIO_BODIES = {
    'mock-summary-json': SUMMARY_BODY,
    'mock-topics-json': TOPICS_BODY,
    'mock-speakers-json': SPEAKERS_BODY,
}

# The boundary token a Gemma-4-class model emits between its chain-of-thought and
# its answer. Reproduced verbatim (issue #439) because canning the *fixed* shape
# only would have made the regression untestable.
#
# Measured against vLLM 0.19 serving Gemma 4 E4B with `--reasoning-parser gemma4`:
# the template only emits the matching `<|channel>` OPENER when the request asks
# for thinking (`chat_template_kwargs={"enable_thinking": true}`). Unasked, the
# model reasons anyway and emits this closer alone — and vLLM's *streaming*
# parser, which enters reasoning mode on the opener, never fires. The whole
# chain-of-thought then arrives on `delta.content` with this control token
# embedded in it. Its non-streaming parser recovers from a lone closer, so only
# the streaming shape below is conditional; that asymmetry is the real server's,
# not an invention of this mock.
THINKING_END_TOKEN = '<channel|>'

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


def _thinking_requested(body: dict) -> bool:
    """Did the caller activate the chat template's thinking mode?

    ``chat_template_kwargs={"enable_thinking": true}`` is how a client asks vLLM
    for a well-delimited thought block. Whether it was sent is the difference
    between reasoning arriving on its own wire field and reasoning arriving as
    the answer (issue #439), so the mock branches on exactly that.
    """
    kwargs = body.get('chat_template_kwargs')
    return bool(isinstance(kwargs, dict) and kwargs.get('enable_thinking'))


def _system_sha256(messages: list[dict]) -> str:
    """SHA-256 of the system message, or of the empty string when there is none."""
    system = next((str(m.get('content') or '') for m in messages if m.get('role') == 'system'), '')
    return hashlib.sha256(system.encode()).hexdigest()


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


# A request that quotes one of these output-schema keys is asking for JSON in that shape: the
# app's topic, speaker-ID and summary prompts each spell their schema out and nothing else
# sends the key. A real model answers such a request with JSON whatever its name, so mock-gpt
# does too; a chat turn carries none of them and still gets the markdown reply.
STRUCTURED_REQUEST_MARKERS = (
    ('"suggested_tags"', TOPICS_BODY),
    ('"speaker_predictions"', SPEAKERS_BODY),
    ('"bluf"', SUMMARY_BODY),
)


def _structured_body(messages: list[dict]) -> dict | None:
    prompt = '\n'.join(str(m.get('content') or '') for m in messages)
    return next((body for marker, body in STRUCTURED_REQUEST_MARKERS if marker in prompt), None)


def _reply_text(model: str, messages: list[dict], system_sha256: str) -> str:
    """The answer a scenario model gives to ``messages``."""
    if model == 'mock-echo':
        # Verbatim, so a test can assert on the prompt the app really built:
        # whether redaction masked the excerpts, and whether the system
        # prompt layers arrived in the documented order.
        return '\n\n'.join(f'[{m.get("role")}]\n{m.get("content") or ""}' for m in messages)
    if model == 'mock-empty':
        return ''
    if model in JSON_SCENARIO_BODIES:
        return json.dumps({**JSON_SCENARIO_BODIES[model], ECHO_FIELD: system_sha256})
    structured = _structured_body(messages) if model == MODEL_ID else None
    if structured is not None:
        return json.dumps({**structured, ECHO_FIELD: system_sha256})
    return REPLY_TEMPLATE.format(topic=_topic_from(messages))


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):  # quieter console
        print(f'  {self.address_string()} {fmt % args}')

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
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
        if self.path.rstrip('/') == '/_mock/requests':
            self._json({'requests': list(REQUEST_LOG)})
            return
        self._json({'status': 'ok', 'model': MODEL_ID})

    def do_POST(self) -> None:
        length = int(self.headers.get('Content-Length') or 0)
        try:
            body = json.loads(self.rfile.read(length) or b'{}')
        except ValueError:
            body = {}

        if self.path.rstrip('/') == '/_mock/requests/clear':
            REQUEST_LOG.clear()
            self._json({'requests': []})
            return

        messages = body.get('messages') or []
        model = str(body.get('model') or MODEL_ID)
        system_sha256 = _system_sha256(messages)
        REQUEST_LOG.append(
            {
                'model': model,
                'system_sha256': system_sha256,
                'max_tokens': body.get('max_tokens'),
                'messages': messages,
            }
        )

        if model == 'mock-error':
            # A provider that IS configured but fails. The app must surface this
            # as an error frame, unlike "no provider configured".
            self._json({'error': {'message': 'Mock provider failure', 'type': 'server_error'}}, 500)
            return

        if model == 'mock-slow':
            time.sleep(SLOW_FIRST_TOKEN_DELAY_S)

        text = _reply_text(model, messages, system_sha256)

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

        if reasoning and not _thinking_requested(body):
            # The unasked-for streaming shape, reproduced from a real vLLM +
            # Gemma 4 run (issue #439): no separate wire field at all, the
            # chain-of-thought inlined ahead of the answer and the boundary token
            # left in the content as literal text. An app that does not activate
            # thinking renders all of this as the answer.
            text = f'{reasoning}{THINKING_END_TOKEN}{text}'
            reasoning = None

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
    # Loopback by DEFAULT: this server answers every prompt with no
    # authentication, so a bare `python scripts/mock-llm-server.py` on a
    # developer's workstation must not expose it to the LAN. The container
    # opts into 0.0.0.0 explicitly in docker-compose.mock-llm.yml, where the
    # container boundary plus a 127.0.0.1-only published port are the isolation.
    parser.add_argument('--host', default='127.0.0.1')
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
