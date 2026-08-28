#!/usr/bin/env python3
"""Parse a chat SSE stream into one self-delimiting JSON record.

Why this exists (GH #611): `ac_chat_completion` in `lib/api-client.sh` used to print the
answer, the citation count, and the error code as three newline-separated "records" on
stdout, read back with `sed -n '1p'` / `'2p'` / `'3p'`. The mock LLM's answer
(`scripts/mock-llm-server.py`'s `REPLY_TEMPLATE`) is multi-paragraph markdown — i.e. the
FIRST record is itself multi-line — so a positional read grabbed the answer's own third
line and reported it as an `event: error` frame's code. The backend never sent one; see
the issue for the four independent proofs.

A newline-delimited scheme cannot fix this: any answer or error message that itself
contains a newline can forge a record boundary (see the adversarial-content test case in
`backend/tests/unit/test_release_chat_sse_parser.py`). `json.dumps` escapes embedded
newlines, so packing the whole result into ONE line of JSON makes that structurally
impossible — that is the property this file exists to provide, not just relocate.

Usage:
    <curl producing an SSE stream> | parse-chat-sse.py

Prints exactly one line of JSON to stdout:
    {"answer": "...", "citations": <int>, "error": "..."}

`error` is the empty string when no `event: error` frame arrived — this is what lets a
caller distinguish "the call was refused" (GH #595) from "the model answered with nothing"
(`mock-empty`). Diagnostics (tracebacks, warnings) go to stderr, which earlier smuggled
the citation count in the three-line scheme; a parser crash could silently corrupt the
result. Stdlib only (`sys`, `json`) — this runs against the host `python3` on a bare
release-rehearsal machine, not inside a venv.

Frame semantics are unchanged from the parser this replaces (relocated, not rewritten):
  - `event: delta`   -> append `data["content"] or data["text"] or ""` to the answer
  - `event: sources` -> `citations = len(data["citations"] or [])`
  - `event: error`   -> append `f"{code}: {message}"` if `message` else `code`;
                        multiple error frames join with "; "
  - a blank line resets the current event name (frame boundary)
  - a `data:` line with no preceding `event:` is ignored
  - an unknown event name is ignored, mirroring `chatStream.ts`'s `known` allowlist
    (forward compatibility with a frame this parser doesn't yet know about)
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable


def parse_chat_sse(stream: Iterable[str]) -> dict:
    """Parse an SSE byte/text stream into `{"answer", "citations", "error"}`.

    Args:
        stream: An iterable of raw SSE lines (e.g. `sys.stdin`).

    Returns:
        A dict with keys `answer` (str), `citations` (int), `error` (str).
    """
    event: str | None = None
    answer_parts: list[str] = []
    citation_count = 0
    error_parts: list[str] = []

    for raw in stream:
        line = raw.rstrip('\r\n')
        if line == '':
            event = None
            continue
        if line.startswith('event:'):
            event = line[len('event:') :].strip()
            continue
        if not line.startswith('data:'):
            continue
        data_str = line[len('data:') :].strip()
        try:
            data = json.loads(data_str) if data_str else {}
        except json.JSONDecodeError:
            print(f'parse-chat-sse: skipping malformed data line: {data_str!r}', file=sys.stderr)
            continue

        if event == 'delta':
            answer_parts.append(data.get('content') or data.get('text') or '')
        elif event == 'sources':
            citation_count = len(data.get('citations') or [])
        elif event == 'error':
            code = data.get('code') or 'unknown'
            message = data.get('message') or ''
            error_parts.append(f'{code}: {message}' if message else code)
        # else: unknown/other event (e.g. "start", "status", "reasoning", "done",
        # a future frame name) — ignored, mirroring chatStream.ts's known allowlist.

    return {
        'answer': ''.join(answer_parts),
        'citations': citation_count,
        'error': '; '.join(error_parts),
    }


def main() -> None:
    result = parse_chat_sse(sys.stdin)
    sys.stdout.write(json.dumps(result))
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
