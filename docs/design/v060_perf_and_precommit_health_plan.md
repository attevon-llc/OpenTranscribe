# #1270 + #1271: reranker torch-thread cap and pre-commit mypy environment parity. Plan

**Status:** PLAN ONLY. Nothing here is implemented.
**Written:** 2026-10-25, against `master` @ `a236125a` (merge of PR #1272).
**Issues:** #1270 (perf(chat): limit torch threads for the CPU cross-encoder reranker), #1271
(chore(precommit): mypy hook environment disagrees with backend/venv). Both milestone v0.6.0.

This document is written for an implementation agent with **no** conversation history. Paths are
relative to the repo root. Re-verify every `path:LINE` before editing; lines drift. The two issues
share no files and no runtime surface: they are one plan only because both are "make a measured
number honest" chores. **Track P (#1270) and track M (#1271) can run in parallel worktrees.**

---

## 0. TL;DR

- **#1270.** `torch.set_num_threads` is not safely scopable with save/restore around
  `model.predict` (measured below: it rewrites a process-wide default that every not-yet-started
  torch thread inherits). The design is a **small dedicated scoring pool** owned by the reranker,
  whose threads latch a capped thread count once at start, after which the process default is
  restored. Query embeddings and output redaction keep torch's default. Cap =
  `min(CHAT_RERANKER_TORCH_THREADS_MAX, max(1, effective_cpu_count() // 2))`, MAX chosen by a
  committed benchmark script (issue measured 7-16 s at 24 threads vs 3.4-5.5 s at 4/8 under load).
  Ranking can only move by float summation order; proven by the benchmark's score-diff report, not
  by `pytest tests/eval` (which never loads the real model).
- **#1271.** The hook/venv disagreement is wider than the issue says: different mypy versions,
  missing py.typed packages beyond the three named, stub sets that differ **in both directions**,
  and an acceptance check that compares different file sets. Measured today: venv mypy over the
  hook's exact 2,269-file scope reports **55 errors in 23 files** (22 test files + `alembic/env.py`;
  `backend/app` is clean). Fix those with real types first (mergeable alone, since venv mypy already
  flags them), then align the hook, add a **derived** parity test, and a negative-control script
  that CI runs.

---

## 1. Verified current state

### 1.1 Reranker (#1270)

| Fact | Evidence |
|---|---|
| Scoring is a bare `model.predict(pairs)` on the **request thread** | `backend/app/services/chat/reranker.py:225-230` |
| Model built CPU-only, `max_length=512`; the literal `device="cpu"` is pinned by an AST scan | `reranker.py:142-149`, `backend/tests/unit/test_laptop_deployment_invariant.py:161` |
| Called once per turn, from the retrieval stage (fan-out legs merge first, rerank once) | `backend/app/services/chat/retrieval.py:404-408`, `legs.py` module docstring |
| Warm-up loads the English model on a daemon thread and scores one probe pair | `backend/app/services/chat/reranker_warmup.py:66-90` (probe at `:85`) |
| Constants block for reranking | `backend/app/core/constants.py:1386-1394` |
| No `set_num_threads` anywhere in the API process. The only callers are Celery: prefork children (`core/celery.py:900-918`, env wins) and the redaction worker (`core/celery.py:1237-1244`, plus `OMP_NUM_THREADS=${REDACTION_TORCH_THREADS:-8}` at `docker-compose.yml:661-662`) | `rg set_num_threads backend/app` |
| Other torch users **in the API process**: the query-embedding `SentenceTransformer` singleton (`backend/app/services/opensearch_service/client.py:102-120`) and the chat output redactor's detectors (`services/chat/output_redactor.py` -> `services/redaction/service.py:97`). Scan-time redaction runs in the redaction worker, not the API | as cited |
| Cgroup/affinity-aware CPU count helper already exists | `backend/app/utils/cpu_budget.py:29-38` |

**Thread-scope semantics, measured 2026-10-25** (torch `2.11.0+cu128`, `backend/venv`, CPU only,
`CUDA_VISIBLE_DEVICES=`; OpenMP + MKL build, `at::get_num_threads()` = 24 on this 48-logical-core host):

1. Thread B ran a matmul (latched), then thread A called `torch.set_num_threads(3)`: **A = 3, B stays 24.**
2. The main thread had not yet run a torch op when A set 3: its first `get_num_threads()` returned **3**.
   A thread started after A's call also reported **3**. So the call **leaks into every thread that
   has not yet latched**.
3. A thread that set 3 and latched kept **3** after a short-lived helper thread called
   `torch.set_num_threads(24)`; a thread started after that helper reported **24**.

So: per-thread once latched, process-wide for anything not yet latched. WP P1 commits this probe
as a script and re-runs it, including a timing check that the reported count is what the parallel
regions actually use.

**Issue-body corrections (#1270):**
- "Scope it around the CrossEncoder predict call" with save/restore is **unsafe**: the restore window
  leaks the cap permanently into any Starlette worker thread that first touches torch during it, and
  two concurrent turns race each other's restore (fact 2 above).
- "`pytest tests/eval` unchanged" cannot show ranking parity: `rg "CrossEncoder|get_reranker" backend/tests/eval`
  finds nothing. The harness runs with stub scorers. Parity needs the real model (section 6.1).
- "torch defaults to 24 threads" is the **physical** core count (48 logical). Any formula built on
  `effective_cpu_count()` (logical) has to account for that.

### 1.2 Pre-commit mypy (#1271)

| Fact | Evidence |
|---|---|
| Hook: `pre-commit/mirrors-mypy` **rev v2.1.0**, `files: ^backend/`, args `--ignore-missing-imports --check-untyped-defs` | `.pre-commit-config.yaml:52-76` |
| Hook deps: `types-requests`, `types-python-dateutil`, `types-pyyaml`, `types-redis`, `prometheus-client`, `sqlalchemy>=2.0`, `joserfc>=1.7.4`, `playwright`. **Most are unpinned** | same |
| venv: **mypy 2.3.1** (`backend/requirements-dev.txt:10`), Python **3.12.3** | `backend/venv/bin/mypy --version` |
| Config: root `pyproject.toml:71-98`, `python_version = "3.12"`, `ignore_missing_imports = true` globally | — |
| CI pre-commit job: Python **3.13**, runs both stages | `.github/workflows/pre-commit.yml:54-58, 85-104` |
| Prod image: Python **3.13** | `backend/Dockerfile.prod:130,193` |
| py.typed in venv but **absent from the hook env** (so they resolve to `Any` there): `pydantic`, `pydantic_settings`, `fastapi`, `starlette`, `httpx`, `requests` (inline `json: _t.JsonType` at `requests/api.py:120`), `assemblyai`, `numpy`, `pytest`, `redis` | `ls <site-packages>/<pkg>/py.typed` |
| Stub sets differ **both ways**: the hook has `types-pyyaml`/`types-python-dateutil`, the venv does not (PyYAML and dateutil ship no py.typed, so the venv sees `yaml` as `Any`). The venv has `types-passlib`, the hook does not | `backend/venv/bin/pip list` |
| `types-redis==4.6.0.20241004` (both envs) shadows `redis==8.1.0`'s own inline types, because stub packages win under PEP 561 | `requirements-dev.txt:36`, `requirements.txt:61` |
| Precedent for a config-vs-workflow gate | `backend/tests/unit/test_precommit_stage_ci_parity.py` (and its docstring's note that `pytest` is absent from the hook env) |

**Measured 2026-10-25 @ `a236125a`:** `backend/venv/bin/mypy --config-file pyproject.toml
--ignore-missing-imports --check-untyped-defs $(git ls-files 'backend/*.py' 'backend/*.pyi')`
(the hook's exact file set, 2,269 files) gives **55 errors in 23 files**. By code: arg-type 26,
call-arg 12, index 6, union-attr 5, comparison-overlap 2, attr-defined 2, assignment 2. The
failure shapes:

- **Tests feeding wire-shaped input to a model constructor** to exercise coercion or aliases:
  `test_speaker_attribute_settings.py` (8), `test_download_settings_schema.py` (2),
  `test_storage_plane_session_lifetime.py:636` (5), `test_speaker_range_precedence.py:285`.
- **Optional not narrowed:** `test_citation_round_trip.py` (5), `test_rate_limit_retry_after.py` (4).
- **Fakes passed where a real type is declared:** `test_presign_identity.py` (4, `Minio`),
  `test_pyannote_provider.py` (2, `httpx.Response`), `test_gladia_provider.py:230`,
  `test_bulk_retry_budget.py:170`, `S3Error(response=None)` x2.
- **`bytes` vs `str`:** `test_mock_llm_fixture.py`, `e2e/test_media_download.py`,
  `integration/test_lite_mode_mocked_providers.py`. Each is either a latent `TypeError` or
  `iter_lines(decode_unicode=True)` typed as `bytes`. Decide which per site.
- Starlette `BaseRoute` has no `.path`/`.endpoint` (2), `dict[str, str | None]` headers (2),
  `alembic/env.py:112` (`get_section` returns `Optional`).

The aligned hook flags at most this set plus whatever the dependency differences add or remove.
WP M1 measures the real number.

**Issue-body corrections (#1271):**
- The acceptance check compares **different scopes**: `pre-commit run mypy --all-files` checks all
  of `backend/` (tests, scripts, alembic), while `backend/venv/bin/mypy app` checks `app/` only.
  Parity has to be checked over the same file list with the same `--config-file`.
- Missing packages are not just `pydantic`/`assemblyai`/`requests`; see the table above.
- The mypy **version** also differs (v2.1.0 vs 2.3.1).
- Root `CLAUDE.md` says the venv is built with `python3.11`. It is 3.12.3. Fix the sentence in M3.

---

## 2. Design decisions

### 2.1 #1270: how the cap is scoped

| Option | Verdict |
|---|---|
| A. `OMP_NUM_THREADS`/`torch.set_num_threads` for the whole API process | Rejected. Caps embeddings and output redaction too, which the issue rules out. It stays the operator's zero-code escape hatch |
| B. Save/restore around `predict` on the request thread | Rejected. Leaks into unlatched threads, and concurrent turns race (§1.1) |
| C. ctypes `omp_set_num_threads` + `mkl_set_num_threads_local` on the request thread | Rejected. Build-specific (CUDA wheel vs `+cpu` lite wheel vs arm64), and torch's lazy init can overwrite it |
| D. Out-of-process scorer (forkserver, like `redaction/pii_pool.py`) with `OMP_NUM_THREADS` in its env | Fully isolated, but a second ~400 MB copy of the model plus IPC per turn. **Fallback only** if P1's probe contradicts §1.1 |
| **E. Dedicated scoring pool: K threads latch the cap at start, then the process default is restored** | **Recommended.** Uses the per-thread semantics measured in §1.1. Also bounds concurrent reranks: K turns at a time, each with `cap` threads, instead of N turns each with 24 |

**Mechanics of E** (new module `backend/app/services/chat/reranker_threads.py`, about 150 lines):

1. `resolve_torch_threads() -> int | None`: returns `None` (no cap) when `OMP_NUM_THREADS` or
   `TORCH_NUM_THREADS` is set. That is the same precedence as `core/celery.py:910`: the operator's
   explicit value wins. Otherwise
   `min(C.CHAT_RERANKER_TORCH_THREADS_MAX, max(1, effective_cpu_count() // 2))`. The `// 2` keeps
   small hosts at about torch's physical-core default (8 logical -> 4, 4 -> 2). Large hosts are capped.
2. `_start_pool()` (lazy, under a lock, once per process): read the process default from a **fresh
   helper thread** (`torch.get_num_threads()` there returns the global default, not some caller's
   latched value). Create `ThreadPoolExecutor(max_workers=K, thread_name_prefix="chat-rerank-score",
   initializer=_latch_cap)`. `_latch_cap` calls `torch.set_num_threads(cap)` and then
   `torch.get_num_threads()`, which forces the lazy latch. Force all K threads into existence by
   submitting K `barrier.wait` tasks (`threading.Barrier(K + 1)`). The pool only creates a thread
   when none is idle, and blocked tasks are never idle. Then restore the global from a **short-lived
   helper thread** calling `torch.set_num_threads(default)`.
3. `score_pairs(model, pairs)`: `_start_pool()`, then `executor.submit(model.predict, pairs).result()`.
   There is **no timeout**, the same as today: a timed-out predict keeps burning CPU. If the pool
   cannot start (no torch, `BrokenThreadPool`, initializer error), log one warning and call
   `model.predict(pairs)` inline. That is exactly today's behaviour, so ranking is never lost to the
   optimisation.
4. `shutdown_for_tests()`: shuts the executor down and clears the singleton. `reranker.reset_reranker_state()`
   calls it.
5. Wiring: `reranker.py:227` `model.predict(pairs)` -> `score_pairs(model, pairs)`.
   `reranker_warmup.py:85` -> `score_pairs(model, [_PROBE_PAIR])`, so the pool starts and latches
   at API startup, off the request path. Only pairs of strings (`(query, hit.content)`) cross
   threads, never `hit` objects.

**Residual risk, accepted and documented:** during the few milliseconds between step 2's latch and
its restore, a thread that first touches torch inherits `cap`. That happens once per process,
normally at startup. The consequence is that this one thread runs capped, which is never wrong,
only slower. A test pins that the default is restored afterwards (§6.1).

**K (scoring workers):** `C.CHAT_RERANKER_SCORING_WORKERS = 2`. One serialises two simultaneous
first-chats, adding about 4 s of queueing. Unbounded concurrency is today's oversubscription. P1
measures `--concurrency 1,2,4` to confirm.

**Settings convention:** both values are **coded constants** in `constants.py`, beside
`DEFAULT_CHAT_RAG_RERANK_WARMUP_ENABLED`. They are not `SystemSettings` rows and not
`CHAT_FLAG_REGISTRY` entries: the pool latches once per process, so an admin-panel value would
suggest an effect that only appears after a restart. That is the reasoning already recorded for
the warm-up flag (`reranker_warmup.py:54-57`). No env var either: `OMP_NUM_THREADS` already exists
as the override.

**Why ranking is unchanged:** same weights, tokenizer, `max_length`, dtype and inputs. The
intra-op thread count only changes how GEMM and reduction loops are partitioned, so scores can
differ by float summation order (about 1e-6 relative), and the sort key (`reranker.py:232`) is
unchanged. Order can flip only on a near-exact tie, which is as arbitrary under the default thread
count. The benchmark reports `max |delta score|` and order identity per N (§6.1). The acceptance
is identical order on the fixture plus an unchanged `benchmark_rag.py --stage rerank` result.

### 2.2 #1271: what "aligned" means

| Option | Verdict |
|---|---|
| A. Add only `pydantic`, `assemblyai`, `requests` (the issue's list) | Rejected as incomplete. FastAPI, Starlette, httpx, numpy and pytest stay `Any`, and the stub sets still diverge |
| B. `repo: local` hook running `backend/venv/bin/mypy` | Rejected. CI's pre-commit job has no venv, and a contributor without the venv gets a broken hook |
| C. Install all of `requirements-ci.txt` into the hook env | Rejected. Pulls torch/CUDA (GBs) into every contributor's pre-commit cache |
| **D. A derived parity contract:** every third-party distribution imported under `backend/` that ships `py.typed` (or has a `types-*` stub in requirements-dev) is either an exact-pinned hook dependency or on a short, reasoned `EXEMPT` list (heavy ML: `torch`, `sentence_transformers`, `transformers`, `whisperx`, ...). Stub sets equal on both sides. Hook mypy rev equals the venv pin | **Recommended.** The list is derived by a test from imports + installed metadata, never hand-recorded (the repo's "version facts are derived" rule) |

`EXEMPT` modules stay checked by venv mypy only. The test names this gap so it is visible, the same
way `test_precommit_stage_ci_parity.py`'s `EXEMPT_STAGES` does.

**Python version:** mypy targets 3.12, while CI and prod run 3.13. Measure both in M1. The
recommended default is in §10 Q3.

**Fix policy for findings (non-negotiable, root `CLAUDE.md` "Fix the finding"):** no `Any`
widening, no `# type: ignore`, no `# noqa`, no `cast`. In order of preference:

1. Test input that is wire-shaped (strings for bools, camelCase aliases, partial payloads): build
   it through `Model.model_validate({...})`. That is the real runtime path for such input, and it
   is typed.
2. `Optional` results: narrow explicitly (`assert x is not None` in tests).
3. Fakes: construct the real object (`httpx.Response(..., request=httpx.Request(...))`, a real
   `urllib3` response for `S3Error`). If production only uses a narrow surface, introduce a
   `typing.Protocol` in the production module. That is a real type and a better seam.
4. `bytes`/`str`: decide per site whether it is a latent bug (fix the code) or imprecise typing
   (use `.text`/explicit `.decode()`).

---

## 3. File-by-file changes

### Track P (#1270)
| File | Change |
|---|---|
| `backend/app/scripts/probe_torch_thread_scope.py` (new, P1) | The §1.1 probe (latched/unlatched/new-thread/restore) plus a matmul timing per thread. Prints JSON |
| `backend/app/scripts/benchmark_reranker_threads.py` (new, P1) | See §6.2. Precedent: `backend/app/scripts/benchmark_redaction.py` |
| `backend/app/services/chat/reranker_threads.py` (new, P2) | §2.1 E |
| `backend/app/services/chat/reranker.py` | `:227` -> `score_pairs`; `reset_reranker_state()` calls `shutdown_for_tests()`; module docstring paragraph |
| `backend/app/services/chat/reranker_warmup.py` | `:85` -> `score_pairs`; docstring line |
| `backend/app/core/constants.py` (~`:1394`) | `CHAT_RERANKER_TORCH_THREADS_MAX` (value from P1, provisional 8), `CHAT_RERANKER_SCORING_WORKERS = 2`, with a comment citing #1270 and the measurement |
| `backend/app/services/chat/CLAUDE.md` | Replace the "Not fixed by this" paragraph (around `:73-77`) with the pool rules and measured numbers |
| `backend/tests/unit/test_chat_reranker_threads.py` (new) | §6.1 |

No API, schema, migration, frontend or i18n change.

### Track M (#1271)
| File | Change |
|---|---|
| The 22 test files + `backend/alembic/env.py` listed in §1.2, plus whatever M1 adds | Real-type fixes (§2.2) |
| `.pre-commit-config.yaml:52-76` | `rev: v2.3.1`. Exact pins copied from `backend/requirements*.txt` for every listed dependency. Add `pydantic`, `pydantic-settings`, `fastapi`, `starlette`, `httpx`, `requests`, `assemblyai`, `numpy`, `pytest`, `types-passlib` (plus anything else M1 derives). Drop `types-requests`. Rewrite the comment block to point at the parity test |
| `backend/requirements-dev.txt` | Add `types-PyYAML==<pin>` and `types-python-dateutil==<pin>`, the same pins as the hook |
| `backend/tests/unit/test_precommit_mypy_env_parity.py` (new) | §6.1 |
| `scripts/mypy-hook-negative-control.sh` + `scripts/mypy_negative_control_fixture.py.txt` (new) | §6.3. The `.txt` suffix keeps it out of normal hook runs |
| `.github/workflows/pre-commit.yml` | New step after the pre-push stage: `scripts/mypy-hook-negative-control.sh` |
| `CLAUDE.md` (root, venv paragraph) | `python3.11` -> the real version; one line on the parity test |

---

## 4. Security and tenant isolation

- **P:** no new surface. Only `(query, chunk text)` strings already in this process cross to the
  pool thread. Contextvars (request id, trace recorder) do not propagate to it, and `predict`
  reads none (verify with `rg contextvar backend/app/services/chat/reranker*.py`, expect none). A
  bounded pool turns N concurrent reranks into a queue: requests wait longer instead of all
  oversubscribing. Chat-level rate limits are unchanged. Negative test:
  `test_scoring_pool_never_receives_hit_objects` (the fake model asserts every pair element is a
  `str`).
- **M:** supply chain. New hook dependencies are exact pins already shipped in the prod image, so
  nothing new is trusted. Pinning the previously unpinned entries also removes "CI got a newer
  wheel than your cache".

---

## 5. Frontend

None. No copy, so no i18n/RTL work. No bundle impact (root layout 16.7/22.5 KB and closure
128.8/139.3 KB gzip are untouched).

---

## 6. Test plan (red first)

Prove every red on a `git archive HEAD` tree, never by reverting files in the shared checkout
(root `CLAUDE.md`):
```bash
RED="$(mktemp -d -t ot-redcheck.XXXXXX)" && git archive HEAD | tar -x -C "$RED"
cp backend/tests/unit/test_chat_reranker_threads.py "$RED/backend/tests/unit/"
cd "$RED/backend" && /mnt/nvm/repos/transcribe-app/backend/venv/bin/python -m pytest -p no:cacheprovider -n0 tests/unit/test_chat_reranker_threads.py -v
```
Design the tests to import `app.services.chat.reranker` (which exists on HEAD) wherever possible,
so the red is an **assertion** failure, not an `ImportError`.

### 6.1 Named tests

`backend/tests/unit/test_chat_reranker_threads.py` (autouse fixture: `reset_reranker_state()` before
and after; a fake `torch` in `sys.modules` recording `(thread name, value)` per `set_num_threads`
call, as `tests/test_cpu_budget.py:93` does):

| Test | Red on HEAD because |
|---|---|
| `test_rerank_scores_on_the_capped_scoring_thread`: the fake model records `threading.current_thread().name`; expects the `chat-rerank-score` prefix | predict runs on the caller (`MainThread`) |
| `test_scoring_threads_latch_the_resolved_cap`: fake torch saw `set_num_threads(cap)` from each scoring thread | no call at all |
| `test_process_default_is_restored_from_a_non_scoring_thread` | no call at all |
| `test_explicit_omp_or_torch_num_threads_skips_the_cap` (control for the env precedence) | the cap path does not exist (ImportError is acceptable here) |
| `test_resolve_torch_threads_bounds`: 48 -> MAX, 8 -> 4, 4 -> 2, 1 -> 1 | ImportError (acceptable) |
| `test_a_broken_pool_still_reranks_inline`: the initializer raises; order still follows fake scores | n/a on HEAD; must fail if the fallback returns retrieval order |
| `test_warmup_starts_the_scoring_pool` | warm-up predicts inline |
| `test_scoring_pool_never_receives_hit_objects` | the thread-name assertion fails first |
| `test_rerank_order_matches_score_order` | **control**: passes on HEAD too, and pins that the move did not change sorting |
| `test_real_torch_cap_is_thread_scoped`: runs `probe_torch_thread_scope.py` logic in a **subprocess** (`sys.executable`). It asserts the scoring thread reports the cap, while a pre-latched thread and a newly started thread report the default. Skips with a reason only if `torch` is not importable (CI's `requirements-ci.txt:84` has torch, so it runs there) | module missing |

`backend/tests/unit/test_precommit_mypy_env_parity.py` (YAML/TOML/requirements reads; raise
`AssertionError`, not `pytest.fail`, per the sibling test's docstring note):

| Test | Red on HEAD because |
|---|---|
| `test_mypy_hook_rev_matches_venv_pin` | v2.1.0 != 2.3.1 |
| `test_every_hook_dependency_is_pinned_like_backend_requirements` | `sqlalchemy>=2.0`, `playwright`, `prometheus-client` unpinned |
| `test_every_typed_import_resolves_in_the_hook_env`: AST-scans `backend/**/*.py` imports, maps them to distributions via `importlib.metadata.packages_distributions()`, keeps those that ship `py.typed`, and requires each to be a hook dependency or in `EXEMPT` with a reason. An import whose distribution is not installed in the test env must also be in `EXEMPT` (fail closed) | `pydantic` and others missing |
| `test_no_stub_package_shadows_a_listed_inline_typed_package` | after derivation `requests` is listed and `types-requests` is present |
| `test_stub_sets_match_hook_and_requirements_dev` | `types-PyYAML` absent from requirements-dev, `types-passlib` absent from the hook |
| `test_detectors_fire_on_synthetic_bad_configs`: **self-test**, feeding each checker an in-memory bad config | must-fire case; also include a must-stay-clean case |

### 6.2 Benchmark (`backend/app/scripts/benchmark_reranker_threads.py`)

Run inside the backend container: `./opentr.sh shell backend`, then
`python -m app.scripts.benchmark_reranker_threads --threads 2,4,8,16,default --runs 7 --pairs 50 --concurrency 1,2 --out /tmp/rr.json`.

- One **subprocess per N** (fresh interpreter, so no latched state carries over), using the same
  `CrossEncoder(C.CHAT_RERANKER_MODEL, device="cpu", max_length=512)` load as production. Two modes:
  `--mode raw` (the issue's experiment: `set_num_threads(N)` then `predict`) and
  `--mode production` (through `reranker.rerank` with the cap overridden to N).
- Input: 50 deterministic synthetic ~150-word passages (fixed seed, committed word list). It
  **never reads dev data**.
- Records per run: latency, `os.getloadavg()` before and after, `effective_cpu_count()`, torch
  version, and N. Per N: median, p90, `max |delta score|` vs `default`, and whether the order is
  identical.
- `--background-load K`: K spinner child processes that stop on a shared `Event` (cooperative exit,
  never a signal), so a "loaded host" run is repeatable on an idle box. `--affinity 0-3` simulates
  a small host via `os.sched_setaffinity` (`effective_cpu_count()` reads affinity).
- **Pre-registered choice rule:** MAX = the smallest N whose loaded median is within 10% of the
  best loaded median **and** whose idle median is within 25% of the best idle median. Report the
  three scenarios (idle, `--background-load 40`, `--affinity 0-3`) in the PR and on #1270.
- Announce the measurement window first (root `CLAUDE.md`). No GPU is involved.

### 6.3 Negative control (`scripts/mypy-hook-negative-control.sh [--ref REF]`)

It builds an `ot-redcheck.` tree from `git archive REF`, runs `git init -q && git add -A`, and copies
the fixture to `backend/app/zz_mypy_negative_control.py`. It then runs
`pre-commit run mypy --files backend/app/zz_mypy_negative_control.py` and asserts that the run
**fails** with each expected `file:line [code]`. The fixture contains:
`_Probe(count="three")` -> `[arg-type]`, `_Probe(cnt=3)` -> `[call-arg]` (pydantic `BaseModel`), and
`requests.post(url, json={1, 2})` -> `[arg-type]` (a set is not `JsonType`). It runs in a
throwaway repo, so it can never stash the shared checkout.

- Red proof: `--ref <commit before M3>` must report "control NOT caught" (exit 1). On the M3 branch
  it must report "caught 3/3" (exit 0).

### 6.4 #1272 hazards, written down for implementers

1. **The full suite in one process catches what targeted runs miss.** `test_task_routes_complete`
   passed alone and failed in CI's full run, because state from another test (a throwaway Celery
   app) leaked into a process-wide registry. Here the equivalents are the module-level scoring pool
   and torch's process-wide thread default. Never call real `torch.set_num_threads` inside the
   pytest process (use the subprocess test). Always `shutdown_for_tests()` in an autouse fixture.
   Then run the **whole** backend suite (`./scripts/run-backend-tests.sh`) and the CI
   `backend-tests` job, not just the new files.
2. **Celery shares `@app.task` registrations across apps.** Do not define tasks in the benchmark or
   probe scripts, and do not import `app.main`/`app.core.celery` from them (the reranker import
   chain needs only `app.core.constants`). A new task would also need a `task_routes` entry and
   `include=` (commits `091909d1`, `e397819f`).
3. M2 edits test files, and `audit-tests` is a whole-tree gate: run
   `cd backend && python3 ../scripts/audit-tests.py tests` to 0 before reporting.
   `model_validate` rewrites must keep the tests able to fail. Check that at least one rewritten
   coercion test still goes red when the validator is broken.

---

## 7. Work packages

| WP | Size | Depends on | Files | Parallel? |
|---|---|---|---|---|
| **P1** Probe + benchmark scripts. Run them and post the numbers on #1270 | S | — | 2 new `backend/app/scripts/*` | with all of M |
| **P2** `reranker_threads.py`, wiring, constants, tests, `chat/CLAUDE.md` | M | P1's MAX (implement with provisional 8, then finalize) | §3 track P | with all of M |
| **M1** Measure the aligned hook in a throwaway tree (no repo edits): `pre-commit run mypy --all-files` with the candidate config, at `python_version` 3.12 and 3.13. Also run the missing-module diff: `ignore_missing_imports = false` in the throwaway `pyproject.toml`, hook and venv each, then diff the "Cannot find implementation" / "missing library stubs or py.typed" module sets. Post the finding list on #1271 | S | — | none | with P |
| **M2** Real-type fixes for venv-flagged plus M1-flagged findings. Mergeable alone: venv mypy already reports these | M | M1 | tests + `alembic/env.py` | split by file group across agents in **own worktrees** (e.g. pydantic-input tests / fakes / bytes-str + the rest) |
| **M3** Hook config, requirements-dev stubs, parity test, negative control + CI step, root `CLAUDE.md` line | S | M2 merged (or in the same PR, after it) | §3 track M config rows | — |

Each agent commits only in its own worktree (root `CLAUDE.md` concurrency table).

---

## 8. Rollout and compat for derived deployments

- Derived deployments that replace `$lib/cloud` or register backend hooks are unaffected: no
  frontend, hook-registry, API or schema change.
- A derived API process that sets `OMP_NUM_THREADS`/`TORCH_NUM_THREADS` keeps full control: the cap
  is skipped. One that calls `reranker.rerank` gets the pool automatically. Code that calls
  `get_reranker(...).predict` directly bypasses it, and should call `score_pairs` instead.
- Derived repos that extend `.pre-commit-config.yaml` with their own mypy hook should copy the parity
  test pattern. The `EXEMPT` mechanism is generic.
- No migration, no settings rows, nothing to reconcile on upgrade.

---

## 9. Verification and exit criteria

**#1270 done when:**
- [ ] P1 numbers are posted for idle / loaded / 4-core, with load averages. The 50-pair loaded
  median with the chosen cap is clearly below the default's (target: about 2x, per the issue's 7-16
  s vs 3.4-5.5 s).
- [ ] Order is identical at every N on the fixture. `max |delta score|` is reported.
  `scripts/benchmark_rag.py --stage rerank` on the eval corpus is unchanged against its committed
  baseline.
- [ ] Every §6.1 reranker test except the control was seen red on an `ot-redcheck.` tree, and is
  green on the branch. The full backend suite is green.
- [ ] Live check: in the dev backend, after startup, the warm-up log line appears.
  `threading.enumerate()` shows `chat-rerank-score_*` threads, and a fresh thread's
  `torch.get_num_threads()` equals the default (via `./opentr.sh shell backend`).

**#1271 done when:**
- [ ] Over the same 2,269-file list with the same config, `pre-commit run mypy --all-files` (in a
  throwaway tree) and `backend/venv/bin/mypy --config-file pyproject.toml <files>` both report 0.
- [ ] `scripts/mypy-hook-negative-control.sh` reports caught 3/3 on the branch and "NOT caught"
  on the pre-M3 ref. The CI step is green.
- [ ] The parity tests were red on HEAD and are green on the branch. The detector self-test
  passes. Both pre-commit tiers pass via `scripts/safe-precommit.sh`. `audit-tests` reports 0.
- [ ] Zero new `type: ignore`, `noqa`, `cast` or `Any` in the diff:
  `git diff master -- backend | rg '^\+.*(type: ignore|noqa|cast\(|: Any\b)'` is empty, or each
  hit is justified in the PR.

---

## 10. Open questions (each has a recommended default)

1. **Should the reranker thread cap be admin-tunable?** Default: **no.** Keep it a coded constant,
   with `OMP_NUM_THREADS` as the override. It latches once per process, so a UI knob would mislead.
2. **K = 2 scoring workers?** Default: **2**, unless P1's `--concurrency 2` run shows 1 is no
   worse.
3. **Move mypy `python_version` from 3.12 to 3.13** (to match CI and prod)? Default: **yes, in M3**,
   if M1's 3.13 run adds 10 or fewer findings. Otherwise file a follow-up issue.
4. **Drop `types-redis`** (4.6 stubs shadowing redis 8.1's inline types) in both envs? Default:
   **yes, in M3**, if M1 shows 20 or fewer new findings from redis's own types. Otherwise keep it
   in both (parity is preserved either way) and file a follow-up.
