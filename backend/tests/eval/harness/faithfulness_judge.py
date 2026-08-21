"""RAGAS ``faithfulness`` — the reference-free half of the judged tier (#463, #518).

**The judged tier is TWO modules, measuring two different things:**

* :mod:`tests.eval.harness.answer_judge` — the **label judge** (FULL/PARTIAL/NONE/
  REFUSED), reference-based: is the human reference's content present in the answer?
  Runs in ``backend/venv`` with a plain OpenAI-compatible call; no ragas anywhere.
  It SUPERSEDED RAGAS ``answer_correctness`` (also reference-based — two paths for
  one axis is the anti-pattern this repo's conventions forbid), which is why this
  module scores faithfulness and nothing else.
* **This module** — RAGAS ``faithfulness``, reference-FREE: does the answer say only
  what the RETRIEVED CONTEXT supports? Nothing in the label judge covers this axis;
  a model can be faithful to bad context (high faithfulness, wrong answer) or
  unfaithful to good context (low faithfulness, accidentally right). The two axes
  are reported separately and never merged into one score.

**Opt-in, D6-safe.** Everything here degrades to an explicit "not measured" value —
never a fabricated score, never a silent skip that reads as a clean pass — when the
eval judge venv is absent. :func:`is_available` and :func:`build_judge` are the only
two functions that ever need to know whether the judge is usable.

⚠️ **``ragas`` is NEVER imported by this module, or by anything in ``backend/venv``.**
It lives in a **separate interpreter**, ``backend/venv-eval/`` (gitignored), talked to
over a **subprocess boundary**: this module writes ``{question, answer, contexts,
ground_truth}`` JSONL records, invokes ``backend/venv-eval/bin/python
harness/judge_runner.py`` as a subprocess, and reads ``{query_id, score}`` JSONL back.
``judge_runner.py`` is the ONE file in this whole tier that imports ``ragas``.

**Why the split, not a relaxed pin.** ``ragas==0.4.3`` requires ``instructor``
unconditionally; ``instructor`` requires ``openai<3.0.0``. ``backend/requirements.txt``
pins ``openai==3.3.0`` — adding ragas to ``backend/venv`` would silently DOWNGRADE the
shared venv's openai for every other consumer (the exact divergence issue #492 exists
to prevent, relocated to venv-vs-venv). A subprocess boundary makes the two dependency
graphs never need to be compatible. This is a permanent structural fix, not a
workaround — it must not be "simplified" back into one venv later.

**``NaN`` is a counted failure, never dropped.** A per-record judge failure comes back
from ``judge_runner.py`` as ``"score": null``, read here as ``float("nan")`` —
:func:`_finalize` counts it in ``judge_failures`` and excludes it from the mean, a
different, explicit thing from silently shrinking the denominator. An INFRASTRUCTURE
failure — the subprocess exits non-zero or times out — raises ``RuntimeError`` instead,
because collapsing "the judge said no" into "the harness is broken" would misreport an
outage as a batch of low scores.

**Empty context is refused, not scored.** Faithfulness against empty
``retrieved_contexts`` degenerates toward ≈1.0 (there is nothing to be unfaithful TO),
which reads as "perfect" for a turn where retrieval failed — the opposite of what
happened. :func:`evaluate_faithfulness` raises before any subprocess runs.

**Judge scores must never enter the byte-identical ``metrics.json`` claim** — see
``harness/report.py``'s ``judged_answer_quality`` block, written to its own artifact.
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

from tests.eval.harness.answer_judge import JUDGE_TEMPERATURE

logger = logging.getLogger(__name__)

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
        concurrency: Max simultaneous judge calls **inside the subprocess**. Matched
            to the server's own ``--max-num-seqs``, never guessed — a mismatch queues
            silently on the server side and every latency number in ``runinfo.json``
            becomes a statement about the queue, not the judge.
    """

    model: str
    base_url: str
    api_key: str = "not-needed"
    concurrency: int = 4

    def as_provenance(self) -> dict[str, Any]:
        """Judge identity for ``runinfo.json`` — never ``metrics.json`` (module
        docstring). No secret: ``api_key`` is deliberately omitted."""
        return {
            "model": self.model,
            "base_url": self.base_url,
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
            f"faithfulness_judge.build_judge: {_EVAL_VENV_PYTHON} does not exist. Call "
            "is_available() first and record 'not measured' instead of calling this "
            "— never let this ImportError surface as a crashed run. Create the venv "
            "with: python3.12 -m venv backend/venv-eval && "
            "backend/venv-eval/bin/pip install -r backend/requirements-eval-judge.txt"
        )
    return Judge(config=config)


def _run_judge_subprocess(records: list[dict[str, Any]], config: JudgeConfig) -> dict[str, float]:
    """Score ``records`` by invoking ``judge_runner.py`` under ``backend/venv-eval``.

    Args:
        records: each ``{"query_id", "question", "answer", "contexts", "ground_truth"}``
            — the full four-field shape; the runner script reads only the fields
            faithfulness needs.
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
            f"faithfulness_judge._run_judge_subprocess: {_EVAL_VENV_PYTHON} does not exist."
        )
    with tempfile.TemporaryDirectory(prefix="faithfulness_judge_") as tmp:
        input_path = Path(tmp) / "records.jsonl"
        output_path = Path(tmp) / "scores.jsonl"
        with input_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record) + "\n")

        command = [
            str(_EVAL_VENV_PYTHON),
            str(_JUDGE_RUNNER_SCRIPT),
            "--mode",
            "faithfulness",
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
                f"faithfulness_judge: judge_runner.py timed out after "
                f"{_SUBPROCESS_TIMEOUT_S}s ({len(records)} records)"
            ) from exc

        if completed.returncode != 0:
            raise RuntimeError(
                f"faithfulness_judge: judge_runner.py exited {completed.returncode}: "
                f"{completed.stderr.strip()}"
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
    """Per-query and aggregate judge scores, plus failure counts.

    ``aggregate`` is the mean over queries that produced a real (non-NaN) score.
    ``judge_failures`` lists the NaNs — reported alongside the mean, never folded
    into it and never silently absent.
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
    per_query = _run_judge_subprocess(records, judge.config)
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
