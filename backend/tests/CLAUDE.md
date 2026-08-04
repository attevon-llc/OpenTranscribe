# backend/tests — the whole test tree (api, unit, e2e, integration, redaction, transcription, onnx)

## Purpose

`./scripts/run-integration-tests.sh` is **THE pre-merge gate**: ungated suite → all `RUN_*`
suites in **both FIPS modes** → `-m integration`. Needs the live stack
(`./opentr.sh start dev`) plus `backend/venv`. GitHub Actions `backend-tests` is a safety net
only — fresh Postgres, CPU-only `backend/requirements-ci.txt` (**never `requirements-dev` in
CI**), `SKIP_S3`/`SKIP_OPENSEARCH` forced `True`. E2E is local-only: `./scripts/e2e/run-e2e.sh`
(3 xdist workers `--dist loadfile`, then `-m visual` serially) and `run-e2e-smoke.sh`.
Per-suite prose lives in `README.md`, `AUTH_TEST_SETUP.md`, `e2e/README.md`.

## Key files

- `conftest.py` — sets env **before** `app.*` imports (DB/MinIO creds via `dotenv_values(.env)`,
  `POSTGRES_HOST=localhost:5176`). `db_session` = savepoint isolation surviving `commit()`;
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

- Registered (pyproject): `slow`, `unit`, `pki`, `e2e`, `integration`, `gpu`, `models`. `addopts` =
  `-n auto --dist loadgroup --tb=short -q --strict-markers -m 'not integration and not gpu'`;
  `norecursedirs=["tests/e2e"]`. **`--strict-markers` makes an unregistered marker a collection
  error** — register any new marker in `[tool.pytest.ini_options] markers` or collection fails.
  (The e2e markers live in `tests/e2e/pytest.ini`, which is a separate rootdir config.)
- `@pytest.mark.models` = needs Presidio/GLiNER/toxicity weights; those modules also
  `importorskip` + `preload()`-skip, so fast CI passes without weights.
- Module-level `skipif` env gates → suite: `RUN_PKI_TESTS`→`test_pki_auth`, `RUN_MFA_TESTS`→
  `test_mfa_security`, `RUN_LLM_TESTS`→`test_llm_settings`, `RUN_FEDRAMP_TESTS`→
  `test_fedramp_compliance`+`_controls`, `RUN_FIPS_TESTS`→`test_fips_140_3`,
  `RUN_AUTH_CONFIG_TESTS`→`test_auth_config_service`, `RUN_ADVANCED_ADMIN_TESTS`→
  `test_admin_security`, `RUN_SEARCH_QUALITY_TESTS`→`test_search_quality` (corpus-dependent,
  deliberately never in CI), `RUN_AUTH_E2E`→`e2e/test_ldap_keycloak` + LDAP half of
  `e2e/test_auth_buttons`, `RUN_PKI_E2E`→`e2e/test_pki`.
- **MinIO/OpenSearch tests auto-enable by TCP probe.** Root conftest `_service_reachable`
  (0.3 s) `setdefault`s `SKIP_S3` from `localhost:5178` and `SKIP_OPENSEARCH` from
  `localhost:5180`, then points the clients at those host ports; an explicit shell value wins.
  Stack down → those suites **skip silently**, so a green local run proves less than it looks.

## Safety rules (non-negotiable)

- **E2E must never persist changes to dev data.** Upload tests delete what they create (API
  delete, falling back to `/force`); transcript-edit tests use the **cancel path only**.
- **Negative-login tests must use a nonexistent account** (`nonexistent@example.com`), never a
  wrong password for `admin@example.com` — progressive per-account lockout poisons every later
  test in the suite.
- Dev relaxes auth limits (`docker-compose.override.yml`: `RATE_LIMIT_AUTH_PER_MINUTE=120`,
  `ACCOUNT_LOCKOUT_THRESHOLD=100`, `DEV_*`-tunable). **Prod never loads that overlay** — don't
  write a test that only passes under the relaxed values. `shared_auth_state`/`gallery_page`
  exist to log in **once per session** for the same reason.

## Gotchas

- **`tests/integration/` is a directory name, not a marker.** Its contents split three ways:
  `integration`-marked (need the live stack), `gpu`-marked (boundary/diarization regression,
  lifecycle, perf gates), and three deliberately service-free tests in
  `test_metering_pipeline.py` that belong in the fast suite. The gate script runs the first two
  as separate phases (`-m integration`, then `-m gpu`); the fast suite and CI deselect both.
  Before #297 `gpu` was unregistered and silenced by a `PytestUnknownMarkWarning` filter, so
  those 17 tests ran in the fast suite *and* CPU-only CI, passing only on their own runtime skip
  guards, while the gate selected none of them.
- `db_session` rolls back the DB, **not MinIO or OpenSearch**. Hence `upload_test_file`'s API
  delete and the forced `AUDIT_LOG_TO_OPENSEARCH=false` — savepoints can't undo index writes
  into the live dev cluster.
- `--dist loadgroup`: tests sharing mutable global state need
  `pytestmark = pytest.mark.xdist_group("<name>")` (`test_auth_config_integration.py`,
  `unit/test_media_mirror_service.py`) or they interleave across workers. User fixtures use
  UUID-suffixed emails for the same reason.
- E2E runs from the repo root against `backend/tests/e2e/`, so `e2e/pytest.ini` becomes the
  rootdir config — pyproject `addopts` (`-n auto`, `-m 'not integration'`) do **not** apply.
