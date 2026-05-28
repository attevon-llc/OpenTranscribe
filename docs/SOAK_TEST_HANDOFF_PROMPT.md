# OpenTranscribe GPU Soak Test — Fresh-Agent Handoff (Bench Stack)

> Paste **everything below the next line** into a fresh Claude Code session.
> Start the agent in `/mnt/nvm/repos/transcribe-app` on branch
> `feat/engine-opimization` with Opus 4.7 (1M context) and
> `--dangerously-skip-permissions`. Unattended overnight (~5–7 h depending on
> how many files are in `benchmark/test_audio/`).
>
> **This soak runs ENTIRELY against the isolated bench stack** —
> `docker-compose.bench.yml` overlays fresh named volumes for postgres, MinIO,
> Redis, OpenSearch, Flower. Production data (NAS-mounted or otherwise) is
> never touched. Source audio comes from `benchmark/test_audio/` (mounted
> read-only into the worker), uploaded into the fresh bench DB during Phase 0.5.
>
> **Required setup (you, the user, must do before launching):**
> 1. **Seed `benchmark/test_audio/` with representative production audio** —
>    while your production / dev stack is up, run:
>    ```bash
>    source backend/venv/bin/activate
>    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
>        python scripts/soak_seed_audio.py --total 20
>    ```
>    This authenticates against `http://localhost:5174`, picks 5 completed
>    files from each duration tier at random, and downloads each via
>    `/api/files/<uuid>/download?original=true` into `benchmark/test_audio/`.
>    Run `--dry-run` first to preview picks. Combined with the 5 existing
>    synthetic test files, you'll have ~25 distinct UUIDs for the soak,
>    enough for conc=24. (`benchmark/` is gitignored — no risk of committing.)
> 2. **Stop the production stack** so the bench stack can claim its ports:
>    ```bash
>    ./opentr.sh stop
>    ```
> 3. Verify `.env` has the keys the soak mutates (see Phase 0.1.1).
> 4. `nvidia-smi` shows GPU 0 and GPU 1 near-idle; GPU 2 is the LLM and will
>    be left alone.

---

You are running the OpenTranscribe GPU concurrency soak test on branch
`feat/engine-opimization`. The branch contains new engine optimizations; the
soak validates them on the **isolated bench stack** (`docker-compose.bench.yml`).
Production data is never touched. Every concurrency level is measured fresh
on this branch — historical numbers in `docs/BENCHMARK_RESULTS.md` are
reference-only, not reused as data points.

Your job:

1. **Phase 0** — bring up the bench stack, verify env, upload all audio from `benchmark/test_audio/` into the fresh bench DB, build `corpus.json` from the resulting UUIDs.
2. **Phase 1** — A6000 (GPU 0) solo: single-file e2e baseline + concurrency sweep covering every level the corpus supports (1, 4, 8, 10, 12, 14, 16, 20, 24 — capped at corpus size).
3. **Phase 2** — 3080 Ti (GPU 1) solo: single-file e2e baseline + sweep at conc 1, 2, 3, 4.
4. **Phase 3** — Dual-GPU `--gpu-scale` (both cards, parallel pipelines), full-corpus run at `A6000_BEST` + `TI_BEST`.
5. **Phase 4** — `--with-gpu-split` (both cards, one pipeline split across them), full-corpus run.
6. **Phase 5** — Duration curve, GPU 0 solo conc=1, sequential over the entire bench corpus.
7. **After all phases** — create `docs/BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md` (new file pairing new vs old measurements), update `docs/gpu-concurrency-soak-test-plan.md`, update `_auto_concurrent()` only if the new numbers justify it, commit on this branch.

You have `--dangerously-skip-permissions` so you can edit `.env`, run Docker
commands, and orchestrate restarts unattended. **Do NOT** modify code or docs
outside the explicit "Files you are allowed to edit" list. If you hit an
unexpected blocker (OOM at conc=1, missing `.env` keys, bench stack fails to
start, dirty working tree), STOP cleanly and write a status summary — do not
destructively try to recover. The user reviews in the morning.

### The bench stack — what it is, how to drive it

Every Docker invocation in this soak uses the bench overlay set:

```bash
BENCH_COMPOSE="-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.bench.yml"
```

Define `BENCH_COMPOSE` once at the top of each phase script and reuse it. The
bench stack:

- Mounts fresh named volumes: `postgres_bench_data`, `minio_bench_data`,
  `redis_bench_data`, `opensearch_bench_data`, `flower_bench_data`.
- Builds backend/frontend images from local source on the current branch
  (`opentranscribe-backend:bench` etc.).
- Mounts `./benchmark/test_audio:/app/benchmark/test_audio:ro` into the worker.
- Reads `.env` via `env_file: .env` (so all the `GPU_*` env-var mutations
  still apply — same mechanism as production stack).
- Exposes the same host ports as production (backend 5174, postgres 5176,
  redis 5177, minio 5179, opensearch 5180, flower 5175). Benchmark scripts
  work without modification.

The `./opentr.sh bench start current` command (modified in this branch) wraps
all this: wipes volumes, switches branch (no-op if already on current),
builds, brings the stack up. Use it for the initial Phase 0 bring-up. For
per-phase mode switches, use direct `docker compose $BENCH_COMPOSE ...`
commands so we can layer additional overlays (`docker-compose.gpu-scale.yml`)
and toggle `COMPOSE_PROFILES`.

---

## Hardware reality check

```
GPU 0  RTX A6000     49 GB   slot 0  — Target for A6000 sweep (the main event). OpenTranscribe primary.
GPU 1  RTX 3080 Ti   12 GB   slot 1  — Target for 3080 Ti sweep. OpenTranscribe secondary.
GPU 2  RTX A6000     49 GB   slot 2  — DO NOT TARGET. Hosts an LLM; leave untouched.
```

This is a *dev/test* deployment. There is **one** `celery-worker` container, and
the GPU it uses is controlled by `GPU_DEVICE_ID` in `.env` — *not* by
`CUDA_VISIBLE_DEVICES` (the container always sees its assigned card as device 0
internally). To switch which GPU the worker is on, you edit `.env` and restart
the worker stack.

If `nvidia-smi` shows GPU 2 with non-trivial VRAM in use (the LLM) that is
expected and correct — do not stop, restart, or otherwise disturb it. All
benchmark `--gpu-id` flags in this document target 0 or 1 only.

The `--gpu-id N` flag on the benchmark scripts only changes which GPU host-side
`nvidia-smi` samples. It does **not** route work. Always keep `GPU_DEVICE_ID`
and `--gpu-id` aligned.

---

## Repository

```
cd /mnt/nvm/repos/transcribe-app
git status                                  # must be clean
git rev-parse --abbrev-ref HEAD             # must print: feat/engine-opimization
source backend/venv/bin/activate            # venv exists at backend/venv/
```

If the branch is wrong or the working tree is dirty, STOP and ask.

---

## `.env` Cheat-Sheet — exactly what to set for each phase

Three test modes. Each needs different `.env` values plus a different bench-stack
docker compose invocation. Always mutate `.env` via `sed -i` between phases (the
bench stack reads `.env` via `env_file:` just like production). All compose
commands assume `BENCH_COMPOSE="-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.bench.yml"`
is exported in your shell.

| Phase | Mode | Required `.env` | Start command |
|---|---|---|---|
| 1, 5 | **GPU 0 alone (A6000)** — solo worker | `GPU_DEVICE_ID=0`, `GPU_CONCURRENT_REQUESTS=N`, `ENGINE_GPU_SPLIT=false`, `GPU_SCALE_ENABLED=false` | `docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker` |
| 2 | **GPU 1 alone (3080 Ti)** — solo worker | `GPU_DEVICE_ID=1`, `GPU_CONCURRENT_REQUESTS=N`, `ENGINE_GPU_SPLIT=false`, `GPU_SCALE_ENABLED=false` | `docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker` |
| 3 | **Both GPUs, parallel pipelines** — dual-GPU `--gpu-scale` | `GPU_SCALE_ENABLED=true`, `GPU_SCALE_DEFAULT_WORKER=1`, `GPU_DEVICE_ID=1`, `GPU_CONCURRENT_REQUESTS=<3080Ti_best>`, `GPU_SCALE_DEVICE_ID=0`, `GPU_SCALE_WORKERS=<A6000_best>`, `ENGINE_GPU_SPLIT=false` | `docker compose $BENCH_COMPOSE down --remove-orphans && COMPOSE_PROFILES=gpu-scale docker compose $BENCH_COMPOSE -f docker-compose.gpu-scale.yml up -d` |
| 4 | **Both GPUs, one pipeline split** — `--with-gpu-split` | `ENGINE_GPU_SPLIT=true`, `GPU_TRANSCRIBE_DEVICE_ID=0`, `GPU_DIARIZE_DEVICE_ID=1`, `GPU_CONCURRENT_REQUESTS=N`, `GPU_SCALE_ENABLED=false` | `docker compose $BENCH_COMPOSE down --remove-orphans && COMPOSE_PROFILES=gpu-split docker compose $BENCH_COMPOSE up -d` |

**One-time stack bring-up before Phase 1:** `./opentr.sh bench start current`
(wipes any existing bench volumes — destructive only to bench, not production
— then builds and starts the stack on the current branch).

In **Phase 3** (`--gpu-scale` dual-GPU), the layout is:

```
GPU 0 (A6000, 49 GB)  →  celery-worker-gpu-scaled  (GPU_SCALE_WORKERS parallel tasks, one pipeline per task)
GPU 1 (3080 Ti, 12 GB) →  celery-worker            (GPU_CONCURRENT_REQUESTS parallel tasks, one pipeline per task)
GPU 2 (A6000 LLM)     →  untouched
```

This matches the dual-GPU example baked into `docker-compose.gpu-scale.yml`'s
header comment. The two workers pull from the same `gpu` queue, so Celery
load-balances files across them — Phase 3 measures **aggregate** throughput of
the host with both cards busy on independent pipelines.

In **Phase 4** (`--with-gpu-split`), the layout is:

```
GPU 0 (A6000)  →  celery-worker-gpu-transcribe  (WhisperX/CTranslate2, GPU_CONCURRENT_REQUESTS in flight)
GPU 1 (3080 Ti) →  celery-worker-gpu-diarize    (PyAnnote, GPU_CONCURRENT_REQUESTS in flight)
GPU 2 (A6000 LLM) → untouched
```

Each file's pipeline is split across both cards: transcribe runs on the A6000,
diarize runs on the 3080 Ti. Per-file latency *should* drop because the two
stages run on separate VRAM budgets, but aggregate throughput is gated by the
slower of the two stages (likely diarize on the 3080 Ti). This is expected to
**under-perform Phase 3** on aggregate throughput — its value is the latency-vs-throughput
trade-off comparison.

**Always-true settings (do not change between phases):**
```
ENABLE_BENCHMARK_TIMING=true
ENABLE_VRAM_PROFILING=true
WHISPER_MODEL=large-v3-turbo
ENGINE_GPU_SPLIT=false               # we are NOT using gpu-split (that's a different mode)
```

**Sanity-check the active mode after every restart:**

- **Solo mode (Phases 1–4)** — exactly one GPU container:
  ```bash
  docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker'
  # Expect: opentranscribe-celery-worker  (and nothing with -gpu-scaled / -gpu-transcribe / -gpu-diarize)
  docker exec opentranscribe-celery-worker env | grep -E 'GPU_DEVICE_ID|CUDA_VISIBLE_DEVICES|GPU_CONCURRENT_REQUESTS'
  ```
- **Dual-GPU `--gpu-scale` mode (Phase 3)** — two GPU containers, one per card:
  ```bash
  docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker'
  # Expect both:  opentranscribe-celery-worker  AND  opentranscribe-celery-worker-gpu-scaled
  # The default worker (celery-worker) runs on GPU_DEVICE_ID; gpu-scaled runs on GPU_SCALE_DEVICE_ID.
  docker exec opentranscribe-celery-worker             env | grep -E 'GPU_DEVICE_ID|CUDA_VISIBLE_DEVICES|GPU_CONCURRENT_REQUESTS'
  docker exec opentranscribe-celery-worker-gpu-scaled  env | grep -E 'GPU_SCALE_DEVICE_ID|CUDA_VISIBLE_DEVICES|GPU_CONCURRENT_REQUESTS'
  # Inside containers, CUDA_VISIBLE_DEVICES is always '0' (Docker maps the reserved card to index 0);
  # what matters is which host card was reserved. Cross-check on the host:
  watch -n 2 "nvidia-smi --query-gpu=index,memory.used,utilization.gpu --format=csv,noheader | grep -E '^[01],'"
  ```
- **`--with-gpu-split` mode (Phase 4)** — two specialized GPU containers, one per stage:
  ```bash
  docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker'
  # Expect: opentranscribe-celery-worker-gpu-transcribe  AND  opentranscribe-celery-worker-gpu-diarize
  # (The plain opentranscribe-celery-worker may also exist, but won't receive gpu-queue tasks.)
  docker exec opentranscribe-celery-worker-gpu-transcribe env | grep -E 'GPU_TRANSCRIBE_DEVICE_ID|CUDA_VISIBLE_DEVICES|ENGINE_GPU_SPLIT'
  docker exec opentranscribe-celery-worker-gpu-diarize    env | grep -E 'GPU_DIARIZE_DEVICE_ID|CUDA_VISIBLE_DEVICES|ENGINE_GPU_SPLIT'
  # Both must show ENGINE_GPU_SPLIT=true. CUDA_VISIBLE_DEVICES is '0' inside both.
  ```

---

## Phase 0 — Prerequisites (bench stack bring-up, env checks, corpus build)

### 0.0 Export the bench-compose flags and verify the bench stack can build

```bash
export BENCH_COMPOSE="-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.bench.yml"
echo "BENCH_COMPOSE = $BENCH_COMPOSE"
# Sanity check: compose can parse the file set on this branch.
docker compose $BENCH_COMPOSE config --quiet && echo "compose config OK"
```

If compose can't parse the file set, STOP and report. Common causes: a
missing image build context, a syntax error introduced in the branch.


### 0.1 Required `.env` settings — *uncommented* and set to `true`

Both benchmark instrumentation flags are gated on env vars that are
**commented out by default**. Verify they are present, uncommented, and
truthy. A bare `grep ENABLE_BENCHMARK_TIMING .env` is insufficient — it will
match the commented-out example line and look like a pass.

```bash
grep -E '^ENABLE_BENCHMARK_TIMING=(true|1|yes|on)$' .env  || echo "MISSING/FALSE"
grep -E '^ENABLE_VRAM_PROFILING=(true|1|yes|on)$'   .env  || echo "MISSING/FALSE"
grep -E '^GPU_DEVICE_ID=[01]$'                       .env  || echo "GPU_DEVICE_ID not 0 or 1"
grep -E '^GPU_CONCURRENT_REQUESTS='                  .env  || echo "GPU_CONCURRENT_REQUESTS missing"
grep -E '^WHISPER_MODEL=large-v3-turbo$'             .env  || echo "WHISPER_MODEL not large-v3-turbo"
```

If any line prints `MISSING/FALSE` (or the GPU_DEVICE_ID line errors), STOP
and ask the user before editing `.env` — `.env` is gitignored and may contain
secrets. Tell the user exactly which lines to fix.

The required values for the sweep:

```
ENABLE_BENCHMARK_TIMING=true
ENABLE_VRAM_PROFILING=true
GPU_DEVICE_ID=0                  # start on the A6000 (slot 0). Will change to 1 for 3080 Ti phase. Never set to 2 — slot 2 hosts the LLM.
GPU_CONCURRENT_REQUESTS=1        # starting value; sweep script will mutate this.
WHISPER_MODEL=large-v3-turbo
```

### 0.1.1 Verify every key the sweep will mutate is present in `.env`

The soak's `sed -i 's/^KEY=.*/KEY=NEW/'` calls only mutate lines that already
exist. If a key is missing, the sed silently no-ops and the worker runs in the
wrong mode. Confirm every key below has a line in `.env` (any value — the
sweep will overwrite it):

```bash
REQUIRED_KEYS=(
    GPU_DEVICE_ID
    GPU_CONCURRENT_REQUESTS
    GPU_SCALE_ENABLED
    GPU_SCALE_DEFAULT_WORKER
    GPU_SCALE_DEVICE_ID
    GPU_SCALE_WORKERS
    GPU_TRANSCRIBE_DEVICE_ID
    GPU_DIARIZE_DEVICE_ID
    WHISPER_MODEL
    ENABLE_BENCHMARK_TIMING
    ENABLE_VRAM_PROFILING
)
MISSING=0
for k in "${REQUIRED_KEYS[@]}"; do
    if ! grep -qE "^${k}=" .env; then
        echo "MISSING: $k"
        MISSING=1
    fi
done
# ENGINE_GPU_SPLIT is special — the soak appends it with `echo` if missing,
# so it doesn't need to pre-exist. Just verify if present, it's a known value.
grep -qE '^ENGINE_GPU_SPLIT=' .env \
    && echo "ENGINE_GPU_SPLIT present: $(grep '^ENGINE_GPU_SPLIT=' .env)" \
    || echo "ENGINE_GPU_SPLIT not present — soak will add it (OK)"
[[ "$MISSING" = "1" ]] && echo "STOP — add the missing keys to .env before continuing"
```

If any keys are missing, STOP and tell the user exactly which lines to add
(use `.env.example` as the source of default values).

### 0.2 Bring up the bench stack (fresh volumes, current branch)

```bash
# Stops any running prod/dev stack on this host AND wipes bench volumes,
# builds backend/frontend images on the current branch, brings the bench
# stack up.
./opentr.sh bench start current
```

If a production stack was running, this stops it first. Production data on NAS
or in production volumes is **not** touched (the bench volumes are entirely
separate named volumes).

Verify:
```bash
docker compose $BENCH_COMPOSE ps
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'opentranscribe-(celery-worker|backend|postgres|redis|opensearch|minio)'
```

You should see exactly ONE GPU worker (`opentranscribe-celery-worker`) and the
infrastructure services. No `gpu-scaled`/`gpu-transcribe`/`gpu-diarize` workers
yet — those activate in Phases 3 and 4.

### 0.3 Target GPUs are idle

```bash
nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv
```

GPU 0 and GPU 1 should be near-idle (single-digit % util, low memory). GPU 2
will likely show significant VRAM in use (the LLM) — that is expected; do not
touch it.

### 0.4 Inventory `benchmark/test_audio/` and create the admin user

```bash
ls -la benchmark/test_audio/ | grep -E '\.(wav|mp3|m4a|mp4|flac)$' | wc -l
ls -la benchmark/test_audio/ | grep -E '\.(wav|mp3|m4a|mp4|flac)$'
```

Record the count `N_AUDIO`. The soak will use whatever's here — minimum 1, ideal
≥ 24 for full concurrency range. **If N_AUDIO < 5, STOP and tell the user the
bench corpus is too small for meaningful sweep data.**

Create the admin user in the fresh bench DB:
```bash
# Use the same default creds the benchmark scripts expect.
docker compose $BENCH_COMPOSE exec -T backend python -c "
from app.db.session_utils import session_scope
from app.models.user import User
from app.core.security import hash_password
with session_scope() as db:
    if not db.query(User).filter(User.email=='admin@example.com').first():
        db.add(User(email='admin@example.com', hashed_password=hash_password('password'),
                    full_name='Bench Admin', is_active=True, is_superuser=True, role='super_admin'))
        db.commit()
        print('admin user created')
    else:
        print('admin user already exists')
"
```

Confirm auth works:
```bash
curl -s -X POST http://localhost:5174/api/auth/token \
    -d 'username=admin@example.com&password=password' | python -m json.tool | head -5
```
You should see an `access_token`.

### 0.5 Upload every audio file from `benchmark/test_audio/` and build `corpus.json`

Uploads each file via the API at conc=1 (sequential), waits for each to reach
`status=completed`, then writes `docs/benchmark-corpus/corpus.json` from the
resulting bench-DB UUIDs. The fresh transcripts ARE the first soak data point
(a sequential conc=1 baseline across the whole corpus) — record their wall
times in the final report.

```bash
mkdir -p docs/benchmark-corpus

python - <<'PY'
"""Upload all benchmark/test_audio/ files into the bench DB and emit corpus.json."""
import json, os, requests, time
from pathlib import Path

BACKEND = os.environ.get("BACKEND_URL", "http://localhost:5174")
EMAIL = os.environ.get("BENCHMARK_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("BENCHMARK_PASSWORD", "password")
AUDIO_DIR = Path("benchmark/test_audio")
EXTS = {".wav", ".mp3", ".m4a", ".mp4", ".flac"}

# Auth
r = requests.post(f"{BACKEND}/api/auth/token",
                  data={"username": EMAIL, "password": PASSWORD}, timeout=15)
r.raise_for_status()
token = r.json()["access_token"]
H = {"Authorization": f"Bearer {token}"}

files = sorted(p for p in AUDIO_DIR.iterdir() if p.suffix.lower() in EXTS)
if not files:
    raise SystemExit("No audio files found in benchmark/test_audio/ — aborting.")

print(f"Uploading {len(files)} files sequentially. This pre-populates the bench DB.")
uploaded = []
for i, p in enumerate(files, 1):
    print(f"  [{i}/{len(files)}] uploading {p.name} ({p.stat().st_size/1e6:.1f} MB) ...")
    with open(p, "rb") as fh:
        r = requests.post(f"{BACKEND}/api/files/", headers=H,
                          files={"file": (p.name, fh, "audio/wav")}, timeout=600)
    r.raise_for_status()
    j = r.json()
    file_id = j.get("uuid") or j.get("id")
    uploaded.append({"path": str(p), "filename": p.name, "uuid": file_id})
    print(f"      uploaded → uuid={file_id}")

# Wait for each to complete
print("\nWaiting for all uploads to finish initial pipeline run ...")
pending = {x["uuid"] for x in uploaded}
deadline = time.time() + 6 * 3600   # 6-hour cap; usually finishes much sooner
while pending and time.time() < deadline:
    for uuid in list(pending):
        info = requests.get(f"{BACKEND}/api/files/{uuid}/info", headers=H, timeout=15).json()
        status = info.get("status", "unknown")
        if status == "completed":
            for u in uploaded:
                if u["uuid"] == uuid:
                    u["duration_s"] = float(info.get("duration") or 0)
                    u["size_mb"] = int(info.get("file_size", 0)) // (1024 * 1024)
            pending.discard(uuid)
            print(f"  [{len(uploaded)-len(pending)}/{len(uploaded)}] {uuid} → completed "
                  f"({info.get('duration', 0):.0f}s audio)")
        elif status == "error":
            raise SystemExit(f"File {uuid} entered error state; aborting.")
    if pending:
        time.sleep(10)

# Tier classification + corpus.json emission
def tier(duration_s: float) -> int:
    if duration_s <  25 * 60: return 1
    if duration_s <  60 * 60: return 2
    if duration_s < 180 * 60: return 3
    return 4

uploaded.sort(key=lambda x: x["duration_s"])
corpus = {
    "version": "bench-soak-2026-05-21",
    "tiers": {
        "1": {"label": "Short",      "range": "< 25 min"},
        "2": {"label": "Medium",     "range": "25-60 min"},
        "3": {"label": "Long",       "range": "1-3 h"},
        "4": {"label": "Extra-long", "range": "> 3 h"},
    },
    "files": [
        {"uuid": u["uuid"], "filename": u["filename"],
         "duration_s": u["duration_s"], "size_mb": u["size_mb"],
         "tier": tier(u["duration_s"])}
        for u in uploaded
    ],
}
# Build profiles by tier — round-robin for "mixed", duration-ascending for "by_duration".
by_idx = list(range(len(corpus["files"])))
tier_buckets: dict[int, list[int]] = {1: [], 2: [], 3: [], 4: []}
for i, f in enumerate(corpus["files"]):
    tier_buckets[f["tier"]].append(i)
mixed = []
while any(tier_buckets.values()):
    for t in (1, 2, 3, 4):
        if tier_buckets[t]:
            mixed.append(tier_buckets[t].pop(0))
corpus["profiles"] = {
    "by_duration": {"description": "Duration-ascending — VRAM ceiling tests fail fast.",
                    "indices": by_idx},
    "mixed":       {"description": "Tier round-robin — realistic scheduler stress.",
                    "indices": mixed},
}
Path("docs/benchmark-corpus/corpus.json").write_text(json.dumps(corpus, indent=2))
print(f"\nWrote corpus.json with {len(uploaded)} files "
      f"({sum(u['duration_s'] for u in uploaded)/3600:.2f} h total audio).")
PY
```

Wall clock for Phase 0.5: roughly sum(durations) × ~3 min/min-audio (the
initial pipeline runs at conc=1). With ~16 h of audio (5 default files),
expect ~50 min. With 24 files averaging similar durations, expect 2–3 h.
This data is recorded — it serves as the soak's "sequential conc=1" baseline.

### 0.6 Dry-run the parallel benchmark against the new corpus

```bash
N_CORPUS=$(python -c "import json; print(len(json.load(open('docs/benchmark-corpus/corpus.json'))['files']))")
echo "Corpus has $N_CORPUS files."

BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile mixed \
    --batches "$N_CORPUS" \
    --dry-run
```

You should see `Found $N_CORPUS files` and a batch listing matching the
upload order. The bench DB is now ready for the sweep phases.

### 0.7 Live VRAM monitor (open in a second terminal — leave running for the whole sweep)

```bash
watch -n 2 "nvidia-smi --query-gpu=index,memory.used,memory.total,utilization.gpu,temperature.gpu \
  --format=csv,noheader | grep -E '^[01],'"
```

If you cannot keep a second terminal open, run periodic samples:
`nvidia-smi --id=0 --query-gpu=memory.used,utilization.gpu --format=csv,noheader`.

---

## Phase plan overview

This soak runs against branch `feat/engine-opimization` which contains the
GPU/engine optimizations we are validating. **All concurrency levels are
re-measured on this branch** — the published numbers in `BENCHMARK_RESULTS.md`
were taken before these optimizations and are not a valid baseline. Treat
them as historical context only.

| Phase | Mode | Runs | Wall clock |
|---|---|---|---|
| 0 | Bench bring-up + corpus build | `bench start current`, upload N audio files at conc=1 | depends on `N_CORPUS` and total audio (~3 min/min-audio at conc=1) |
| 1 | A6000 solo (GPU 0) | single-file e2e baseline (3 iter) + sweep at conc 1, 4, 8, 10, 12, 14, 16, 20, 24 — capped at `N_CORPUS` | ~2 h (at full 24-file corpus) |
| 2 | 3080 Ti solo (GPU 1) | single-file e2e baseline (3 iter) + sweep at conc 1, 2, 3, 4 | ~1 h |
| 3 | Dual-GPU `--gpu-scale` | one full-corpus run at `A6000_BEST` + `TI_BEST` | ~15 min |
| 4 | `--with-gpu-split` | one full-corpus run at `TI_BEST` | ~20 min |
| 5 | Duration curve, GPU 0 conc=1, sequential | all `N_CORPUS` files | ~45 min (at 24 files) |

Total wall clock: ~5–7 h depending on how big the bench corpus is. Run in
order 0 → 1 → 2 → 3 → 4 → 5.

**Corpus-size note.** `benchmark_parallel.py` silently drops batches larger
than `N_CORPUS`, and the reprocess API cancels in-flight tasks on the same
UUID — so the same file can't occupy two worker threads. Concurrency levels
above `N_CORPUS` are automatically skipped. The user populates
`benchmark/test_audio/` before launching; if fewer than ~16 files are
present, conc=16/20/24 sweep rows will be marked `N/A — corpus capped at
N_CORPUS files` in the final report.

The conc=`N_CORPUS` run is the **full-corpus aggregate RTF** headline number
— no separate run needed.

---

## Phase 1 — A6000 solo (GPU 0): single-file baseline + full concurrency sweep

This is the headline of the soak. We re-measure **every** level on this branch
so the new optimizations get a fresh apples-to-apples comparison against the
pre-optimization numbers in `docs/BENCHMARK_RESULTS.md`. Do **not** skip any
level.

### 1.0 Single-file e2e baseline (3 warm iterations)

Captures per-stage timing (preprocess / queue / GPU / postprocess) on the 0.5 h
synthetic anchor file. This is the data point that surfaces stage-level changes
from the optimization commits — without it, only aggregate RTF is visible.

```bash
mkdir -p benchmarks docs/engine-benchmark-results

# Ensure solo mode, on GPU 0, conc=1
sed -i 's/^GPU_SCALE_ENABLED=.*/GPU_SCALE_ENABLED=false/'             .env
sed -i 's/^GPU_SCALE_DEFAULT_WORKER=.*/GPU_SCALE_DEFAULT_WORKER=0/'   .env
grep -q '^ENGINE_GPU_SPLIT=' .env \
    && sed -i 's/^ENGINE_GPU_SPLIT=.*/ENGINE_GPU_SPLIT=false/' .env \
    || echo 'ENGINE_GPU_SPLIT=false' >> .env
sed -i 's/^GPU_DEVICE_ID=.*/GPU_DEVICE_ID=0/'                         .env
sed -i 's/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=1/'     .env

docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker && sleep 60
docker exec opentranscribe-celery-worker env | grep -E 'GPU_DEVICE_ID|GPU_CONCURRENT_REQUESTS'

BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_e2e.py \
    --file-uuid ce471b5a-b4ae-45e5-8905-af7420d50f79 \
    --iterations 3 --detailed \
    --output "docs/engine-benchmark-results/e2e_a6000_gpu0_$(date +%Y%m%d).csv"
```

Acceptance: 3 iterations complete. Warm GPU time should be in the same ballpark
as the published 41.9 s (RTF ~44×). A *meaningful change* in either direction
is the optimization result — capture it verbatim in the report. If warm GPU
time exceeds 60 s, STOP and report — something else is wrong.

### 1.1 Concurrency sweep — every level (the worker stays running between levels in *solo* mode; only restart when needed)

```bash
# Sweep all relevant levels on the branch. Levels 1, 4, 8, 10, 12 are
# re-measured (not reused from history). 14, 16 are the new whitepaper
# territory. 20, 24 require the expanded 24-file corpus from Phase 0.5.

for CONC in 1 4 8 10 12 14 16 20 24; do
    sed -i "s/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=$CONC/" .env
    docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker && sleep 60

    docker exec opentranscribe-celery-worker env | grep -E 'GPU_DEVICE_ID|GPU_CONCURRENT_REQUESTS'

    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
    python scripts/benchmark_parallel.py \
        --corpus-file docs/benchmark-corpus/corpus.json \
        --profile mixed --shuffle \
        --batches "$CONC" \
        --gpu-id 0 \
        --cooldown 0 \
        --output "docs/engine-benchmark-results/a6000_conc${CONC}_$(date +%Y%m%d_%H%M%S)/" \
        2>&1 | tee -a "benchmarks/phase1_a6000_$(date +%Y%m%d).log"

    # Hard OOM check — break the loop, do not continue to higher levels.
    if docker logs opentranscribe-celery-worker --tail=400 2>&1 | grep -iE 'out of memory|cuda.*oom'; then
        echo "OOM at conc=$CONC — stopping A6000 sweep."
        break
    fi
done
```

**Headline metric:** the `*** Full-corpus aggregate RTF (batch=16): X× ***`
line in the conc=16 output is the whitepaper full-corpus aggregate RTF — same
number, no separate run. If you expanded to 24 files in Phase 0.5, the
`batch=24` run also prints a corpus-wide line; record both.

**Phase 1 stop criteria** — abort if any hit:

- Peak VRAM > 49 500 MB (plateau broken — new allocation).
- Aggregate RTF drops below 40× at any level (SM contention too severe to push further).
- Any CUDA OOM in worker logs (the loop checks and breaks).
- Any file ends `status=error` more than twice in one batch.

Record `A6000_BEST` = the concurrency level with the best aggregate RTF that
stayed within the VRAM ceiling. Phase 3 needs this value.

**Optimization-vs-baseline check:** after the sweep finishes, list each level's
new RTF and VRAM next to the historical `docs/BENCHMARK_RESULTS.md` numbers
(Test 2 table). Highlight any improvement or regression > 3 %. This delta is
the load-bearing claim for the whitepaper update.

---

## Phase 2 — 3080 Ti solo (GPU 1): single-file baseline + concurrency sweep

### 2.0 Single-file e2e baseline (3 warm iterations on the 0.5 h anchor)

```bash
sed -i 's/^GPU_DEVICE_ID=.*/GPU_DEVICE_ID=1/'                       .env
sed -i 's/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=1/'   .env

docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker && sleep 60
docker exec opentranscribe-celery-worker env | grep -E 'GPU_DEVICE_ID|GPU_CONCURRENT_REQUESTS'

BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_e2e.py \
    --file-uuid ce471b5a-b4ae-45e5-8905-af7420d50f79 \
    --iterations 3 --detailed \
    --output "docs/engine-benchmark-results/e2e_3080ti_gpu1_$(date +%Y%m%d).csv"
```

If conc=1 OOMs (the 3080 Ti can't host `large-v3-turbo` natively), STOP and ask.
Hybrid mode or a smaller model fixes it but changes the test scope.

### 2.1 Concurrency sweep (conc 1 — 4)

Uses `by_duration` profile (Tier 1 only, short files) so OOM fails fast on a
12 GB card.

```bash
for CONC in 1 2 3 4; do
    sed -i "s/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=$CONC/" .env
    docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker && sleep 60

    docker exec opentranscribe-celery-worker env | grep -E 'GPU_DEVICE_ID|GPU_CONCURRENT_REQUESTS'

    BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
    python scripts/benchmark_parallel.py \
        --corpus-file docs/benchmark-corpus/corpus.json \
        --profile by_duration \
        --batches "$CONC" \
        --gpu-id 1 \
        --cooldown 10 \
        --output "docs/engine-benchmark-results/3080ti_conc${CONC}_$(date +%Y%m%d_%H%M%S)/" \
        2>&1 | tee -a "benchmarks/phase2_3080ti_$(date +%Y%m%d).log"

    if docker logs opentranscribe-celery-worker --tail=300 2>&1 | grep -iE 'out of memory|cuda.*oom'; then
        echo "OOM at conc=$CONC — stopping 3080 Ti sweep."
        break
    fi
done
```

**Phase 2 stop criteria** — abort if any hit:

- Peak VRAM > 11 500 MB (95 % of 12 GB).
- CUDA OOM (loop breaks).
- Aggregate RTF below 20× (not worth pushing further — 3080 Ti is small).

**Note:** if `conc=1` already OOMs, the 3080 Ti cannot host `large-v3-turbo`
natively. STOP and ask — fixing it (hybrid mode, smaller model) changes the
test scope.

Record `TI_BEST` = highest stable concurrency. Likely 2 or 3.

---

## Phase 3 — Dual-GPU `--gpu-scale` full corpus (GPU 0 + GPU 1, parallel pipelines)

Both cards busy on **independent** file pipelines. Uses Phase 1 + 2 best values.

```bash
A6000_BEST=16             # <-- edit from Phase 1 result (12, 14, or 16)
TI_BEST=2                 # <-- edit from Phase 2 result (1, 2, or 3)

sed -i 's/^GPU_SCALE_ENABLED=.*/GPU_SCALE_ENABLED=true/'             .env
sed -i 's/^GPU_SCALE_DEFAULT_WORKER=.*/GPU_SCALE_DEFAULT_WORKER=1/'  .env
sed -i 's/^GPU_DEVICE_ID=.*/GPU_DEVICE_ID=1/'                        .env
sed -i "s/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=$TI_BEST/" .env
sed -i 's/^GPU_SCALE_DEVICE_ID=.*/GPU_SCALE_DEVICE_ID=0/'            .env
sed -i "s/^GPU_SCALE_WORKERS=.*/GPU_SCALE_WORKERS=$A6000_BEST/"      .env
sed -i 's/^ENGINE_GPU_SPLIT=.*/ENGINE_GPU_SPLIT=false/'              .env

docker compose $BENCH_COMPOSE down --remove-orphans && COMPOSE_PROFILES=gpu-scale docker compose $BENCH_COMPOSE -f docker-compose.gpu-scale.yml up -d && sleep 75

# Confirm BOTH containers are alive and on the right cards
docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker'
docker exec opentranscribe-celery-worker             env | grep -E 'GPU_DEVICE_ID|GPU_CONCURRENT_REQUESTS'
docker exec opentranscribe-celery-worker-gpu-scaled  env | grep -E 'GPU_SCALE_DEVICE_ID|GPU_CONCURRENT_REQUESTS'

BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile mixed --shuffle \
    --batches 16 \
    --gpu-id 0 \
    --cooldown 0 \
    --output "docs/engine-benchmark-results/dual_scale_a6000_${A6000_BEST}_ti_${TI_BEST}_$(date +%Y%m%d)/"
```

### Phase 3 expectations & stop criteria

- Aggregate RTF should be roughly `(Phase 1 RTF at A6000_BEST) + (Phase 2 RTF at TI_BEST)`
  minus 10–15 % overhead. Big shortfall = scheduler contention.
- Per-card VRAM should match the solo numbers (GPU 0 ~48.5 GB, GPU 1 ~11 GB).
- Stop if either card exceeds its safe ceiling or any file errors more than twice.

---

## Phase 4 — `--with-gpu-split` full corpus (one pipeline per file, split across both GPUs)

Lower priority — expected to under-perform Phase 3 on aggregate throughput,
the value is the latency comparison. Run last so a failure here doesn't block
the higher-priority phases.

```bash
SPLIT_CONC=$TI_BEST       # diarize on the 3080 Ti gates the pipeline; cap at TI_BEST

sed -i 's/^GPU_SCALE_ENABLED=.*/GPU_SCALE_ENABLED=false/'             .env
sed -i 's/^GPU_SCALE_DEFAULT_WORKER=.*/GPU_SCALE_DEFAULT_WORKER=0/'   .env
sed -i 's/^ENGINE_GPU_SPLIT=.*/ENGINE_GPU_SPLIT=true/'                .env
sed -i 's/^GPU_TRANSCRIBE_DEVICE_ID=.*/GPU_TRANSCRIBE_DEVICE_ID=0/'   .env
sed -i 's/^GPU_DIARIZE_DEVICE_ID=.*/GPU_DIARIZE_DEVICE_ID=1/'         .env
sed -i "s/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=$SPLIT_CONC/" .env

docker compose $BENCH_COMPOSE down --remove-orphans && COMPOSE_PROFILES=gpu-split docker compose $BENCH_COMPOSE up -d && sleep 75

docker ps --format '{{.Names}}' | grep -E '^opentranscribe-celery-worker-gpu-'
docker exec opentranscribe-celery-worker-gpu-transcribe env | grep -E 'ENGINE_GPU_SPLIT|GPU_TRANSCRIBE_DEVICE_ID|CUDA_VISIBLE_DEVICES'
docker exec opentranscribe-celery-worker-gpu-diarize    env | grep -E 'ENGINE_GPU_SPLIT|GPU_DIARIZE_DEVICE_ID|CUDA_VISIBLE_DEVICES'

BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile mixed --shuffle \
    --batches 16 \
    --gpu-id 0 \
    --cooldown 0 \
    --output "docs/engine-benchmark-results/gpusplit_conc${SPLIT_CONC}_$(date +%Y%m%d)/"
```

### Phase 4 expectations & stop criteria

- Per-file wall time should be **lower** than Phase 1 conc=1 (less VRAM contention per stage).
- Aggregate RTF will likely be **lower** than Phase 3 (one slow card gates the pipeline) — that is the interesting result.
- Stop on any 3080 Ti OOM: `docker logs opentranscribe-celery-worker-gpu-diarize --tail=300 | grep -i oom`.

---

## Phase 5 — Duration curve (GPU 0 solo, conc=1, sequential)

RTF-vs-duration curve for the whitepaper figure. Sequential, one file at a
time, full 16-file corpus, smallest to largest. The Phase 4 transition already
mutated `.env` — restore solo defaults first.

```bash
sed -i 's/^ENGINE_GPU_SPLIT=.*/ENGINE_GPU_SPLIT=false/'              .env
sed -i 's/^GPU_SCALE_ENABLED=.*/GPU_SCALE_ENABLED=false/'            .env
sed -i 's/^GPU_SCALE_DEFAULT_WORKER=.*/GPU_SCALE_DEFAULT_WORKER=0/'  .env
sed -i 's/^GPU_DEVICE_ID=.*/GPU_DEVICE_ID=0/'                        .env
sed -i 's/^GPU_CONCURRENT_REQUESTS=.*/GPU_CONCURRENT_REQUESTS=1/'    .env

docker compose $BENCH_COMPOSE up -d --force-recreate --no-deps celery-worker && sleep 60

BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password \
python scripts/benchmark_parallel.py \
    --corpus-file docs/benchmark-corpus/corpus.json \
    --profile by_duration --sequential \
    --cooldown 10 \
    --gpu-id 0 \
    --output "docs/engine-benchmark-results/duration_curve_a6000_$(date +%Y%m%d)/"
```

Wall clock: ~30–45 min (24 files run back-to-back at ~1.5 min/min audio average, post-expansion).

---

## After all phases — update docs and (maybe) code

### Files you are allowed to edit

1. **CREATE** `docs/BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md` — a new file that
   documents this branch's bench-stack soak results. Required sections:
   - **Branch & commit** — `feat/engine-opimization` at `$(git rev-parse HEAD)`.
   - **Test conditions** — bench stack (isolated volumes), corpus contents,
     hardware, model, dates. List every bench file UUID with filename, duration,
     tier so the run is reproducible.
   - **Single-file e2e** — 3-iteration warm GPU time, RTF, per-stage breakdown
     for the anchor file on GPU 0 and GPU 1.
   - **Concurrency sweep** — one row per level with `conc | peak VRAM | aggregate RTF | GPU util% | stable?`.
   - **Multi-GPU comparison** — Phase 3 (`--gpu-scale`) and Phase 4
     (`--with-gpu-split`) full-corpus RTFs side-by-side with the Phase 1
     full-corpus solo number.
   - **Duration curve** — Phase 5 sequential per-file numbers (RTF vs duration).
   - **Comparison to pre-optimization baseline** — call out the **scaling
     pattern** comparison vs `BENCHMARK_RESULTS.md` Test 2 (VRAM-plateau
     shape, RTF curve shape, throughput peak conc). The bench corpus contents
     differ from the historical 5-file production set, so absolute RTF deltas
     are **not** apples-to-apples — only the curve shape and per-conc scaling
     behavior are comparable. State this explicitly so readers don't
     misinterpret raw delta numbers.
   - **Net findings** — 5-bullet "what changed" summary citing specific
     measurable scaling or stability improvements (the whitepaper bullet points).
2. `docs/gpu-concurrency-soak-test-plan.md` — fill in the TODO cells:
   - GPU 1 / 3080 Ti table (rows A–D) — from Phase 2.
   - "GPU 2 / A6000" table (rows F, G, H, I for conc 14, 16, 20, 24).
     **Rename the heading from "GPU 2 — RTX A6000" to "GPU 0 — RTX A6000"
     and update the `CUDA_VISIBLE_DEVICES=2` note. That label is stale (GPU 2
     hosts the LLM); the A6000 we tested is slot 0.**
   - "Safe maximum" / "Recommended default" lines below each table.
   - "Result Tables" section at the bottom (rebrand any GPU-index labels to GPU 0).
   - "Formula Update Criteria" section if the formula needs revision.
   - "Hardware Inventory" table — fix the role columns so GPU 0 reads
     "Primary A6000 (this soak test target)" and GPU 2 reads "Reserved for
     LLM — not touched by this test."
   - Add a "Cross-reference" link to the new
     `BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md` for the optimization deltas.
3. `docs/BENCHMARK_RESULTS.md` — do **NOT** append to or modify this file.
   It is the pre-optimization production-data baseline and remains historical
   reference. The new bench-stack soak results live in their own file
   (`BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md`).
4. `backend/app/transcription/config.py` — `_auto_concurrent()` **only if** the
   new data changes the safe ceiling. Current code:
   ```python
   concurrent = int((total_mb - 7000) // 4000)
   return max(1, min(concurrent, 12))
   ```
   - Use the new (re-measured) RTF and VRAM curve to pick the cap, not the
     old `BENCHMARK_RESULTS.md` numbers.
   - If the highest stable level on this branch is 16 → raise cap to 16.
   - If 20 or 24 also stayed within plateau and RTF ≥ 40× on the expanded
     corpus → raise accordingly.
   - If the new conc=12 measurement is *worse* than the historical one,
     **investigate the optimization regression** before changing the cap.
   - Update the docstring with the new measured values.

You may NOT touch any other code file. If you think another file needs an
edit, stop and ask the user.

### Commit

```bash
git add docs/BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md \
        docs/gpu-concurrency-soak-test-plan.md \
        docs/engine-benchmark-results/ \
        backend/app/transcription/config.py
# NOTE: docs/benchmark-corpus/corpus.json was regenerated from the bench files.
# Do NOT commit it — the canonical corpus references production-data UUIDs.
# Restore it from git after the soak: `git checkout -- docs/benchmark-corpus/corpus.json`
git status                              # review before committing
git diff --stat HEAD                    # sanity check
git commit -m "perf(engine): soak test on optimized branch — <one-line headline delta>"

# Restore the production corpus.json so the file stays apples-to-apples for future runs.
git checkout -- docs/benchmark-corpus/corpus.json
```

Use conventional commits. Do NOT push. Do NOT merge. Tell the user the branch
is ready for review.

---

## Reporting back

Post a final summary to the chat with:

1. Hardware confirmation (`nvidia-smi` line for GPUs 0 & 1 before starting; GPU 2 left untouched).
2. Corpus expansion summary: how many files added, total corpus size, the new 8 UUIDs.
3. **Phase 1.0** A6000 single-file e2e: warm GPU time mean, RTF, per-stage breakdown — **next to** the historical 41.92 s warm-iter-2 baseline from `BENCHMARK_RESULTS.md` Test 1. Highlight the delta.
4. **Phase 1.1** A6000 sweep table: every level (1, 4, 8, 10, 12, 14, 16, 20, 24 — or wherever OOM stopped you) with `conc | peak VRAM | aggregate RTF | GPU util% | stable?` AND the historical RTF for levels 1, 4, 8, 10, 12 with a Δ column. Mark which row is the conc=16 full-corpus headline.
5. **Phase 2.0** 3080 Ti single-file e2e: warm GPU time mean, RTF, per-stage breakdown (no historical counterpart — this is the first 3080 Ti data).
6. **Phase 2.1** 3080 Ti sweep table (conc 1–4 or whichever completed before OOM): same columns.
7. **Phase 3** dual-GPU `--gpu-scale` full-corpus aggregate RTF + per-card VRAM. Compare to Phase 1 RTF + Phase 2 RTF predicted sum.
8. **Phase 4** `--with-gpu-split` full-corpus aggregate RTF + per-card VRAM + per-file wall-time vs Phase 1 conc=1.
9. **Phase 5** duration curve: RTF-vs-duration data points for all 24 files — alongside the historical Test 3 column from `BENCHMARK_RESULTS.md`.
10. **Whitepaper headline** numbers, verbatim: the `*** Full-corpus aggregate RTF ***` lines from Phase 1 conc=16 (and conc=24 if the expanded corpus run completed).
11. **Net optimization findings** — the 5-bullet summary from `BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md` "Net findings" section.
12. Recommended new `_auto_concurrent()` cap, with one-paragraph reasoning based on the **new** numbers.
13. Any anomalies, OOMs, retries, or files that failed.
14. Total wall-clock time used (expected: ~4–5 h).

Do not push or merge. Hand off to the user for review.

---

## Quick reference

| Thing | Location |
|---|---|
| Branch | `feat/engine-opimization` |
| Working dir | `/mnt/nvm/repos/transcribe-app` |
| Venv | `backend/venv/` |
| Corpus | `docs/benchmark-corpus/corpus.json` (16 files default; expanded to 24 in Phase 0.5) |
| Sweep script | `scripts/benchmark_concurrency_sweep.sh` |
| Parallel runner | `scripts/benchmark_parallel.py` (`--corpus-file`, `--profile`, `--shuffle`, `--sequential`, `--batches`, `--gpu-id`, `--cooldown`, `--dry-run`) |
| E2E single-file runner | `scripts/benchmark_e2e.py` (`--file-uuid`, `--iterations`, `--detailed`, `--output`) |
| Config to maybe update | `backend/app/transcription/config.py` `_auto_concurrent()` |
| Existing plan | `docs/gpu-concurrency-soak-test-plan.md` |
| Existing results | `docs/BENCHMARK_RESULTS.md` (conc 1–12 known) |
| 0.5 h anchor UUID | `ce471b5a-b4ae-45e5-8905-af7420d50f79` |
| Auth creds (test admin) | `admin@example.com` / `password` |

| GPU routing knob | Effect |
|---|---|
| `GPU_DEVICE_ID` in `.env` | Sets which host GPU the celery-worker container reserves. **This is the real routing knob.** |
| `--gpu-id N` (benchmark scripts) | Sets which host GPU `nvidia-smi` samples for VRAM data. **Monitoring only.** |
| `CUDA_VISIBLE_DEVICES` inside the container | Already set automatically by `docker-compose.gpu.yml` to `${GPU_DEVICE_ID}`. Do not override. |

| Sanity-check command | What it tells you |
|---|---|
| `docker exec opentranscribe-celery-worker env \| grep -E "GPU_DEVICE_ID\|CUDA_VISIBLE_DEVICES"` | The worker's GPU is correctly set |
| `docker logs opentranscribe-celery-worker --tail=200 \| grep -iE "concurrent_requests\|out of memory\|cuda.*oom"` | Worker concurrency confirmation + OOM check |
| `nvidia-smi --id=0 --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader` | Single-line GPU 0 snapshot |
