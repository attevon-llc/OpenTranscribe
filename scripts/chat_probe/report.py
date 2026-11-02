"""Turn probe results into JSON records and a Markdown report."""

from __future__ import annotations

import json
from typing import Any

from chat_probe.questions import Result


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
