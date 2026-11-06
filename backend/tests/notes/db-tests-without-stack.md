# backend/tests — running DB-backed tests without the dev stack

Moved intact from `backend/tests/CLAUDE.md`.

## Running DB-backed tests without the dev stack

Most API/service tests need Postgres but nothing else. When the dev stack is down, is being used
by someone else, or sits on another branch's schema, `--throwaway-db` gives the run its own
disposable instance. It works from the main checkout and from any git worktree, and cannot touch
the dev database, its volumes, or the NAS data:

```bash
./scripts/run-backend-tests.sh --throwaway-db                       # whole suite
./scripts/run-backend-tests.sh --throwaway-db tests/unit/test_x.py -q -n0 --no-cov
./scripts/run-backend-tests.sh --throwaway-db --keep tests/unit/test_x.py   # leave it up to poke at
```

The flags may appear anywhere; everything else is forwarded to pytest. This is the single
implementation (`scripts/lib/throwaway-pg.sh`, sourced by the runner); there is no second
worktree-specific script. What it does:

- starts `postgres:17.5-alpine` as `ot-testdb-<random-hex>` on a kernel-assigned free loopback
  port (never 5173-5180, the dev stack's range), random password, tmpfs data, image platform
  pinned to the daemon's, a lost bind race retried on another port at most 5 times;
- labels it `ot-test-artifact=1`, `org.opentranscribe.purpose=throwaway-test-db` and a private
  `com.docker.compose.project=opentranscribe-pytest-<hex32>`. The last one is the namespace that
  `./opentr.sh test-cleanup` (`scripts/lib/test_artifact_sweep.py`) recognises as a test
  leftover, and it keeps every project-scoped command against the live stack from matching it. A
  compose-project label of any other shape would make the sweep refuse the container;
- refuses to start if that exact name already exists, and every docker call names only the one
  container it started (the host daemon is shared with unrelated services);
- runs `alembic upgrade head` from **this checkout's** `backend/` (so the schema matches the
  branch), then pytest, then removes the container and a `mktemp` dir (`ot-throwaway-db.*`) on
  every exit: failure, INT and TERM included. `--keep` leaves both and prints the removal command;
- uses `backend/venv` of the checkout, else the main checkout's (the parent of the shared git
  dir); it never installs anything;
- exit codes: 2 misuse, 3 precondition (no docker, no venv, stock port, not ready), 4 migration
  failed, otherwise pytest's.

Only postgres is provisioned: redis is already a per-worker fixture, and S3/OpenSearch tests are
skipped (`SKIP_S3`/`SKIP_OPENSEARCH` forced); they need a `./opentr.sh start dev --fresh <name>`
stack. Inspect leftovers with `docker ps -a --filter label=org.opentranscribe.purpose=throwaway-test-db`.
Tests: `unit/test_run_backend_tests_throwaway_db.py` (stub `docker`; each property above has an
assertion that fails if the property is removed).

Why it exports what it does:
- **A free random port**, not 5176 — conftest only overrides `POSTGRES_PORT` when it is unset or
  `5432`, so any other value is respected, and the dev stack's port stays free.
- **`POSTGRES_HOST` explicitly** — conftest forces `localhost` for pytest, but `alembic`
  does not import conftest and would otherwise resolve the compose service name.
- **`DATA_DIR`/`MODELS_DIR`/`TEMP_DIR`** — `config.py` defaults these to `/app/...` and
  `Settings.__init__` mkdirs them, which fails outside the container.
- **`SKIP_S3=True`** — conftest TCP-probes `localhost:5178` and enables S3-backed tests if
  *anything* answers. An unrelated service on that port produces `SignatureDoesNotMatch`
  failures that look like real bugs.
