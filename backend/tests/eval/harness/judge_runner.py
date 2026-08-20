#!/usr/bin/env python3
"""RAGAS judge subprocess — runs ONLY under ``backend/venv-eval``, never ``backend/venv``.

This is the ONE file in the answer-quality tier that imports ``ragas`` (and therefore
``instructor``, which pins ``openai<3.0.0`` — see ``requirements-eval.txt``'s header for the
exact version chain). ``backend/venv``'s own ``openai==3.3.0`` must never be in the same
process as that import, so the boundary is a subprocess: this script knows nothing about the
app, the harness package, or the rest of this eval suite — it reads four fields per record
(``question``, ``answer``, ``contexts``, ``ground_truth``) from a JSONL file, scores them, and
writes ``{"query_id", "score"}`` JSONL back. It is invoked exactly as
``backend/venv-eval/bin/python judge_runner.py --mode ... --input ... --output ...`` by
``harness/answer_judge.py``, which runs in ``backend/venv``.

A record's judge failure (a model error, a timeout, a structured-output parse failure) writes
``"score": null`` for that record — read back as ``NaN`` by the caller, a COUNTED failure, never
a crash of the whole batch (``answer_judge.py``'s module docstring: "NaN is a counted failure,
never dropped"). An INFRASTRUCTURE failure (the provider is unreachable, ragas itself won't
import, the input file is malformed) exits non-zero with a message on stderr — the caller
distinguishes the two explicitly, because collapsing "the judge said no" into "the harness is
broken" would misreport an infrastructure outage as a batch of low scores.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

#: Pinned identically to `answer_judge.JUDGE_TEMPERATURE` — kept as a literal here rather
#: than imported, because this script must not import anything from `backend/venv`'s package
#: tree (the whole point of the subprocess boundary is that it doesn't need to).
DEFAULT_TEMPERATURE = 0.0


def _read_records(path: Path) -> list[dict[str, Any]]:
    records = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _write_results(path: Path, results: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row) + "\n")


def _build_metric(
    mode: str, *, model: str, base_url: str, api_key: str, embedding_model: str, temperature: float
) -> Any:
    from openai import AsyncOpenAI
    from ragas.embeddings.huggingface_provider import HuggingFaceEmbeddings
    from ragas.llms.base import llm_factory
    from ragas.metrics.collections import AnswerCorrectness
    from ragas.metrics.collections import Faithfulness

    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    llm = llm_factory(model, client=client, temperature=temperature)
    if mode == "faithfulness":
        return Faithfulness(llm=llm)
    embeddings = HuggingFaceEmbeddings(model=embedding_model, use_api=False)
    return AnswerCorrectness(llm=llm, embeddings=embeddings)


async def _score_one(metric: Any, mode: str, record: dict[str, Any]) -> dict[str, Any]:
    try:
        if mode == "faithfulness":
            result = await metric.ascore(
                user_input=record["question"],
                response=record["answer"],
                retrieved_contexts=list(record["contexts"]),
            )
        else:
            result = await metric.ascore(
                user_input=record["question"],
                response=record["answer"],
                reference=record["ground_truth"],
            )
        return {"query_id": record["query_id"], "score": float(result.value)}
    except Exception as exc:  # noqa: BLE001 - a per-record judge failure is data (null), not a crash
        print(f"[judge_runner] record {record.get('query_id')!r} failed: {exc}", file=sys.stderr)
        return {"query_id": record["query_id"], "score": None}


async def _score_all(
    metric: Any, mode: str, records: list[dict[str, Any]], *, concurrency: int
) -> list[dict[str, Any]]:
    semaphore = asyncio.Semaphore(concurrency)

    async def _bounded(record: dict[str, Any]) -> dict[str, Any]:
        async with semaphore:
            return await _score_one(metric, mode, record)

    return await asyncio.gather(*(_bounded(record) for record in records))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("faithfulness", "answer_correctness"))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", default="not-needed")
    parser.add_argument("--embedding-model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    args = parser.parse_args(argv)

    try:
        records = _read_records(args.input)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[judge_runner] failed to read {args.input}: {exc}", file=sys.stderr)
        return 2
    if not records:
        print("[judge_runner] input file has no records", file=sys.stderr)
        return 2

    try:
        metric = _build_metric(
            args.mode,
            model=args.model,
            base_url=args.base_url,
            api_key=args.api_key,
            embedding_model=args.embedding_model,
            temperature=args.temperature,
        )
    except ImportError as exc:
        print(
            f"[judge_runner] ragas (or one of its optional providers, e.g. "
            f"sentence-transformers for the local embedder) is not importable in "
            f"this interpreter: {exc}",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:  # noqa: BLE001 - construction failure is infra, not a record failure
        print(f"[judge_runner] failed to build the {args.mode} judge: {exc}", file=sys.stderr)
        return 3

    results = asyncio.run(_score_all(metric, args.mode, records, concurrency=args.concurrency))
    _write_results(args.output, results)
    failures = sum(1 for row in results if row["score"] is None)
    print(
        f"[judge_runner] scored {len(results)} records ({failures} judge failures) in "
        f"mode={args.mode}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
