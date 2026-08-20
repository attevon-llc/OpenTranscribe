"""Metrics-only extraction for the live chat-RAG probe (issue #72).

``scripts/probe_chat_rag.py`` drives the **real chat HTTP path** end to end — login,
create a conversation with a file scope, send a message, read the SSE stream, re-fetch
the thread for ``msg_metadata`` — against a **real LLM**. That is qualitatively
different from every other module in this package: ``harness/runner.py`` drives
``retrieve_chunks`` in-process and never touches an LLM (D6), so this is the one place
in the eval tree that can even observe an app answer.

⚠️ **That is also why this module exists, rather than the probe just writing its own
JSON.** The probe's question sets can be built from QMSum, whose README asks
research-only use for anything derived from it (see
``docs-site/docs/developer-guide/rag-evaluation.md``), and its own reference answers are
copied out verbatim for readability during a debugging session. This repository is
**public**. A results file that carries the question text, the QMSum reference answer,
the app's generated answer prose, or a citation's ``snippet`` (the transcript excerpt
itself) cannot be committed. What CAN be committed — and is genuinely useful as a
regression baseline — is the shape of the *outcome*: how many files a multi-file scope
query actually consulted, how many chunks were used, whether a warning fired, how long
it took to answer. None of that is QMSum's content; all of it is our own measurement of
our own system's behaviour.

Every extractor and builder in this module touches only the numeric/structural fields
named on :class:`ProbeTurnMetrics`. :func:`assert_no_prose` is the second, independent
line of defence: it walks the assembled artifact and refuses to serialise it if any
forbidden key appears anywhere in it, so a future field added to the raw probe record
(or copy-pasted from it) cannot leak text into a committed baseline by accident.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.eval.harness.report import dumps as _dumps_canonical

#: Keys that name licence-encumbered or simply large prose content in the probe's raw
#: per-turn record (``scripts/probe_chat_rag.py``'s ``Result``/JSON shape). Never emit
#: any of these into a committed artifact. Deliberately broad — a snippet, an answer, or
#: a reasoning trace can arrive under a few different names depending on which layer
#: produced the dict (SSE frame, persisted message row, or the probe's own JSON), and
#: the cost of a false positive here is zero (the field was never wanted in the
#: artifact) while the cost of a miss is a licence violation in a public repo.
FORBIDDEN_KEYS = frozenset(
    {
        "question",
        "reference_answer",
        "app_answer",
        "answer",
        "answer_text",
        "content",
        "reasoning",
        "reasoning_text",
        "citations",
        "snippet",
        "scope_desc",
        "scope_requested",
    }
)


class ProseLeakError(ValueError):
    """A metrics payload carries a forbidden prose/reference-answer field."""


def assert_no_prose(payload: Any, *, path: str = "$") -> None:
    """Refuse to proceed if ``payload`` carries a forbidden key anywhere in it.

    Walks dicts and lists/tuples recursively rather than trusting the builder
    functions below to have been careful — the point of a second, independent check
    is that it still catches a leak introduced by a future edit that never reads this
    docstring.

    Args:
        payload: A JSON-serialisable structure — the artifact about to be written.
        path: Internal, for the error message; callers should not pass this.

    Raises:
        ProseLeakError: ``payload`` contains a dict with a key in
            :data:`FORBIDDEN_KEYS` at any depth.
    """
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key) in FORBIDDEN_KEYS:
                raise ProseLeakError(
                    f"{path}.{key} is a forbidden prose/reference field — see "
                    "tests.eval.harness.probe_metrics module docstring (#72)"
                )
            assert_no_prose(value, path=f"{path}.{key}")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            assert_no_prose(item, path=f"{path}[{index}]")


def coverage_ratio(files_consulted: int, scope_size: int) -> float | None:
    """The fraction of a multi-file scope a turn actually drew citations from.

    Args:
        files_consulted: Distinct files with at least one citation in the answer.
        scope_size: Files named in the conversation's scope.

    Returns:
        ``files_consulted / scope_size``, capped at 1.0 (a citation whose file falls
        outside the requested scope would otherwise push this above 1.0 and read as
        "more than complete"). ``None`` when ``scope_size`` is 0 — an unscoped or
        malformed request, where a ratio would divide by zero and a 0.0 would misread
        as "measured and found zero coverage".
    """
    if scope_size <= 0:
        return None
    return min(1.0, files_consulted / scope_size)


@dataclass(frozen=True)
class ProbeTurnMetrics:
    """One chat turn's metrics-only record. Every field is a count, a ratio, or a
    boolean — never text the app or a human wrote.

    Attributes:
        query_id: The probe's own label for this question (e.g. ``"multi-1-..."``),
            never the question text itself.
        category: The probe's query-shape bucket (e.g. ``"multi_file"``).
        scope_size: Files named in the conversation's scope.
        expect_refusal: Whether this turn was a negative control expected to decline.
        errored: Whether the probe recorded an error for this turn (HTTP failure,
            exception) rather than a completed answer.
        files_consulted: Distinct files the answer actually cited.
        chunks_used: ``msg_metadata.chunks_used`` from the persisted message, or
            ``None`` if the metadata was never read back (e.g. the turn errored).
        retrieved: ``msg_metadata.retrieved`` — the candidate pool size before
            reranking/budgeting.
        coverage_ratio: See :func:`coverage_ratio`.
        warning_codes: SSE warning codes this turn raised, sorted and de-duplicated.
    """

    query_id: str
    category: str
    scope_size: int
    expect_refusal: bool
    errored: bool
    files_consulted: int
    chunks_used: int | None
    retrieved: int | None
    coverage_ratio: float | None
    warning_codes: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """JSON-safe, deterministic form. Field order matches the dataclass."""
        return {
            "query_id": self.query_id,
            "category": self.category,
            "scope_size": self.scope_size,
            "expect_refusal": self.expect_refusal,
            "errored": self.errored,
            "files_consulted": self.files_consulted,
            "chunks_used": self.chunks_used,
            "retrieved": self.retrieved,
            "coverage_ratio": self.coverage_ratio,
            "warning_codes": list(self.warning_codes),
        }


def _warning_codes(record: dict[str, Any]) -> tuple[str, ...]:
    warnings = record.get("warnings") or []
    codes = {
        str(warning["code"])
        for warning in warnings
        if isinstance(warning, dict) and warning.get("code")
    }
    return tuple(sorted(codes))


def extract_turn_metrics(record: dict[str, Any]) -> ProbeTurnMetrics:
    """Build one turn's metrics-only record from a raw probe result.

    ``record`` is shaped like one entry of the probe's per-question JSON (label,
    category, scope, citations, msg_metadata, warnings, error, ...) but this function
    reads ONLY the fields :class:`ProbeTurnMetrics` names. Everything else — question
    text, reference answers, app answer prose, citation snippets — is never touched,
    which is what keeps a licence-encumbered field from reaching the artifact through
    a field this function was never told to read.

    Args:
        record: One raw per-question result, as the probe assembles it before writing
            its own (uncommitted, full-fidelity) ``results.json``.

    Returns:
        The metrics-only record for this turn.

    Raises:
        KeyError: ``record`` is missing ``"label"`` or ``"category"`` — the two
            identifying fields with no safe default.
    """
    scope_size = len(record.get("scope_file_uuids") or [])
    files_consulted = len(record.get("files_consulted_uuids") or [])
    return ProbeTurnMetrics(
        query_id=str(record["label"]),
        category=str(record["category"]),
        scope_size=scope_size,
        expect_refusal=bool(record.get("expect_refusal", False)),
        errored=record.get("error") is not None,
        files_consulted=files_consulted,
        chunks_used=record.get("chunks_used"),
        retrieved=record.get("retrieved"),
        coverage_ratio=coverage_ratio(files_consulted, scope_size),
        warning_codes=_warning_codes(record),
    )


def build_probe_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One :class:`ProbeTurnMetrics` per record, sorted by ``query_id``.

    Sorted rather than left in probe (dict-insertion / list) order for the same
    reason ``harness/answers.py`` sorts every set it emits: a deterministic order is
    what makes two runs' artifacts diffable.
    """
    rows = [extract_turn_metrics(record) for record in records]
    return [row.as_json() for row in sorted(rows, key=lambda row: row.query_id)]


def summarize_probe_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Per-category rollup: counts, error rate, and coverage-ratio stats.

    Mirrors ``harness/chat_instrumentation.summarize_instrumentation``'s rule: a
    statistic is only reported over the subset that actually measured it, and a
    field with nothing to average reports its count and nothing else — never a 0.0
    that would read as a measured zero.

    Args:
        rows: :class:`ProbeTurnMetrics.as_json` dicts, e.g. from
            :func:`build_probe_rows`.

    Returns:
        A dict keyed by category, each with ``queries``, ``errored``, and — only
        when at least one row in the category has a non-``None`` coverage ratio —
        ``mean_coverage_ratio`` and ``min_coverage_ratio``.
    """
    by_category: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)

    summary: dict[str, Any] = {}
    for category in sorted(by_category):
        members = by_category[category]
        entry: dict[str, Any] = {
            "queries": len(members),
            "errored": sum(1 for row in members if row["errored"]),
        }
        ratios = [row["coverage_ratio"] for row in members if row["coverage_ratio"] is not None]
        if ratios:
            entry["mean_coverage_ratio"] = round(sum(ratios) / len(ratios), 6)
            entry["min_coverage_ratio"] = min(ratios)
        summary[category] = entry
    return summary


def build_probe_results(
    *,
    run_name: str,
    target: dict[str, Any],
    records: list[dict[str, Any]],
    notes: list[str] | None = None,
) -> dict[str, Any]:
    """The committed, metrics-only probe artifact. Deterministic by construction.

    Args:
        run_name: A short identifier for this run (e.g. the baseline directory name).
        target: Which stack/model config was probed, in structural terms only — host,
            port, LLM provider/model name. Never a URL that embeds a credential.
        records: Raw per-question probe results (see :func:`extract_turn_metrics`).
        notes: Free-text notes ABOUT THE MEASUREMENT (e.g. "multi-file coverage was
            3/4, 3/4, 2/4, 2/4") — never question text or an answer. Checked by
            :func:`assert_no_prose` like everything else in the artifact, so a note
            that accidentally quotes the app's answer is still refused.

    Returns:
        The results document, already validated by :func:`assert_no_prose`.

    Raises:
        ProseLeakError: A forbidden field reached the artifact.
    """
    rows = build_probe_rows(records)
    results: dict[str, Any] = {
        "schema_version": 1,
        "run_name": run_name,
        "target": target,
        "licence_note": (
            "Metrics only. No question text, reference answers, app answer prose, "
            "or citation snippets are recorded here — see the module docstring of "
            "tests.eval.harness.probe_metrics (issue #72)."
        ),
        "rows": rows,
        "summary": summarize_probe_rows(rows),
        "notes": list(notes or []),
    }
    assert_no_prose(results)
    return results


def render_probe_table(rows: list[dict[str, Any]]) -> str:
    """The per-turn metrics table, as GitHub-flavoured Markdown."""
    header = [
        "query_id",
        "category",
        "scope",
        "files_consulted",
        "coverage",
        "chunks_used",
        "retrieved",
        "errored",
        "warnings",
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * len(header)) + "|",
    ]
    for row in rows:
        coverage = "n/a" if row["coverage_ratio"] is None else f"{row['coverage_ratio']:.4f}"
        lines.append(
            "| "
            + " | ".join(
                [
                    row["query_id"],
                    row["category"],
                    str(row["scope_size"]),
                    str(row["files_consulted"]),
                    coverage,
                    str(row["chunks_used"]),
                    str(row["retrieved"]),
                    str(row["errored"]),
                    ",".join(row["warning_codes"]) or "-",
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def dumps(results: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, fixed separators, trailing newline.

    Delegates to ``harness.report.dumps`` so a probe baseline and a retrieval
    baseline are byte-formatted the same way — one convention for "what does a
    committed metrics.json look like" in this repo, not two.
    """
    return _dumps_canonical(results)
