# Stack targeting: `scripts/lib/stack_target.py` (never resolve a stack by name or port arithmetic)

Every script that talks to a running stack resolves it through this library, then asserts what it
needs. It uses compose LABELS (`docker ps --filter label=k=v` is exact; `name=` is a substring match)
and the ports the project's containers PUBLISH, never `5174 + offset`, a container-name guess or an
offset file that may live in another checkout. Python: `import`/run `scripts/lib/stack_target.py`;
shell: `source scripts/lib/stack-target.sh` then `st_target --fresh NAME`, `st_require schema-head
code=bind healthy asset:frontend-fonts`, `st_container backend`, `st_prepare pg_stat_statements`.
`python3 scripts/lib/stack_target.py --help` lists every flag and requirement.

| Exit | Name | Meaning |
|---|---|---|
| 0 | OK | every requirement met (overrides recorded as `OVERRIDDEN`) |
| 2 | USAGE | bad flags, or `prepare` asked of a non-fresh target |
| 20 | NO_PROJECT | the project is missing or ambiguous |
| 21 | WRONG_PROJECT | a port owned by another project, mixed stack, not isolated, refused target |
| 22 | SCHEMA | `alembic_version` differs from this checkout's head (says behind or ahead) |
| 23 | STALE_CODE | wrong checkout bind-mounted, or image sha is not HEAD with image inputs changed |
| 24 | UNHEALTHY | a required service is not running or healthy within `ST_HEALTH_TIMEOUT` |
| 25 | MISSING_SETTING | extension, preload library or container env missing |
| 26 | MISSING_ASSET | a gitignored worktree asset (fonts, ffmpeg, nginx certs) is absent |
| 27 | MISSING_ENV | a required variable is unset (the value is never printed) |
| 28 | DOCKER | docker unreachable: cannot judge, so never a pass |

Release stages keep their own contract: `st_require ... || exit 3`. There is no bare skip:
`--allow CHECK=REASON` (or `ST_ALLOW='check=reason;...'`) prints `OVERRIDDEN` and is recorded.
`prepare --fresh NAME --pg-stat-statements` is the one home of the `ALTER SYSTEM` block (the load
test docs point at it); it refuses the live, stock and `ot-reltest-*` projects and restarts through
`./opentr.sh restart-db --fresh NAME`. The guard `backend/tests/unit/test_stack_targeting_guard.py`
fails on a name-substring filter, a hardcoded stock container, a live-port default or a
project-less `docker compose -f`; `PENDING` is empty; new findings fail; exemptions need a reason (`EXEMPT`, written next to the site's key). Python scripts use `stack_target.observed_port(VAR)` / `observed_container(SERVICE)` / `refuse_split()`; pytest modules use `tests.stack_preflight.stack_port(VAR)`.
