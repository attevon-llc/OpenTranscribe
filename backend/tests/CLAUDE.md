# backend/tests — the whole test tree (api, unit, e2e, integration, redaction, transcription, onnx)

## ⚠️ A NOT MEASURED phase is not a pass, and the gate's exit code now says so

A phase that declines to be counted prints `NOT MEASURED` and `run-integration-tests.sh` exits 4 (`run-dev-tests.sh` exits 5, `60-test.sh` records `not-measured`); it is never a pass. The exit codes, the `RUN_*` read-by-a-test rule (there is no `GATED_FILES` list any more) and the measurements behind them: `backend/tests/notes/gate-exit-codes.md`.
## Purpose

`./scripts/run-integration-tests.sh` is **THE pre-merge gate**: Unit/API suite → the security
suites under `FIPS_MODE=true` (**one** pass — see above; there is no FIPS-off phase and no
`RUN_*`-gated phase any more) → `-m integration` → `-m gpu` → model-vs-schema drift. Needs the
live stack
(`./opentr.sh start dev`) plus `backend/venv`. GitHub Actions `backend-tests` is a safety net
only — fresh Postgres, CPU-only `backend/requirements-ci.txt` (**never `requirements-dev` in
CI**), `SKIP_S3`/`SKIP_OPENSEARCH` forced `True`. E2E is local-only: `./scripts/e2e/run-e2e.sh`
(3 xdist workers `--dist loadfile`, then `-m visual` serially) and `run-e2e-smoke.sh`.
Per-suite prose lives in `README.md`, `AUTH_TEST_SETUP.md`, `e2e/README.md`.

## ⚠️ A branch that adds a migration must be tested against a stack that has APPLIED it

`conftest` defaults `POSTGRES_PORT` to **5176 — the dev stack**, which runs whatever is checked
out in the *main* repo. A worktree on a feature branch that adds a migration is therefore, by
default, **testing the branch's code against master's schema**.

That is not hypothetical. It produced **13 failures** that read as real breakage and were
reported as pre-existing: 10 in `test_v389_migration_consistency`, 2 ORM-cascade, 1 DDL
divergence — every one of them `relation "file_facts" does not exist`, because the dev stack sat
at `v388` while the branch added `v389`. Re-run against a stack holding the migration and all 13
pass:

```bash
POSTGRES_PORT=5276 OPENSEARCH_PORT=5280 pytest tests/...     # an isolated --fresh stack
```

Check before believing a schema failure:
```bash
docker exec <pg-container> psql -U postgres -d opentranscribe -tAc "SELECT version_num FROM alembic_version;"
```

Same family as everything else in this file: a red number that describes something other than
what it appears to. A green one from the wrong schema is worse.

## Key files

- `conftest.py` — sets env **before** `app.*` imports (DB/MinIO creds via `dotenv_values(.env)`,
  `POSTGRES_HOST=localhost:5176` — see the migration warning above). `db_session` = savepoint
  isolation surviving `commit()`;
  `client` overrides `get_db`; an autouse session fixture patches `Task.apply_async` so
  `.delay()` never reaches a real broker.
- `e2e/conftest.py` (`login_page`, `authenticated_page`, `auth_helper`, `api_helper`,
  ffmpeg-generated `sample_audio`/`sample_video`; creds `admin@example.com`/`password`) and
  `e2e/pytest.ini` (its own marker set, `--browser chromium`).
- `onnx/conftest.py` — `--onnx-device` (named around pytest-playwright's `--device`); skips
  without `models/onnx/{segmentation,embedding}.onnx` or `HF_TOKEN`.
- Golden fixtures: `fixtures/boundary/karpathy_10m.{rawinfer,ref.words,baseline}.json` (frozen
  GPU output replayed through the CPU smoother — WSER/island/DER drift gate) and
  `fixtures/redaction/{segments,expected_label_style}.json`.

## Markers and gates

- **A test that cannot run here is DESELECTED by marker, never left to skip.** A skip inflates
  the phase's skip total toward `run-integration-tests.sh`'s ceiling, and once the phase trips
  that ceiling it reports NOT MEASURED — so a permanently-skipping test does not merely prove
  nothing itself, it buries the skips that mean something. Two markers exist for this:
  - **`multi_gpu`** — needs a multi-*worker* GPU topology (`--gpu-scale` / `--gpu-split`, i.e. a
    running `celery-worker-gpu-scaled` or `celery-worker-gpu-diarize`), **not merely a second
    card**. The gate DETECTS that topology and selects these back in when it is up, so this
    hides no coverage on a host that can run them — which matters, because root `CLAUDE.md` is
    explicit that GPU 2 is usable and that treating this host as single-GPU is a documented cost.
    6 tests, all in `test_gpu_scale_smoke_live` / `test_diar_native_cross_card_placement_live` /
    `test_diar_native_multigpu_provider_live`.
  - **`opt_in_gate`** — expensive or deployment-scoped, behind its own `RUN_*` variable
    (`RUN_EXPORT_CAPABILITY_TEST`, `RUN_INDEX_AUDIT`). The env gate inside the test is still the
    authority on whether the work happens; the marker only stops an opt-in-by-design test from
    being counted. `--export-capability` selects the first back in.

    **Integration phase lanes (I1).** `run-integration-tests.sh` runs `INTEGRATION_SELF_CONTAINED`
    (throwaway-container modules, `-n 4 --dist loadfile`) concurrently with the serial live-stack lane,
    whose `--ignore`s are derived from that array. A new integration module is serial by default; list
    it only if it touches no live service. Guards: `test_integration_split_is_a_partition.py` and
    `test_self_contained_integration_modules_touch_no_live_stack.py`. Never list
    `test_fusion_strategy_switch`, `test_redaction_pipeline`, `test_lite_mode_mocked_providers`,
    `test_keyword_search_latency_opensearch`.

    ⚠️ **Worked example (2026-09-12): adding ONE new integration test tripped this.**
    `test_chat_cache_share_revocation` needs a real, password-protected, in-network-only Redis
    (the cache under test IS Redis) — unsatisfiable from a bare host run of the gate. Adding it
    took the **Integration-marked tests** phase's skip count from 7 to 8, past
    `INTEGRATION_SKIP_CEILING` (see `run-integration-tests.sh`), and the whole phase reported
    `NOT MEASURED` — not "one more
    skip", the entire phase's result became unusable. The fix was `pytest.mark.opt_in_gate`
    (visibly deselected, not silently skipped), **not raising the ceiling**. The reason this
    matters beyond the one test: a permanently-skipping test doesn't just fail to prove anything
    itself — it consumes ceiling headroom, so it buries whatever OTHER skips in that phase would
    have meant something. Raising the ceiling to "make room" reopens exactly that hole for the
    next unrelated skip.

    **Redis per phase.** The gate's `stack-redis-env` block resolves the compose project's Redis
    host port and password (from the stack's own containers) and hands `REDIS_HOST`/`REDIS_PORT`/
    `REDIS_PASSWORD` to the **integration, GPU and e2e smoke** phases; you no longer export them by
    hand. A variable you DID export wins on its own and only the missing ones are resolved, so a
    shell that exported just `REDIS_PORT` for a `--fresh` stack still gets the password (the e2e
    phase used to get neither, and its fixtures hit `AuthenticationError`). The unit and FIPS
    phases run under `UNIT_ENV`, which UNSETS all of them even if your shell exported them: they
    flip about ten memory-path unit tests onto the Redis path and fail them. Separately,
    `tests/unit/conftest.py` clears `STACK_POINTER_VARS` (`COMPOSE_*`, `REDIS_*`,
    `CELERY_BROKER_URL`) for every unit test, so a test that builds `Settings(...)` or runs an
    `opentr.sh` snippet sees the same environment in your shell as in CI. A bare
    `pytest tests/integration` outside the gate still needs the three variables set yourself.

    **Why the ceiling is 0** (it was 7 until 2026-10-30): the seven were 4 skips in
    `test_fusion_strategy_switch.py` and 3 in `test_rag_eval_harness.py`, tests that asserted on
    whatever corpus the target deployment held. On a fresh isolated stack it was really 13, because
    the 4 hybrid completeness tests also skipped (nothing set `OPENSEARCH_TEST_MODEL_ID`). All three
    now own their data: the fusion test seeds a throwaway index, the rag-eval test injects a
    QMSum-format fixture (`fixtures/qmsum_corpus.py`) and `purge_injected`s it, and the gate's
    `integration-env` block resolves the stack's embedding model and verifies it is DEPLOYED with 384
    dimensions. A skip in this phase therefore means a precondition broke (missing overlay, no
    model), and the phase reports NOT MEASURED; never raise the ceiling to make room.
- Registered (pyproject): `slow`, `unit`, `pki`, `e2e`, `integration`, `gpu`, `models`,
  `ddl_exclusive`, `multi_gpu`, `export_capability`, `opt_in_gate`. `addopts` =
  `-n auto --dist loadgroup --tb=short -q --strict-markers --durations=25 --timeout=300 -m 'not integration and not gpu'`;
  `norecursedirs=["tests/e2e"]`. **`--strict-markers` makes an unregistered marker a collection
  error** — register any new marker in `[tool.pytest.ini_options] markers` or collection fails.
  (The e2e markers live in `tests/e2e/pytest.ini`, which is a separate rootdir config.)
- **No module-level `import torch` under `tests/unit` or `tests/api`** (U2): import it inside the
  test or fixture that uses it, never `pytest.importorskip`. Every collecting xdist worker pays
  torch's import time and memory otherwise. `test_no_module_level_torch_import.py` is the AST
  guard; `tests/integration` and `tests/onnx` are exempt (real GPU/model modules).
- **CI shards the suite (issue #1099).** `.github/workflows/pre-commit.yml` runs `tests/` as
  `--splits 8 --group N` (pytest-split, `duration_based_chunks`, balanced by the committed
  `backend/.test_durations`); a stale file unbalances legs but never drops a test.
  `shard_manifest.py` (`-p tests.shard_manifest`, needs `python -m pytest` so `backend/` is on
  `sys.path`) records each leg's selected ids and `scripts/guards/check_shard_coverage.py`
  proves legs are disjoint and complete against one unsplit collection. `test_ci_sharding.py`
  pins it. Per-leg coverage is NOT enforced (`--cov-fail-under=0`); the pyproject floor applies to
  the merged data in the `Backend Tests (pytest)` aggregate job. Refresh recipe:
  `docs-site/docs/developer-guide/testing.md`.
- **CUDA device guard (issue #719).** `conftest.py`'s `pytest_configure` asks pytest's own
  `Expression` parser whether the active `-m` selection can ever satisfy `gpu` (satisfiability,
  not a `"gpu" in markexpr` substring check — the default expression `not integration and not
  gpu` CONTAINS that substring and must still leave the guard ON). When it cannot, it sets
  `CUDA_VISIBLE_DEVICES=-1` before any worker imports torch, so `torch.cuda.is_available()`
  reads `False` for the whole run. `-m gpu` and `-m integration` (two files carry both markers)
  keep real device access. A `gpu`-marked test that somehow runs under the guard **ERRORS**
  (`pytest.fail`, message contains `cuda-device-guard`) rather than skipping on "CUDA not
  available" — a skip there would look like a pass. Escape hatch: `OT_TEST_CUDA_VISIBLE_DEVICES`
  (`=1` sets it and bypasses the marker check, `=all` leaves the ambient value alone, `=""` is an
  explicit blind). An ambient `CUDA_VISIBLE_DEVICES` in the shell does **not** release the guard —
  the block deliberately does not use `os.environ.setdefault`. Tests: `unit/test_cuda_device_guard.py`.
- `@pytest.mark.models` = needs Presidio/GLiNER/toxicity weights; those modules also
  `importorskip` + `preload()`-skip, so fast CI passes without weights.
- Module-level `skipif` env gates → suite. ⚠️ **The seven security-suite gates that used to
  head this list are GONE** (`RUN_PKI_TESTS`, `RUN_MFA_TESTS`, `RUN_LLM_TESTS`,
  `RUN_FEDRAMP_TESTS`, `RUN_FIPS_TESTS`, `RUN_AUTH_CONFIG_TESTS`, `RUN_ADVANCED_ADMIN_TESTS`).
  All eight files run by DEFAULT now, and each still *mentions* its dead variable in a header
  comment — which is exactly what let a substring-matching guard pass over a mechanism that no
  longer existed. **Grep for `gate_enabled("<VAR>")`, never for the bare name**, and see
  `unit/test_gate_run_env_vars_are_live.py`. Verified 2026-09-07: those seven have **zero**
  `gate_enabled` call sites in `backend/tests`. What remains live:
  `RUN_SEARCH_QUALITY_TESTS`→`test_search_quality` (plus two tests in `e2e/test_search.py`; self-seeding — injects
  its own 6-meeting corpus via `app/scripts/corpus_injection` through a throwaway `searchqual-`
  user, see `tests/fixtures/search_corpus.py`; still deliberately never in CI, since CI forces
  `SKIP_OPENSEARCH=True`), `RUN_SCHEMA_DRIFT_TESTS`→`unit/test_schema_drift` (needs the live
  migrated DB; now its own phase in `run-integration-tests.sh` — it was previously set only by
  the release pipeline's `warn`-severity `schema-drift` criterion, so it never ran pre-merge),
  `RUN_AUTH_E2E`→`e2e/test_ldap_oidc` + LDAP half of
  `e2e/test_auth_buttons`, `RUN_PKI_E2E`→`e2e/test_pki`, `RUN_INDEX_AUDIT`→
  `integration/test_speaker_label_index_drift`, `RUN_EXPORT_CAPABILITY_TEST`→
  `integration/test_export_toolchain_in_shipped_images`.
- **MinIO/OpenSearch tests auto-enable by TCP probe.** Root conftest `_service_reachable`
  (0.3 s) `setdefault`s `SKIP_S3` from `localhost:5178` and `SKIP_OPENSEARCH` from
  `localhost:5180` (or whatever `MINIO_PORT` / `OPENSEARCH_PORT` you exported), then points the clients at those host ports; an explicit shell value wins.
  Stack down → those suites **skip silently**, so a green local run proves less than it looks.

## Test-quality gate — `scripts/audit-tests.py` + `audit-allowlist.txt`

`audit-tests.py` is only the entry point; the detectors, allowlist, selftest cases and CLI live in the `scripts/audit_tests/` package.

**Runs on every commit** (`.pre-commit-config.yaml`: `audit-tests-selftest` then `audit-tests`)
and in the `backend-tests` CI job. Before this it was in neither — `rg audit-tests` found only
prose. 21 AST detectors; full inventory and the calibration traps live in `scripts/CLAUDE.md`.
What matters when you are writing a test here:

- **`tests/e2e` is scanned too.** It used to be excluded, which hid 21 findings in the only
  suite that drives a real browser.
- **The shapes it rejects**, and the fix each one wants:
  - `assert r.status_code != 403` → assert the exact status. `!=` passes on a **500**, so
    seven "admin can access" tests in `api/endpoints/test_permissions.py` passed against a
    completely broken endpoint.
  - the real assertion inside `if r.status_code == 200:` → assert `== 200` unconditionally
    first, then assert the payload. Two of these survived in `test_permissions.py` after their
    siblings were fixed, because a weak unguarded assert beside them hid them from
    `conditional-only`.
  - every assertion inside `for x in <runtime iterable>` → add a non-emptiness assertion
    *outside* the loop. `redaction/test_presidio.py::test_offsets_slice_back` is the fix shape:
    zero detected spans ran the loop zero times and passed, on the invariant that
    `redaction/spans.py` is a mutation target *for*.
  - `assert some_local` where the local is an `or` chain → assert the value you mean.
  - only `mock.assert_called_once_with(...)` → assert real state as well. See
    `unit/test_dispatch.py`: it patched `update_media_file_status`/`update_task_status`/
    `send_error_notification` — **the effects `on_pipeline_error` exists to produce** — so a
    handler that left a file `processing` forever kept 13 of 15 tests green. It now creates real
    `MediaFile`/`Task` rows and asserts on them, patching only `session_scope` (the handler
    opens its own session, invisible under the savepoint harness) plus the MinIO/Redis/WebSocket
    seams, once, in one fixture.
  - `except ...: pass` in a test body → let it raise. The assertions inside the `try` silently
    stop running and nothing reports it.
- **Allowlist entries are `<file>::<test>::<category>  # reason`, all three segments required.**
  A reason starting `BACKLOG` marks deferred work and is counted and printed separately on
  every run, so a green gate is never read as a clean tree. A **stale** entry — one whose
  finding is gone — **fails the run**: fix a test, delete its line. Never widen an assertion to
  clear a finding.
- **Run `python3 scripts/audit-tests.py --selftest` after touching a detector**, and give any
  new one a must-fire *and* a must-stay-clean fixture. `unit/test_audit_tests_selftest.py` runs
  the `--selftest` cases under pytest (the count is printed by `--selftest`) for the same reason: a detector that matches nothing reports zero
  findings, which is indistinguishable from a clean suite.

## ⚠️ A gate test may not depend on the dev deployment's DATA, or on a fixed wall-clock budget

A gate test must seed what it asserts on and must not rest on a fixed wall-clock budget. The failure history and the rules: `backend/tests/notes/gate-tests-and-dev-data.md`.
## Platform access matrix (issue #1281)

`unit/test_platform_access_matrix.py` compares `docs/security/platform-access-matrix.tsv` with
what `fixtures/platform_access_matrix.py` derives from the live route table and an AST scan of
`app/`; `api/test_support_access_matrix.py` drives the real routes (per-chokepoint grant quartet,
a no-grant leak sweep over every parameterless GET, an org-grant list-scope test, the metadata
audit test) on the `support_world` fixture. A new platform route, `.allows(` site or bypass
construction fails the unit test until the TSV is regenerated
(`scripts/generate-platform-access-matrix.py --write`) and its diff read. The leak sweep is only
as strong as what it seeds: when you add a tenant-content table a platform route could list, seed
a canary row for it in `_seed_tenant`.

The hand-written half is `fixtures/platform_access_matrix_tables.py`, and every table in it is
keyed so that a NEW item has no entry and shows `UNCLASSIFIED`: `CONTENT_GUARDS` (per content
route; the handler's CODE is checked against the claim, never a substring, so a docstring saying
"bypasses the schedule" cannot make a route look grant-gated, which it did for five routes),
`INLINE_ROLE_ROUTES` (routes that test the signed-in user's role inline, which the dependency
walk cannot see; the scan covers EVERY route, including ones that take the request context, and
follows a helper one call deep), and `SCOPE_WIDENER_SITES` / `REVEAL_WIDENER_SITES` with `TENANT_MARKERS` (a name
the widening function must still mention, so dropping the tenant predicate fails the unit test).

## Safety rules (non-negotiable) and cleaning up after tests (issue #1195)

E2E and integration tests must never persist changes to dev data; the rules are enforced by `unit/test_e2e_data_hygiene.py`, and leftovers are swept by `./opentr.sh test-cleanup`. The rules, the cleanup planes and the registered shapes: `backend/tests/e2e/CLAUDE.md`.
## Before you debug a "flaky" E2E test, rule out the STACK (issue #431)

A flapping backend and similar stack faults mimic a flaky test (the runner only checked that ports were open); the session preflight and what it catches: `backend/tests/e2e/CLAUDE.md`.
## Gotchas

- **How fixture plugins load** (the pytest 9.1 conftest-visibility workaround, why `tests/` has
  no `__init__.py`, and per-run values from `tests.run_identity`): `fixtures/CLAUDE.md`.
- **Many endpoints `db.close()` the request session** before slow work (`rg 'db\.close\(\)'
  app/api`); `client` shares `db_session`, so held ORM rows detach. Capture ids before the
  request, or re-read with `db_session.get(Model, id, populate_existing=True)`.
- **The shared `unit/_fake_liveness_redis.FakeRedis` must answer every command** the app sends
  via `get_redis()`/`get_request_redis()`: `unit/test_fake_redis_covers_app_calls.py`.
- **Patch a name where it is looked up**, not on a package re-export the route never reads
  (`unit/test_patch_targets_reach_their_lookup.py`).
- **`tests/integration/` is a directory name, not a marker.** Its contents split three ways:
  `integration`-marked (need the live stack), `gpu`-marked (boundary/diarization regression,
  lifecycle, perf gates), and three deliberately service-free tests in
  `test_metering_pipeline.py` that belong in the fast suite. The gate script runs the first two
  as separate phases (`-m integration`, then `-m gpu`); the fast suite and CI deselect both.
  Before #297 `gpu` was unregistered and silenced by a `PytestUnknownMarkWarning` filter, so
  those 17 tests ran in the fast suite *and* CPU-only CI, passing only on their own runtime skip
  guards, while the gate selected none of them.
- **`integration/test_deletion_residue_live.py` is the "does delete really delete" gate.** It
  seeds a synthetic, fully-indexed file into every store (`deletion_seed.py` — the real
  indexing task writes the OpenSearch planes), deletes it through each entry point (single,
  pending/requeued, force, bulk, retention, admin/users account delete, GDPR erasure), then
  scans Postgres/OpenSearch/MinIO/Redis GENERICALLY (`deletion_residue.py`: the FK closure
  from `pg_constraint`, every non-system index, every bucket). Before each delete it asserts
  every scan FIRES and every table depending on `media_file` was populated — a new table the
  seed does not fill fails the test by name (`content_hold_file`, `takedown_notice_file` and both
  `speaker_correction_carryover` targets are seeded, issue #1306; the hold is a RELEASED one in
  a throwaway organization, because an active hold refuses the delete). A child with a
  composite key never enters the closure, so the scan counts its survivors by foreign key
  instead; before that it was blind to `content_hold_file`. Two transports: `inprocess` (this checkout's app)
  and `live` (`DELETION_RESIDUE_BACKEND_URL`, else `E2E_BACKEND_URL`, else :5174 — it must
  share the Postgres named by `POSTGRES_PORT`, and refuses a mixed stack). The GDPR legs leave
  id-only `erasure_ledger` rows behind by design (the ledger outlives what it records).
  ⚠️ **`live` measures the code the backend at that URL serves**, which on an isolated
  `--fresh` stack is whatever checkout started it — not your branch. To run the live legs
  against a branch, serve that checkout's app on a free port with the suite's own environment
  (`TESTING=True`, so no startup task runs) and point `DELETION_RESIDUE_BACKEND_URL` at it;
  Celery work it publishes is still consumed by the stack's workers, which run the stack's
  code. `teardown_seed` attempts every step whatever an earlier one did and raises
  `TeardownIncompleteError` listing every failure (#1233) — a transient
  `search_context_missing` used to abort it and leak the `e2e-delres-*` rows of every later
  step. `test_delete_of_a_requeued_file_leaves_no_residue[live]` races the stack's own
  workers, which move a seeded PENDING file on before the precondition reads it.
- **`integration/test_orphan_sweep_storage.py` proves the storage orphan sweep (#1212) on a
  THROWAWAY bucket**, never the real media bucket: a fixture makes `e2e-orphan-sweep-<hex>`,
  points `settings.MEDIA_BUCKET_NAME` at it, and empties and removes it in `finally`. The
  "existing file" is a real committed `media_file` row named with `DELRES_PREFIX` (so
  `cleanup-test-data.py` already covers a leaked row); orphan ids are far above any real
  sequence value and asserted absent. The sweep skips objects younger than its grace period,
  so tests that expect deletion move `storage_orphan_sweep_task._now` forward (`old_clock`)
  instead of waiting an hour, and `test_recent_objects_are_skipped` is the control that
  proves the clock shift is what makes the others delete. Needs `MINIO_PORT` exported for an
  isolated stack (`SKIP_S3` otherwise skips it). The route contract (confirm gate, authz,
  audit) is `api/endpoints/test_admin_storage_integrity_routes.py`, with the task replaced.
- **The three diarization `gpu` suites do NOT run in the gate's `-m gpu` phase — they need a
  container, and the gate runs the venv.** `test_diarizer_lifecycle.py`,
  `test_diarization_perf_gates.py` and `test_diarization_regression.py` each open with an
  `ensure_container` fixture that skips unless `/.dockerenv` exists or
  `OPENTRANSCRIBE_IN_CONTAINER=1`, and they read fixtures from `/app/benchmark/test_audio` and
  `/app/docs/...`. `run-integration-tests.sh`'s GPU phase therefore reports them as skips, not
  passes. Their entry point is:

  ```bash
  ./scripts/run-diarization-gpu-tests.sh            # all three, on real GPU hardware
  ```

  which builds `opentranscribe-backend-test:latest` (`backend/Dockerfile.test` = the prod image
  plus `requirements-test.txt`) and runs the `diarization-tests` service from
  `docker-compose.benchmark.yml` in its own compose project. Before #577 there was no working
  path at all: the prod image ships no pytest, and installing it at run time with `--user root`
  moves `site.getusersitepackages()` from `/home/appuser/.local/...` to `/root/.local/...`, which
  drops the entire application dependency tree off `sys.path` — the reported "`fastapi` is
  missing" and "meeteval wheel won't build" symptoms are both that one cause. Never run these
  with `--user root`; see `backend/Dockerfile.test`'s header.

  ⚠️ **Four separate ways this suite exits 0 having measured nothing**: outside a container it
  skips (`ensure_container`); without `-o addopts= -m gpu` pytest deselects every test; without
  the gitignored `benchmark/test_audio/*.wav` fixtures each test skips individually; and without
  a visible CUDA device `torch_cuda` skips. `run-diarization-gpu-tests.sh` hard-fails on the
  first three rather than handing you a green vacuum. **The CUDA device guard (issue #719,
  above) is a fifth potential source of that state** — but the hook converts it to a hard
  `pytest.fail` (`cuda-device-guard` in the message) rather than a fifth silent skip, so a
  `gpu`-marked test that runs without `-m gpu` errors loudly instead of passing vacuously.
- **`tests/integration/test_scheduled_backup_restore_roundtrip.py` needs the backend image
  built** (`./opentr.sh build` or `./opentr.sh start dev --build`) — its Tier-1 "trustworthy
  evidence" test runs the real `backup_service.run_pg_dump` inside a throwaway container from
  `opentranscribe-backend:latest`, because `pg_dump`/`pg_restore` are not installed on the host
  (`backend/venv` has no PostgreSQL client tools; only `gpg` is present there). Named skip, not
  silent, if the image is missing. Its `encrypt=True` variant additionally skips citing issue
  #604 — the *published* image has no `gpg` on PATH either (`Dockerfile.prod` installs
  `postgresql-client` but never `gnupg`), so `backup.encrypt` fails in production today.
- `db_session` rolls back the DB, **not MinIO or OpenSearch**. Hence `upload_test_file`'s API
  delete and the forced `AUDIT_LOG_TO_OPENSEARCH=false` — savepoints can't undo index writes
  into the live dev cluster.
- `--dist loadgroup`: tests sharing mutable global state need
  `pytestmark = pytest.mark.xdist_group("<name>")` (`test_auth_config_integration.py`,
  `unit/test_media_mirror_service.py`, `api/test_proxy_auth_endpoint.py`,
  and — group per overlapping `SystemSettings` key namespace, issue #389 — `"backup_system_settings"`
  on `unit/test_backup_metrics.py` + `unit/test_backup_service.py` + `unit/test_backup_alerts.py`,
  `"engine_system_settings"` on `test_engine_settings.py` + `api/test_engine_settings_endpoints.py`)
  or they interleave across workers and can deadlock on `system_settings_key_key`
  (two workers inserting the same overlapping keys in reversed order). User fixtures
  use UUID-suffixed emails for the same reason.
- **DDL tests need more than `xdist_group`, and the marker goes on the TEST, never the module.**
  `DROP TABLE`/`ALTER TABLE ... DROP CONSTRAINT` takes `ACCESS EXCLUSIVE`, and when the dropped
  object is a foreign key Postgres needs that lock on the *referenced* table too (removing the
  FK's enforcement trigger, which lives there) — dropping `scim_token` also locks `user`, and
  `v377`/`v381` `ALTER TABLE "user"` directly. Since nearly every other test touches a `user`
  row, that can deadlock against any worker (issue #389). `xdist_group` cannot fix it: sharing a
  worker only stops DDL tests colliding with *each other*. `@pytest.mark.ddl_exclusive` makes
  `db_session` take a Postgres advisory lock (`tests/db_locks.py`) — SHARED for every ordinary
  test, EXCLUSIVE for a `ddl_exclusive` test — which is real cross-worker mutual exclusion.
  **Every EXCLUSIVE acquisition is a stop-the-world barrier**: it drains all other workers and
  queues every new one behind it. Applying the marker at module scope therefore turns each
  read-only schema assertion in the module into a full-suite barrier — that is how the
  `migration_ddl` group came to be 414 s of a 511 s wall clock with 111 tests marked and ~12
  actually running DDL (issue #431). `unit/test_ddl_marker_discipline.py` enforces both
  directions by AST: DDL without the marker fails, and the marker without DDL fails. It
  discriminates *executed* DDL from DDL merely mentioned in a string, because three suites
  assert on a migration's own source text and one passes `"'; DROP TABLE media_file; --"` as an
  injection payload. `CREATE TEMP TABLE` is exempt (session-private `pg_temp_*` schema).
  A test that opens its **own** connection cannot be reached by the marker — it must call
  `tests/db_locks.py`'s `acquire_ddl_lock_exclusive[_raw]()` itself, as
  `unit/test_uuid7_migration_guard.py` does for the raw v368 guard block.
  Measured 2026-09-05, at ~12,900 tests total: the barrier now costs **6-13 s of a ~154 s
  suite**, across 43 `ddl_exclusive` tests with ~4 s of actual DDL work — the rest is lock
  queueing, not execution. **A two-pass split (running `ddl_exclusive` tests in a separate
  pytest process) was measured and rejected**: the second process re-pays interpreter startup,
  import, and collection, costing +20.3 s to +48.6 s to remove a barrier worth at most 12.9 s —
  a net loss. Don't re-propose it without new evidence the collection cost can be shared.
- E2E runs from the repo root against `backend/tests/e2e/`, so `e2e/pytest.ini` becomes the
  rootdir config — pyproject `addopts` (`-n auto`, `-m 'not integration'`) do **not** apply.

## Mutation testing (opt-in, never in the gate or CI)

`./scripts/run-mutation-tests.sh --module <name>`; a surviving mutant is a finding (add the assertion or delete the dead line, never loosen a test). Harness traps, scoping and results: `backend/tests/notes/mutation-testing.md`.
## Running DB-backed tests without the dev stack

See `backend/tests/notes/db-tests-without-stack.md`.
## `tests/eval/` — the RAG evaluation harness (issue #403 Stage 1)

Harness layout, corpora, qrels, metrics and `runinfo.json`: `backend/tests/eval/CLAUDE.md`.
## Chat suites, mock LLM, mock cloud ASR, document export fixtures

Chat suites (issue #52), the mock LLM provider (`--with-mock-llm`, scenario models `mock-gpt`/`mock-echo`/`mock-empty`/`mock-error`/`mock-slow`/`mock-reasoning`, plus the JSON-shaped `mock-summary-json`/`mock-topics-json`/`mock-speakers-json`), the mock cloud ASR provider (`--with-mock-asr`) and the document export fixtures (#1278): `backend/tests/fixtures/CLAUDE.md`.
