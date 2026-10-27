<div align="center">
  <img src="../../assets/logo-banner.png" alt="OpenTranscribe Logo" width="250">

  # Backend Utility Scripts
</div>

Utility scripts that live inside the backend image (`/app/scripts/`) and import the application
code. Repo-level scripts (test gates, release, builds, benchmarks that drive the stack from outside)
are in the top-level [`scripts/`](../../scripts/README.md) directory instead.

Each script's module docstring is the authority for its arguments; this file only says what
exists and what it is for. Many of these are one-off experiments from specific issues.

## Running these scripts

They import `app.*`, so run them from the backend root (`/app` in a container) with the
application's environment. In a Docker deployment use the project script for the shell:

```bash
./opentr.sh shell backend                 # or: ./opentr.sh shell celery-worker for GPU scripts
python scripts/<name>.py [args]           # or: python -m scripts.<name> [args]
```

GPU scripts (anything that runs the engine) need the GPU worker container and a visible CUDA
device. Several docstrings show `docker exec opentranscribe-<service> python ...`; prefer
`./opentr.sh shell <service>` so you land in the right stack.
Check `nvidia-smi` before starting anything GPU-bound.

## Database and data repair

| Script | Purpose |
|--------|---------|
| `db_inspect.py` | Direct database inspection of the tag tables. Uses `settings.DATABASE_URL`. |
| `query_tags.py` | Queries the tag tables for debugging. |
| `fix_error_status.py` | Reclassifies files marked ERROR: files with transcripts become COMPLETED, retriable system errors reset to PENDING, permanent errors get an `error_category`. **Writes to the database.** |
| `retry_youtube_auth_errors_staggered.py` | Resets false-positive YouTube auth-error files in small batches with delays (`--dry-run` available). **Writes to the database.** |

## Infrastructure setup

Normally unnecessary: backend startup creates the bucket and indexes.

| Script | Purpose |
|--------|---------|
| `create_minio_bucket.py` | Creates the media bucket (`MEDIA_BUCKET_NAME`) through the application's own storage client, so it honours `MINIO_HOST`/`MINIO_PORT`/`MINIO_SECURE` and the S3 backend settings. No public policy is set; media is served by presigned URL. Run inside the backend container. |
| `create_opensearch_indexes.py` | Calls the same bootstrap as backend startup (`ensure_indices_exist`, `ensure_v4_index_exists`, `ensure_chunks_index_exists`), so it creates `transcripts`, the versioned `speakers_v*` indexes with their `speakers` alias, and `transcript_chunks`. Run inside the backend container. |
| `celery_pool_healthcheck.py` | Docker healthcheck for the GPU transcription worker (wired in `docker-compose.yml`): fails when the Celery prefork pool is dead or in a respawn storm. |
| `blackwell_patches.py` | Applies Blackwell (SM_121) compatibility patches at Docker build time for `Dockerfile.blackwell`. |

## Tests run from inside the stack

| Script | Purpose |
|--------|---------|
| `e2e_watch_sources.py` | End-to-end runner for Watch Sources (issue #26); run it through `../scripts/test-watch-e2e.sh`. |
| `test_cloud_asr.py` | One-off test of a cloud ASR provider using its DB-configured key (`python -m scripts.test_cloud_asr`). Not a pytest test. |

## Performance benchmarks

| Script | Purpose |
|--------|---------|
| `benchmark_queries.py` | Database query timing benchmark, JSON output (`--user-id`, `--output`). |
| `compare_benchmarks.py` | Compares two `benchmark_queries.py` JSON files. |
| `benchmark_migration.py` | Throughput of the v3 to v4 speaker-embedding migration (does not write to OpenSearch). |

## Diarization-boundary tooling (issue #193)

The diarization-boundary smoothing work and its benchmark live in
[`benchmark/diarization-boundary/`](../../benchmark/diarization-boundary/README.md); these are its
backend-side harnesses. Most need the GPU worker; `PYTHONPATH=. python -m scripts.<name>` is the form several docstrings use.

| Script | Purpose |
|--------|---------|
| `build_fixture.py` | Runs the engine GPU stage on a WAV and saves the raw inference as JSON (refuses without CUDA). |
| `build_regression_fixture.py` | Builds the committed boundary-regression fixture trio; see `backend/tests/fixtures/boundary/README.md`. |
| `benchmark_boundary.py` | Boundary smoothing benchmark over a corpus (smoothing off/on/sweep, bootstrap CIs). |
| `sweep_boundary.py` | Sweeps the smoother's N/T/M/G knobs against a labeled reference. |
| `measure_dataset_boundaries.py` | Per-file WSER and bleed-island measurement against a ground-truth RTTM. |
| `diagnose_boundaries.py` | Flags likely wrong-speaker islands in a transcribed file from the database. |
| `compare_models_boundary.py` | Compares Whisper models for the boundary fix, including word-timestamp validation. |
| `compare_cloud_boundaries.py` | Cloud-versus-local boundary and speed comparison on a labeled clip. |
| `make_seam_labels.py` | `emit` writes Audacity seam-label tracks from a transcript; `to-rttm` converts the corrected labels into a reference RTTM. |
| `nlp_to_rttm.py`, `oracle_to_rttm.py` | Convert an Earnings-21 `.nlp` reference, or a cloud provider's diarized JSON, to RTTM. |
| `score_earnings21_cpwer.py` | Earnings-21 cpWER scorer (speaker-attributed WER without timings). |
| `phase3_acoustic_probe.py`, `phase3_measure.py`, `phase3_e2e.py` | Phase 3 acoustic re-check experiments; GPU worker only. |
