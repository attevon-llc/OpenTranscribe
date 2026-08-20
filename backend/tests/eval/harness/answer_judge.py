"""LLM-judged answer quality — RAGAS ``faithfulness`` + ``answer_correctness`` (#463).

**Opt-in, D6-safe.** Every function here degrades to an explicit "not measured" value
— never a fabricated score, never a silent skip that reads as a clean pass — when the
eval judge venv is absent or no judge is configured. D6 makes the no-LLM deployment
first-class; a judged tier that raised or hung with no provider configured would
contradict that, so :func:`is_available` and :func:`build_judge` are the only two
functions that ever need to know whether the judge is usable, and every caller checks
one of them before scoring anything.

⚠️ **``ragas`` is NEVER imported by this module, or by anything in ``backend/venv``.**
It lives in a **separate interpreter**, ``backend/venv-eval/`` (gitignored), talked to
over a **subprocess boundary**: this module writes ``{question, answer, contexts,
ground_truth}`` JSONL records, invokes ``backend/venv-eval/bin/python
harness/judge_runner.py`` as a subprocess, and reads ``{query_id, score}`` JSONL back.
``judge_runner.py`` is the ONE file in this whole tier that imports ``ragas``.

**Why the split, not a relaxed pin.** ``ragas==0.4.3`` requires ``instructor``
unconditionally (no extras gate); ``instructor==1.15.4`` (the newest version PyPI has,
checked 2026-08-19) requires ``openai<3.0.0,>=2.0.0``. ``backend/requirements.txt``
pins ``openai==3.3.0`` — the app's real LLM client, exercised by host-venv tests and
mypy alike, in a checkout with other agents actively running against ``backend/venv``
at the same time. ``pip install --dry-run`` with ragas added to ``backend/venv``
resolved to ``Would install ... openai-2.54.0`` — i.e. it would have silently
DOWNGRADED the shared venv's openai package for every other consumer. That is the exact
venv/image divergence issue #492 exists to prevent, just relocated to venv-vs-venv.
A subprocess boundary makes the two dependency graphs never need to be compatible,
because they are never the same Python process — this is a permanent structural fix,
not a workaround, and it must not be "simplified" back into one venv later.

**Two axes, reported separately, NEVER merged into one score:**

* **faithfulness** — reference-free: does the answer say only what the RETRIEVED
  CONTEXT supports? Scored against the context a real retrieval turn produced, not
  against QMSum's gold answer. A model can be faithful to bad context (and score
  high while being wrong) or unfaithful to good context (and score low while
  accidentally being right) — this axis answers neither "is it right" nor "was the
  context good", only "did the answer stay inside what it was given".
* **answer_correctness** — reference-based: is the answer actually right, compared
  against QMSum's human-written gold answer via a LOCAL sentence-transformers
  embedder (never a remote embedding call — the judge may be local-only per D6, and
  an embedding call some GPU-less deployment cannot make would silently narrow what
  "local" means).

**``NaN`` is a counted failure, never dropped.** A per-record judge failure (model
error, timeout, structured-output parse failure) comes back from ``judge_runner.py``
as ``"score": null``, read here as ``float("nan")`` — dropping those from the mean's
denominator would silently discard exactly the queries where the judge itself broke,
flattering every aggregate the same way an unanswered query would if excluded instead
of scored. :func:`_finalize` counts a NaN in ``judge_failures`` and does **not**
include it in the measure's mean — a different, explicit thing from silently shrinking
the denominator: the failure rate is itself a reported number, not an absence.

An INFRASTRUCTURE failure — the subprocess exits non-zero (bad input, ragas won't
import in ``venv-eval``, the provider is unreachable) — is a different thing again and
is NOT folded into per-record NaNs: :func:`_run_judge_subprocess` raises
``RuntimeError`` with the subprocess's stderr, because collapsing "the judge said no"
into "the harness is broken" would misreport an outage as a batch of low scores.

**Empty context is refused, not scored.** Faithfulness against an empty
``retrieved_contexts`` degenerates toward ≈1.0 (there is nothing to be unfaithful
TO), which reads as "perfect" for a turn where retrieval failed — the opposite of
what happened. :func:`evaluate_faithfulness` raises rather than sending such a record
to the subprocess at all.

**Judge scores must never enter the byte-identical ``metrics.json`` claim.**
``report.build_results``'s determinism guarantee (same corpus in, byte-identical
file out) cannot hold for a judge whose model, sampling, or even prompt template can
change between runs without this harness's control — see
``harness/report.py``'s ``judged_answer_quality`` block, deliberately written to
its own artifact, never merged into ``rows``/``retrieval_per_query``.
"""

from __future__ import annotations

import json
import logging
import math
import subprocess
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Pinned at every construction call inside ``judge_runner.py`` — never left to a
#: client or library default, which could silently vary between the local vLLM and
#: any other OpenAI-compatible provider a future caller points this at.
JUDGE_TEMPERATURE = 0.0

MEASURES = ("faithfulness", "answer_correctness")

#: ``backend/venv-eval/bin/python`` — sibling of ``backend/venv``, derived from this
#: file's own location rather than a hardcoded absolute path, so this module works
#: from any checkout.
_EVAL_VENV_PYTHON = Path(__file__).resolve().parents[3] / "venv-eval" / "bin" / "python"
_JUDGE_RUNNER_SCRIPT = Path(__file__).resolve().with_name("judge_runner.py")

#: Wall-clock ceiling for one subprocess call, covering every record in the batch.
#: A hang here (a stalled provider, a wedged event loop) must become a clear timeout
#: error, not a silently-forever-running measurement.
_SUBPROCESS_TIMEOUT_S = 1800.0


def eval_venv_python() -> Path:
    """Path to ``backend/venv-eval/bin/python`` — may not exist; see :func:`is_available`."""
    return _EVAL_VENV_PYTHON


def is_available() -> bool:
    """Whether the eval judge venv exists on disk. The one check every caller should
    make before doing anything else in this module.

    Deliberately a cheap on-disk check, not a live ``import ragas`` probe in that
    interpreter — a venv that exists but is missing a package still fails informatively
    inside :func:`_run_judge_subprocess` (``judge_runner.py`` exits 3 with a clear
    stderr message), which is the same failure class as "absent" from this function's
    caller's point of view: not available to score with right now.
    """
    return _EVAL_VENV_PYTHON.is_file()


@dataclass(frozen=True)
class JudgeConfig:
    """Everything :func:`build_judge` needs, and everything a results file records.

    Attributes:
        model: Model name as the OpenAI-compatible server names it (e.g.
            ``"gemma-4-e4b"``).
        base_url: The server's OpenAI-compatible base URL (e.g.
            ``"http://localhost:5195/v1"``).
        api_key: Bearer token, or a placeholder string for a server that does not
            check one — the OpenAI SDK requires a non-empty value even then.
        embedding_model: A local sentence-transformers model name for
            ``answer_correctness``'s similarity term. Never a remote embedding
            endpoint (see module docstring).
        concurrency: Max simultaneous judge calls **inside the subprocess**. Matched
            to the server's own ``--max-num-seqs``, never guessed — a mismatch queues
            silently on the server side and every latency number in ``runinfo.json``
            becomes a statement about the queue, not the judge.
    """

    model: str
    base_url: str
    api_key: str = "not-needed"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    concurrency: int = 4

    def as_provenance(self) -> dict[str, Any]:
        """Judge identity for ``runinfo.json`` — never ``metrics.json`` (module
        docstring). No secret: ``api_key`` is deliberately omitted."""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "embedding_model": self.embedding_model,
            "temperature": JUDGE_TEMPERATURE,
            "concurrency": self.concurrency,
            "engine": "ragas==0.4.3 (Apache-2.0), via backend/venv-eval subprocess",
        }


@dataclass(frozen=True)
class Judge:
    """A validated, ready-to-use judge configuration. No live ragas objects are held
    here — there is nothing to hold across a subprocess boundary; every call builds
    its own subprocess. This exists so a caller passes one value (checked once by
    :func:`build_judge`) instead of re-checking :func:`is_available` at every call."""

    config: JudgeConfig


def build_judge(config: JudgeConfig) -> Judge:
    """Validate that the eval judge venv is present and wrap ``config`` for reuse.

    Raises:
        ImportError: the eval judge venv is absent — call :func:`is_available` first
            and record 'not measured' instead of calling this.
    """
    if not is_available():
        raise ImportError(
            f"answer_judge.build_judge: {_EVAL_VENV_PYTHON} does not exist. Call "
            "is_available() first and record 'not measured' instead of calling this "
            "— never let this ImportError surface as a crashed run. Create the venv "
            "with: python3.12 -m venv backend/venv-eval && "
            "backend/venv-eval/bin/pip install -r backend/requirements-eval-judge.txt"
        )
    return Judge(config=config)


def _run_judge_subprocess(
    mode: str, records: list[dict[str, Any]], config: JudgeConfig
) -> dict[str, float]:
    """Score ``records`` by invoking ``judge_runner.py`` under ``backend/venv-eval``.

    Args:
        mode: ``"faithfulness"`` or ``"answer_correctness"``.
        records: each ``{"query_id", "question", "answer", "contexts", "ground_truth"}``
            — the full four-field shape regardless of ``mode``; the runner script reads
            only the fields its mode needs.
        config: judge identity/settings.

    Returns:
        query_id -> score, with ``float("nan")`` for any record the judge itself
        failed to score (see module docstring — a per-record failure, not an
        infrastructure one).

    Raises:
        ImportError: the eval judge venv is absent.
        RuntimeError: the subprocess exited non-zero (bad input, ragas/provider
            infrastructure failure) or timed out — an INFRASTRUCTURE failure, never
            silently folded into per-record NaNs.
    """
    if not is_available():
        raise ImportError(
            f"answer_judge._run_judge_subprocess: {_EVAL_VENV_PYTHON} does not exist."
        )
    with tempfile.TemporaryDirectory(prefix="answer_judge_") as tmp:
        input_path = Path(tmp) / "records.jsonl"
        output_path = Path(tmp) / "scores.jsonl"
        with input_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

        command = [
            str(_EVAL_VENV_PYTHON),
            str(_JUDGE_RUNNER_SCRIPT),
            "--mode",
            mode,
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--model",
            config.model,
            "--base-url",
            config.base_url,
            "--api-key",
            config.api_key,
            "--embedding-model",
            config.embedding_model,
            "--concurrency",
            str(config.concurrency),
            "--temperature",
            str(JUDGE_TEMPERATURE),
        ]
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv, no shell, trusted interpreter
                command, capture_output=True, text=True, timeout=_SUBPROCESS_TIMEOUT_S
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"answer_judge: judge_runner.py timed out after {_SUBPROCESS_TIMEOUT_S}s "
                f"(mode={mode}, {len(records)} records)"
            ) from exc

        if completed.returncode != 0:
            raise RuntimeError(
                f"answer_judge: judge_runner.py exited {completed.returncode} "
                f"(mode={mode}): {completed.stderr.strip()}"
            )
        if completed.stderr:
            logger.info("judge_runner.py: %s", completed.stderr.strip())

        scores: dict[str, float] = {}
        with output_path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                score = row["score"]
                scores[row["query_id"]] = float("nan") if score is None else float(score)
    return scores


@dataclass
class JudgedResult:
    """Per-query and aggregate judge scores for ONE measure, plus failure counts.

    ``aggregate`` is the mean over queries that produced a real (non-NaN) score.
    ``judge_failures`` is the count of NaNs — reported alongside the mean, never
    folded into it and never silently absent.
    """

    per_query: dict[str, float] = field(default_factory=dict)
    aggregate: float | None = None
    query_count: int = 0
    judge_failures: list[str] = field(default_factory=list)


def score_faithfulness_one(
    judge: Judge, *, question: str, answer: str, contexts: Sequence[str]
) -> float:
    """Faithfulness of ``answer`` to ``contexts`` (reference-free). Convenience
    single-record wrapper around :func:`evaluate_faithfulness` — for a real batch,
    call :func:`evaluate_faithfulness` directly and avoid one subprocess per record.

    Raises:
        ValueError: ``contexts`` is empty (see module docstring).
        ImportError: the eval judge venv is absent.
        RuntimeError: the subprocess failed (infrastructure, not a per-record score).
    """
    result = evaluate_faithfulness(judge, {"_single": (question, answer, contexts)})
    return result.per_query.get("_single", float("nan"))


def score_answer_correctness_one(
    judge: Judge, *, question: str, answer: str, reference: str
) -> float:
    """Correctness of ``answer`` against QMSum's human ``reference`` answer.
    Convenience single-record wrapper — see :func:`score_faithfulness_one`."""
    result = evaluate_answer_correctness(judge, {"_single": (question, answer, reference)})
    return result.per_query.get("_single", float("nan"))


def evaluate_faithfulness(
    judge: Judge, queries: Mapping[str, tuple[str, str, Sequence[str]]]
) -> JudgedResult:
    """Score faithfulness for every query in ONE subprocess call.

    Args:
        judge: from :func:`build_judge`.
        queries: query id -> ``(question, answer, contexts)``. A query whose
            ``contexts`` is empty is a caller bug and is NOT silently skipped here —
            the ``ValueError`` propagates before any subprocess runs, because
            filtering it out client-side would hide exactly the "retrieval produced
            nothing" case this measure most needs to catch.

    Returns:
        Per-query scores, the mean over non-NaN scores, and the NaN count.

    Raises:
        ValueError: ``queries`` is empty, or any entry has empty ``contexts``.
        ImportError: the eval judge venv is absent.
        RuntimeError: the judge subprocess failed.
    """
    if not queries:
        raise ValueError("evaluate_faithfulness: queries is empty")
    ids = sorted(queries)
    for query_id in ids:
        _question, _answer, contexts = queries[query_id]
        if not contexts or not any(c.strip() for c in contexts):
            raise ValueError(
                f"evaluate_faithfulness: query {query_id!r} has empty contexts — "
                "faithfulness against no context is not a real measurement "
                "(see module docstring)"
            )

    records = [
        {
            "query_id": query_id,
            "question": queries[query_id][0],
            "answer": queries[query_id][1],
            "contexts": list(queries[query_id][2]),
            "ground_truth": "",
        }
        for query_id in ids
    ]
    per_query = _run_judge_subprocess("faithfulness", records, judge.config)
    return _finalize(ids, per_query)


def evaluate_answer_correctness(
    judge: Judge, queries: Mapping[str, tuple[str, str, str]]
) -> JudgedResult:
    """Score answer_correctness for every query in ONE subprocess call.

    Args:
        judge: from :func:`build_judge`.
        queries: query id -> ``(question, answer, gold_reference)``.

    Raises:
        ValueError: ``queries`` is empty.
        ImportError: the eval judge venv is absent.
        RuntimeError: the judge subprocess failed.
    """
    if not queries:
        raise ValueError("evaluate_answer_correctness: queries is empty")
    ids = sorted(queries)
    records = [
        {
            "query_id": query_id,
            "question": queries[query_id][0],
            "answer": queries[query_id][1],
            "contexts": [],
            "ground_truth": queries[query_id][2],
        }
        for query_id in ids
    ]
    per_query = _run_judge_subprocess("answer_correctness", records, judge.config)
    return _finalize(ids, per_query)


def _finalize(ids: list[str], per_query: dict[str, float]) -> JudgedResult:
    result = JudgedResult(per_query=per_query, query_count=len(ids))
    real_scores = []
    for query_id in ids:
        score = per_query[query_id]
        if math.isnan(score):
            result.judge_failures.append(query_id)
        else:
            real_scores.append(score)
    result.aggregate = (sum(real_scores) / len(real_scores)) if real_scores else None
    return result
