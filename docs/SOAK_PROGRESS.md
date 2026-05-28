# GPU Soak — Live Progress / Resume State

## HOW IT WORKS NOW (simple, no DB hacking)
One self-contained orchestrator: `scripts/soak_orchestrate.sh`. Flow:
1. **Phase 0 (once):** if the bench DB is empty, upload all 40 files from
   `benchmark/test_audio/` (a normal user upload) and build `corpus.json`.
2. **Phases 1-5 (back-to-back):** REPROCESS those same files at each GPU
   concurrency level — the app's normal reprocess feature. No per-level DB
   wiping, no TRUNCATE, nothing that can lock the database.
3. **End:** wipe the bench deployment (only `*_bench_data` volumes). All metrics
   are already saved to the host, so nothing is lost.

### Launch / resume (detached; survives session death)
```bash
cd /mnt/nvm/repos/transcribe-app
setsid bash scripts/soak_orchestrate.sh >/dev/null 2>&1 &
```
Re-running the same command resumes from the checkpoint (skips finished levels).

### Check progress (no tokens; run these yourself)
```bash
cd /mnt/nvm/repos/transcribe-app
tail -f benchmarks/soak_state/orchestrate.log       # live activity
cat   benchmarks/soak_state/checkpoint.txt           # completed phases/levels
column -t -s$'\t' benchmarks/soak_state/results.tsv  # captured metrics so far
pgrep -af soak_orchestrate.sh                         # confirm still running
```

- **Branch:** `feat/engine-opimization`  ·  **Working dir:** `/mnt/nvm/repos/transcribe-app`
- **Bench compose:** `-f docker-compose.yml -f docker-compose.gpu.yml -f docker-compose.bench.yml` (+ `-f docker-compose.bench-gpu.yml` for Phase 3/4)
- **Auth:** admin@example.com / password (bench DB)
- **Corpus:** 40 files in `benchmark/test_audio/` (tiers 1-4, 58h audio).

## DATA SAFETY GUARANTEE (verified)
The bench stack is fully isolated from the real dataset:
- Bench volumes: `postgres_bench_data`, `minio_bench_data`, `redis_bench_data`,
  `opensearch_bench_data`, `flower_bench_data` — DISTINCT names from prod
  (`postgres_data`, `minio_data`, ...). Zero overlap.
- `docker-compose.bench.yml` loads NO NAS bind mounts (those live in nas overlay, not loaded).
- `bench start current` wipes ONLY `*_bench_data`. Orchestrator `down` calls never use `-v`.
- Net: testing/restarting the bench deployment can never touch, change, or delete production data.

## What is tracked (where to look)
- **Files / corpus:** `docs/benchmark-corpus/corpus.json` (every UUID, duration, size, tier, conc=1 wall_s).
- **Per-run raw metrics:** `docs/engine-benchmark-results/<phase>_conc<N>_<ts>/benchmark_{files,summary,vram}_*.csv`.
- **Rolled-up metrics:** `benchmarks/soak_state/results.tsv` (phase, mode, conc, gpu, peak_vram_mb, agg_rtf, util, stable).
- **Progress / checkpoint:** `benchmarks/soak_state/checkpoint.txt` (completed units) + `orchestrate.log` (live).
- **Phase 0 upload log:** `benchmarks/phase0_upload_*.log`.

## Deviations from the handoff doc (already fixed)
- Upload endpoint is `POST /api/files` (NO trailing slash) + `X-File-Hash` header. Doc said `/api/files/` → 404.
- Status polling endpoint is `GET /api/files/{uuid}` (NOT `/info`). Field: `status` == `completed`/`error`.
- Password hash fn is `get_password_hash` (NOT `hash_password`).
- Custom uploader written: `scripts/soak_upload_corpus.py` (corrected endpoints; builds corpus.json).
- Seed downloader `scripts/soak_seed_audio.py` fixed (guaranteed extension via content_type fallback).
- JWT_SECRET_KEY is 35 bytes (<64 for HS512) — warning only, auth still works.

## Phase status

| Phase | Status | Notes |
|---|---|---|
| 0 — bench up + corpus upload | IN PROGRESS | Stack up, 40 files uploaded, waiting for initial conc=1 pipeline pass; corpus.json pending |
| 1 — A6000 solo sweep (conc 1,4,8,10,12,14,16,20,24) | NOT STARTED | headline |
| 2 — 3080 Ti solo sweep (conc 1-4) | NOT STARTED | |
| 3 — dual-GPU --gpu-scale | NOT STARTED | needs A6000_BEST + TI_BEST from phases 1,2 |
| 4 — --gpu-split | NOT STARTED | |
| 5 — duration curve (conc=1 sequential) | NOT STARTED | |
| Docs/commit | NOT STARTED | write BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md |

## Resumable orchestration (THE KEY MECHANISM)
Phases 1-5 run via `scripts/soak_orchestrate.sh` — bench-aware, checkpointed.
Every concurrency level + phase is recorded in `benchmarks/soak_state/checkpoint.txt`.
Re-running the script SKIPS completed units and resumes. Re-running a level is
idempotent (reprocess API cancels/replaces in-flight tasks on the same UUID).

**Launch / resume (detached so it survives a dead session):**
```bash
cd /mnt/nvm/repos/transcribe-app
setsid bash scripts/soak_orchestrate.sh >/dev/null 2>&1 &
```
- Live state: `benchmarks/soak_state/orchestrate.log`, results: `benchmarks/soak_state/results.tsv`
- A6000_BEST / TI_BEST are auto-derived from results.tsv (best stable RTF), so Phase 3/4 need no manual edit.

## How to resume each phase
- **Phase 0 (corpus) not done?** If `docs/benchmark-corpus/corpus.json` is missing OR
  still the old 16-file production corpus: files persist in the bench DB, so just
  rebuild from completed DB files (no re-upload):
  `BENCHMARK_EMAIL=admin@example.com BENCHMARK_PASSWORD=password backend/venv/bin/python scripts/soak_rebuild_corpus.py`
  (only run once enough files show status=completed). To re-upload from scratch:
  `... backend/venv/bin/python scripts/soak_upload_corpus.py`.
- **Phases 1-5:** just relaunch `scripts/soak_orchestrate.sh` (see above). It resumes.
- Manual fallback per-phase commands: `docs/SOAK_TEST_HANDOFF_PROMPT.md`.

## Result artifacts (as produced)
- Raw CSVs: `docs/engine-benchmark-results/`
- Phase logs: `benchmarks/phase*_*.log`
- Final report: `docs/BENCHMARK_RESULTS_ENGINE_OPTIMIZATION.md` (TBD)

## Key values captured (fill in as phases complete)
- A6000_BEST = TBD
- TI_BEST = TBD
- conc=16 full-corpus aggregate RTF = TBD
- Peak VRAM @ A6000_BEST = TBD
