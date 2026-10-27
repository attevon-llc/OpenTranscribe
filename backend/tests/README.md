# Backend Tests

This directory contains the pytest suite for the OpenTranscribe backend (and, under `e2e/`, the
Playwright browser tests). [CLAUDE.md](CLAUDE.md) in this directory holds the long-form rules,
traps and rationale; this file is the how-to-run reference.

## Quick Start

```bash
# One-time: create the venv (see backend/CLAUDE.md for the full, current install steps)
cd backend
source venv/bin/activate

# The dev stack provides Postgres (and MinIO/OpenSearch/Redis for the tests that use them)
../opentr.sh start dev

# Run the fast suite. pyproject.toml's addopts already add -n auto, --strict-markers,
# --timeout=300 and -m 'not integration and not gpu'; tests/e2e is excluded by norecursedirs.
pytest

# Or capture the run once and re-read it without re-running (full suite, no e2e)
../scripts/run-backend-tests.sh
../scripts/run-backend-tests.sh --summary     # re-report the LAST run; runs no tests
../scripts/run-backend-tests.sh --failures
```

Run `pytest` from `backend/` so `pyproject.toml` is the rootdir config. The `e2e/` suite has its
own `pytest.ini` and is run through `scripts/e2e/` (see [e2e/README.md](e2e/README.md)).

## The test gates

| Command | What it runs |
|---------|--------------|
| `./scripts/run-integration-tests.sh` | **The pre-merge gate** against the live stack: unit/API suite, the security suites under `FIPS_MODE=true`, `-m integration`, `-m gpu`, model-vs-schema drift, and the dependency-parity, session-lifetime, collection-determinism and mutation-ratchet phases. Flags: `--coverage`, `--e2e-smoke`, `--search-quality`, `--cleanup`, `--skip-gpu`, `--export-capability`. Exit 4 means a phase reported NOT MEASURED (not a pass). |
| `./scripts/run-dev-tests.sh --full` | Quick branch check: backend gate, then e2e, then the frontend check, with one consolidated report. `--fast`, `--backend-only`, `--e2e-only`, `--frontend-only` select phases. |
| `./scripts/run-diarization-gpu-tests.sh` | The container-only `-m gpu` diarization suites (the gate's `-m gpu` phase skips them in the venv). |
| `./scripts/e2e/run-e2e.sh`, `./scripts/e2e/run-e2e-smoke.sh` | Playwright suite and a smoke subset. |
| GitHub Actions `backend-tests` | A safety net only: fresh Postgres, CPU-only `requirements-ci.txt`, `SKIP_S3`/`SKIP_OPENSEARCH` forced on. |

`scripts/test-matrix.sh` is the separate, exhaustive deployment-mode rehearsal run before a
release, not a development loop.

## Test Layout

| Directory | Contents | Requirements |
|-----------|----------|--------------|
| `unit/` | Unit and guard tests (the bulk of the suite), including AST/static guards over the scripts and compose files | Postgres for those that use `db_session`; otherwise none |
| `api/` | API endpoint tests (`api/endpoints/` plus `api/test_*.py`) | Postgres |
| top-level `test_*.py` | Older suites: ASR/LLM/auth-config settings, MFA, PKI, FIPS, FedRAMP, chat, admin, search quality, etc. | Postgres unless noted in the file |
| `services/`, `transcription/`, `redaction/`, `onnx/` | Service, ASR/diarization, redaction and ONNX tests | Some need model weights (`@pytest.mark.models`) or `HF_TOKEN` |
| `integration/` | Tests that need the live stack (`integration` marker), GPU (`gpu`), or a few service-free ones | See "Markers" |
| `eval/` | The RAG evaluation harness and its logic tests | See `CLAUDE.md` ("`tests/eval/`") |
| `e2e/` | Playwright browser tests | Running stack + browser; see its README |
| `fixtures/` | Shared fixtures and golden data (mock LLM/ASR providers, search corpus, boundary and media fixtures); registered via `pytest_plugins` in `conftest.py` | n/a |

## Markers

Registered in `backend/pyproject.toml` under `[tool.pytest.ini_options] markers`; the list there
is authoritative (`--strict-markers` makes an unregistered marker a collection error, so register
a new one before using it). The default `addopts` deselect `integration` and `gpu`.

`slow`, `unit`, `pki`, `e2e`, `integration` (needs the running dev environment), `gpu`,
`models` (needs ML weights), `ddl_exclusive` (schema DDL; put it on the test, never the module),
`multi_gpu` (needs a `--gpu-scale`/`--gpu-split` worker topology), `export_capability`,
`opt_in_gate` (behind its own `RUN_*` variable).

The `e2e/` markers live in `e2e/pytest.ini`, a separate rootdir config.

## Environment Variables

`conftest.py` sets these before any `app.*` import:

| Variable | Behaviour |
|----------|-----------|
| `TESTING`, `SKIP_CELERY`, `SKIP_REDIS`, `SKIP_WEBSOCKET` | Forced on; `ENVIRONMENT=testing`, `RATE_LIMIT_ENABLED=false` |
| `SKIP_S3`, `SKIP_OPENSEARCH` | Auto-set by a TCP probe of `MINIO_HOST:MINIO_PORT` (default `localhost:5178`) and `OPENSEARCH_HOST:OPENSEARCH_PORT` (`localhost:5180`); an explicit shell value wins. Stack down means those suites **skip silently**. |
| `POSTGRES_HOST` | Forced to `localhost` |
| `POSTGRES_PORT` | `5176` (the dev stack) unless you export another value; for an isolated `--fresh` stack export its ports |
| `AUDIT_LOG_TO_OPENSEARCH` | Defaults to `false` |
| `CUDA_VISIBLE_DEVICES` | Set to `-1` unless the `-m` selection can pick a `gpu` test (issue #719); `OT_TEST_CUDA_VISIBLE_DEVICES` is the escape hatch |

Database credentials come from the repo's `.env` via `conftest.py`; the tests need that file (or
the equivalent variables in the shell).

### Opt-in `RUN_*` gates

Opt-in gates are parsed by `tests/env_gate.py`'s `gate_enabled()` (accepts `1/true/yes/on`,
raises on a typo). The seven security-suite gates that used to exist (`RUN_PKI_TESTS`,
`RUN_MFA_TESTS`, `RUN_LLM_TESTS`, `RUN_FEDRAMP_TESTS`, `RUN_FIPS_TESTS`,
`RUN_AUTH_CONFIG_TESTS`, `RUN_ADVANCED_ADMIN_TESTS`) are gone: those suites run by default, and
files that still mention the names do so only in comments. To list the gates that are live, grep
for the call, not the bare name:

```bash
rg 'gate_enabled\(' backend/tests
```

Currently that finds `RUN_SEARCH_QUALITY_TESTS` (`test_search_quality.py`, self-seeding),
`RUN_SCHEMA_DRIFT_TESTS` (`unit/test_schema_drift.py`), `RUN_AUTH_E2E`, `RUN_PKI_E2E` (e2e), plus
the opt-in `RUN_INDEX_AUDIT` and `RUN_EXPORT_CAPABILITY_TEST` and `RUN_PIPELINE_SMOKE`. The gate
script exports the ones it owns; `backend/tests/unit/test_gate_run_env_vars_are_live.py` fails if
a gate script sets a `RUN_*` that no test reads.

## Coverage

Backend coverage is enforced, not report-only. Derive the current floor from the config instead
of trusting a number written here:

```bash
rg -n "fail_under" backend/pyproject.toml
cd backend && pytest --cov=app --cov-report=term-missing        # or: ../scripts/run-integration-tests.sh --coverage
```

The floor is a ratchet: raise it when coverage rises, never lower it to make a run pass.
Frontend coverage (`cd frontend && npm run test:coverage`) is report-only.

## Test-quality tooling

A test that cannot fail is worse than no test. These keep the suite honest:

```bash
python3 scripts/audit-tests.py backend/tests              # AST detectors; exits 1 on a new finding
python3 scripts/audit-tests.py --selftest                 # run after touching a detector
python3 scripts/audit-tests.py --help                     # lists the detector categories
python3 scripts/analyze-test-timing.py <junit.xml>        # barriers / slow tail
python3 scripts/audit-route-coverage.py                   # API routes no test references
cd frontend && npm run test:audit && npm run test:audit:selftest   # the vitest sibling
./scripts/run-mutation-tests.sh --module spans            # opt-in mutation testing, never in CI
```

The pytest auditor is a pre-commit hook and runs in the `backend-tests` CI job. Findings are
exempted only through `backend/tests/audit-allowlist.txt`
(`<file>::<test>::<category>  # reason`); a stale entry fails the run.

## Running Specific Tests

```bash
cd backend
pytest tests/api/endpoints/test_auth_comprehensive.py -v
pytest tests/test_asr_settings.py -v
pytest tests/api/endpoints/test_admin.py -v
pytest tests/api/endpoints/test_users.py -v
pytest tests/api/endpoints/test_tags.py -v

pytest -n0 -x -s tests/unit/test_something.py      # serial, stop on first failure, show prints
pytest -m integration                               # the live-stack tests (stack must be up)
pytest -m gpu                                       # needs a GPU; the CUDA guard releases for this marker
```

Passing several paths from different directories in one invocation is a known sharp edge with
pytest 9.1 (see `CLAUDE.md`, "Gotchas"); `fixtures/dir_collector_memo.py` works around it.

## Database Setup

Tests need PostgreSQL. Start the dev stack through the project script (not bare compose):

```bash
./opentr.sh start dev
```

The tests use the same PostgreSQL instance as development, isolated by a savepoint that is
rolled back after each test (`db_session`). That rollback does **not** cover MinIO, OpenSearch,
Redis or the filesystem; tests that touch those must clean up after themselves (see
`CLAUDE.md`, "Cleaning up after tests"). A branch that adds an Alembic migration must be tested
against a stack that has applied it. To run DB-backed tests without the dev stack, use the
throwaway-Postgres recipe in `CLAUDE.md` ("Running DB-backed tests without the dev stack").

## Test Fixtures

Key fixtures in `conftest.py`:

| Fixture | Scope | Description |
|---------|-------|-------------|
| `db_session` | function | Database session using nested transactions (savepoints), rolled back per test |
| `client` | function | FastAPI TestClient wired to the test session |
| `normal_user` | function | Creates a regular user |
| `admin_user` | function | Creates an admin user |
| `user_token_headers` | function | Auth headers for the regular user |
| `admin_token_headers` | function | Auth headers for the admin user |

Provider and corpus fixtures under `fixtures/` (for example `mock_llm`, `mock_asr`,
`search_corpus`) are registered through `pytest_plugins` in the root `conftest.py`.

## Writing New Tests

```python
def test_example_endpoint(client, user_token_headers):
    """Test description."""
    response = client.get("/api/endpoint", headers=user_token_headers)
    assert response.status_code == 200
    assert "expected_field" in response.json()
```

- Assert the exact status code (`!= 403` also passes on a 500) and assert a non-empty result
  before looping over it. `scripts/audit-tests.py` rejects both shapes.
- A test that cannot run on a given host should be deselected by marker, not left to skip.
- Gate an opt-in suite with `gate_enabled`:

```python
import pytest

from tests.env_gate import gate_enabled

pytestmark = pytest.mark.skipif(
    not gate_enabled("RUN_FEATURE_TESTS"),
    reason="Feature tests disabled (set RUN_FEATURE_TESTS=true to run)",
)
```

- Tests that share mutable global state need `pytest.mark.xdist_group("<name>")`; tests that run
  schema DDL need `@pytest.mark.ddl_exclusive` on the test. See `CLAUDE.md`.
- Never put an `__init__.py` in `tests/`.

## Troubleshooting

### "Connection refused" errors

Ensure the stack is up:
```bash
./opentr.sh status
```

### Import errors

Activate the virtual environment (`source backend/venv/bin/activate`); the install steps are in
`backend/CLAUDE.md`.

### Everything skipped under S3 / OpenSearch

Those suites auto-enable only when `localhost:5178` / `localhost:5180` answer. A green run with
the stack down proves less than it looks.

### A schema error ("relation does not exist")

You are probably running a feature branch against a stack at another revision. See
`CLAUDE.md`, "A branch that adds a migration must be tested against a stack that has APPLIED it".

### Test isolation issues

If data leaks between tests, check that the test uses `db_session` correctly and, for
committed-to-another-store data, that it cleans up in a `finally`/fixture teardown.
