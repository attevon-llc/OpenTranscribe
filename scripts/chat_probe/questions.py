"""The ``Question``/``Result`` records and loading a question set."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    #: ``id``/``file_uuid``/``kind``/``content_chars`` only (see
    #: :func:`_offered_citation_refs`) — every one of those a count or an enum value,
    #: never source text. This is the FULL set the model was offered — a superset of
    #: ``citations`` above, which is the PERSISTED/used subset re-fetched after the
    #: stream closes (issue #384: "the citations a user can click are exactly the
    #: excerpts the model was given" is a claim about the offered set, not the used
    #: one). Traceability metrics (``tests.eval.harness.traceability``) need this to
    #: check the #384 invariant and a scope leak per turn, not only in a unit test.
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
