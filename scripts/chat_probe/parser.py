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

Read-only against the corpus it points at: it never reindexes or dispatches a bulk task.
What it changes it puts back (issue #1195): its conversations (titled ``probe-<hex8>: ``, a
shape ``scripts/cleanup-test-data.py`` sweeps) are deleted at the end unless
``--keep-conversations``, and the admin chat flags and the user's active LLM config it
switched are restored, on failure and Ctrl-C too.

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

from chat_probe.config import DEFAULT_EMAIL, DEFAULT_PASSWORD


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

    parser.add_argument(
        '--concurrency',
        type=int,
        default=1,
        help=(
            'Questions to run in parallel (default 1, serial). Bounded by the app'
            " admin's chat.limits.max_concurrent_streams AND by vLLM's --max-num-seqs;"
            ' exceeding either just queues. Latency becomes non-comparable above 1.'
        ),
    )

    parser.add_argument('--llm-name', default='probe-llm-config')
    parser.add_argument(
        '--keep-conversations',
        action='store_true',
        help='Leave the probe conversations on the stack for inspection (default: delete)',
    )
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
        '--chat-flag',
        action='append',
        default=[],
        metavar='FIELD=true|false',
        help='Set and read-back-verify a chat.* SystemSettings flag before running '
        '(e.g. --chat-flag speaker_resolver_enabled=true). Repeatable. See '
        'ensure_chat_flags() — this closes the exact applied-check gap that made '
        "issue #523's ami81-routerfix artifact invalid: a flag flipped by hand on "
        'the target stack, with nothing to catch it not being flipped at all.',
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
