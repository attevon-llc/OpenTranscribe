"""Probe how far ``torch.set_num_threads`` reaches, per thread (issue #1270).

Whether a per-call thread cap can be applied safely around ``CrossEncoder.predict``
depends on one question: does ``torch.set_num_threads(n)`` on one thread change what
OTHER threads see? This script answers it on the host it runs on, and prints JSON so
the answer is a recorded measurement rather than a belief::

    python -m app.scripts.probe_torch_thread_scope [--cap 3] [--matmul-iters 20]

Scenarios, each on its own thread so no thread's first torch call is made by the main
thread (importing torch alone does not latch a count):

``latched``      ran a matmul BEFORE the cap was set; its count must not move.
``setter``       called ``set_num_threads(cap)``; must report ``cap``.
``unlatched``    started before the cap but first touched torch after it; reports
                 whether an unlatched thread inherits the cap (the leak that rules out
                 save/restore around a request thread).
``new_after``    started after the cap was set.
``restorer``     a short-lived thread that restored the default afterwards.
``new_restored`` started after the restore; must report the default again.

Each thread also times a fixed matmul loop, so the reported count can be compared with
what the parallel region actually does. Timing is noisy on a loaded host: the load
average is recorded before and after, and the verdicts use counts, never timings.

Imports only ``torch`` and the CPU-budget helper. It defines no Celery task and never
imports ``app.main`` or ``app.core.celery``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any

DEFAULT_CAP = 3
DEFAULT_MATMUL_ITERS = 20
_MATMUL_SIZE = 768


def _host() -> dict[str, Any]:
    from app.utils.cpu_budget import effective_cpu_count

    load = os.getloadavg()
    return {
        "cpu_count": os.cpu_count(),
        "effective_cpu_count": effective_cpu_count(),
        "loadavg": [round(v, 2) for v in load],
        "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        "torch_num_threads": os.environ.get("TORCH_NUM_THREADS"),
    }


def _timed_matmul(torch: Any, iters: int) -> float:
    a = torch.randn(_MATMUL_SIZE, _MATMUL_SIZE)
    b = torch.randn(_MATMUL_SIZE, _MATMUL_SIZE)
    started = time.perf_counter()
    for _ in range(iters):
        a @ b
    return time.perf_counter() - started


def _run_on_thread(name: str, fn: Any) -> Any:
    """Run ``fn`` on a fresh thread and return its result (re-raising its error)."""
    box: dict[str, Any] = {}

    def body() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            box["error"] = exc

    thread = threading.Thread(target=body, name=name)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def run_probe(cap: int = DEFAULT_CAP, matmul_iters: int = DEFAULT_MATMUL_ITERS) -> dict[str, Any]:
    """Run every scenario and return counts, timings and verdicts."""
    import torch

    host_before = _host()
    # Counts only. The default is read from a fresh thread: that is what a thread nothing
    # has latched sees, and it is what a restore has to put back.
    default = _run_on_thread("probe-default", torch.get_num_threads)

    def latch_and_time() -> dict[str, Any]:
        elapsed = _timed_matmul(torch, matmul_iters)
        return {"threads": torch.get_num_threads(), "matmul_s": round(elapsed, 4)}

    latched_before = _run_on_thread("probe-latched", latch_and_time)

    # ``latched`` must keep its count while others change the default, so it is parked.
    latched_ready = threading.Event()
    latched_release = threading.Event()
    latched_result: dict[str, Any] = {}

    def latched_body() -> None:
        latched_result["before"] = latch_and_time()
        latched_ready.set()
        latched_release.wait(timeout=60)
        latched_result["after"] = latch_and_time()

    latched_thread = threading.Thread(target=latched_body, name="probe-latched-parked")
    latched_thread.start()
    latched_ready.wait(timeout=60)

    unlatched_go = threading.Event()
    unlatched_result: dict[str, Any] = {}

    def unlatched_body() -> None:
        unlatched_go.wait(timeout=60)
        unlatched_result.update(latch_and_time())

    unlatched_thread = threading.Thread(target=unlatched_body, name="probe-unlatched")
    unlatched_thread.start()

    def set_cap() -> dict[str, Any]:
        torch.set_num_threads(cap)
        return latch_and_time()

    setter = _run_on_thread("probe-setter", set_cap)
    unlatched_go.set()
    unlatched_thread.join()
    new_after = _run_on_thread("probe-new-after", latch_and_time)

    _run_on_thread("probe-restorer", lambda: torch.set_num_threads(default))
    new_restored = _run_on_thread("probe-new-restored", latch_and_time)
    latched_release.set()
    latched_thread.join()

    # A thread that latched ``cap`` and is still alive after the restore, on the real
    # production shape: the setter's own count is read again from the same thread.
    def set_then_survive_restore() -> dict[str, Any]:
        torch.set_num_threads(cap)
        first = torch.get_num_threads()
        _run_on_thread("probe-restorer-2", lambda: torch.set_num_threads(default))
        return {"before_restore": first, "after_restore": torch.get_num_threads()}

    survivor = _run_on_thread("probe-survivor", set_then_survive_restore)
    _run_on_thread("probe-restorer-3", lambda: torch.set_num_threads(default))

    counts = {
        "default": default,
        "cap": cap,
        "latched_before_cap": latched_before["threads"],
        "latched_parked_before": latched_result["before"]["threads"],
        "latched_parked_after": latched_result["after"]["threads"],
        "setter": setter["threads"],
        "unlatched": unlatched_result["threads"],
        "new_after": new_after["threads"],
        "new_restored": new_restored["threads"],
        "survivor_before_restore": survivor["before_restore"],
        "survivor_after_restore": survivor["after_restore"],
    }
    verdicts = {
        "setter_reports_cap": counts["setter"] == cap,
        "latched_thread_unmoved": counts["latched_parked_after"] == counts["latched_parked_before"],
        "unlatched_thread_inherits_cap": counts["unlatched"] == cap,
        "new_thread_after_cap_inherits_cap": counts["new_after"] == cap,
        "restore_returns_new_threads_to_default": counts["new_restored"] == default,
        "latched_thread_survives_restore": counts["survivor_after_restore"] == cap,
    }
    return {
        "torch_version": torch.__version__,
        "python": sys.version.split()[0],
        "host_before": host_before,
        "host_after": _host(),
        "counts": counts,
        "timings_s": {
            "latched_parked_before": latched_result["before"]["matmul_s"],
            "setter": setter["matmul_s"],
            "unlatched": unlatched_result["matmul_s"],
            "new_after": new_after["matmul_s"],
            "new_restored": new_restored["matmul_s"],
        },
        "verdicts": verdicts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--cap", type=int, default=DEFAULT_CAP)
    parser.add_argument("--matmul-iters", type=int, default=DEFAULT_MATMUL_ITERS)
    args = parser.parse_args(argv)
    if args.cap < 1 or args.matmul_iters < 1:
        parser.error("--cap and --matmul-iters must be positive")
    print(json.dumps(run_probe(args.cap, args.matmul_iters), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
