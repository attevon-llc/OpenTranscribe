"""Benchmark the chat reranker at different torch thread counts (issue #1270).

Scores a fixed synthetic pool through the same ``CrossEncoder`` load production uses
(``device="cpu"``, ``max_length=512``) once per thread count N, each in a FRESH
subprocess so no latched torch state carries across runs::

    python -m app.scripts.benchmark_reranker_threads --threads 2,4,8,16,default \
        --runs 7 --pairs 50 --concurrency 1,2 --out /tmp/rr.json

Run it inside the backend container (``./opentr.sh shell backend``), or on a host venv
with ``TEMP_DIR``/``DATA_DIR``/``MODELS_DIR`` pointing at writable directories and
``HF_HOME``/``HF_HUB_OFFLINE=1`` at a populated model cache. No GPU is used and no dev data
is read: passages come from a committed word list with a fixed seed.

Modes:

``raw``         the issue's experiment: ``torch.set_num_threads(N)`` before any torch work,
                then bare ``predict`` (``default`` leaves torch alone).
``production``  ``reranker.rerank`` with ``CHAT_RERANKER_TORCH_THREADS=N`` in the worker's
                environment, i.e. through the scoring pool. ``default`` is not available
                here; use ``auto`` or an integer.

``--concurrency`` runs that many scoring calls at once (concurrent chat turns).
``--background-load K`` starts K busy-loop children that stop on a shared Event (never a
signal), so a loaded host is repeatable on an idle box. ``--affinity 0-3`` pins each worker
to those CPUs to simulate a small host.

Every run records the load average before and after. The report gives, per
(mode, concurrency, N): median and p90 latency, ``max |delta score|`` against the
``default`` (raw) or first (production) row, and whether the order is identical.

Choice rule (pre-registered, see :func:`pick_cap`): the cap is the smallest N whose LOADED
median is within 10% of the best loaded median AND whose IDLE median is within 25% of the
best idle median. Defines no Celery task and never imports ``app.main``.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any

_QUERY = "what did the team decide about the quarterly budget and the hiring plan?"
_VOCAB = (
    "budget quarter review team decision hiring plan customer release schedule risk vendor "
    "contract meeting action owner deadline revenue forecast support ticket migration "
    "database server latency incident postmortem roadmap priority feature design prototype "
    "feedback survey interview candidate offer onboarding training policy compliance audit "
    "security access password rotation backup restore network storage cost invoice payment "
    "approval legal privacy consent retention export dashboard metric target growth churn "
    "pricing discount partner launch marketing campaign webinar article draft edit publish "
    "translate caption transcript speaker"
)
_WORDS = tuple(_VOCAB.split())
_SEED = 1270
LOADED_TOLERANCE = 1.10
IDLE_TOLERANCE = 1.25


@dataclass
class _Hit:
    """Minimal stand-in for a retrieval hit: only what ``rerank`` reads and writes."""

    content: str
    score: float = 0.0
    language: str = "en"


def synthetic_passages(count: int, words: int = 150) -> list[str]:
    """Deterministic ~``words``-word passages. Same output on every host and run."""
    # Index arithmetic rather than a PRNG: identical on every host and Python version, and
    # nothing here needs randomness, only variety between passages.
    n = len(_WORDS)
    return [
        " ".join(_WORDS[(_SEED + p * 37 + j * j * 11 + j * 7) % n] for j in range(words))
        for p in range(count)
    ]


def pick_cap(idle: dict[int, float], loaded: dict[int, float]) -> int | None:
    """Apply the pre-registered rule to ``{threads: median seconds}`` maps.

    Returns:
        The smallest N within 10% of the best loaded median and 25% of the best idle
        median, or None when no N satisfies both.
    """
    best_idle = min(idle.values())
    best_loaded = min(loaded.values())
    for n in sorted(set(idle) & set(loaded)):
        if loaded[n] <= best_loaded * LOADED_TOLERANCE and idle[n] <= best_idle * IDLE_TOLERANCE:
            return n
    return None


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(pct * (len(ordered) - 1)))]


def _worker(args: argparse.Namespace) -> dict[str, Any]:
    """One measurement in this (fresh) process. Prints nothing but its JSON result."""
    concurrency = int(args.concurrency)
    if args.affinity:
        lo, _, hi = args.affinity.partition("-")
        os.sched_setaffinity(0, range(int(lo), int(hi or lo) + 1))
    from app.utils.cpu_budget import effective_cpu_count

    if args.mode == "production":
        os.environ["CHAT_RERANKER_TORCH_THREADS"] = args.threads
    import torch

    if args.mode == "raw" and args.threads != "default":
        torch.set_num_threads(int(args.threads))
    from sentence_transformers import CrossEncoder

    from app.core import constants as c

    model = CrossEncoder(c.CHAT_RERANKER_MODEL, device="cpu", max_length=512)
    passages = synthetic_passages(args.pairs)
    pairs = [(_QUERY, p) for p in passages]

    if args.mode == "production":
        from app.services.chat import reranker
        from app.services.chat import reranker_threads

        reranker._rerankers[c.CHAT_RERANKER_MODEL] = model
        reranker_threads.score_pairs(model, [pairs[0]])  # starts the pool, like warm-up

        def call() -> list[float]:
            hits = [_Hit(p) for p in passages]
            ranked = reranker.rerank(_QUERY, hits, max_pairs=args.pairs)
            by_content = {id(h): h.score for h in ranked}
            return [by_content[id(h)] for h in hits]

        applied: Any = reranker_threads.resolve_thread_cap().threads
    else:

        def call() -> list[float]:
            return [float(s) for s in model.predict(pairs)]

        applied = None if args.threads == "default" else int(args.threads)

    def warm() -> None:
        model.predict([pairs[0]])

    warmers = [threading.Thread(target=warm) for _ in range(concurrency)]
    for t in warmers:
        t.start()
    for t in warmers:
        t.join()

    latencies: list[float] = []
    scores: list[float] = []
    for _ in range(args.runs):
        results: list[list[float]] = [[] for _ in range(concurrency)]
        durations = [0.0] * concurrency

        def one(i: int, results: list[list[float]] = results, durations: list[float] = durations):
            started = time.perf_counter()
            results[i] = call()
            durations[i] = time.perf_counter() - started

        threads = [threading.Thread(target=one, args=(i,)) for i in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        latencies.extend(durations)
        scores = results[0]
    return {
        "latencies_s": [round(v, 4) for v in latencies],
        "scores": scores,
        "effective_cpu_count": effective_cpu_count(),
        "applied_threads": applied,
        "torch_version": torch.__version__,
    }


def _spin(stop: Any) -> None:
    while not stop.is_set():
        for _ in range(20000):
            pass


def _worker_command(args: argparse.Namespace, threads: str, concurrency: int) -> list[str]:
    cmd = [sys.executable, "-m", "app.scripts.benchmark_reranker_threads", "--worker"]
    cmd += ["--threads", threads, "--runs", str(args.runs), "--pairs", str(args.pairs)]
    cmd += ["--concurrency", str(concurrency), "--mode", args.mode]
    return cmd + (["--affinity", args.affinity] if args.affinity else [])


def _run_all(args: argparse.Namespace) -> dict[str, Any]:
    thread_values = [t.strip() for t in args.threads.split(",") if t.strip()]
    concurrencies = [int(c) for c in args.concurrency.split(",")]
    stop = multiprocessing.Event()
    spinners = [
        multiprocessing.Process(target=_spin, args=(stop,)) for _ in range(args.background_load)
    ]
    rows: list[dict[str, Any]] = []
    try:
        for p in spinners:
            p.start()
        time.sleep(2 if spinners else 0)
        for conc in concurrencies:
            baseline: list[float] | None = None
            for threads in thread_values:
                load_before = os.getloadavg()
                proc = subprocess.run(  # noqa: S603  # nosec B603 - argv is sys.executable plus literals
                    _worker_command(args, threads, conc),
                    capture_output=True,
                    text=True,
                    check=False,
                )
                load_after = os.getloadavg()
                if proc.returncode != 0:
                    raise SystemExit(f"worker failed (threads={threads}): {proc.stderr[-800:]}")
                out = json.loads(proc.stdout.strip().splitlines()[-1])
                scores = out.pop("scores")
                if baseline is None:
                    baseline = scores
                lat = out["latencies_s"]
                rows.append(
                    {
                        "mode": args.mode,
                        "concurrency": conc,
                        "threads": threads,
                        "median_s": round(statistics.median(lat), 3),
                        "p90_s": round(_percentile(lat, 0.9), 3),
                        "max_abs_score_delta": max(
                            abs(a - b) for a, b in zip(scores, baseline, strict=True)
                        ),
                        "order_identical": _order(scores) == _order(baseline),
                        "loadavg_before": [round(v, 2) for v in load_before],
                        "loadavg_after": [round(v, 2) for v in load_after],
                        **out,
                    }
                )
    finally:
        stop.set()
        for p in spinners:
            p.join(timeout=30)
    return {
        "args": {k: getattr(args, k) for k in ("threads", "runs", "pairs", "concurrency", "mode")}
        | {"background_load": args.background_load, "affinity": args.affinity},
        "cpu_count": os.cpu_count(),
        "rows": rows,
    }


def _order(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: (-scores[i], i))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--threads", default="2,4,8,16,default")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--pairs", type=int, default=50)
    parser.add_argument("--concurrency", default="1")
    parser.add_argument("--mode", choices=("raw", "production"), default="raw")
    parser.add_argument("--background-load", type=int, default=0)
    parser.add_argument("--affinity", default="")
    parser.add_argument("--out", default="")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.worker:
        print(json.dumps(_worker(args)))
        return 0
    report = _run_all(args)
    text = json.dumps(report, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    for r in report["rows"]:
        print(
            f"{r['mode']:10} c={r['concurrency']} N={r['threads']:>7} median={r['median_s']:7.3f}s "
            f"p90={r['p90_s']:7.3f}s dscore={r['max_abs_score_delta']:.2e} "
            f"order_same={r['order_identical']} load={r['loadavg_before'][0]}->{r['loadavg_after'][0]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
