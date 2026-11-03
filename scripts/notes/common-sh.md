# scripts — `common.sh`, database backup/restore primitives, and the unset-variable guard

Moved intact from `scripts/CLAUDE.md`. Read before editing `common.sh`, `opentr.sh` or `opentranscribe.sh`.

- `common.sh` is sourced by **both** front ends: `opentr.sh` (dev, docker checks, model-cache
  chown, OpenSearch model bootstrap) and, since issue #613, `opentranscribe.sh` (production —
  conditionally, `if [ -f ./scripts/common.sh ]`, and BEFORE its own `fix_model_cache_permissions`
  definition so the local one still wins — bash keeps the last definition). `offline-common.sh`
  is sourced only by the two offline/Windows builders.
- `common.sh`'s **`backup_database`/`restore_database`** (moved out of `opentr.sh` by issue #613)
  are the ONE shared implementation of the backup/restore CLI both front ends dispatch into —
  `opentr.sh`'s `backup)`/`restore)` arms and `opentranscribe.sh`'s `backup|restore)` arm both call
  them. Two new LEADING parameters on top of the primitives' existing "first arg is an exec
  prefix" contract: `$1` is the compose-files chain (`""` for `opentr.sh` — a repo clone
  auto-loads `docker-compose.override.yml`, which supplies `image:`/`build:` for every
  application service, so bare `docker compose` is unchanged; `"$(get_compose_files)"` for
  `opentranscribe.sh` — MEASURED that the base compose file alone is an invalid project, so a
  curl install needs the real chain), `$2` is the front-end name (`"./opentr.sh"` |
  `"./opentranscribe.sh"`) used only in operator-facing next-step messages, so a production
  install is never told to run a script it does not have. `opentranscribe.sh`'s arm wraps the
  call in `set +e` / `set -e`: the shared functions are written for `opentr.sh`'s deliberate
  absence of `set -e` (many unchecked `docker compose ...` statements on the assumption a
  failure there is non-fatal), and `opentranscribe.sh` runs `set -e` at file scope — without the
  adapter, any one of those unchecked commands failing would abort the WHOLE restore mid-flight.
  It also explicitly reads `POSTGRES_USER`/`POSTGRES_DB`/`BACKUP_HOST_PATH` from `.env` before
  calling in — unlike `opentr.sh` (which does `set -a; source ./.env` in its prologue),
  `opentranscribe.sh` has no such prologue, so without this a restore would silently target the
  DEFAULT database name on any install that customised it.
- `common.sh`'s **database restore primitives** (issues #599/#600) back `restore_database`'s two
  replay backends: `pg_drop_and_recreate_database` / `pg_replay_dump` / `pg_verify_restore` for
  plain-SQL dumps, `pg_replay_custom_dump` / `pg_custom_dump_expected_head` /
  `pg_verify_custom_restore` for `pg_dump -Fc` (custom-format — what the scheduled/S3 backup
  feature produces) dumps, sharing one comparison routine (`_pg_verify_against`) so the two
  verifiers cannot independently drift. Every function's first argument is an **exec prefix**
  (`"docker compose exec -T postgres"` in production, `"docker exec -i <throwaway-container>"`
  in the integration tests) — that contract now covers both dump formats, not just plain SQL.
  `pg_replay_custom_dump` deliberately never passes `-j`/`--jobs`: it is mutually exclusive with
  `--single-transaction` (measured), so the safe path forfeits parallel restore for atomicity.
  `pg_restore_restart_decision` (issue #610) is the pure function `restore_database` calls to
  decide whether to restart the app services it stopped, or leave them stopped because the
  backup's alembic head does not match the live DB's head read just before the restore — the
  backend migrates on startup, so restarting the previously-running image over a
  freshly-restored *older* dump silently re-migrates it forward, and over a *newer* one crashes
  on an unknown revision. Echoes `restart` / `hold:no-restart` / `hold:schema-mismatch`; takes no
  docker/psql/globals, only the two heads plus the `--migrate-forward`/`--no-restart` flags, so
  it is unit-testable without Postgres
  (`backend/tests/unit/test_restore_restart_decision.py`).
- ⚠️ **Every `$VAR` in `opentr.sh` + `common.sh` must be defaulted — enforced by
  `backend/tests/unit/test_shell_expansion_guards.py`** (static, fast unit suite, no execution).
  Both run under `set -uo pipefail`, so an unguarded optional `.env` variable is a hard abort,
  not a style nit: `common.sh` read a bare `[ -n "$GPU_DEVICE_ID" ]` while `opentr.sh` defaulted
  five *other* optional variables and not that one, so `./opentr.sh` died with
  `GPU_DEVICE_ID: unbound variable` in **any checkout without a `.env`** — i.e. every git
  worktree (`.env` is gitignored and never comes along), which blocked exactly the
  isolated-worktree workflow. `opentranscribe.sh` runs only `set -e` — no `set -u` — so an
  unguarded expansion there is empty rather than fatal, but the same test suite scans it too
  (issue #613) to keep the guard uniform across the code the two front ends now share.
  Guard at the use site (`${VAR:-default}`) or add
  `: "${VAR:=}"` to the `opentr.sh` prologue block; the prologue runs at top level before any
  function, which is why it also covers references inside `common.sh`. Exemptions are a
  `_ALLOWLIST` dict keyed `<script>::<VAR>` with a mandatory reason, and a **stale entry fails**.
  The `_ALLOWLIST` is currently **empty** — the three `BACKLOG` offenders this file used to
  list (`GPU_DEVICE_ID`, `ENVIRONMENT`, `USER`, all in `common.sh`) were fixed, and the entries
  went with them. Read `_ALLOWLIST` in `test_shell_expansion_guards.py` rather than this
  paragraph: a count transcribed into prose is a measurement that rots, and this one had.
  **An assignment inside a function of the other file does not count as a guard** — that is
  precisely how `ENVIRONMENT` slipped past a first draft that pooled assignments across both
  files. `${#VAR}` and `${VAR%…}` are **not** guards (both still abort); escaped `\$VAR` in help
  text, single-quoted `'$VAR'`, and `$VAR` in a comment are not expansions and must not be
  reported — all three were false positives in a draft, and each now has a must-stay-clean case.
