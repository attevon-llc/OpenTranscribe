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
