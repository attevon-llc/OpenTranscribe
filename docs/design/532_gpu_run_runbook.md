# #532 GPU measurement run: runbook

**Status:** RUNBOOK. Nothing in it has been run. Written 2026-10-09 against
`origin/feat/v0.6.0-frontend-ux` @ `990c26db` (the hybrid map tier `fbf98585` is an ancestor).
**Design and decision rules:** `docs/design/532_hybrid_summary_synthesis_plan.md` (sections 4-6).
This runbook says how to execute that plan's Phase 1. Where they disagree, the code is right; see section 8.
Re-check every `path:LINE` before relying on it.

---

## 1. What is measured, and the rule that decides it

**The gap (#532):** retrieval offers ~97% of the files in scope, but the answer cites ~73% (shipped
control, re-derived 2026-09-09). This is a synthesis problem, not a retrieval one.

**Where things stand (from the #532 thread):**

| Arm | Flag (`ChatAdminSettingsUpdate` field / `system_settings` key) | Coded default | Status |
|---|---|---|---|
| (d) abstractive map | `map_tier_summaries` / `chat.rag.map_tier_summaries` (`constants.py:1380`) | False | **Lost** on 2026-09-21 at `c9ec0380`: USED −17.0 pts. Re-run here as **D** at the same SHA |
| **H** hybrid (summary + closing section) | `map_tier_hybrid` / `chat.rag.map_tier_hybrid` (`constants.py:1391`, `experimental=True`) | False | Built (`fbf98585`). Only takes effect when `map_tier_summaries` is also on. **This run's subject** |
| (a) citable overview | `overview_citable` / `chat.rag.overview_citable` (`:1499`) | False | **Not runnable with H.** See 8.2 |
| (b) rule-12 reattached | `overview_block_rule` / `chat.rag.overview_block_rule` (`:1500`) | False | Not in this window (no threshold, no counter) |
| (c) overview after excerpts | `overview_after_excerpts` / `chat.rag.overview_after_excerpts` (`:1501`) | False | Not in this window |
| (ctx) context expansion | `context_expansion_enabled` (`:1488`) | **True** | Shipped (#523). Leave it at the default in every arm |

All of these are DB-backed `SystemSettings`. They can be edited with no restart through
`PUT /api/admin/chat-settings`. Set them **only** with `probe_chat_rag.py --chat-flag FIELD=bool`.
It PUTs the value, reads it back, and exits on a mismatch (`ensure_chat_flags`,
`scripts/probe_chat_rag.py:335`). Never edit the rows by hand.

**Arms, in this order, on one stack, one SHA, with nothing edited in between:**

| Arm | `map_tier_summaries` | `map_tier_hybrid` | `overview_citable` | Question sets |
|---|---|---|---|---|
| **C1** control | false | false | false | expanded (140) + AMI-81 |
| **D** arm (d) | true | false | false | expanded |
| **H** hybrid | true | true | false | expanded + AMI-81 |
| **C2** A/A control | false | false | false | expanded |

Pass all three flags explicitly on every arm, including the ones that are off. Also pass
`overview_block_rule=false overview_after_excerpts=false` so that state left over from an earlier
session cannot leak into a run.

**Decision rule** (pre-registered in plan section 5; per-turn paired diffs vs C̄ = mean(C1, C2),
bootstrap 95% CI, 20,000 resamples, seed 0):

- **Void window:** the C1−C2 USED CI excludes 0. Do not grade.
- **Sanity:** H − D CI lower bound > 0 on both M1 (USED) and M2 (content coverage). If not, the
  implementation or the instrument is broken.
- **WIN:** all of the following, then promote per plan 6.1.
  - M1 H − C̄ ≥ +0.05 with CI lower bound > 0.
  - M2 CI lower > −0.03.
  - M3 pooled ≥ C̄ − 2%.
  - M4 strict ≥ C̄ − 0.05.
  - M5 ≤ C̄ + 0.10.
  - Guards G1-G7 pass.
  - Due-outs judge slice ≥ 0.50.
- **Content-only win** (M2 CI lower > 0, M1 short): build U5, then run H+a vs H in a later window (8.2).
- **Null or loss:** delete `map_tier_hybrid` (plan 6.2) and post the table on #532.
- **M1 up, M2 down:** a divergence, not a win. Escalate to David.
- **Metric hygiene:** the headline is USED / `files_in_scope`. That is `coverage_ratio` in
  `metrics.json` (`files_consulted / scope_size`). Read OFFERED from `offered_citations`
  (`files_offered`), **never** from `files_consulted`, which is what the model chose to cite.

---

## 2. Bring up the isolated stack

**Stack choice.** Reuse `otfresh-v060synth` at **offset 900**. Its volumes still exist and hold
the 137 QMSum Product meetings plus the summaries generated on 2026-09-21. Its `.fresh/` record
was lost when the hybrid worktree was deleted, so pass `--port-offset 900` explicitly.

Offsets already in use: 0 (live), 100 (`visual`), 200 (`v060`, **running**, owned by another
session, on GPU 1), 300 (`v060rag`), 400 (`gr782`). Ports at +900 were free on 2026-10-09:
backend 6074, frontend 6073, postgres 6076, redis 6077, opensearch 6080, vLLM 6095, mock-llm 6099.
**Fallback** if the volumes turn out to be unusable: a new name `m532` at **+500**, built as in
section 2.4. Do **not** `fresh-destroy v060synth` without David's OK.

Run everything from a **dedicated worktree** checked out at the SHA under test, with a clean
`git status`. `opentr.sh` symlinks the main `.env` into it; never copy or read it. Write every
artifact that must survive to **absolute paths under the main checkout**:
`/mnt/nvm/repos/transcribe-app/.rag-403/`. The expanded question set was lost along with a
worktree's `.rag-403/` once already.

```bash
R=/mnt/nvm/repos/transcribe-app/.rag-403/probe-runs   # survives worktree removal
PY=/mnt/nvm/repos/transcribe-app/backend/venv/bin/python
SHA=$(git rev-parse --short HEAD); ./opentr.sh fresh-list; ./opentr.sh data-paths
nvidia-smi                                  # section 4 pre-flight; do not continue unless it passes
export LLM_TEST_GPU_DEVICE_ID=2             # NOT offset by --port-offset; never 0
export LLM_TEST_VLLM_GPU_UTIL=0.85          # see section 4; keep it identical for every arm
./opentr.sh start dev --fresh v060synth --port-offset 900 --no-bindmount \
  --gpu-device 1 --no-diar-native --with-llm-test --with-mock-llm --dry-run   # read the chain
./opentr.sh start dev --fresh v060synth --port-offset 900 --no-bindmount \
  --gpu-device 1 --no-diar-native --with-llm-test --with-mock-llm             # run_in_background
```

`--no-bindmount` runs the baked image with no hot reload, and `fresh_verify_baked_git_sha`
(`opentr.sh:1297`) exits 1 unless HEAD, the image, the backend and every worker share one
`GIT_SHA`. `--gpu-device 1` pins the six app device ids but does not move the vLLM.
`--no-diar-native` avoids a 2.2 GB sidecar on the shared GPU 1. `--with-mock-llm` uses no GPU and
exists only for the prompt dump (2.3). Confirm the banner prints `GPU: 2` for the LLM and `[gpu: 1]` reservations for the app. With
`--fresh`, the vLLM runs as `otfresh-v060synth-llm-test-vllm` on the stack's own network
(`opentr.sh:1107-1122`), so `http://llm-test-vllm:8000/v1` resolves. **No
`docker network connect` is needed** (8.4).

### 2.1 Settle and preconditions (P0)

The startup migrations and `search_index_maintenance` may rewrite the indexes and digests on a
September volume. Wait until the backend logs show maintenance idle. Then:

```bash
docker exec otfresh-v060synth-postgres psql -U postgres -d opentranscribe -c \
 "SELECT count(*) total, count(*) FILTER (WHERE mf.summary_status='completed' AND
  mf.summary_data->'metadata'->>'source_fingerprint'=ff.source_fingerprint) fresh
  FROM media_file mf JOIN file_facts ff ON ff.media_file_id=mf.id WHERE mf.title LIKE 'QMSum Product%';"
```

The postgres user and db names are the compose defaults. If they differ, take them from
`./opentr.sh shell postgres`.

**Gate P0:** total = 137, and fresh ≥ 131 (95%). If fewer are fresh, a digest regenerated since
September has moved the fingerprints. Regenerate the summaries (2.4 step 3) **before** any arm.
D and H silently degrade to C on a stale file (plan T9).

### 2.2 Build both question sets at this SHA

The file uuids are uuid5 values, deterministic per meeting.

```bash
$PY scripts/build_probe_question_set.py --pg-container otfresh-v060synth-postgres \
    --per-stratum 25 --seed 20260820 --out $R/ami81-v060synth-$SHA.json
$PY scripts/build_probe_question_set.py --pg-container otfresh-v060synth-postgres \
    --multi-file-expanded --out $R/mfx-v060synth-$SHA.json      # expect 140 entries
```

Assert that the AMI-81 labels and uuids match `$R/532-control-c9ec0380/results.json`, and that the
expanded set has 140 entries (the count the oracle recorded in `probe-532-oracle/oracle.md`).
Re-run the no-GPU oracle (`scripts/overview_content_oracle.py --question-set $R/mfx-… --pg-container
otfresh-v060synth-postgres --out /tmp/oracle-$SHA`). It must still print `gate_p0_pass=True`, and K0
must not trigger. Its realised H2/C char ratio was 1.47× on 2026-09-24 (Q3's trigger is 1.5×).

### 2.3 Smoke turns (before spending the window)

1. **LLM plus CW-1:** run one multi-file question with gemma:
   `--question-set <a 1-entry multi_file file> --llm-max-tokens 60000 --chat-flag …(C1 flags)`.
   Check that `msg_metadata.budget_chars` ≈ 175,000. It must not be ≈ 15,000.
2. **Prompt dump** (there is no app-side dump). Repeat the same question with
   `--llm-name probe-echo --llm-provider custom --llm-model mock-echo
   --llm-base-url http://mock-llm:5199/v1 --llm-max-tokens 60000`, under H's flags and then under C1's.
   In `--out/results.md`, the H answer must contain `Summary (machine-generated): ` and
   `Closing discussion (verbatim): ` once per listed file (`reducers.py:31-32`). C must contain
   neither. A probe run re-activates its own config by base URL, so the next gemma run switches
   back. Confirm this in that run's log.
3. **Cache:** set `cache_ttl_seconds=0` once for the whole window (`set_cached` no-ops at ttl ≤ 0,
   `retrieval_cache.py:172`). It is an int, so `--chat-flag` cannot set it. Use the admin UI
   (Settings → AI chat tuning) or the snippet in section 5 with `{"cache_ttl_seconds": 0}`. This
   replaces the plan's "flush the cache", for which no command exists. Record it in the notes; it
   does not change the retrieval result.

### 2.4 Fallback: a new stack `m532` at +500

Only if `v060synth` fails P0 irrecoverably.

1. Start it with the same command as above, using `--fresh m532 --port-offset 500`.
2. Inject the Product split, ASR-free. The settle check is built in:
   ```bash
   Q=/mnt/nas/opentranscribe-benchmarks/qmsum/QMSum-83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e/data/Product/all
   ./scripts/inject-eval-corpus.sh --fresh m532 --corpus qmsum \
     --manifest-dir /mnt/nvm/repos/transcribe-app/.rag-403/injections/qmsum-m532 \
     --only $(ls $Q | sed 's/\.json$//')        # 137 ids; blocks until settled, exits non-zero otherwise
   ```
   Re-verify at any time with `$PY scripts/verify-eval-corpus-settled.py --manifest-dir <that dir>`.
   Export `OPENSEARCH_PORT=5680` first, the same way the injector does.
3. Activate gemma with the smoke turn in 2.3 step 1. Then call
   `POST /api/files/<uuid>/summarize` with `{"force_regenerate": true}` once for each of the 137
   files (use the login session from the section 5 snippet). Expect about 1-2 h through
   `celery-nlp-worker`. Poll the P0 SQL until fresh = 137. Record the summarizer model (plan H7).

---

## 3. The arms: exact commands

For each arm, `N=<arm>`. Use `S=mfx` (expanded) or `S=ami81`, and set FLAGS from the table in
section 1:

```bash
FLAGS="--chat-flag map_tier_summaries=<bool> --chat-flag map_tier_hybrid=<bool> \
 --chat-flag overview_citable=false --chat-flag overview_block_rule=false \
 --chat-flag overview_after_excerpts=false"
$PY scripts/probe_chat_rag.py --port 6074 --question-set $R/$S-v060synth-$SHA.json \
  --llm-model gemma-4-e4b --llm-max-tokens 60000 --llm-temperature 0.0 --concurrency 1 $FLAGS \
  --out $R/532h-$N-$S-$SHA \
  --metrics-out backend/tests/eval/baselines/probe-532h-$N-$S --run-name 532h-$N-$S \
  2>&1 | tee $R/532h-$N-$S-$SHA.log          # run_in_background; capture once
```

`--llm-max-tokens` defaults to **8192**, which is the CW-1 trap. Order: C1-mfx, C1-ami81, D-mfx,
H-mfx, H-ami81, C2-mfx. `--out` holds prose; it is gitignored and is never committed.
`--metrics-out` writes `metrics.{json,md}` and `traceability.{json,md}`, metrics only, enforced by
`assert_no_prose`. Those are what get committed, under `backend/tests/eval/baselines/probe-532h-*`.

**After each arm, before starting the next one**, run the applied-checks over that arm's
`results.json` (section 5). The arm is **void** if any check fails:

| Arm | Must hold on every multi_file turn |
|---|---|
| C1, C2 | `overview.entries_hybrid == 0` and `entries_summary_only == 0` |
| D | `entries_hybrid == 0`; median `entries_summary_only / files_listed ≥ 0.95` |
| H | `entries_hybrid ≥ 1`; median `entries_hybrid / files_listed ≥ 0.95` |
| all | `min(budget_chars) > 100_000`; `cache_hit` never true; no `error`; no `provider_error` warning; `overview.truncated` false; `overview.reducer == "code"`; labels equal the question set's |

**Metrics per arm** (multi_file turns; the AMI-81 legs also feed the guards):

| Metric | Source |
|---|---|
| M1 (USED) | mean `coverage_ratio` |
| OFFERED | `files_offered / scope_size` |
| M2/M3 (content coverage, item recall) | `ami_recall.score_answer(app_answer, reference_answer)` |
| M4 (quote fidelity, strict and tolerant) | `traceability.json` |
| M5 (uncited-sentence fraction) | plan F3 regex |
| G1 | single_specific M1 within ±0.04 of C1 |
| G2 | 6/6 negative controls declined |
| G3/G4 | `traceability.json` |
| G5 | median latency < 120 s, from the AMI-81 legs |
| G7 | OFFERED unchanged vs C̄ |

U7 does not exist yet (8.3). Run the window anyway, because `results.json` keeps everything. Build
U7 afterwards (no GPU needed) and commit its output to `backend/tests/eval/baselines/probe-532h-compare/`.

**After the window:**

1. Restore the defaults: all five flags false and `cache_ttl_seconds=300` (section 5 snippet).
   Paste the read-back into the notes.
2. Judge the AMI-81 legs:
   ```bash
   $PY scripts/judge_chat_answers.py judge \
     --results $R/532h-<arm>-ami81-$SHA/results.json \
     --out /mnt/nvm/repos/transcribe-app/.rag-403/labels/532h-<arm>-judgements.jsonl \
     --judge-base-url <URL> --judge-model <not gemma-4-e4b>
   ```
   The script refuses a judge that is the answering model. The κ=0.857 calibration used qwen3.8
   as judge. Where to serve it (it needs a GPU) is David's call, and it is a separate session.
3. Run the acceptance suite:
   ```bash
   cd backend && RAG_ACCEPTANCE_RUN=$R/532h-H-ami81-$SHA RAG_ACCEPTANCE_JUDGEMENTS=<jsonl> \
     $PY -m pytest tests/eval/test_acceptance_query_shapes.py -v
   ```
   Check the count: "8 skipped" means the artifacts are missing, not a pass (T6).

---

## 4. Wall-clock and VRAM

- **Wall-clock**, from 2026-09-21 latencies of about 74 s per multi-file turn at `--concurrency 1`:
  2.9 h per expanded arm and about 1 h per AMI-81 leg. That is **about 14 h** for the six legs,
  plus about 1 h of setup (build, vLLM load in 3-5 min, settle, smokes), plus 1-2 h for summary
  regeneration if P0 fails. `--concurrency 2` roughly halves the window. It is acceptable for the
  quality metrics but makes latency non-comparable (T15). That choice is plan Q6 and is
  **David's call before starting**.
- **VRAM, GPU 2.** The overlay's default `--gpu-memory-utilization 0.85` reserves about
  **41.8 GiB** of 48 GiB. It is **not** about 20 GB (8.6). vLLM requires util × total to be free at
  startup. That leaves about 5.9 GiB for the other session, which it says needs about 2 GB. If
  David wants about 20 GB, set `LLM_TEST_VLLM_GPU_UTIL=0.45` (about 21.6 GiB; weights are about
  9.3 GiB). Then check that the vLLM log line "Maximum concurrency for 60000 tokens per request" is
  ≥ the `--concurrency` used. Keep the value identical across all arms.
- **GPU 1 (3080 Ti, 12 GiB):** `v060` already holds about 2.5 GiB there. This stack's workers stay
  near idle (no ASR), unless `.env` sets `PRELOAD_GPU_MODELS=true`.

**`nvidia-smi` before starting:** GPU 0 shows tritonserver at about 16 GiB and is **never
touched**. GPU 2 shows ≤ 2 GiB, from the other session only; if anything larger is there, stop and
ask. GPU 1 has ≥ 6 GiB free.

**During the run** (every couple of hours, read-only): `nvidia-smi --query-compute-apps=gpu_uuid,pid,process_name,used_memory --format=csv`
should show GPU 2 steady near the reservation and nothing of ours on GPU 0.
`docker ps --filter label=com.docker.compose.project=otfresh-v060synth` should show everything healthy.

---

## 5. Helper snippets (run from the measurement worktree)

```python
# applied-checks.py <results.json> <arm>   — prints void reasons; exit 1 if any
import json, statistics, sys
rows, arm = json.load(open(sys.argv[1])), sys.argv[2]
rows = rows if isinstance(rows, list) else rows.get("results", rows)
bad, ratio = [], []
for r in rows:
    md = r.get("msg_metadata") or {}; ov = md.get("overview") or {}
    if r.get("error") or any((w or {}).get("code") == "provider_error" for w in r.get("warnings") or []): bad.append((r["label"], "error"))
    if md.get("cache_hit"): bad.append((r["label"], "cache_hit"))
    if (md.get("budget_chars") or 0) <= 100_000: bad.append((r["label"], f"budget_chars={md.get('budget_chars')}"))
    if r["category"] != "multi_file": continue
    if ov.get("truncated") or ov.get("reducer") != "code": bad.append((r["label"], "overview"))
    h, s, n = ov.get("entries_hybrid", 0), ov.get("entries_summary_only", 0), ov.get("files_listed") or 1
    if arm in ("C1", "C2") and (h or s): bad.append((r["label"], "control has summary entries"))
    if arm == "D": ratio.append(s / n); h and bad.append((r["label"], "hybrid in D"))
    if arm == "H": ratio.append(h / n); h or bad.append((r["label"], "no hybrid entry"))
if ratio and statistics.median(ratio) < 0.95: bad.append(("*", f"median ratio {statistics.median(ratio):.2f}"))
print(*bad, sep="\n"); sys.exit(1 if bad else 0)
```

```python
# set-chat-settings.py <port> '<json>'  — PUT + read back (ints such as cache_ttl_seconds too)
import json, sys, requests; sys.path.insert(0, "scripts"); import probe_chat_rag as p
s, base, want = requests.Session(), f"http://localhost:{sys.argv[1]}/api", json.loads(sys.argv[2])
p.login(s, base, p.DEFAULT_EMAIL, p.DEFAULT_PASSWORD)
s.put(f"{base}/admin/chat-settings", json=want, timeout=30).raise_for_status()
got = s.get(f"{base}/admin/chat-settings", timeout=30).json()
print({k: got.get(k) for k in want}); sys.exit(any(got.get(k) != v for k, v in want.items()))
```

Before using them, verify that the `results.json` top-level shape matches (`probe_chat_rag.py:833`)
and that the `budget_chars` location matches (`prompting.py:881`, folded into `msg_metadata`).

---

## 6. Stopping cleanly

- `./opentr.sh stop --fresh v060synth` stops the app **and** the recorded aux services
  (`llm-test-vllm`, `mock-llm`). The volumes are kept.
- Then confirm with `docker ps --filter label=com.docker.compose.project=otfresh-v060synth` (expect
  empty) and with `nvidia-smi` (GPU 2 back to the other session's footprint only).
- `./opentr.sh fresh-destroy <name>` is the only destructive operation. It asks y/N; do it only
  with David's OK.
- **Never** use `pkill`/`kill`/`docker kill` on a vLLM or worker process, and never
  `nvidia-smi --gpu-reset`. A CUDA context killed this way has wedged this host twice.
- If a probe leg must be aborted, stop the **probe** (a host Python HTTP client; Ctrl-C or the
  harness task stop is fine). Leave the stack running. The leg is then void and is re-run in full.

---

## 7. Failure modes, and what to capture *before* teardown

| Symptom | Likely cause | Capture first |
|---|---|---|
| `budget_chars` ≈ 15k | `--llm-max-tokens` omitted, so an 8k window was measured | the `llm_configs` row (`max_tokens`), the probe log |
| H with `entries_hybrid == 0` | stale fingerprints, or the flag not applied | P0 SQL output, the `--chat-flag` read-back lines in the log |
| coverage collapse plus `provider_error` | a crash, not a result (T2) | `docker logs --tail 500 otfresh-v060synth-backend`, `…-llm-test-vllm` |
| vLLM unhealthy at start | GPU 2 not free enough for util × total | `nvidia-smi`, vLLM log. Ask the other session; do not lower util between arms |
| `GIT_SHA` mismatch exit | stale baked image | `opentr.sh` output. Re-run the start command; never hand-recreate |
| C1−C2 CI excludes 0 | drift or nondeterminism | both `results.json` files, `cache_hit` counts, the vLLM log for preemption/swap |
| AMI-81 labels/uuids differ from `532-control-c9ec0380` | wrong corpus or seed | both question-set files |
| acceptance "8 skipped" | env var path wrong, or judgements missing | the `pytest -v` output with its counts |

General order: dump state first (`docker ps -a` filtered by project label, `nvidia-smi`, the
logs above, `SELECT key,value FROM system_settings WHERE key LIKE 'chat.%'`, a copy of the partial
`--out` dir). Stop afterwards. Find error fields by **name** in `results.json` (`error`,
`warnings[].code`); do not grep for "error".

---

## 8. Coordination, and what is stale or wrong in #532 vs the code

**8.1 Coordinate with the session that owns GPU 2.** Send **one** message before
`./opentr.sh start`. Include: start time; container `otfresh-v060synth-llm-test-vllm`; reservation
(~41.8 GiB, or ~21.6 GiB at 0.45); expected end (start + ~16 h, or ~9 h at concurrency 2); a request
to stay under 2 GiB and never stop, restart or signal that container; and that
`./opentr.sh stop --fresh v060synth` releases GPU 2. Do not start if their footprint is above
~5 GiB at that moment. GPU 1 is also shared with the `v060` stack (+200); touch nothing of theirs.

**8.2 H+a cannot run.** U5 (per-part overview citations) was never built.
`_guard_hybrid_overview_citations` (`service.py:1437-1457`) nulls the overview citation ids whenever
a summary is hybrid (`meta["overview_citable_suppressed"]="hybrid"`). So `overview_citable=true`
with H is **silently identical to H**.

**8.3 U7 is missing.** `scripts/compare_probe_arms.py` and `harness/arm_compare.py` do not exist.
The 2026-09-24 comment does not mention it. Plan step 7 cannot run as written. Use section 5 plus
`harness/significance.py` (`paired_bootstrap_ci`) until U7 is built.

**8.4 Stale vLLM-network gotcha.** The `probe_chat_rag.py` docstring, `rag-evaluation.md:2425-2437`
and the `docker-compose.llm-test.yml` header ("Not wired into --fresh isolation") all still require
`docker network connect --alias`. `opentr.sh:1107-1122` now isolates `llm-test-vllm` per fresh
project, so running the connect would put a second container under the same alias.

**8.5 Stale "no read-back guard"** (2026-09-09 plan 5.0). `--chat-flag` now PUTs, reads back and
exits on a mismatch. The prompt-dump caveat for (b)/(c) still applies.

**8.6 "~20 GB vLLM" is not the default.** `LLM_TEST_VLLM_GPU_UTIL:-0.85`
(`docker-compose.llm-test.yml:84`) gives ~41.8 GiB on GPU 2.

**8.7 Line refs moved.** The 2026-09-09 plan's `constants.py:1385-1387`/`:1286` are now
`:1499-1501`/`:1380`. `map_tier_hybrid` is at `:1391`. The registry entries are at
`chat_flag_registry.py:221/228/301/308/315`. `chat/CLAUDE.md`'s rule numbering is already fixed.

**8.8 Counts.** The expanded set has **140** turns, not ~130, so ~2.9 h per arm rather than 2.7 h.
The 2026-09-21 "6 better/9 worse/10 same" does not reproduce; plan F1 gives 5/11/9 (p = 0.21). The
body's 99.0%/75.0% (`792c4060`) is superseded by 97.0%/72.7% (shipped) and 76.7% USED (`c9ec0380`
control).

**8.9 Framing.** The body's arm order and the 2026-09-09 "D2: arms (a)(b)(c)" sequencing are
superseded: (d) lost, and the live question is H. (b) and (c) still have no threshold and no applied
counter. Whether to delete their flags (the four-edit experiment-flag contract) is David's call;
this window does not decide it.

**8.10 Lost state.** `v060synth`'s `.fresh/` files and the expanded set were stored in the deleted
`v0.6.0-hybrid-summary` worktree. The volumes and the `c9ec0380` `results.json` files (main
`.rag-403/`) survive. That is why this runbook writes to absolute `$R` paths.
