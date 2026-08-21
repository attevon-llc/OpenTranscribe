#!/usr/bin/env python3
"""Live probe of the real chat-RAG HTTP path (issue #72), against a real LLM.

    python3 scripts/probe_chat_rag.py --port 5274 --question-set my-questions.json \\
        --llm-base-url http://llm-test-vllm:8000/v1 --llm-model gemma-4-e4b \\
        --out /tmp/ot-probe --metrics-out tests/eval/baselines/probe-my-run

Drives **login -> create a scoped conversation -> POST a message -> read the SSE
stream -> re-fetch the thread for msg_metadata**, i.e. exactly what a browser does.
Nothing else in ``backend/tests/eval/`` does that: ``harness/runner.py`` calls
``retrieve_chunks`` in-process and never involves an LLM (D6). This tool exists to
observe the one thing that path cannot: what the PRODUCT actually returns — how many
files a multi-file scope query really consulted, whether a negative control gets a
refusal, how long an answer takes end to end.

Read-only against the corpus it points at: it creates conversations (which persist)
but never reindexes, dispatches a bulk task, or deletes anything.

Two environment gotchas that cost a debugging cycle each, the first time:

* **vLLM publishes on 127.0.0.1 only, on the DEV project's network.** A fresh/
  measurement stack cannot resolve it until the container is joined to that stack's
  network with the alias vLLM's own hostname is configured under, e.g.::

      docker network connect --alias llm-test-vllm otfresh-<name>_default \\
          opentranscribe-llm-test-vllm

  ⚠️ The ``--alias`` is not optional. A plain ``docker network connect`` registers
  the CONTAINER name on the new network, not the compose SERVICE alias every LLM
  config's ``base_url`` names — DNS resolution from the backend fails without it.
* **Every mutating request needs an ``X-CSRF-Token`` header matching the
  ``csrf_token`` cookie** (double-submit, ``backend/app/middleware/csrf.py``). GETs
  are exempt, so login followed by a read-only call looks fine right up until the
  first POST. :func:`login` attaches it automatically.

Question sets are supplied at runtime via ``--question-set`` (a JSON file, never
hardcoded here) precisely so this file can be committed without carrying whatever
question/reference content a question set happens to be built from — see
``tests/eval/harness/probe_metrics.py``'s module docstring for the licence reasoning.
A question-set entry has the shape::

    {
      "label": "multi-1-...",        // required, this tool's own id, never dataset text
      "category": "multi_file",      // required, free-form bucket for the report
      "question": "...",             // required, sent to the app verbatim
      "file_uuids": ["...", "..."],  // required, the conversation's file scope
      "scope_desc": "...",           // optional, human-readable, full-output only
      "reference": "...",            // optional, human-readable, full-output only
      "expect_refusal": false        // optional, default false
    }

For a single ad hoc question, pass ``--question``/``--scope`` instead of
``--question-set``.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT / 'backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

logger = logging.getLogger('probe_chat_rag')

#: Test credentials documented throughout this repo (backend/tests/CLAUDE.md,
#: e2e/conftest.py) — not a secret, just the seeded dev-stack admin account.
DEFAULT_EMAIL = 'admin@example.com'
DEFAULT_PASSWORD = 'password'  # noqa: S105 - documented dev-stack test credential, not a secret


@dataclass
class Question:
    """One question to send, and the scope to send it against."""

    label: str
    category: str
    question: str
    file_uuids: list[str]
    scope_desc: str = ''
    reference: str | None = None
    expect_refusal: bool = False


@dataclass
class Result:
    """One question's full outcome. Carries prose (question/answer/reference) —
    this is the FULL-fidelity shape, written only to ``--out``, never to
    ``--metrics-out``. See :mod:`tests.eval.harness.probe_metrics` for the
    metrics-only shape that IS safe to commit.
    """

    q: Question
    conversation_uuid: str | None = None
    answer_text: str = ''
    reasoning_text: str = ''
    latency_s: float = 0.0
    error: str | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    msg_metadata: dict[str, Any] = field(default_factory=dict)
    citations: list[dict[str, Any]] = field(default_factory=list)
    files_consulted_uuids: list[str] = field(default_factory=list)
    chunks_used: int | None = None
    retrieved: int | None = None
    #: The ``sources`` SSE frame's citations, captured mid-stream and stripped down to
    #: ``id``/``file_uuid`` only (see :func:`_offered_citation_refs`). This is the FULL
    #: set the model was offered — a superset of ``citations`` above, which is the
    #: PERSISTED/used subset re-fetched after the stream closes (issue #384: "the
    #: citations a user can click are exactly the excerpts the model was given" is a
    #: claim about the offered set, not the used one). Traceability metrics
    #: (``tests.eval.harness.traceability``) need this to check the #384 invariant and
    #: a scope leak per turn, not only in a unit test.
    offered_citations: list[dict[str, Any]] = field(default_factory=list)


def parse_scope(value: str) -> list[str]:
    """Comma-separated file uuids -> a list, dropping blanks and whitespace."""
    return [item.strip() for item in value.split(',') if item.strip()]


def load_question_set(path: Path) -> list[Question]:
    """Read and validate a ``--question-set`` JSON file.

    Args:
        path: Path to a JSON file holding a list of question objects — see the
            module docstring for the required shape.

    Returns:
        One :class:`Question` per entry, in file order.

    Raises:
        SystemExit: The file is not a JSON list, or an entry is missing one of
            ``label``/``category``/``question``/``file_uuids``.
    """
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except OSError as exc:
        raise SystemExit(f'--question-set: cannot read {path}: {exc}') from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f'--question-set: {path} is not valid JSON: {exc}') from exc
    if not isinstance(payload, list):
        raise SystemExit(f'--question-set: {path} must be a JSON list of question objects')

    questions: list[Question] = []
    required = ('label', 'category', 'question', 'file_uuids')
    for index, entry in enumerate(payload):
        if not isinstance(entry, dict):
            raise SystemExit(f'--question-set: entry {index} in {path} is not an object')
        missing = [key for key in required if key not in entry]
        if missing:
            raise SystemExit(f'--question-set: entry {index} in {path} is missing {missing}')
        questions.append(
            Question(
                label=str(entry['label']),
                category=str(entry['category']),
                question=str(entry['question']),
                file_uuids=[str(u) for u in entry['file_uuids']],
                scope_desc=str(entry.get('scope_desc', '')),
                reference=(None if entry.get('reference') is None else str(entry['reference'])),
                expect_refusal=bool(entry.get('expect_refusal', False)),
            )
        )
    return questions


def login(session: Any, base_url: str, email: str, password: str) -> None:
    """Authenticate and attach the double-submit CSRF header for every later POST.

    Args:
        session: A ``requests.Session`` to authenticate in place.
        base_url: e.g. ``http://localhost:5274/api``.
        email: Login email.
        password: Login password.

    Raises:
        requests.HTTPError: The login request itself failed.
        SystemExit: Login succeeded but set no ``csrf_token`` cookie — every
            mutating request after this would then 403, which reads as a much
            more confusing failure than refusing here.
    """
    response = session.post(
        f'{base_url}/auth/login',
        data={'username': email, 'password': password},
        timeout=30,
    )
    response.raise_for_status()
    logger.info('login ok: %s cookies=%s', response.status_code, list(session.cookies.keys()))
    csrf = session.cookies.get('csrf_token')
    if not csrf:
        raise SystemExit(
            'login succeeded but no csrf_token cookie was set — every mutating '
            'request below would 403 (backend/app/middleware/csrf.py)'
        )
    session.headers.update({'X-CSRF-Token': csrf})


def ensure_llm_config(
    session: Any,
    base_url: str,
    *,
    name: str,
    provider: str,
    model_name: str,
    llm_base_url: str,
    max_tokens: int,
    temperature: str,
) -> str:
    """Create (or reuse) a UserLLMSettings row for the probe, and make it active.

    Args:
        session: An authenticated ``requests.Session``.
        base_url: The app's API base URL.
        name: Config display name. Reused across runs by matching ``llm_base_url``,
            not by this name, so re-running the probe never accumulates configs.
        provider: e.g. ``"vllm"``, ``"openai"``.
        model_name: Model identifier the provider expects.
        llm_base_url: The provider's OpenAI-compatible base URL, e.g.
            ``http://llm-test-vllm:8000/v1`` — resolved from the BACKEND
            CONTAINER's network, not this script's host.
        max_tokens: Context window declared for the config.
        temperature: Sampling temperature, as the API expects it (a string).

    Returns:
        The configuration's uuid.
    """
    response = session.get(f'{base_url}/llm-settings', timeout=30)
    response.raise_for_status()
    for config in response.json().get('configurations', []):
        if config.get('base_url') == llm_base_url:
            config_uuid = str(config['uuid'])
            logger.info('reusing existing LLM config %s', config_uuid)
            break
    else:
        payload = {
            'name': name,
            'provider': provider,
            'model_name': model_name,
            'base_url': llm_base_url,
            'max_tokens': max_tokens,
            'temperature': temperature,
            'is_active': True,
            'is_shared': False,
        }
        response = session.post(f'{base_url}/llm-settings', json=payload, timeout=30)
        response.raise_for_status()
        config_uuid = str(response.json()['uuid'])
        logger.info('created LLM config %s', config_uuid)

    response = session.post(
        f'{base_url}/llm-settings/set-active',
        json={'configuration_id': config_uuid},
        timeout=30,
    )
    response.raise_for_status()
    return config_uuid


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
    """Strip a ``sources`` frame's citations down to ``id``/``file_uuid`` only.

    The full citation payload carries a ``snippet`` (a transcript excerpt) and a
    ``title`` — prose this public repo cannot commit (see
    ``tests.eval.harness.probe_metrics``'s module docstring, ``FORBIDDEN_KEYS``).
    Traceability only needs to know WHICH excerpt id was offered and WHICH file it
    belongs to, so everything else is dropped before it ever reaches :class:`Result`.
    A malformed entry (missing ``id`` or ``file_uuid``) is skipped rather than raising —
    this is a probe against a live server, and one odd frame must not abort the run.
    """
    refs: list[dict[str, Any]] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        cid, file_uuid = citation.get('id'), citation.get('file_uuid')
        if cid is None or file_uuid is None:
            continue
        refs.append({'id': int(cid), 'file_uuid': str(file_uuid)})
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
    with session.post(
        f'{base_url}/chat/conversations/{conversation_uuid}/messages',
        json={'content': content},
        stream=True,
        timeout=180,
        headers={'Accept': 'text/event-stream'},
    ) as response:
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


def run_question(session: Any, base_url: str, question: Question) -> Result:
    """Run one question end to end, catching everything into ``Result.error``.

    A caught exception here is the probe's own equivalent of a scored "unanswered"
    turn: it is recorded, not raised, so one bad question does not abort the run.
    """
    result = Result(q=question)
    try:
        conversation_uuid = create_conversation(
            session, base_url, question.file_uuids, f'probe: {question.label}'
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
    except Exception as exc:  # noqa: BLE001 - probe script, capture everything and continue
        result.error = f'{type(exc).__name__}: {exc}'
    return result


def result_to_record(result: Result) -> dict[str, Any]:
    """The FULL-fidelity per-question record — includes prose. ``--out`` only."""
    return {
        'label': result.q.label,
        'category': result.q.category,
        'question': result.q.question,
        'reference_answer': result.q.reference,
        'scope_requested': result.q.scope_desc,
        'scope_file_uuids': result.q.file_uuids,
        'expect_refusal': result.q.expect_refusal,
        'conversation_uuid': result.conversation_uuid,
        'app_answer': result.answer_text,
        'reasoning_text': result.reasoning_text,
        'latency_s': round(result.latency_s, 2),
        'error': result.error,
        'warnings': result.warnings,
        'msg_metadata': result.msg_metadata,
        'citations': result.citations,
        'offered_citations': result.offered_citations,
        'files_consulted_uuids': result.files_consulted_uuids,
        'chunks_used': result.chunks_used,
        'retrieved': result.retrieved,
    }


def render_full_markdown(records: list[dict[str, Any]], title: str) -> str:
    """The full-fidelity report. Contains prose. ``--out`` only, never committed."""
    lines = [f'# {title}\n']
    for record in records:
        lines.append(f'## {record["label"]} ({record["category"]})\n')
        lines.append(
            f'**Scope requested:** {record["scope_requested"]}  \n'
            f'**File UUIDs:** {record["scope_file_uuids"]}\n'
        )
        lines.append(f'**Question:**\n\n> {record["question"]}\n')
        if record['reference_answer']:
            lines.append(f'**Reference answer:**\n\n> {record["reference_answer"]}\n')
        lines.append(f'**App answer:**\n\n```\n{record["app_answer"]}\n```\n')
        if record['reasoning_text']:
            lines.append(f'**Reasoning:**\n\n```\n{record["reasoning_text"]}\n```\n')
        lines.append(f'**Latency:** {record["latency_s"]} s\n')
        lines.append(f'**Error:** {record["error"]}\n')
        lines.append(f'**Warnings:** {record["warnings"]}\n')
        lines.append(f'**msg_metadata:** `{json.dumps(record["msg_metadata"], default=str)}`\n')
        lines.append(
            f'**chunks_used:** {record["chunks_used"]}  **retrieved:** {record["retrieved"]}\n'
        )
        lines.append(f'**Files consulted:** {record["files_consulted_uuids"]}\n')
        lines.append('\n---\n')
    return '\n'.join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('--host', default='localhost', help='App host')
    parser.add_argument(
        '--port',
        type=int,
        default=5174,
        help='App port. The dev default is 5174; a --fresh --port-offset stack '
        'publishes elsewhere (the 2026-08-20 measurement run used 5274).',
    )
    parser.add_argument('--email', default=DEFAULT_EMAIL)
    parser.add_argument('--password', default=DEFAULT_PASSWORD)

    question_group = parser.add_mutually_exclusive_group(required=True)
    question_group.add_argument(
        '--question-set', default=None, help='Path to a JSON question-set file (see module doc)'
    )
    question_group.add_argument(
        '--question', default=None, help='Ad hoc single question text (pairs with --scope)'
    )
    parser.add_argument(
        '--scope', default=None, help='Comma-separated file uuids, required with --question'
    )
    parser.add_argument('--label', default='ad-hoc', help='Label for an ad hoc --question')
    parser.add_argument('--category', default='ad_hoc', help='Category for an ad hoc --question')

    parser.add_argument('--llm-name', default='probe-llm-config')
    parser.add_argument('--llm-provider', default='vllm')
    parser.add_argument('--llm-model', default='gemma-4-e4b')
    parser.add_argument(
        '--llm-base-url',
        default='http://llm-test-vllm:8000/v1',
        help="Resolved from the BACKEND CONTAINER's network — see the module "
        "docstring's docker network connect --alias gotcha.",
    )
    parser.add_argument('--llm-max-tokens', type=int, default=8192)
    parser.add_argument('--llm-temperature', default='0.0')
    parser.add_argument(
        '--skip-llm-config',
        action='store_true',
        help='Skip creating/activating an LLM config; use whatever is already active',
    )

    parser.add_argument(
        '--out', default='/tmp/ot-probe', help='Full-fidelity output dir (contains prose)'
    )
    parser.add_argument(
        '--metrics-out',
        default=None,
        help='Metrics-only output dir (safe to commit as a tests/eval/baselines/<name> '
        'entry). Omit to skip writing it.',
    )
    parser.add_argument('--run-name', default=None, help="Defaults to --out's directory name")
    parser.add_argument('-v', '--verbose', action='store_true')
    return parser


def _resolve_questions(args: argparse.Namespace) -> list[Question]:
    if args.question_set:
        return load_question_set(Path(args.question_set))
    if not args.scope:
        raise SystemExit('--question requires --scope (comma-separated file uuids)')
    return [
        Question(
            label=args.label,
            category=args.category,
            question=args.question,
            file_uuids=parse_scope(args.scope),
        )
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s'
    )

    import requests

    questions = _resolve_questions(args)
    base_url = f'http://{args.host}:{args.port}/api'
    session = requests.Session()

    login(session, base_url, args.email, args.password)
    if not args.skip_llm_config:
        config_uuid = ensure_llm_config(
            session,
            base_url,
            name=args.llm_name,
            provider=args.llm_provider,
            model_name=args.llm_model,
            llm_base_url=args.llm_base_url,
            max_tokens=args.llm_max_tokens,
            temperature=args.llm_temperature,
        )
        logger.info('active LLM config: %s', config_uuid)

    records: list[dict[str, Any]] = []
    for question in questions:
        logger.info('=== %s ===', question.label)
        result = run_question(session, base_url, question)
        logger.info(
            'latency=%.1fs error=%s chunks_used=%s retrieved=%s files_consulted=%d',
            result.latency_s,
            result.error,
            result.chunks_used,
            result.retrieved,
            len(result.files_consulted_uuids),
        )
        records.append(result_to_record(result))

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'results.json').write_text(
        json.dumps(records, indent=2, default=str) + '\n', encoding='utf-8'
    )
    (out_dir / 'results.md').write_text(
        render_full_markdown(records, f'Live RAG probe — {args.host}:{args.port}'),
        encoding='utf-8',
    )
    logger.info('Wrote full-fidelity output to %s (contains prose — do not commit)', out_dir)

    if args.metrics_out:
        from tests.eval.harness import probe_metrics

        run_name = args.run_name or out_dir.name
        results = probe_metrics.build_probe_results(
            run_name=run_name,
            target={
                'host': args.host,
                'port': args.port,
                'llm_provider': args.llm_provider,
                'llm_model': args.llm_model,
            },
            records=records,
        )
        rows = results['rows']
        metrics_dir = Path(args.metrics_out)
        metrics_dir.mkdir(parents=True, exist_ok=True)
        (metrics_dir / 'metrics.json').write_text(probe_metrics.dumps(results), encoding='utf-8')
        (metrics_dir / 'metrics.md').write_text(
            probe_metrics.render_probe_table(rows), encoding='utf-8'
        )
        (metrics_dir / 'runinfo.json').write_text(
            json.dumps(
                {
                    'target': {'host': args.host, 'port': args.port},
                    'latency_s': {r['label']: r['latency_s'] for r in records},
                    'conversation_uuids': {r['label']: r['conversation_uuid'] for r in records},
                },
                indent=2,
            )
            + '\n',
            encoding='utf-8',
        )
        logger.info('Wrote metrics-only output to %s (safe to commit)', metrics_dir)

        from tests.eval.harness import traceability

        trace_results = traceability.build_traceability_results(
            run_name=run_name,
            target={
                'host': args.host,
                'port': args.port,
                'llm_provider': args.llm_provider,
                'llm_model': args.llm_model,
            },
            records=records,
        )
        (metrics_dir / 'traceability.json').write_text(
            traceability.dumps(trace_results), encoding='utf-8'
        )
        (metrics_dir / 'traceability.md').write_text(
            traceability.render_traceability_table(trace_results['rows']), encoding='utf-8'
        )
        logger.info('Wrote traceability-only output to %s (safe to commit)', metrics_dir)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
