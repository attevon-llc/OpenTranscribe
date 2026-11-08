# Migrate as a one-shot step, not a per-replica lifespan job

Status: implemented on `v070/migrate-job` (v0.7.0). This file is the plan the lane worked
from; sections marked **Decision** are what shipped.

## Problem

`app/main.py`'s lifespan runs `run_migrations()` on every API process. With N replicas every
one of them races Alembic: they queue on `pg_advisory_lock(42)` (a blocking wait with no
bound), each repeats the post-upgrade work (concurrent index retries, role reconcile, the
v230 repair check), and a wedged holder hangs every replica forever. Several other startup
steps were also written for one process.

## Documented patterns this follows

| Source | What it says | How it is used here |
|---|---|---|
| FastAPI, *Deployments Concepts → Previous Steps Before Starting* (fastapi.tiangolo.com/deployment/concepts/) | "you will want to have a **single process** to perform those previous steps, before starting the application ... even if afterwards, you start multiple processes"; "if the steps were something delicate like a database migration, they could cause conflicts". Names an init container or a script as the vehicle. | One `migrate` step runs before any API process. |
| FastAPI, *Lifespan Events* (fastapi.tiangolo.com/advanced/events/) | Lifespan runs "once, before the application starts receiving requests" — per application instance. | Lifespan keeps only per-process work when a migrate step exists. |
| Docker Compose, *Control startup order* (docs.docker.com/compose/how-tos/startup-order/) | `service_completed_successfully`: "a dependency is expected to run to successful completion before starting a dependent service." | `backend` and the Celery workers depend on `migrate` with that condition. |
| docker/compose#9273 and the current `up --wait` rule | `--wait` accepts an exited-0 one-shot only when a started service depends on it with `service_completed_successfully`. | `backend` declares that dependency, so `./opentr.sh start` (which uses `up --wait`) is satisfied. |
| Kubernetes, *Jobs* (kubernetes.io/docs/concepts/workloads/controllers/job/) | "Jobs represent one-off tasks that run to completion and then stop." | Orchestrated deploys run `python -m app.startup.migrate` as a Job (or a Helm pre-upgrade hook). |
| Kubernetes, *Init Containers* | Init containers run per Pod, to completion, before app containers. | Not recommended for migrations: N replicas = N runs. Acceptable only because the lock makes it safe. |
| Alembic, *Cookbook → Sharing a Connection across one or more programmatic migration commands* (alembic.sqlalchemy.org/en/latest/cookbook.html) | Programmatic `command.upgrade(config, ...)` is supported. The cookbook has no recipe for concurrent runners. | The runner keeps calling `command.upgrade`; concurrency stays our responsibility (the advisory lock). |
| PostgreSQL, *Advisory Lock Functions* (postgresql.org/docs/current/functions-admin.html) | `pg_advisory_lock` "waiting if necessary"; `pg_try_advisory_lock` returns `false` "without waiting"; session locks are released at session end "even if the client disconnects ungracefully". | Bounded wait = poll `pg_try_advisory_lock` until a deadline. A crashed holder frees the lock on disconnect. |
| PostgreSQL, *Client Connection Defaults → lock_timeout* | "applies both to explicit locking requests ... and to implicitly-acquired locks"; advisory locks are not mentioned. **Measured** on postgres 17.5 (throwaway DB, 2026-11-08): `SET lock_timeout='500ms'; SELECT pg_advisory_lock(777)` against a held key fails with `canceling statement due to lock timeout` — it DOES apply. | Either mechanism would bound the wait. The poll loop was kept because it can log progress and name the holder while waiting, and it needs no session GUC (a `SET` through a transaction-mode pooler does not stick). |

## Decisions

1. **Entrypoint `python -m app.startup.migrate`** (`backend/app/startup/migrate.py`).
   It runs `run_migrations()`, which already includes the post-upgrade work that belongs
   with the schema: v230 repair, concurrent index retries, role reconcile and the idle
   timeout. It then seeds the initial data (`init_db`).
   Exit codes are stable:
   - `0` done;
   - `1` migration failed;
   - `3` timed out waiting for the advisory lock;
   - `4` database unreachable within `DB_STARTUP_WAIT_S`.

   It is idempotent, and two concurrent runs serialise on the lock; the second one finds
   the database at head.
2. **Bounded lock wait.** `MIGRATION_LOCK_TIMEOUT_S` defaults to 900 s, covering the
   longest measured upgrade chain of 2-5 min with room to spare; `0` waits forever.
   `run_migrations()` polls `pg_try_advisory_lock(42)` every 2 s. On the first miss it logs
   the holder's pid, application name, state and age from `pg_locks` and
   `pg_stat_activity`, then logs progress every 30 s. At the deadline it raises
   `MigrationLockTimeoutError`, which names the holder.
   This applies to the lifespan path as well: a replica that cannot get the lock exits
   instead of hanging.
3. **Compose has a one-shot `migrate` service.** It is defined in `docker-compose.yml`
   and given an image in every overlay that gives `backend` one. `backend` and the base
   Celery services depend on it with `service_completed_successfully`. It has
   `restart: "no"` and its healthcheck is disabled.
4. **`RUN_MIGRATIONS_ON_STARTUP` per overlay.** The default stays `true`, because it is
   public config and an old compose file must still migrate.

   | Chain | migrate service | backend `RUN_MIGRATIONS_ON_STARTUP` | Why |
   |---|---|---|---|
   | dev (`override`) | yes, bind-mounted source | `true` (exempt) | Hot reload and `restart-backend` must keep applying a new revision on reload. The second run is a no-op under the lock. |
   | `pki-dev` | yes | `true` (inherits dev) | It is a dev variant. |
   | prod, prod+nginx, prod+pki, prod+lite, prod+db-roles, demo | yes | `false` | The migrate step owns it, so replicas never migrate. |
   | offline | yes | `false` | Same as prod. |
   | bench | yes (`otbench-migrate`) | `false` | Same as prod; the bench image is baked. |
   | old compose files (pre-0.7.0) | no | `true` (default) | The upgrade keeps working unchanged. |

5. **Paths that bypass `depends_on`** (`--no-deps`, `compose start`) and would otherwise
   leave a `false` backend at 503:
   - `scripts/common.sh` adds `compose_run_migrate`, which runs
     `docker compose run --rm --no-deps migrate` when the chain defines the service.
     Restore calls it before restarting the app services.
   - `perform_phased_restart` (`opentranscribe.sh update` / `update-full`) now checks the
     exit of its `up` and prints the migrate log on failure. `up backend` already pulls
     `migrate` in as a dependency.
   - `verify_stack_health` (`opentr.sh`) accepts an exited-0 container whose restart
     policy is `no`. Without that, a completed migrate reads as a broken service.
   - `opentr.sh` adds `migrate` to `FRESH_NAMED_SERVICES` and `FRESH_BAKED_SERVICES`, so
     `--fresh` re-pins it.
   - The dev `restart-backend` / `rebuild-backend` paths need nothing, because the dev
     backend still migrates itself.
6. **Readiness.** It is unchanged. When `RUN_MIGRATIONS_ON_STARTUP=false`, `/health/ready`
   returns 503 until the schema is at head. A behavioural test now pins this; before, a
   source-text check was the only guard.

## Lifespan audit (item → decision)

See the table in `backend/app/db/CLAUDE.md` ("Startup work and replicas"); it is the
maintained copy.

## Verification

- Unit: the entrypoint's exit codes; the lock timeout against real Postgres
  (`--throwaway-db`); compose wiring through real `docker compose config`; readiness 503
  on a stale schema; that each newly elected dispatch consults `run_once_per_boot`; and
  `verify_stack_health` with an exited-0 one-shot.
- Live (lead): dev start, prod start, upgrade from a previous DB, and two backend replicas.
  Exact steps are in the lane report.
