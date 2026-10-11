# v0.6.0 test hygiene audit (issue #1195)

**Rule:** tests and test scripts leave nothing behind (OpenSearch indexes and documents, MinIO
buckets and objects, Postgres rows and databases, users/tags/collections/watch sources/speaker
profiles/clusters/chat conversations, Redis keys, docker containers/networks/volumes, `/tmp`
trees), including when a run fails or is interrupted. One tool sweeps what an interrupted run
did leave: `scripts/cleanup-test-data.py`, run unattended by `./opentr.sh test-cleanup`.

Audited 2026-10-11 on branch `chore/v060-test-hygiene` (from `feat/v0.6.0-frontend-ux`).
Evidence that started it, on the isolated `otfresh-v060` stack: 120 empty
`test_summary_api_<hex>` indexes from killed pytest workers, a leaked speaker profile
`E2E Propagated 794e5669`, a scratch database `ot_perfile_test`, and on the host 7,358
`/tmp/opentranscribe_test_*` trees, 111 stale `.testruns` markers and 4.1 GB of hand-named
`/tmp/redcheck*` trees.

## How to read the table

- **FAIL**: what happens when the test/script fails partway. **INT**: Ctrl-C/SIGTERM, and
  SIGKILL of a pytest worker.
- **Status**: `FIXED` (changed on this branch), `SWEPT` (teardown was already right; a killed
  run's leftover now has a registered shape the sweep removes), `PASS` (nothing persists, or
  it was already correct and sweepable), `KEEP` (intended output: reports, junit, caches,
  release stacks left up for `--cleanup`), `OPEN` (not fixed here; reason given).
- Before this branch, nothing outside Postgres rows was sweepable at all, so every row below
  that creates an index, bucket, object, database, container or `/tmp` tree was a LEAK on
  interruption.

## Summary

Counts are table ROWS below (several rows group sibling suites with the same defect). A row is
a LEAK if it left something behind on failure or interruption before this branch.

| area | rows | LEAK | of which FIXED | SWEPT only (killed-run backstop) | OPEN | PASS / KEEP |
|---|---|---|---|---|---|---|
| backend/tests | 39 | 36 | 28 | 6 | 2 | 3 |
| gate / e2e / dev-loop scripts | 10 | 9 | 9 | 0 | 0 | 1 |
| release + rehearsal harness | 10 | 9 | 7 | 1 | 1 | 1 |
| benchmark / eval / seed / ad hoc | 10 | 8 | 5 | 1 | 2 | 2 |
| **total** | **69** | **62** | **49** | **8** | **5** | **7** |

## 1. backend/tests

### OpenSearch (all on the live cluster)

| location | artifact | before | status |
|---|---|---|---|
| `fixtures/summary_plane.py` | index `test_summary_api_<hex12>` per worker | finally; killed worker leaks | SWEPT |
| `test_transcripts_index_tenancy.py` (`world`, pre-tenant) | `test_transcripts_{tenancy,pre_tenant}_<hex10>`; `ensure_indices_exist()` could create production-named speaker indexes | try opened after inserts+commit | FIXED: create directly before try; speaker indexes repointed to `test_tenancy_speakers_<hex10>` and deleted |
| `integration/test_keyword_search_latency_opensearch.py` | `test_kw_latency_<hex12>` | bulk seed + forcemerge before try | FIXED |
| `integration/test_summary_plane_opensearch.py` (2 fixtures) | `test_summary_plane[_semantic]_<hex12>` | `ensure_chunks_index_exists()`/monkeypatches before try | FIXED |
| `integration/test_search_completeness_opensearch.py` | `test_completeness_<hex12>` + pipeline `test-completeness-ingest-<hex8>` | seed before try; pipeline outside it | FIXED |
| `integration/test_voiceprint_erasure_opensearch.py` | `test_vp_erase_<hex10>_{v3,v4,v3_backup}` + alias | 3 creates + alias before try | FIXED |
| `unit/test_speaker_{metadata,maintenance (2),write}.py` | `test_speakers_*_<hex10>_{v3,v4}`, `test_transcripts_meta_<hex10>` | ensures/alias/asserts before try | FIXED |
| 7 chunk-index suites (quarantine, chat-cache, digest, chunk-prune, label-drift, rename-chunks, rename-sites) | `test_*_<hex12>` | monkeypatches between create and try | FIXED |
| `test_knn_probe_live.py`, `test_orphan_reconcile_opensearch.py`, `test_deletion_residue_live.py`, `unit/test_speaker_id_fields.py`, `test_reindex_on_mutation_666.py` | `knn_probe_live_test_*`, `delres-*`, `test_speaker_id_fields_*`, `test_reindex666_*` | correct | SWEPT |
| `integration/deletion_seed.py` | docs in the REAL transcripts/chunks/speaker indexes | `teardown_seed` in finally | PASS (orphans are Tier C: data-integrity reconcile) |
| `fixtures/search_corpus_stack.py` | chunks in real `transcript_chunks` | setup failure orphaned chunks | FIXED |
| `integration/test_corpus_injection_synthetic_e2e.py` | rows + chunks on the targeted stack | injection before yield, no try | FIXED (teardown still raw ORM, not `purge_media_file`: OPEN, see below) |

### MinIO

| location | artifact | before | status |
|---|---|---|---|
| `unit/test_erasure_journal_object_storage.py` | bucket `test-erasure-journal-<hex10>` | monkeypatch between create and try | FIXED + SWEPT |
| `integration/test_presign_revocation.py` | `tests/presign-revocation-907/<hex32>[-bystander].txt` in the media bucket | post-yield delete | SWEPT |
| `api/endpoints/test_user_delete_storage_reclaim.py` | `tests/issue695/...` objects, `derived/<id>_issue695_*` | puts before try (5 tests) | FIXED + SWEPT (derived render Tier B) |
| `test_media_security.py` | `media/test/thumbs/<uuid>.webp` | put + commit before yield | FIXED + SWEPT |
| `backend/scripts/e2e_watch_sources.py` | bucket `e2e-watch-<hex8>` | per-test finally | SWEPT |

### Docker

| location | artifact | before | status |
|---|---|---|---|
| `integration/conftest.py` → `throwaway_pg.py` | `ot-shared-throwaway-pg-<hex12>` | start outside try | FIXED; label `ot-test-artifact=1`; sweep Tier B |
| `test_cleanup_test_{data,users}_isolated*.py` | `ot-cleanup[-data]-test-<hex12>` | unlabelled | FIXED (label, start in try); Tier B |
| `test_scheduled_backup_restore_roundtrip.py`, `test_voiceprint_backup_roundtrip.py` | `ot-restore600-*`, `ot-vp658-*` containers + networks | unlabelled, creates before try | FIXED; Tier B |
| `unit/test_migration_progress_service.py`, `unit/test_backup_tasks.py` | `ot-test-redis-*-<hex10>` | client-side timeout + retry could leave a running container | FIXED (`docker_ops.remove_container` in finally) |
| `unit/test_opentr_stop_container_scoping.py` | `*-pytest-<hex32>` compose projects | post-yield | PASS; sweep lists by project label, Tier B |
| `integration/test_dockerfile_targetarch_build.py` | dangling build images | none | OPEN: image cache policy, not test data |
| `e2e/test_ldap_oidc.py` | LLDAP/Keycloak overlays it starts | none | OPEN by design: shared overlay infrastructure; `run-dev-tests.sh` owns overlay teardown |

### Postgres (live commits) and Redis

| location | artifact | before | status |
|---|---|---|---|
| `e2e/test_speaker_gender_clusters.py` rename test | profile `E2E Propagated <hex8>` minted by the background auto-create racing teardown | no teardown, unregistered shape | FIXED: `e2e-propagated-<hex8>` + `propagation_cleanup`; legacy shape swept |
| `e2e/test_speaker_gender_clusters.py` `owned_profile` | `e2e-gender-<hex8>` | delete failures swallowed | FIXED (warns with uuid) |
| profile-backed clusters (`e2e-*` labels) | `speaker_cluster` rows | not reaped with the profile | SWEPT (new `speaker_cluster` resource) |
| `integration/test_reindex_on_mutation_666.py` | `reindex666-<hex8>@example.invalid` + file | finally | SWEPT (registered owner) |
| `e2e/owned_corpus.py` | `e2e-owned-<hex8>` | readback asserts before try | FIXED |
| `e2e/test_full_pipeline_smoke.py`, two diar-native live suites | `pipeline-smoke-<hex8>.wav`, `diar-native-*-<pid>.wav` | one unchecked DELETE | FIXED (`delete_media_file` / `live_file_cleanup.delete_uploaded_file`) + SWEPT |
| `e2e/test_chat_trace_panel.py`, `test_full_pipeline_smoke.py` `llm_config` | active LLM pointer | activation outside try | FIXED |
| `integration/test_celery_queue_depth_live.py`, `test_orphaned_delivery_recovery_live.py` | Redis db15 fixed keys | post-yield only; leftover disabled the sibling module | FIXED (delete at setup too; shared `xdist_group`) |
| 21 savepoint-based suites (api/unit speaker, profile, cluster) | rows | rolled back | PASS |

### /tmp

| location | artifact | before | status |
|---|---|---|---|
| `tests/conftest.py` | `opentranscribe_test_*` per process (7,358 found) | never removed | FIXED (atexit) + SWEPT |
| e2e storage state (7 fixtures) | `tmp*.json` holding a session cookie | unlinked post-yield; login failure leaked | FIXED: `ot-e2e-storage-*.json`, finally + SWEPT |
| `unit/test_admin_status_surfaces_are_opaque.py` | `ot-backup-status-test-*` | never removed | FIXED (`tmp_path`) |
| `unit/test_guardrail_volume_prefix_match.py`, `unit/test_e2e_runner_skip_accounting.py`, `unit/test_run_auth_e2e_idp_cleanup.py` | bash `mktemp` trees/files | never removed | FIXED |
| `test_fedramp_controls.py` (5), `integration/test_boundary_regression.py` | `NamedTemporaryFile(delete=False)` | finally; killed leaks | FIXED (`tmp_path`) |
| `integration/test_export_toolchain_in_shipped_images.py` | `/tmp/export-capability-probe` in the backend container | rm not in finally | FIXED |

## 2. Gate, e2e and dev-loop scripts

| script | artifact | before | status |
|---|---|---|---|
| `run-integration-tests.sh` | test data on the stack | swept only at the NEXT run's start | FIXED: end-of-run sweep in an EXIT trap |
| `run-integration-tests.sh` | per-phase `mktemp` | leaked on interrupt | FIXED (`RUN_SCRATCH`, removed by the trap) |
| `e2e/run-e2e.sh` | test data | start sweep only | FIXED: EXIT-trap sweep |
| `run-dev-tests.sh` | test data; overlays; `--with-llm-test` vLLM (holds a GPU) | vLLM stopped only on the success path; overlays recorded only after a successful start | FIXED: one EXIT trap (sweep + vLLM + overlays), overlays recorded before start |
| `e2e/update-visual-baselines.sh` | partially started `otfresh-*` stack | `STACK_STARTED_BY_US` set only on success | FIXED |
| `run-diarization-gpu-tests.sh` | compose network | never removed | FIXED (`down --remove-orphans` trap) |
| `run-backend-tests.sh` | `run-<pid>.{log,xml}` | orphaned on interrupt | FIXED (INT/TERM trap) |
| `test-matrix.sh` | `mktemp` cleanup log | orphaned on interrupt | FIXED (ledger dir) |
| `.testruns/*.lock` | one per run | never removed (111 stale) | FIXED: `testrun_end`, pytest marker release, stale-marker pruning |
| junit dirs, `/tmp/ot-run-dev-tests.*`, `.test-matrix/<ts>` | reports | | KEEP |

## 3. Release and rehearsal harness

| script | artifact | before | status |
|---|---|---|---|
| `test-{fresh-install,upgrade,lite-mode}.sh --cleanup` | TEST_ROOT tree | a bare `--cleanup` targeted a NEW timestamp, so the tree was never removed | FIXED (last root recorded and reused) |
| `lib/guardrails.sh` `gr_cleanup` | git worktree registered under TEST_ROOT | stale registration after rm | FIXED (worktree removed by path first) |
| `test-upgrade.sh`, `test-lite-mode.sh` | bare `mktemp` (transcript, cookie jar) | orphaned on interrupt | FIXED (under TEST_ROOT) |
| `release/85-smoke.sh` | probe stderr `mktemp` | orphaned on interrupt | FIXED (registered prefix) |
| `selftest-cleanup.sh` | volumes, probe, TEST_ROOT | not in `cleanup_all` | FIXED |
| `selftest-rollback-fault-injection.sh` | `ot-selftest-rollback.*` | EXIT trap; SIGKILL leaks | SWEPT |
| `gpu-scale-smoke.sh` | uploads named `sample_short.wav`; plain DELETE only | 409 on a processing file left it; unsweepable name | FIXED (`gpu-scale-smoke-<i>-<pid>.wav`, `/force` fallback) |
| `test-fedramp.sh` | `fedramp-test-<ts>@example.com` account | never deleted, no pattern | FIXED (`fedramp-e2e-`, registered; legacy shape Tier B) |
| rehearsal stacks, stock volumes, release images, `.release/` | | | KEEP (by design; `--cleanup`) |
| `pki/run-pki-e2e-leg.sh` | PKI prod stack | stopped only on success | OPEN: needs a run on the PKI leg to verify the trap |

## 4. Benchmark, eval, seed and ad hoc

| script | artifact | before | status |
|---|---|---|---|
| `run_benchmark.py` | `otbench` stack + volumes | `sys.exit`/Ctrl-C skipped teardown | FIXED (try/finally) |
| `benchmark_transcription.sh` | 4 `docker compose logs -f` followers | killed only on 3 paths | FIXED (EXIT trap) |
| `benchmark_concurrency_sweep.sh` | `.env` `GPU_CONCURRENT_REQUESTS` | left at the last level | FIXED (original line restored on exit) |
| `probe_chat_rag.py` | conversations `probe: <label>`, chat flags, active LLM | none restored | FIXED (`probe-<hex8>:` titles deleted, flags + LLM restored, logout) |
| red-first trees (`/tmp/redcheck*`, 4.1 GB) | ad hoc | no shape | FIXED (recipe uses `mktemp -t ot-redcheck.XXXXXX`, swept) |
| scratch database `ot_perfile_test` | hand-made | | SWEPT (Tier B: confirm) |
| `seed-fresh-deployment.sh`, `--seed-benchmark` corpus | fixture media | | KEEP (never swept; not a registered shape) |
| `benchmark_parallel.py --upload` standalone | uploads with corpus names | | OPEN: real corpus filenames cannot be told apart from user data; use `run_benchmark.py` (otbench stack) |
| `inject-eval-corpus.sh` | `<corpus>__<meeting>.transcript` | no purge command | OPEN: needs a `--purge --seed` mode in `app/scripts/corpus_injection` (product code) |
| `benchmark/results/`, `docs/engine-benchmark-results/` | results | | KEEP (retention is a separate decision) |

## Not fixable here, and why

1. **SIGKILL of the process that owns cleanup** runs no `finally`/`atexit`/trap. That is the
   sweep's job, which is why every artifact above has a registered shape with a uniquifier.
2. **Generic `/tmp/tmp.*`, `pymp-*`, `pytest-of-*`, and other repos' files**: no name proves
   ownership; the producers in this repo were fixed instead.
3. **OpenSearch docs a savepoint-based test writes into a real index**: a rollback cannot undo
   them and their ids carry no test shape. The sweep reports orphan `profile_<uuid>` docs (Tier C);
   the fix for the rest is the admin data-integrity reconcile.
4. **Speaker relabelling across a user's library** by the gender/propagation e2e test (a real
   rename can attach the test profile to matching speakers in other files): deleting the profile
   detaches them but cannot restore their old display name. Isolating it needs a dedicated user
   or an app-side scope.
5. **Docker images** (`opentranscribe-backend-test`, release builds) are caches, not test data.

## The sweep

`scripts/cleanup-test-data.py` (planes in `scripts/lib/test_artifact_sweep.py`):

| plane | what | Tier A | Tier B | refused |
|---|---|---|---|---|
| records | media, collections, tags, watch sources, speaker profiles (incl. legacy `E2E Propagated <hex8>`), speaker clusters, chat conversations, users, mock LLM configs | registered prefix + 8-hex | review email patterns | everything else; deleted via the API |
| indexes | throwaway indexes + ingest pipelines | registered shape, EMPTY | registered shape, has docs | `transcripts`, `transcript_chunks`, `speakers*`, `audit-logs*`, `top_queries*`, `.*`, unregistered |
| databases | scratch databases | | `ot_<word>_test`, no connections | `postgres`, `template*`, `opentranscribe`, the target DB, any with connections |
| buckets | test buckets + test-keyed objects in the media/cache buckets | registered shape | `derived/<id>_issue695_*` | configured media/cache buckets themselves |
| tmp | `/tmp` trees/files of this user | registered shape, older than the cutoff | | other users', unregistered |
| docker | labelled throwaway containers/networks, `*-pytest-<hex32>` projects | never | label + shape + age | anything without the test label or a test compose project |
| (report) | orphan `profile_<uuid>` speaker docs | | | never deleted here |

Every plane: liveness cutoff from `.testruns` (now written by every pytest controller too), a
grace window, `--min-age-minutes`, a re-check at the point of deletion, and a refusal (exit 2)
when the exported store ports point at different stacks.
