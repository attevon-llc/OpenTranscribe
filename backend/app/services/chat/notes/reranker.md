# chat — the reranker (warm-up #1269, thread pool #1270)

Moved intact from `backend/app/services/chat/CLAUDE.md`. Read before touching `reranker.py`, `reranker_warmup.py` or `reranker_threads.py`.

## The reranker is warmed at startup, not on the first chat (issue #1269)

`get_reranker()` is lazy, so the first chat in each fresh API process paid the cold load
inside retrieval. Measured in the dev backend container, fresh process, host load average
59-90 on 48 cores: **9.9-12.3 s** for the first `get_reranker()` (6 runs); the cloud report
was ~15 s. `main.lifespan` → `startup.warmups._start_reranker_warmup()` → `reranker_warmup.start_reranker_warmup()`
now does that load on a daemon thread: the lifespan pays `Thread.start()` (**0.4-4 ms**, 3
runs), and once the thread finishes the first `get_reranker()` is **0.000 s**.

- **Same function, same model, so retrieval cannot change.** The warm-up calls
  `get_reranker(C.CHAT_RERANKER_MODEL)` and scores one throwaway pair; a turn gets the object
  it would have loaded itself.
- **Loads once.** A chat arriving mid-warm-up blocks on `get_reranker`'s lock and gets the
  warm-up's model — it waits for the remainder of the load, it does not start a second one.
- **A failed warm-up does not start the 300 s cooldown** (`start_cooldown_on_failure=False`).
  With the cooldown, a model cache mounted seconds after the API came up would have made every
  chat for five minutes skip reranking — worse than not warming. The request path keeps its
  cooldown.
- **Gated where chat runs and uses it.** Only the ASGI lifespan starts it (Celery imports
  `app.main` without running the lifespan; `TESTING=true` returns before it;
  `test_only_the_api_lifespan_starts_the_warmup` fails on a second caller). Skipped when
  `chat.rag.rerank_enabled` is off, and when the `SystemSettings` row
  `chat.rag.rerank_warmup_enabled` is false (the opt-out, default
  `DEFAULT_CHAT_RAG_RERANK_WARMUP_ENABLED = True`; read once per process, so deliberately not a
  `CHAT_FLAG_REGISTRY` admin-panel flag). A settings read that fails skips the warm-up.
- **Only the English model.** The multilingual arm (`CHAT_RERANKER_MODEL_MULTILINGUAL`) is used
  only for a non-English pool with its own flag on; its first such turn still pays its load.

## The reranker scores on a capped thread pool (issue #1270)

Warm-up fixed the load; the **scoring** was the larger first-chat cost on a busy host. 50
~150-word pairs through `CrossEncoder.predict` at torch's default (24 intra-op threads on the
48-thread dev host) oversubscribes the CPU. `reranker_threads.score_pairs` runs `predict` on a
2-thread pool (`CHAT_RERANKER_SCORING_WORKERS`) whose threads latch a cap, then restores the
process default so the query-embedding model and the output redactor keep torch's own.

**Why a pool, not `set_num_threads` around `predict`.** The call rewrites a PROCESS default that
every thread not yet latched inherits on its first torch op; a latched thread keeps its count.
Measured by `python -m app.scripts.probe_torch_thread_scope` (torch 2.11.0+cu128, default 24,
cap 3, all six verdicts true): setter 3, latched thread stays 24, an unlatched thread and a thread
started after the call both get **3** (the leak), a thread started after the restore gets 24, and
a thread that latched 3 keeps 3 after the restore. So save/restore on a request thread leaks the
cap into other threads and two turns race each other's restore. Re-run the probe when torch is
bumped; `test_real_torch_cap_is_thread_scoped` runs it and the real pool in a subprocess.

**The cap is a deployment setting, not an admin setting.** `CHAT_RERANKER_TORCH_THREADS` (env,
`auto` or a positive integer, `Settings` field with a validator; `.env.example`,
`docs-site/.../environment-variables.md`). It is applied once per process because torch latches
per thread, so it is deliberately not a `SystemSettings` row or `CHAT_FLAG_REGISTRY` entry; the
effective value is shown read-only as `system.chat_reranker_threads` in `/system/stats` (Settings
-> System Statistics -> AI Models). `auto` = `min(CHAT_RERANKER_TORCH_THREADS_AUTO_MAX = 8,
max(1, effective_cpu_count() // 2))`. An explicit integer always wins; `auto` steps aside (no pool,
inline `predict`) when the operator set `OMP_NUM_THREADS`/`TORCH_NUM_THREADS`. A pool that cannot
start (no torch, initializer error) logs once and falls back to inline `predict`, i.e. today's
behaviour. Code that calls `get_reranker(...).predict` directly bypasses the cap: call
`score_pairs`. The warm-up probe goes through `score_pairs`, so the pool starts at API startup.

**Measured 2026-10-25** (`python -m app.scripts.benchmark_reranker_threads`, raw mode, 50 pairs,
7 runs, medians, one fresh process per N, torch 2.11.0+cu128, 48 logical CPUs). The host is
shared and its load average moved 19 -> 133 during the runs (the benchmark's own threads
included), so read the ratios, not the absolute seconds:

| 1 caller, median s | N=2 | 4 | 8 | 16 | torch default (24) |
|---|---|---|---|---|---|
| ambient host (load 19-46) | 2.95 | 1.81 | 1.27 | 1.27 | 2.38 |
| + 40 busy-loop processes (load 83-110) | 8.67 | 4.63 | 4.24 | 5.34 | 7.73 |

With 2 concurrent callers the default measured 8.0 s (ambient) and 11.8 s (loaded) against 2.4 s
and 8.1 s at 8. The pre-registered rule (`pick_cap`: smallest N within 10% of the best loaded
median and 25% of the best idle median) picks **8** for both 1 and 2 callers, hence
`AUTO_MAX = 8`. Scores differ from the default by at most 2.9e-6 and the order is identical at
every N. Through the real pool (`--mode production`, `auto` = 8) against bare predict at the
default, back to back at ambient load 40-100: 1 caller 1.03/1.24 s vs 0.72/0.72 s, 2 callers
1.33/1.69 s vs 1.91/2.15 s. **The cap costs a single turn roughly 0.3-0.5 s on a quiet 48-thread
host and pays back under contention and concurrency**; a deployment that is dedicated and quiet can
set a larger integer. The `--affinity 0-3` scenario was dominated by other tenants on those four
CPUs (load 46-133) and did not discriminate between N.
Tests: `tests/unit/test_chat_reranker_threads.py`, `tests/unit/test_chat_reranker_warmup.py`.
