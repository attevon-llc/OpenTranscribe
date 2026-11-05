# backend/tests — running DB-backed tests without the dev stack

Moved intact from `backend/tests/CLAUDE.md`.

## Running DB-backed tests without the dev stack

Most API/service tests need Postgres but nothing else. When the dev stack is down (or is
being used by someone else), `--throwaway-db` gives the run its own disposable instance — it
cannot touch the dev database, its volumes, or the NAS data:

```bash
./scripts/run-backend-tests.sh --throwaway-db                       # whole suite
./scripts/run-backend-tests.sh --throwaway-db tests/unit/test_x.py -q -n0 --no-cov
```

The flag may appear anywhere; everything else is forwarded to pytest. It starts
`ot-testdb-<random-hex>` (random per-run suffix, never derived from the directory; tmpfs data dir, loopback port from 55432 up, random password, both
`com.docker.compose.project` and `org.opentranscribe.purpose=throwaway-test-db` labels), runs
`alembic upgrade head`, runs the tests, and stops the container by its exact name on exit. It
refuses to start if that name already exists. Inspect with
`docker ps -a --filter label=org.opentranscribe.purpose=throwaway-test-db`. Why it exports what it
does: conftest respects an explicit `POSTGRES_PORT` (it only overrides unset/`5432`) but forces
`POSTGRES_HOST=localhost`, so the script exports `POSTGRES_HOST=127.0.0.1` for `alembic`, which does
not import conftest; `DATA_DIR`/`MODELS_DIR`/`TEMP_DIR` point at a `mktemp -d` because `config.py`
defaults them to `/app/...`; `SKIP_S3=True`/`SKIP_OPENSEARCH=True` stop an unrelated service on
`:5178`/`:5180` enabling S3/OpenSearch-backed tests that then fail as `SignatureDoesNotMatch`.

## From a git worktree: `scripts/test-in-worktree.sh`

Most API/service tests need Postgres but nothing else. When the dev stack is down (or is
being used by someone else, or sits on another branch's schema), use the script — from any
worktree, it finds its own repo root:

```bash
scripts/test-in-worktree.sh [--services pg] [--keep] -- tests/unit/test_x.py -n0 --no-cov
```

It starts a throwaway `postgres:17.5-alpine` (`ot-wt-test-<hex>`, label `ot-test-artifact=1`,
tmpfs data) on a free random loopback port, runs `alembic upgrade head` from **this worktree's**
code (so the schema matches the branch), exports the env below, runs pytest with your args, and
removes the container and temp dir on every exit (failure, INT, TERM). `--keep` leaves them and
prints the removal command. It refuses ports 5173-5180, uses the worktree's `backend/venv`
(else the main checkout's), never installs anything, and only ever names its own container.
Only postgres is provisioned; MinIO/OpenSearch tests skip (`SKIP_S3`/`SKIP_OPENSEARCH` are
forced) and need a `--fresh` stack. Tests: `unit/test_test_in_worktree.py` (stub `docker`).
Cannot touch the dev database, its volumes, or the NAS data. What it sets, and why:

Why each piece:
- **A free random port**, not 5176 — conftest only overrides `POSTGRES_PORT` when it is unset or
  `5432`, so any other value is respected, and the dev stack's port stays free.
- **`POSTGRES_HOST` explicitly** — conftest forces `localhost` for pytest, but `alembic`
  does not import conftest and would otherwise resolve the compose service name.
- **`DATA_DIR`/`MODELS_DIR`/`TEMP_DIR`** — `config.py` defaults these to `/app/...` and
  `Settings.__init__` mkdirs them, which fails outside the container.
- **`SKIP_S3=True`** — conftest TCP-probes `localhost:5178` and enables S3-backed tests if
  *anything* answers. An unrelated service on that port produces `SignatureDoesNotMatch`
  failures that look like real bugs.
