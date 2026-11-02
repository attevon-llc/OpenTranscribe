"""Resolve questions and run the probe, with cleanup of everything it created."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from chat_probe.config import logger
from chat_probe.conversation import run_question
from chat_probe.parser import build_parser
from chat_probe.questions import Question, load_question_set, parse_scope
from chat_probe.report import render_full_markdown, result_to_record
from chat_probe.stack_state import (
    active_llm_config,
    ensure_chat_flags,
    ensure_llm_config,
    login,
    restore_state,
    snapshot_chat_flags,
)


def _parse_chat_flags(raw: list[str]) -> dict[str, bool]:
    """``["speaker_resolver_enabled=true"]`` -> ``{"speaker_resolver_enabled": True}``.

    Raises:
        SystemExit: A ``--chat-flag`` entry is malformed, or its value is not
            ``true``/``false`` — this instrument only carries boolean chat flags
            today, and a silently-mis-parsed value would defeat the whole point of
            :func:`ensure_chat_flags`'s read-back check.
    """
    parsed: dict[str, bool] = {}
    for entry in raw:
        if '=' not in entry:
            raise SystemExit(f'--chat-flag expects FIELD=true|false, got {entry!r}')
        field, _, value = entry.partition('=')
        field = field.strip()
        normalized = value.strip().lower()
        if normalized not in ('true', 'false'):
            raise SystemExit(
                f'--chat-flag {field}={value!r}: only true/false are supported '
                '(every chat flag this probe drives today is boolean)'
            )
        parsed[field] = normalized == 'true'
    return parsed


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
    chat_flag_updates = _parse_chat_flags(args.chat_flag)
    prior_flags = snapshot_chat_flags(session, base_url, list(chat_flag_updates))
    prior_llm = None if args.skip_llm_config else active_llm_config(session, base_url)
    config_uuid: str | None = None
    records: list[dict[str, Any]] = []
    try:
        return _run(args, session, base_url, questions, chat_flag_updates, records, requests)
    finally:
        if not args.skip_llm_config:
            config_uuid = active_llm_config(session, base_url)
        conversations = (
            []
            if args.keep_conversations
            else [str(r['conversation_uuid']) for r in records if r.get('conversation_uuid')]
        )
        restore_state(
            session,
            base_url,
            chat_flags=prior_flags,
            prior_llm=prior_llm,
            current_llm=config_uuid,
            conversations=conversations,
        )
        with contextlib.suppress(Exception):
            session.post(f'{base_url}/auth/logout', timeout=15)


def _run(
    args: argparse.Namespace,
    session: Any,
    base_url: str,
    questions: list[Question],
    chat_flag_updates: dict[str, bool],
    records: list[dict[str, Any]],
    requests: Any,
) -> int:
    """The probe proper. Appends each finished record to ``records`` as it goes, so the
    caller's cleanup sees every conversation even when a later question raises."""
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

    if chat_flag_updates:
        ensure_chat_flags(session, base_url, chat_flag_updates)

    total = len(questions)

    def run_one(index_and_question: tuple[int, Question], sess: Any) -> dict[str, Any]:
        index, question = index_and_question
        logger.info('=== [%d/%d] %s ===', index + 1, total, question.label)
        result = run_question(sess, base_url, question)
        logger.info(
            '[%d/%d] %s latency=%.1fs error=%s chunks_used=%s retrieved=%s files_consulted=%d',
            index + 1,
            total,
            question.label,
            result.latency_s,
            result.error,
            result.chunks_used,
            result.retrieved,
            len(result.files_consulted_uuids),
        )
        return result_to_record(result)

    if args.concurrency <= 1:
        for item in enumerate(questions):
            records.append(run_one(item, session))
    else:
        # One Session per worker THREAD, not one shared: requests.Session is not
        # thread-safe, and each carries its own auth + CSRF cookie pair (see the
        # module docstring). ThreadPoolExecutor.map preserves input order, so the
        # records stay in question order however the turns interleave.
        local = threading.local()

        def worker(item: tuple[int, Question]) -> dict[str, Any]:
            sess = getattr(local, 'session', None)
            if sess is None:
                sess = requests.Session()
                login(sess, base_url, args.email, args.password)
                local.session = sess
            return run_one(item, sess)

        logger.warning(
            'concurrency=%d: per-question latency is measured UNDER LOAD and is NOT '
            'comparable to a serial run. Answer content is unaffected.',
            args.concurrency,
        )
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            records.extend(pool.map(worker, enumerate(questions)))

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
