"""Create a scoped conversation, send one message over SSE and collect the answer."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from chat_probe.config import logger
from chat_probe.questions import Question, Result

#: Bounded retries for a 429 on the message POST. The app's concurrency cap is a
#: real limit, not a failure, so the probe waits it out rather than recording a
#: per-question error that reads as the arm under test breaking.
_RATE_LIMIT_RETRIES = 5
_RATE_LIMIT_BACKOFF_S = 3.0


def create_conversation(session: Any, base_url: str, file_uuids: list[str], title: str) -> str:
    """POST a new chat conversation scoped to ``file_uuids``. Returns its uuid."""
    body = {
        'title': title,
        'scope': {
            'file_uuids': file_uuids,
            'collection_uuids': [],
            'tag_names': [],
            'speakers': [],
        },
    }
    response = session.post(f'{base_url}/chat/conversations', json=body, timeout=30)
    response.raise_for_status()
    return str(response.json()['uuid'])


def _offered_citation_refs(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip a ``sources`` frame's citations down to safe, non-prose fields only.

    The full citation payload carries a ``snippet`` (a transcript excerpt) and a
    ``title`` — prose this public repo cannot commit (see
    ``tests.eval.harness.probe_metrics``'s module docstring, ``FORBIDDEN_KEYS``).
    Traceability needs to know WHICH excerpt id was offered, WHICH file it belongs
    to, WHAT KIND it is (``chunk``/``digest``/``summary``/``recurrence`` — an enum
    value, never text drawn from the transcript), and its pre-truncation length
    (``content_chars``, a plain integer count — issue #832), so everything else is
    dropped before it ever reaches :class:`Result`. A malformed entry (missing
    ``id`` or ``file_uuid``) is skipped rather than raising — this is a probe
    against a live server, and one odd frame must not abort the run.
    """
    refs: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        cid, file_uuid = citation.get('id'), citation.get('file_uuid')
        if cid is None or file_uuid is None:
            continue
        ref: dict[str, Any] = {'id': int(cid), 'file_uuid': str(file_uuid)}
        kind = citation.get('kind')
        if kind is not None:
            ref['kind'] = str(kind)
        content_chars = citation.get('content_chars')
        if content_chars is not None:
            ref['content_chars'] = int(content_chars)
        refs.append(ref)
    return refs


def send_message_and_collect(
    session: Any, base_url: str, conversation_uuid: str, content: str
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]], float]:
    """POST a message and read its SSE stream to completion.

    Args:
        session: An authenticated ``requests.Session``.
        base_url: The app's API base URL.
        conversation_uuid: Target conversation.
        content: The message text.

    Returns:
        ``(answer_text, reasoning_text, warnings, offered_citations, latency_seconds)``.
        ``offered_citations`` is the ``sources`` frame's citation list, stripped by
        :func:`_offered_citation_refs` — empty if no ``sources`` frame arrived (e.g. a
        no-context turn, issue #384's frame is conditional on there being any excerpt).
    """
    start = time.monotonic()
    answer_parts: list[str] = []
    reasoning_parts: list[str] = []
    warnings: list[dict[str, Any]] = []
    offered_citations: list[dict[str, Any]] = []
    # A 429 here is the app's CONCURRENCY cap, not a failure of the thing under
    # test — two eval arms running in parallel, or a --concurrency above
    # chat.limits.max_concurrent_streams, will hit it. Recording it as a per-
    # question error made 58 of 81 questions look like the arm breaking, which is
    # exactly the misreading a retry removes. Bounded and backed off so a genuine
    # sustained refusal still surfaces rather than hanging the run.
    for attempt in range(_RATE_LIMIT_RETRIES):
        response = session.post(
            f'{base_url}/chat/conversations/{conversation_uuid}/messages',
            json={'content': content},
            stream=True,
            timeout=180,
            headers={'Accept': 'text/event-stream'},
        )
        if response.status_code != 429:
            break
        response.close()
        delay = _RATE_LIMIT_BACKOFF_S * (attempt + 1)
        logger.warning(
            'rate limited (429) on attempt %d/%d — backing off %.1fs',
            attempt + 1,
            _RATE_LIMIT_RETRIES,
            delay,
        )
        time.sleep(delay)

    with response:
        response.raise_for_status()
        event_name: str | None = None
        for raw_line in response.iter_lines(decode_unicode=True):
            if raw_line is None:
                continue
            line = raw_line.strip('\r')
            if line == '':
                event_name = None
                continue
            if line.startswith('event:'):
                event_name = line[len('event:') :].strip()
                continue
            if not line.startswith('data:'):
                continue
            data_str = line[len('data:') :].strip()
            try:
                data = json.loads(data_str) if data_str else {}
            except json.JSONDecodeError:
                data = {'raw': data_str}
            if event_name == 'delta':
                answer_parts.append(data.get('content') or data.get('text') or '')
            elif event_name == 'reasoning':
                reasoning_parts.append(data.get('content') or data.get('text') or '')
            elif event_name == 'warning':
                warnings.append(data)
            elif event_name == 'error':
                warnings.append({'code': 'error_frame', **data})
            elif event_name == 'sources':
                offered_citations = _offered_citation_refs(data.get('citations') or [])
    latency = time.monotonic() - start
    return ''.join(answer_parts), ''.join(reasoning_parts), warnings, offered_citations, latency


def fetch_thread_metadata(session: Any, base_url: str, conversation_uuid: str) -> dict[str, Any]:
    """Re-fetch the thread and return the last assistant message.

    ``msg_metadata`` (``chunks_used``/``retrieved``/...) and the persisted
    ``citations`` only exist on the PERSISTED row, not on the SSE stream — see
    ``backend/tests/CLAUDE.md``'s chat-suites section.
    """
    response = session.get(
        f'{base_url}/chat/conversations/{conversation_uuid}/messages', timeout=30
    )
    response.raise_for_status()
    messages = response.json().get('messages', [])
    assistant_messages = [m for m in messages if m.get('role') == 'assistant']
    return assistant_messages[-1] if assistant_messages else {}


#: One uniquifier per probe run: conversation titles are ``probe-<hex8>: <label>``, a shape
#: registered in scripts/cleanup-test-data.py (NAME_PREFIXES['chat_conversation']).
RUN_TAG = uuid.uuid4().hex[:8]


def run_question(session: Any, base_url: str, question: Question) -> Result:
    """Run one question end to end, catching everything into ``Result.error``.

    A caught exception here is the probe's own equivalent of a scored "unanswered"
    turn: it is recorded, not raised, so one bad question does not abort the run.
    """
    result = Result(q=question)
    try:
        conversation_uuid = create_conversation(
            session, base_url, question.file_uuids, f'probe-{RUN_TAG}: {question.label}'
        )
        result.conversation_uuid = conversation_uuid
        answer, reasoning, warnings, offered_citations, latency = send_message_and_collect(
            session, base_url, conversation_uuid, question.question
        )
        result.answer_text = answer
        result.reasoning_text = reasoning
        result.warnings = warnings
        result.offered_citations = offered_citations
        result.latency_s = latency

        # Persistence lands shortly after the stream closes; msg_metadata/citations
        # are read-after-write against the persisted row, not the stream.
        time.sleep(0.5)
        last_message = fetch_thread_metadata(session, base_url, conversation_uuid)
        result.msg_metadata = last_message.get('msg_metadata') or {}
        result.citations = last_message.get('citations') or []
        result.chunks_used = result.msg_metadata.get('chunks_used')
        result.retrieved = result.msg_metadata.get('retrieved')
        persisted_content = last_message.get('content')
        if persisted_content:
            result.answer_text = persisted_content
        if not result.answer_text and last_message.get('error'):
            result.error = last_message.get('error')
        files = {
            str(citation.get('file_uuid') or citation.get('fileUuid'))
            for citation in result.citations
            if citation.get('file_uuid') or citation.get('fileUuid')
        }
        result.files_consulted_uuids = sorted(files)
    except Exception as exc:  # probe script, capture everything and continue
        result.error = f'{type(exc).__name__}: {exc}'
    return result
